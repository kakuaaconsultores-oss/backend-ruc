import json
from datetime import datetime
from flask import request, jsonify

TIPOS_COMPROBANTE_DEFAULT = [
    ("FACTURA", "Factura", 1),
    ("NOTA_CREDITO", "Nota de Crédito", 1),
    ("NOTA_DEBITO", "Nota de Débito", 1),
    ("RECIBO", "Recibo", 1),
    ("OTRO", "Otro", 1),
]
ESTADOS_COMPROBANTE = ["borrador", "registrado", "validado", "pendiente_contabilizar", "contabilizado", "anulado", "pagado"]
ESTADOS_OC = ["borrador", "emitida", "recibida", "facturada", "cerrada", "anulada"]
ESTADOS_OP = ["borrador", "solicitada", "aprobada", "pagada", "anulada"]

def _now():
    return datetime.utcnow().isoformat(timespec="seconds")

def _cliente_id(conn):
    raw = request.headers.get("X-Cliente-ID") or request.args.get("cliente_id")
    if not raw:
        return None, "Seleccioná un cliente activo."
    try:
        cid = int(raw)
    except (TypeError, ValueError):
        return None, "Cliente inválido."
    u = conn.execute("SELECT id FROM usuarios WHERE token_sesion_hash = ?", (__import__("hashlib").sha256((request.cookies.get("__Host-kakuaa_session","")).encode()).hexdigest(),)).fetchone()
    if not u:
        return None, "No autorizado."
    ok = conn.execute("SELECT 1 FROM usuario_clientes WHERE usuario_id = ? AND cliente_id = ?", (u["id"], cid)).fetchone()
    if not ok and u["rol"] not in ("superadmin", "admin"):
        return None, "No autorizado para este cliente."
    exists = conn.execute("SELECT id FROM clientes WHERE id = ? AND estado = 'activo'", (cid,)).fetchone()
    return (cid, None) if exists else (None, "Cliente no encontrado o inactivo.")

def register(app, get_db, staff_required, usuario_required, insertar_id):
    def init_compras():
        conn = get_db()
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS tipos_comprobante_compra (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL,
                nombre TEXT NOT NULL, activo INTEGER NOT NULL DEFAULT 1, creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(cliente_id,codigo))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS condiciones_compra (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL,
                nombre TEXT NOT NULL, dias_credito INTEGER NOT NULL DEFAULT 0, activo INTEGER NOT NULL DEFAULT 1,
                creado_en TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(cliente_id,codigo))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS formas_pago_compra (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL,
                nombre TEXT NOT NULL, tipo TEXT NOT NULL DEFAULT 'contado', activo INTEGER NOT NULL DEFAULT 1,
                creado_en TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(cliente_id,codigo))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS proveedores (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER NOT NULL, ruc TEXT DEFAULT '',
                razon_social TEXT NOT NULL, nombre_comercial TEXT DEFAULT '', documento TEXT DEFAULT '',
                correo TEXT DEFAULT '', telefono TEXT DEFAULT '', direccion TEXT DEFAULT '',
                condicion_compra_id INTEGER DEFAULT NULL, forma_pago_id INTEGER DEFAULT NULL,
                cuenta_contable_id INTEGER DEFAULT NULL, estado TEXT NOT NULL DEFAULT 'activo',
                creado_por INTEGER, creado_en TEXT DEFAULT CURRENT_TIMESTAMP, actualizado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_proveedores_cliente ON proveedores(cliente_id, estado, razon_social)")
            conn.execute("""CREATE TABLE IF NOT EXISTS conceptos_compra (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL,
                nombre TEXT NOT NULL, descripcion TEXT DEFAULT '', tipo TEXT DEFAULT 'servicio',
                cuenta_contable_id INTEGER DEFAULT NULL, tasa_iva REAL DEFAULT 10, activo INTEGER NOT NULL DEFAULT 1,
                creado_en TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(cliente_id,codigo))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS ordenes_compra (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER NOT NULL, proveedor_id INTEGER NOT NULL,
                numero TEXT, fecha TEXT NOT NULL, fecha_entrega TEXT DEFAULT NULL, condicion_id INTEGER DEFAULT NULL,
                forma_pago_id INTEGER DEFAULT NULL, estado TEXT NOT NULL DEFAULT 'borrador',
                observacion TEXT DEFAULT '', total REAL NOT NULL DEFAULT 0, creado_por INTEGER, creado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS ordenes_compra_detalle (
                id INTEGER PRIMARY KEY AUTOINCREMENT, orden_id INTEGER NOT NULL, concepto_id INTEGER,
                descripcion TEXT NOT NULL, cantidad REAL NOT NULL DEFAULT 1, precio_unitario REAL NOT NULL DEFAULT 0,
                iva_tasa REAL NOT NULL DEFAULT 10, subtotal REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(orden_id) REFERENCES ordenes_compra(id) ON DELETE CASCADE)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS comprobantes_compra (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER NOT NULL, proveedor_id INTEGER NOT NULL,
                tipo_comprobante_id INTEGER, numero TEXT NOT NULL, cdc TEXT DEFAULT '', fecha TEXT NOT NULL,
                condicion_id INTEGER DEFAULT NULL, forma_pago_id INTEGER DEFAULT NULL,
                estado TEXT NOT NULL DEFAULT 'registrado', moneda TEXT NOT NULL DEFAULT 'PYG',
                gravado_10 REAL DEFAULT 0, gravado_5 REAL DEFAULT 0, exento REAL DEFAULT 0,
                iva_10 REAL DEFAULT 0, iva_5 REAL DEFAULT 0, total REAL NOT NULL DEFAULT 0,
                orden_compra_id INTEGER DEFAULT NULL, origen TEXT DEFAULT 'MANUAL',
                observacion TEXT DEFAULT '', creado_por INTEGER, creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
                actualizado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_compras_cliente_fecha ON comprobantes_compra(cliente_id, fecha, estado)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_compras_proveedor ON comprobantes_compra(proveedor_id, fecha)")
            conn.execute("""CREATE TABLE IF NOT EXISTS comprobantes_compra_detalle (
                id INTEGER PRIMARY KEY AUTOINCREMENT, comprobante_id INTEGER NOT NULL, concepto_id INTEGER,
                descripcion TEXT NOT NULL, cantidad REAL NOT NULL DEFAULT 1, precio_unitario REAL NOT NULL DEFAULT 0,
                iva_tasa REAL NOT NULL DEFAULT 10, subtotal REAL NOT NULL DEFAULT 0,
                cuenta_contable_id INTEGER DEFAULT NULL,
                FOREIGN KEY(comprobante_id) REFERENCES comprobantes_compra(id) ON DELETE CASCADE)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS notas_compra (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER NOT NULL, comprobante_id INTEGER NOT NULL,
                tipo TEXT NOT NULL, numero TEXT NOT NULL, fecha TEXT NOT NULL, monto REAL NOT NULL DEFAULT 0,
                motivo TEXT DEFAULT '', estado TEXT NOT NULL DEFAULT 'registrado', creado_por INTEGER,
                creado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS ordenes_pago (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER NOT NULL, proveedor_id INTEGER NOT NULL,
                numero TEXT, fecha TEXT NOT NULL, estado TEXT NOT NULL DEFAULT 'borrador',
                forma_pago_id INTEGER, total REAL NOT NULL DEFAULT 0, observacion TEXT DEFAULT '',
                creado_por INTEGER, creado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS ordenes_pago_detalle (
                id INTEGER PRIMARY KEY AUTOINCREMENT, orden_pago_id INTEGER NOT NULL, comprobante_id INTEGER,
                concepto TEXT DEFAULT '', monto REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(orden_pago_id) REFERENCES ordenes_pago(id) ON DELETE CASCADE)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS anticipos_proveedores (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER NOT NULL, proveedor_id INTEGER NOT NULL,
                fecha TEXT NOT NULL, monto REAL NOT NULL DEFAULT 0, saldo REAL NOT NULL DEFAULT 0,
                forma_pago_id INTEGER, estado TEXT NOT NULL DEFAULT 'activo', observacion TEXT DEFAULT '',
                creado_por INTEGER, creado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
            for code,name,active in TIPOS_COMPROBANTE_DEFAULT:
                try: conn.execute("INSERT INTO tipos_comprobante_compra(cliente_id,codigo,nombre,activo) SELECT id,?,?,? FROM clientes ON CONFLICT(cliente_id,codigo) DO NOTHING", (code,name,active))
                except Exception: pass
            conn.commit()
        finally: conn.close()
    init_compras()

    def parse_json():
        return request.get_json(silent=True) or {}

    @app.get("/api/compras/catalogos")
    @usuario_required
    def compras_catalogos():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            def rows(sql): return [dict(x) for x in conn.execute(sql,(cid,)).fetchall()]
            return jsonify({
                "proveedores": rows("SELECT * FROM proveedores WHERE cliente_id=? AND estado='activo' ORDER BY razon_social"),
                "tipos_comprobante": rows("SELECT * FROM tipos_comprobante_compra WHERE cliente_id=? AND activo=1 ORDER BY nombre"),
                "condiciones": rows("SELECT * FROM condiciones_compra WHERE cliente_id=? AND activo=1 ORDER BY nombre"),
                "formas_pago": rows("SELECT * FROM formas_pago_compra WHERE cliente_id=? AND activo=1 ORDER BY nombre"),
                "conceptos": rows("SELECT * FROM conceptos_compra WHERE cliente_id=? AND activo=1 ORDER BY nombre")
            })
        finally: conn.close()

    @app.get("/api/compras/proveedores")
    @usuario_required
    def compras_proveedores():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            return jsonify([dict(x) for x in conn.execute("SELECT * FROM proveedores WHERE cliente_id=? ORDER BY razon_social",(cid,)).fetchall()])
        finally: conn.close()

    @app.post("/api/compras/proveedores")
    @staff_required
    def crear_proveedor():
        d=parse_json(); conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            if not d.get("razon_social"): return jsonify({"error":"La razón social es obligatoria."}),400
            rid=insertar=get_db # placeholder replaced below
            cur=conn.execute("INSERT INTO proveedores(cliente_id,ruc,razon_social,nombre_comercial,documento,correo,telefono,direccion,condicion_compra_id,forma_pago_id,cuenta_contable_id,creado_por) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid,d.get("ruc",""),d["razon_social"],d.get("nombre_comercial",""),d.get("documento",""),d.get("correo",""),d.get("telefono",""),d.get("direccion",""),d.get("condicion_compra_id"),d.get("forma_pago_id"),d.get("cuenta_contable_id"),None))
            conn.commit(); return jsonify({"id":cur.lastrowid}),201
        finally: conn.close()

    def crud_catalogo(path, table, fields):
        @app.post(path)
        @staff_required
        def create_item():
            d=parse_json(); conn=get_db()
            try:
                cid,err=_cliente_id(conn)
                if err:return jsonify({"error":err}),401
                if not d.get("codigo") or not d.get("nombre"): return jsonify({"error":"Código y nombre son obligatorios."}),400
                cols="cliente_id,"+",".join(fields); vals=[cid]+[d.get(x) for x in fields]
                ph=",".join(["?"]*len(cols))
                conn.execute(f"INSERT INTO {table}({cols}) VALUES({ph})",vals); conn.commit()
                return jsonify({"ok":True}),201
            except Exception as e:
                conn.rollback(); return jsonify({"error":str(e)}),400
            finally: conn.close()
        @app.get(path)
        @usuario_required
        def list_items():
            conn=get_db()
            try:
                cid,err=_cliente_id(conn)
                if err:return jsonify({"error":err}),401
                return jsonify([dict(x) for x in conn.execute(f"SELECT * FROM {table} WHERE cliente_id=? ORDER BY nombre",(cid,)).fetchall()])
            finally: conn.close()

    crud_catalogo("/api/compras/condiciones","condiciones_compra",["codigo","nombre","dias_credito"])
    crud_catalogo("/api/compras/formas-pago","formas_pago_compra",["codigo","nombre","tipo"])
    crud_catalogo("/api/compras/conceptos","conceptos_compra",["codigo","nombre","descripcion","tipo","cuenta_contable_id","tasa_iva"])

    @app.post("/api/compras/proveedores/<int:proveedor_id>")
    @staff_required
    def editar_proveedor(proveedor_id):
        d=parse_json(); conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            campos=["ruc","razon_social","nombre_comercial","documento","correo","telefono","direccion","condicion_compra_id","forma_pago_id","cuenta_contable_id","estado"]
            sets=", ".join(f"{x}=?" for x in campos)
            vals=[d.get(x) for x in campos]+[cid,proveedor_id]
            conn.execute(f"UPDATE proveedores SET {sets}, actualizado_en=CURRENT_TIMESTAMP WHERE cliente_id=? AND id=?",vals); conn.commit()
            return jsonify({"ok":True})
        finally: conn.close()

    @app.get("/api/compras/comprobantes")
    @usuario_required
    def listar_compras():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            rows=conn.execute("""SELECT c.*,p.razon_social proveedor,t.nombre tipo_nombre
                FROM comprobantes_compra c JOIN proveedores p ON p.id=c.proveedor_id
                LEFT JOIN tipos_comprobante_compra t ON t.id=c.tipo_comprobante_id
                WHERE c.cliente_id=? ORDER BY c.fecha DESC,c.id DESC""",(cid,)).fetchall()
            return jsonify([dict(x) for x in rows])
        finally: conn.close()

    @app.post("/api/compras/comprobantes")
    @staff_required
    def crear_comprobante():
        d=parse_json(); conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            required=["proveedor_id","numero","fecha"]
            if any(d.get(x) in (None,"") for x in required): return jsonify({"error":"Proveedor, número y fecha son obligatorios."}),400
            if int(d["proveedor_id"]) and not conn.execute("SELECT 1 FROM proveedores WHERE id=? AND cliente_id=?",(int(d["proveedor_id"]),cid)).fetchone(): return jsonify({"error":"Proveedor inválido."}),400
            cols=["cliente_id","proveedor_id","tipo_comprobante_id","numero","cdc","fecha","condicion_id","forma_pago_id","estado","moneda","gravado_10","gravado_5","exento","iva_10","iva_5","total","orden_compra_id","origen","observacion","creado_por"]
            vals=[cid,d["proveedor_id"],d.get("tipo_comprobante_id"),d["numero"],d.get("cdc",""),d["fecha"],d.get("condicion_id"),d.get("forma_pago_id"),d.get("estado","registrado"),d.get("moneda","PYG"),float(d.get("gravado_10",0) or 0),float(d.get("gravado_5",0) or 0),float(d.get("exento",0) or 0),float(d.get("iva_10",0) or 0),float(d.get("iva_5",0) or 0),float(d.get("total",0) or 0),d.get("orden_compra_id"),d.get("origen","MANUAL"),d.get("observacion",""),None]
            cidc=insertar_id(conn, "INSERT INTO comprobantes_compra("+",".join(cols)+") VALUES("+",".join(["?"]*len(cols))+")", vals)
            for item in d.get("detalle",[]):
                conn.execute("""INSERT INTO comprobantes_compra_detalle(comprobante_id,concepto_id,descripcion,cantidad,precio_unitario,iva_tasa,subtotal,cuenta_contable_id) VALUES(?,?,?,?,?,?,?,?)""",
                    (cidc,item.get("concepto_id"),item.get("descripcion",""),float(item.get("cantidad",1) or 1),float(item.get("precio_unitario",0) or 0),float(item.get("iva_tasa",10) or 0),float(item.get("subtotal",0) or 0),item.get("cuenta_contable_id")))
            conn.commit(); return jsonify({"id":cidc}),201
        except Exception as e:
            conn.rollback(); return jsonify({"error":str(e)}),400
        finally: conn.close()

    @app.post("/api/compras/comprobantes/<int:comp_id>/estado")
    @staff_required
    def cambiar_estado_compra(comp_id):
        d=parse_json(); estado=d.get("estado")
        if estado not in ESTADOS_COMPROBANTE:return jsonify({"error":"Estado inválido."}),400
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            conn.execute("UPDATE comprobantes_compra SET estado=?, actualizado_en=CURRENT_TIMESTAMP WHERE id=? AND cliente_id=?",(estado,comp_id,cid)); conn.commit()
            return jsonify({"ok":True})
        finally: conn.close()

    @app.get("/api/compras/reportes/proveedor")
    @usuario_required
    def reporte_proveedor():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            rows=conn.execute("""SELECT p.razon_social proveedor,COUNT(c.id) comprobantes,
                COALESCE(SUM(c.total),0) total FROM proveedores p LEFT JOIN comprobantes_compra c
                ON c.proveedor_id=p.id AND c.cliente_id=p.cliente_id
                WHERE p.cliente_id=? GROUP BY p.id,p.razon_social ORDER BY p.razon_social""",(cid,)).fetchall()
            return jsonify([dict(x) for x in rows])
        finally: conn.close()

    @app.get("/api/compras/reportes/periodo")
    @usuario_required
    def reporte_periodo():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            desde=request.args.get("desde"); hasta=request.args.get("hasta")
            q="SELECT fecha,COUNT(*) comprobantes,COALESCE(SUM(total),0) total FROM comprobantes_compra WHERE cliente_id=?"
            params=[cid]
            if desde:q+=" AND fecha>=?";params.append(desde)
            if hasta:q+=" AND fecha<=?";params.append(hasta)
            q+=" GROUP BY fecha ORDER BY fecha"
            return jsonify([dict(x) for x in conn.execute(q,params).fetchall()])
        finally: conn.close()

    @app.get("/api/compras/reportes/pendientes-pago")
    @usuario_required
    def pendientes_pago():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            rows=conn.execute("""SELECT c.id,c.fecha,c.numero,p.razon_social proveedor,c.total,c.estado
                FROM comprobantes_compra c JOIN proveedores p ON p.id=c.proveedor_id
                WHERE c.cliente_id=? AND c.estado NOT IN ('pagado','anulado') ORDER BY c.fecha""",(cid,)).fetchall()
            return jsonify([dict(x) for x in rows])
        finally: conn.close()
