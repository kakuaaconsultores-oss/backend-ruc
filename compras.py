import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from flask import request, jsonify
from erp_modulos import registrar_ingreso_compra

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
    id_col = "BIGSERIAL" if os.environ.get("DATABASE_URL") else "INTEGER"
    def init_compras():
        conn = get_db()
        try:
            conn.execute(f"""CREATE TABLE IF NOT EXISTS tipos_comprobante_compra (
                id {id_col} PRIMARY KEY, cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL,
                nombre TEXT NOT NULL, activo INTEGER NOT NULL DEFAULT 1, creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(cliente_id,codigo))""")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS condiciones_compra (
                id {id_col} PRIMARY KEY, cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL,
                nombre TEXT NOT NULL, tipo TEXT NOT NULL DEFAULT 'dias', dias_credito INTEGER NOT NULL DEFAULT 0, cuotas INTEGER NOT NULL DEFAULT 1, activo INTEGER NOT NULL DEFAULT 1,
                creado_en TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(cliente_id,codigo))""")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS formas_pago_compra (
                id {id_col} PRIMARY KEY, cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL,
                nombre TEXT NOT NULL, tipo TEXT NOT NULL DEFAULT 'contado', cuenta_contable_id INTEGER DEFAULT NULL,
                activo INTEGER NOT NULL DEFAULT 1, creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(cliente_id,codigo))""")
            # Estas columnas ya forman parte del CREATE TABLE. Solo se agregan
            # cuando la base existente proviene de una versión anterior.
            def _column_exists(table, column):
                if os.environ.get("DATABASE_URL"):
                    return bool(conn.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema=current_schema() AND table_name=? AND column_name=?",
                        (table, column)
                    ).fetchone())
                return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())

            for table, column, definition in (
                ("conceptos_compra", "unidad_medida_id", "INTEGER"),
                ("condiciones_compra", "tipo", "TEXT NOT NULL DEFAULT 'dias'"),
                ("condiciones_compra", "cuotas", "INTEGER NOT NULL DEFAULT 1"),
                ("formas_pago_compra", "cuenta_contable_id", "INTEGER"),
                ("conceptos_compra", "unidad_medida", "TEXT NOT NULL DEFAULT ''"),
                ("conceptos_compra", "stock_minimo", "REAL NOT NULL DEFAULT 0"),
                ("conceptos_compra", "concepto_presupuestario", "TEXT DEFAULT NULL"),
            ):
                if not _column_exists(table, column):
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS proveedores (
                id {id_col} PRIMARY KEY, cliente_id INTEGER NOT NULL, ruc TEXT DEFAULT '',
                razon_social TEXT NOT NULL, nombre_comercial TEXT DEFAULT '', documento TEXT DEFAULT '',
                correo TEXT DEFAULT '', telefono TEXT DEFAULT '', direccion TEXT DEFAULT '',
                condicion_compra_id INTEGER DEFAULT NULL, forma_pago_id INTEGER DEFAULT NULL,
                cuenta_contable_id INTEGER DEFAULT NULL, estado TEXT NOT NULL DEFAULT 'activo',
                creado_por INTEGER, creado_en TEXT DEFAULT CURRENT_TIMESTAMP, actualizado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_proveedores_cliente ON proveedores(cliente_id, estado, razon_social)")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS proveedor_timbrados (
                id {id_col} PRIMARY KEY, cliente_id INTEGER NOT NULL, proveedor_id INTEGER NOT NULL,
                tipo_comprobante_id INTEGER NOT NULL, modalidad TEXT NOT NULL DEFAULT 'IMPRESO',
                numero_timbrado TEXT NOT NULL, establecimiento TEXT DEFAULT '', punto_expedicion TEXT DEFAULT '',
                numero_desde INTEGER NOT NULL DEFAULT 1, numero_hasta INTEGER NOT NULL DEFAULT 1,
                fecha_inicio TEXT DEFAULT NULL, fecha_vencimiento TEXT DEFAULT NULL,
                activo INTEGER NOT NULL DEFAULT 1, observacion TEXT DEFAULT '',
                creado_en TEXT DEFAULT CURRENT_TIMESTAMP, actualizado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_proveedor_timbrados ON proveedor_timbrados(cliente_id, proveedor_id, activo)")

            conn.execute(f"""CREATE TABLE IF NOT EXISTS unidades_medida (
                id {id_col} PRIMARY KEY, cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL,
                nombre TEXT NOT NULL, abreviatura TEXT DEFAULT '', activo INTEGER NOT NULL DEFAULT 1,
                creado_en TEXT DEFAULT CURRENT_TIMESTAMP, actualizado_en TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(cliente_id,codigo))""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_unidades_medida_cliente ON unidades_medida(cliente_id, activo, codigo)")

            conn.execute(f"""CREATE TABLE IF NOT EXISTS conceptos_compra (
                id {id_col} PRIMARY KEY, cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL,
                nombre TEXT NOT NULL, descripcion TEXT DEFAULT '', tipo TEXT DEFAULT 'servicio',
                unidad_medida TEXT NOT NULL DEFAULT '', unidad_medida_id INTEGER DEFAULT NULL,
                stock_minimo REAL NOT NULL DEFAULT 0,
                cuenta_contable_id INTEGER DEFAULT NULL, concepto_presupuestario TEXT DEFAULT NULL,
                tasa_iva REAL DEFAULT 10, activo INTEGER NOT NULL DEFAULT 1,
                creado_en TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(cliente_id,codigo))""")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS ordenes_compra (
                id {id_col} PRIMARY KEY, cliente_id INTEGER NOT NULL, proveedor_id INTEGER NOT NULL,
                numero TEXT, fecha TEXT NOT NULL, fecha_entrega TEXT DEFAULT NULL, condicion_id INTEGER DEFAULT NULL,
                forma_pago_id INTEGER DEFAULT NULL, estado TEXT NOT NULL DEFAULT 'borrador',
                observacion TEXT DEFAULT '', total REAL NOT NULL DEFAULT 0, creado_por INTEGER, creado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS ordenes_compra_detalle (
                id {id_col} PRIMARY KEY, orden_id INTEGER NOT NULL, concepto_id INTEGER,
                descripcion TEXT NOT NULL, cantidad REAL NOT NULL DEFAULT 1, precio_unitario REAL NOT NULL DEFAULT 0,
                iva_tasa REAL NOT NULL DEFAULT 10, subtotal REAL NOT NULL DEFAULT 0)""")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS comprobantes_compra (
                id {id_col} PRIMARY KEY, cliente_id INTEGER NOT NULL, proveedor_id INTEGER NOT NULL,
                tipo_comprobante_id INTEGER, numero TEXT NOT NULL, cdc TEXT DEFAULT '', fecha TEXT NOT NULL,
                condicion_id INTEGER DEFAULT NULL, forma_pago_id INTEGER DEFAULT NULL,
                estado TEXT NOT NULL DEFAULT 'registrado', moneda TEXT NOT NULL DEFAULT 'PYG',
                gravado_10 REAL DEFAULT 0, gravado_5 REAL DEFAULT 0, exento REAL DEFAULT 0,
                iva_10 REAL DEFAULT 0, iva_5 REAL DEFAULT 0, total REAL NOT NULL DEFAULT 0,
                orden_compra_id INTEGER DEFAULT NULL, timbrado_id INTEGER DEFAULT NULL, origen TEXT DEFAULT 'MANUAL',
                observacion TEXT DEFAULT '', creado_por INTEGER, creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
                actualizado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
            if not _column_exists("comprobantes_compra", "timbrado_id"):
                conn.execute("ALTER TABLE comprobantes_compra ADD COLUMN timbrado_id INTEGER")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_compras_cliente_fecha ON comprobantes_compra(cliente_id, fecha, estado)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_compras_proveedor ON comprobantes_compra(proveedor_id, fecha)")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS comprobantes_compra_detalle (
                id {id_col} PRIMARY KEY, comprobante_id INTEGER NOT NULL, concepto_id INTEGER,
                descripcion TEXT NOT NULL, cantidad REAL NOT NULL DEFAULT 1, precio_unitario REAL NOT NULL DEFAULT 0,
                iva_tasa REAL NOT NULL DEFAULT 10, subtotal REAL NOT NULL DEFAULT 0,
                cuenta_contable_id INTEGER DEFAULT NULL)""")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS cuotas_compras (
                id {id_col} PRIMARY KEY, cliente_id INTEGER NOT NULL, comprobante_id INTEGER NOT NULL,
                numero_cuota INTEGER NOT NULL, fecha_vencimiento TEXT NOT NULL, importe REAL NOT NULL DEFAULT 0,
                saldo REAL NOT NULL DEFAULT 0, estado TEXT NOT NULL DEFAULT 'pendiente',
                creado_en TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(comprobante_id,numero_cuota))""")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS notas_compra (
                id {id_col} PRIMARY KEY, cliente_id INTEGER NOT NULL, comprobante_id INTEGER NOT NULL,
                tipo TEXT NOT NULL, numero TEXT NOT NULL, fecha TEXT NOT NULL, monto REAL NOT NULL DEFAULT 0,
                motivo TEXT DEFAULT '', estado TEXT NOT NULL DEFAULT 'registrado', creado_por INTEGER,
                creado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS ordenes_pago (
                id {id_col} PRIMARY KEY, cliente_id INTEGER NOT NULL, proveedor_id INTEGER NOT NULL,
                numero TEXT, fecha TEXT NOT NULL, estado TEXT NOT NULL DEFAULT 'borrador',
                forma_pago_id INTEGER, total REAL NOT NULL DEFAULT 0, observacion TEXT DEFAULT '',
                creado_por INTEGER, creado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS ordenes_pago_detalle (
                id {id_col} PRIMARY KEY, orden_pago_id INTEGER NOT NULL, comprobante_id INTEGER,
                concepto TEXT DEFAULT '', monto REAL NOT NULL DEFAULT 0)""")
            for column, definition in (
                ("cuota_id", "INTEGER"),
                ("monto_aplicado", "REAL NOT NULL DEFAULT 0"),
            ):
                if not _column_exists("ordenes_pago_detalle", column):
                    conn.execute(
                        f"ALTER TABLE ordenes_pago_detalle ADD COLUMN {column} {definition}"
                    )
            conn.execute(f"""CREATE TABLE IF NOT EXISTS anticipos_proveedores (
                id {id_col} PRIMARY KEY, cliente_id INTEGER NOT NULL, proveedor_id INTEGER NOT NULL,
                fecha TEXT NOT NULL, monto REAL NOT NULL DEFAULT 0, saldo REAL NOT NULL DEFAULT 0,
                forma_pago_id INTEGER, estado TEXT NOT NULL DEFAULT 'activo', observacion TEXT DEFAULT '',
                creado_por INTEGER, creado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
            clientes = conn.execute("SELECT id FROM clientes").fetchall()
            unidades_base = [
                ("UNI","Unidad","UNI"),
                ("KG","Kilogramo","kg"),
                ("G","Gramo","g"),
                ("MG","Miligramo","mg"),
                ("LT","Litro","L"),
                ("ML","Mililitro","ml"),
                ("MT","Metro","m"),
                ("CM","Centímetro","cm"),
                ("M2","Metro cuadrado","m²"),
                ("M3","Metro cúbico","m³"),
                ("TN","Tonelada","t"),
                ("HS","Hora","h"),
                ("MIN","Minuto","min"),
                ("DIA","Día","día"),
                ("MES","Mes","mes"),
                ("SERV","Servicio","SERV"),
                ("CAJ","Caja","CAJ"),
                ("PAQ","Paquete","PAQ"),
            ]
            for cliente in clientes:
                cliente_id = cliente["id"] if hasattr(cliente, "keys") else cliente[0]
                tiene_unidades = conn.execute(
                    "SELECT 1 FROM unidades_medida WHERE cliente_id=? LIMIT 1",
                    (cliente_id,)
                ).fetchone()
                if not tiene_unidades:
                    for codigo_um, nombre_um, abreviatura_um in unidades_base:
                        conn.execute(
                            "INSERT INTO unidades_medida(cliente_id,codigo,nombre,abreviatura,activo) VALUES(?,?,?,?,1) "
                            "ON CONFLICT (cliente_id,codigo) DO NOTHING",
                            (cliente_id,codigo_um,nombre_um,abreviatura_um)
                        )
                # Migración de conceptos existentes: conservamos unidad_medida
                # para compatibilidad y asignamos una unidad maestra.
                conceptos_legacy = conn.execute(
                    "SELECT id, unidad_medida FROM conceptos_compra "
                    "WHERE cliente_id=? AND (unidad_medida_id IS NULL OR unidad_medida_id=0)",
                    (cliente_id,)
                ).fetchall()
                for concepto in conceptos_legacy:
                    texto_um = str(concepto["unidad_medida"] or "").strip()
                    if not texto_um:
                        continue
                    unidad = conn.execute(
                        "SELECT id,nombre FROM unidades_medida WHERE cliente_id=? "
                        "AND (UPPER(codigo)=UPPER(?) OR UPPER(nombre)=UPPER(?)) LIMIT 1",
                        (cliente_id,texto_um,texto_um)
                    ).fetchone()
                    if not unidad:
                        codigo_legacy = "UM" + str(concepto["id"])
                        conn.execute(
                            "INSERT INTO unidades_medida(cliente_id,codigo,nombre,abreviatura,activo) VALUES(?,?,?,?,1) "
                            "ON CONFLICT (cliente_id,codigo) DO NOTHING",
                            (cliente_id,codigo_legacy,texto_um,texto_um[:10])
                        )
                        unidad = conn.execute(
                            "SELECT id,nombre FROM unidades_medida WHERE cliente_id=? AND codigo=?",
                            (cliente_id,codigo_legacy)
                        ).fetchone()
                    if unidad:
                        conn.execute(
                            "UPDATE conceptos_compra SET unidad_medida_id=?, unidad_medida=? "
                            "WHERE id=? AND cliente_id=?",
                            (unidad["id"],unidad["nombre"],concepto["id"],cliente_id)
                        )
            for cliente in clientes:
                cliente_id = cliente["id"] if hasattr(cliente, "keys") else cliente[0]
                existe = conn.execute("SELECT 1 FROM tipos_comprobante_compra WHERE cliente_id=? LIMIT 1", (cliente_id,)).fetchone()
                if not existe:
                    for code,name,active in TIPOS_COMPROBANTE_DEFAULT:
                        conn.execute(
                            "INSERT INTO tipos_comprobante_compra(cliente_id,codigo,nombre,activo) "
                            "VALUES(?,?,?,?) ON CONFLICT (cliente_id,codigo) DO NOTHING",
                            (cliente_id,code,name,active)
                        )
            conn.commit()
        finally: conn.close()
    init_compras()

    def parse_json():
        return request.get_json(silent=True) or {}

    def _validar_timbrado(conn,cid,proveedor_id,tipo_comprobante_id,numero,fecha,timbrado_id=None):
        raw=str(numero or "").strip()
        partes=raw.split("-")
        if len(partes)!=3 or not all(p.isdigit() for p in partes):
            return None,"El número debe tener formato 001-001-0000001."
        establecimiento=partes[0].zfill(3); punto=partes[1].zfill(3); secuencia=int(partes[2])
        params=[proveedor_id,cid]
        sql="""SELECT pt.*, t.nombre tipo_nombre FROM proveedor_timbrados pt
               LEFT JOIN tipos_comprobante_compra t ON t.id=pt.tipo_comprobante_id
               WHERE pt.proveedor_id=? AND pt.cliente_id=? AND pt.activo=1"""
        if tipo_comprobante_id not in (None,""):
            sql+=" AND pt.tipo_comprobante_id=?"; params.append(int(tipo_comprobante_id))
        if timbrado_id not in (None,""):
            sql+=" AND pt.id=?"; params.append(int(timbrado_id))
        sql+=" ORDER BY pt.fecha_vencimiento DESC,pt.id DESC"
        rows=conn.execute(sql,params).fetchall()
        f=str(fecha or "")[:10]
        for row in rows:
            if row["establecimiento"] and str(row["establecimiento"]).zfill(3)!=establecimiento: continue
            if row["punto_expedicion"] and str(row["punto_expedicion"]).zfill(3)!=punto: continue
            if not (int(row["numero_desde"])<=secuencia<=int(row["numero_hasta"])): continue
            if row["fecha_inicio"] and f<str(row["fecha_inicio"])[:10]: continue
            venc=str(row["fecha_vencimiento"] or "")[:10]
            if venc and venc!="3000-12-31" and f>venc: continue
            return dict(row),""
        return None,"El número no corresponde a ningún timbrado activo del proveedor para ese tipo, rango y fecha."


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
                "tipos_comprobante": rows("SELECT * FROM tipos_comprobante_compra WHERE cliente_id=? ORDER BY nombre"),
                "condiciones": rows("SELECT * FROM condiciones_compra WHERE cliente_id=? ORDER BY nombre"),
                "formas_pago": rows("SELECT * FROM formas_pago_compra WHERE cliente_id=? AND activo=1 ORDER BY nombre"),
                "conceptos": rows("""SELECT c.*, cc.codigo AS cuenta_codigo, cc.nombre AS cuenta_nombre,
                    CASE WHEN c.activo=1 AND COALESCE(TRIM(c.concepto_presupuestario),'')<>'' AND c.cuenta_contable_id IS NOT NULL
                         THEN 1 ELSE 0 END AS habilitado_compras
                    FROM conceptos_compra c
                    LEFT JOIN cuentas_contables cc ON cc.id=c.cuenta_contable_id
                    WHERE c.cliente_id=? ORDER BY c.codigo, c.nombre""")
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

    @app.get("/api/compras/proveedores/consulta-ruc/<path:ruc>")
    @usuario_required
    def consultar_ruc_proveedor(ruc):
        """Consulta RUC usando el servicio oficial de DNIT.

        La interfaz de Kakuaa se mantiene estable. La fuente oficial se
        configura mediante DNIT_RUC_API_KEY en el entorno de Render.
        Mientras no exista la clave, se conserva TuRuc como respaldo
        temporal para no interrumpir el ERP durante la migración.
        """
        ruc = urllib.parse.unquote(str(ruc or "")).strip().upper()
        if not ruc:
            return jsonify({"error":"Ingresá un RUC."}),400

        try:
            # DNIT recibe RUC y DV por separado. Kakuaa normalmente recibe
            # "RUC-DV", por ejemplo "80012345-6".
            if "-" in ruc:
                ruc_base, dv = ruc.rsplit("-", 1)
                ruc_base = ruc_base.strip()
                dv = dv.strip()
            else:
                ruc_base, dv = ruc, ""

            dnit_key = (os.environ.get("DNIT_RUC_API_KEY") or "").strip()
            if dnit_key:
                if not ruc_base or not dv:
                    return jsonify({"error":"Ingresá el RUC con su DV, por ejemplo 80012345-6."}),400

                query = urllib.parse.urlencode({
                    "apiKey": dnit_key,
                    "ruc": ruc_base,
                    "dv": dv,
                })
                url = "https://servicios.set.gov.py/EsetApiWS/ApiWS/consultaRUC?" + query
                req = urllib.request.Request(
                    url,
                    headers={
                        "Accept":"application/json",
                        "User-Agent":"Kakuaa-ERP/1.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))

                contribuyente = payload.get("contribuyente") or {}
                estado_respuesta = str(payload.get("estado") or "").upper()
                codigo = str(payload.get("codigo") or "").upper()

                if estado_respuesta not in ("VALIDO", "VÁLIDO") and codigo not in ("VALIDO", "VÁLIDO"):
                    return jsonify({
                        "error":"La DNIT no encontró un contribuyente válido para ese RUC."
                    }),404

                if not contribuyente or not contribuyente.get("razonSocial"):
                    return jsonify({"error":"La DNIT no devolvió datos para ese RUC."}),404

                tipo_persona = str(contribuyente.get("tipoPersona") or "").upper()
                return jsonify({"ok":True,"data":{
                    "ruc":ruc,
                    "razon_social":contribuyente.get("razonSocial") or "",
                    "dv":dv,
                    "documento":ruc_base,
                    "estado":contribuyente.get("estado") or "",
                    "categoria":contribuyente.get("categoria") or "",
                    "mes_cierre":contribuyente.get("mesCierre") or "",
                    "tipo_persona":contribuyente.get("tipoPersona") or "",
                    "ruc_anterior":contribuyente.get("rucAnterior") or "",
                    "tipo_sociedad":contribuyente.get("tipoSociedad") or "",
                    "nombre_comercial":contribuyente.get("nombreComercial") or "",
                    "es_persona_juridica":tipo_persona in ("JURIDICO","JURÍDICO"),
                    "es_entidad_publica":False,
                    "fuente":"DNIT",
                }})

            # Respaldo temporal: se mantiene hasta cargar DNIT_RUC_API_KEY.
            url = "https://turuc.com.py/api/contribuyente/" + urllib.parse.quote(ruc, safe="-")
            req = urllib.request.Request(
                url,
                headers={"Accept":"application/json","User-Agent":"Kakuaa-ERP/1.0"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            data = payload.get("data") or {}
            if not data or not data.get("ruc"):
                return jsonify({"error":payload.get("message") or "No se encontró el contribuyente para ese RUC."}),404
            return jsonify({"ok":True,"data":{
                "ruc":data.get("ruc") or ruc,
                "razon_social":data.get("razonSocial") or "",
                "dv":data.get("dv"),
                "documento":data.get("doc"),
                "estado":data.get("estado") or "",
                "es_persona_juridica":bool(data.get("esPersonaJuridica")),
                "es_entidad_publica":bool(data.get("esEntidadPublica")),
                "fuente":"TuRuc",
            }})
        except urllib.error.HTTPError as e:
            return jsonify({"error":"La fuente de consulta de RUC no respondió correctamente."}),502
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return jsonify({"error":"No se pudo consultar la fuente de RUC en este momento. Podés volver a intentar."}),502
        except Exception:
            return jsonify({"error":"No se pudo consultar la fuente de RUC en este momento. Podés volver a intentar."}),502

    @app.post("/api/compras/proveedores")
    @staff_required
    def crear_proveedor():
        d=parse_json(); conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            ruc=(d.get("ruc") or "").strip().upper()
            razon=(d.get("razon_social") or "").strip()
            if not ruc: return jsonify({"error":"El RUC es obligatorio."}),400
            if not razon: return jsonify({"error":"La razón social es obligatoria. Consultá el RUC antes de guardar."}),400
            existente=conn.execute("SELECT id FROM proveedores WHERE cliente_id=? AND UPPER(ruc)=? LIMIT 1",(cid,ruc)).fetchone()
            if existente:return jsonify({"error":"Ya existe un proveedor con ese RUC en este cliente."}),409
            rid=insertar_id(conn, "INSERT INTO proveedores(cliente_id,ruc,razon_social,nombre_comercial,documento,correo,telefono,direccion,condicion_compra_id,forma_pago_id,cuenta_contable_id,creado_por) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid,ruc,razon,(d.get("nombre_comercial") or "").strip(),d.get("documento",""),(d.get("correo") or "").strip(),(d.get("telefono") or "").strip(),(d.get("direccion") or "").strip(),d.get("condicion_compra_id"),d.get("forma_pago_id"),d.get("cuenta_contable_id"),None))
            conn.commit(); return jsonify({"id":rid}),201
        except Exception as e:
            conn.rollback(); return jsonify({"error":str(e)}),400
        finally: conn.close()

    @app.get("/api/compras/proveedores/<int:proveedor_id>/timbrados")
    @usuario_required
    def listar_timbrados_proveedor(proveedor_id):
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            if not conn.execute("SELECT 1 FROM proveedores WHERE id=? AND cliente_id=?",(proveedor_id,cid)).fetchone():
                return jsonify({"error":"Proveedor inválido."}),404
            rows=conn.execute("""SELECT pt.*, t.codigo tipo_codigo, t.nombre tipo_nombre
                FROM proveedor_timbrados pt
                LEFT JOIN tipos_comprobante_compra t ON t.id=pt.tipo_comprobante_id
                WHERE pt.proveedor_id=? AND pt.cliente_id=?
                ORDER BY pt.activo DESC, pt.fecha_vencimiento DESC, pt.id DESC""",(proveedor_id,cid)).fetchall()
            return jsonify([dict(x) for x in rows])
        finally: conn.close()

    @app.post("/api/compras/proveedores/<int:proveedor_id>/timbrados")
    @staff_required
    def crear_timbrado_proveedor(proveedor_id):
        d=parse_json(); conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            if not conn.execute("SELECT 1 FROM proveedores WHERE id=? AND cliente_id=?",(proveedor_id,cid)).fetchone():
                return jsonify({"error":"Proveedor inválido."}),404
            tipo_id=d.get("tipo_comprobante_id")
            if tipo_id in ("",None): return jsonify({"error":"Seleccioná el tipo de comprobante."}),400
            if not conn.execute("SELECT 1 FROM tipos_comprobante_compra WHERE id=? AND cliente_id=?",(int(tipo_id),cid)).fetchone():
                return jsonify({"error":"Tipo de comprobante inválido."}),400
            numero=(d.get("numero_timbrado") or "").strip()
            modalidad=(d.get("modalidad") or "IMPRESO").strip().upper()
            establecimiento=(d.get("establecimiento") or "").strip()
            punto=(d.get("punto_expedicion") or "").strip()
            if not numero:return jsonify({"error":"El número de timbrado es obligatorio."}),400
            if modalidad not in ("IMPRESO","ELECTRONICO","AUTO"): modalidad="IMPRESO"
            if modalidad=="AUTO": modalidad="ELECTRONICO"
            try:
                desde=int(d.get("numero_desde")); hasta=int(d.get("numero_hasta"))
            except (TypeError,ValueError):
                return jsonify({"error":"El rango desde/hasta debe ser numérico."}),400
            if desde<1 or hasta<desde:return jsonify({"error":"El rango de numeración no es válido."}),400
            fecha_inicio=(d.get("fecha_inicio") or "").strip() or None
            fecha_venc=(d.get("fecha_vencimiento") or "").strip() or None
            if modalidad=="ELECTRONICO" and not fecha_venc: fecha_venc="3000-12-31"
            if fecha_venc=="3000-12-31": modalidad="ELECTRONICO"
            rid=insertar_id(conn, """INSERT INTO proveedor_timbrados
                (cliente_id,proveedor_id,tipo_comprobante_id,modalidad,numero_timbrado,establecimiento,punto_expedicion,
                 numero_desde,numero_hasta,fecha_inicio,fecha_vencimiento,activo,observacion)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid,proveedor_id,int(tipo_id),modalidad,numero,establecimiento,punto,desde,hasta,fecha_inicio,fecha_venc,1,d.get("observacion","")))
            conn.commit(); return jsonify({"id":rid}),201
        except Exception as e:
            conn.rollback(); return jsonify({"error":str(e)}),400
        finally: conn.close()

    @app.put("/api/compras/proveedores/<int:proveedor_id>/timbrados/<int:timbrado_id>")
    @staff_required
    def editar_timbrado_proveedor(proveedor_id,timbrado_id):
        d=parse_json(); conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            row=conn.execute("SELECT * FROM proveedor_timbrados WHERE id=? AND proveedor_id=? AND cliente_id=?",(timbrado_id,proveedor_id,cid)).fetchone()
            if not row:return jsonify({"error":"Timbrado no encontrado."}),404
            tipo_id=d.get("tipo_comprobante_id")
            numero=(d.get("numero_timbrado") or "").strip()
            modalidad=(d.get("modalidad") or "IMPRESO").strip().upper()
            if modalidad=="AUTO": modalidad="ELECTRONICO"
            try:
                desde=int(d.get("numero_desde")); hasta=int(d.get("numero_hasta"))
            except (TypeError,ValueError): return jsonify({"error":"El rango desde/hasta debe ser numérico."}),400
            if desde<1 or hasta<desde:return jsonify({"error":"El rango de numeración no es válido."}),400
            fecha_venc=(d.get("fecha_vencimiento") or "").strip() or None
            if modalidad=="ELECTRONICO" and not fecha_venc:fecha_venc="3000-12-31"
            if fecha_venc=="3000-12-31":modalidad="ELECTRONICO"
            conn.execute("""UPDATE proveedor_timbrados SET tipo_comprobante_id=?,modalidad=?,numero_timbrado=?,establecimiento=?,
                punto_expedicion=?,numero_desde=?,numero_hasta=?,fecha_inicio=?,fecha_vencimiento=?,activo=?,observacion=?
                WHERE id=? AND proveedor_id=? AND cliente_id=?""",
                (int(tipo_id),modalidad,numero,d.get("establecimiento",""),d.get("punto_expedicion",""),desde,hasta,
                 d.get("fecha_inicio") or None,fecha_venc,1 if d.get("activo",1) else 0,d.get("observacion",""),timbrado_id,proveedor_id,cid))
            conn.commit();return jsonify({"ok":True})
        except Exception as e:
            conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.delete("/api/compras/proveedores/<int:proveedor_id>/timbrados/<int:timbrado_id>")
    @staff_required
    def desactivar_timbrado_proveedor(proveedor_id,timbrado_id):
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            conn.execute("UPDATE proveedor_timbrados SET activo=0 WHERE id=? AND proveedor_id=? AND cliente_id=?",(timbrado_id,proveedor_id,cid))
            conn.commit();return jsonify({"ok":True})
        finally:conn.close()

    def crud_catalogo(path, table, fields):
        @app.post(path, endpoint="compras_create_"+table)
        @staff_required
        def create_item():
            d=parse_json(); conn=get_db()
            try:
                cid,err=_cliente_id(conn)
                if err:return jsonify({"error":err}),401
                if not d.get("codigo") or not d.get("nombre"): return jsonify({"error":"Código y nombre son obligatorios."}),400
                if table == "formas_pago_compra":
                    cuenta=d.get("cuenta_contable_id")
                    if cuenta not in (None,"","null"):
                        cuenta=int(cuenta)
                        ok=conn.execute("SELECT id FROM cuentas_contables WHERE id=? AND (cliente_id=? OR cliente_id IS NULL)",(cuenta,cid)).fetchone()
                        if not ok:return jsonify({"error":"La cuenta contable seleccionada no existe o no pertenece al cliente."}),400
                        d["cuenta_contable_id"]=cuenta
                    else:d["cuenta_contable_id"]=None
                if table == "condiciones_compra":
                    tipo = (d.get("tipo") or "dias").strip().lower()
                    dias = int(d.get("dias_credito") or 0); cuotas = int(d.get("cuotas") or 1)
                    if tipo not in ("dias","cuotas"): return jsonify({"error":"Tipo de condición inválido."}),400
                    if tipo == "dias" and dias < 0: return jsonify({"error":"Los días no pueden ser negativos."}),400
                    if tipo == "cuotas" and cuotas < 1: return jsonify({"error":"La cantidad de cuotas debe ser al menos 1."}),400
                    d["tipo"], d["dias_credito"], d["cuotas"] = tipo, (dias if tipo=="dias" else 0), (cuotas if tipo=="cuotas" else 1)
                cols="cliente_id,"+",".join(fields)
                vals=[cid]+[d.get(x) for x in fields]
                # Construimos los placeholders según el motor para evitar que
                # PostgreSQL interprete accidentalmente parámetros de una consulta
                # anterior o de una cadena literal.
                ph=",".join(["?"]*(len(fields)+1))
                sql=f"INSERT INTO {table}({cols}) VALUES({ph})"
                conn.execute(sql, vals)
                conn.commit()
                return jsonify({"ok":True}),201
            except Exception as e:
                conn.rollback(); return jsonify({"error":str(e)}),400
            finally: conn.close()
        @app.put(path+"/<int:item_id>", endpoint="compras_update_"+table)
        @staff_required
        def update_item(item_id):
            d=parse_json(); conn=get_db()
            try:
                cid,err=_cliente_id(conn)
                if err:return jsonify({"error":err}),401
                if not d.get("codigo") or not d.get("nombre"):
                    return jsonify({"error":"Código y nombre son obligatorios."}),400
                row=conn.execute(f"SELECT id FROM {table} WHERE id=? AND cliente_id=?",(item_id,cid)).fetchone()
                if not row:return jsonify({"error":"Registro no encontrado."}),404
                if table == "condiciones_compra":
                    tipo=(d.get("tipo") or "dias").strip().lower()
                    dias=int(d.get("dias_credito") or 0); cuotas=int(d.get("cuotas") or 1)
                    if tipo not in ("dias","cuotas"):return jsonify({"error":"Tipo de condición inválido."}),400
                    if tipo=="dias" and dias<0:return jsonify({"error":"Los días no pueden ser negativos."}),400
                    if tipo=="cuotas" and cuotas<1:return jsonify({"error":"La cantidad de cuotas debe ser al menos 1."}),400
                    d["tipo"],d["dias_credito"],d["cuotas"]=tipo,(dias if tipo=="dias" else 0),(cuotas if tipo=="cuotas" else 1)
                sets=",".join(f"{field}=?" for field in fields)
                vals=[d.get(field) for field in fields]+[item_id,cid]
                conn.execute(f"UPDATE {table} SET {sets} WHERE id=? AND cliente_id=?",vals)
                conn.commit(); return jsonify({"ok":True})
            except Exception as e:
                conn.rollback(); return jsonify({"error":str(e)}),400
            finally: conn.close()
        @app.get(path, endpoint="compras_list_"+table)
        @usuario_required
        def list_items():
            conn=get_db()
            try:
                cid,err=_cliente_id(conn)
                if err:return jsonify({"error":err}),401
                return jsonify([dict(x) for x in conn.execute(f"SELECT * FROM {table} WHERE cliente_id=? ORDER BY nombre",(cid,)).fetchall()])
            finally: conn.close()

    crud_catalogo("/api/compras/tipos-comprobante","tipos_comprobante_compra",["codigo","nombre","activo"])
    crud_catalogo("/api/compras/condiciones","condiciones_compra",["codigo","nombre","tipo","dias_credito","cuotas"])
    crud_catalogo("/api/compras/formas-pago","formas_pago_compra",["codigo","nombre","tipo","cuenta_contable_id"])
    @app.get("/api/compras/unidades-medida")
    @usuario_required
    def listar_unidades_medida():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            rows=conn.execute(
                "SELECT * FROM unidades_medida WHERE cliente_id=? ORDER BY activo DESC,nombre,codigo",
                (cid,)
            ).fetchall()
            return jsonify([dict(x) for x in rows])
        finally:
            conn.close()

    @app.post("/api/compras/unidades-medida")
    @staff_required
    def crear_unidad_medida():
        d=parse_json(); conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            codigo=(d.get("codigo") or "").strip().upper()
            nombre=(d.get("nombre") or "").strip()
            abreviatura=(d.get("abreviatura") or "").strip()
            if not codigo:return jsonify({"error":"El código de la unidad de medida es obligatorio."}),400
            if len(codigo)>10:return jsonify({"error":"El código no puede superar 10 caracteres."}),400
            if not nombre:return jsonify({"error":"El nombre de la unidad de medida es obligatorio."}),400
            if len(nombre)>80:return jsonify({"error":"El nombre no puede superar 80 caracteres."}),400
            if not abreviatura: abreviatura=codigo
            existe=conn.execute(
                "SELECT id FROM unidades_medida WHERE cliente_id=? AND UPPER(codigo)=UPPER(?) LIMIT 1",
                (cid,codigo)
            ).fetchone()
            if existe:return jsonify({"error":"Ya existe una unidad de medida con ese código."}),409
            rid=insertar_id(conn,
                "INSERT INTO unidades_medida(cliente_id,codigo,nombre,abreviatura,activo) VALUES(?,?,?,?,1)",
                (cid,codigo,nombre,abreviatura)
            )
            conn.commit()
            return jsonify({"ok":True,"id":rid}),201
        except Exception as e:
            conn.rollback();return jsonify({"error":str(e)}),400
        finally:
            conn.close()

    @app.put("/api/compras/unidades-medida/<int:unidad_id>")
    @staff_required
    def editar_unidad_medida(unidad_id):
        d=parse_json(); conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            row=conn.execute(
                "SELECT * FROM unidades_medida WHERE id=? AND cliente_id=?",
                (unidad_id,cid)
            ).fetchone()
            if not row:return jsonify({"error":"Unidad de medida no encontrada."}),404
            codigo=(d.get("codigo") if "codigo" in d else row["codigo"] or "").strip().upper()
            nombre=(d.get("nombre") if "nombre" in d else row["nombre"] or "").strip()
            abreviatura=(d.get("abreviatura") if "abreviatura" in d else row["abreviatura"] or "").strip()
            if not codigo:return jsonify({"error":"El código de la unidad de medida es obligatorio."}),400
            if len(codigo)>10:return jsonify({"error":"El código no puede superar 10 caracteres."}),400
            if not nombre:return jsonify({"error":"El nombre de la unidad de medida es obligatorio."}),400
            if not abreviatura:abreviatura=codigo
            activo=1 if str(d.get("estado","activo")).lower() in ("activo","1","true","on") else 0
            existe=conn.execute(
                "SELECT id FROM unidades_medida WHERE cliente_id=? AND UPPER(codigo)=UPPER(?) AND id<>? LIMIT 1",
                (cid,codigo,unidad_id)
            ).fetchone()
            if existe:return jsonify({"error":"Ya existe otra unidad de medida con ese código."}),409
            conn.execute(
                "UPDATE unidades_medida SET codigo=?,nombre=?,abreviatura=?,activo=?,actualizado_en=CAST(CURRENT_TIMESTAMP AS TEXT) "
                "WHERE id=? AND cliente_id=?",
                (codigo,nombre,abreviatura,activo,unidad_id,cid)
            )
            # Mantener el texto legado sincronizado para los conceptos asociados.
            conn.execute(
                "UPDATE conceptos_compra SET unidad_medida=? WHERE unidad_medida_id=? AND cliente_id=?",
                (nombre,unidad_id,cid)
            )
            conn.commit()
            return jsonify({"ok":True})
        except Exception as e:
            conn.rollback();return jsonify({"error":str(e)}),400
        finally:
            conn.close()

    @app.delete("/api/compras/unidades-medida/<int:unidad_id>")
    @staff_required
    def eliminar_unidad_medida(unidad_id):
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            row=conn.execute(
                "SELECT id FROM unidades_medida WHERE id=? AND cliente_id=?",
                (unidad_id,cid)
            ).fetchone()
            if not row:return jsonify({"error":"Unidad de medida no encontrada."}),404
            usada=conn.execute(
                "SELECT id FROM conceptos_compra WHERE unidad_medida_id=? AND cliente_id=? LIMIT 1",
                (unidad_id,cid)
            ).fetchone()
            if usada:
                return jsonify({"error":"No se puede eliminar esta unidad porque está asociada a uno o más ítems. Podés inactivarla para conservar la trazabilidad."}),409
            conn.execute("DELETE FROM unidades_medida WHERE id=? AND cliente_id=?",(unidad_id,cid))
            conn.commit()
            return jsonify({"ok":True})
        except Exception as e:
            conn.rollback();return jsonify({"error":str(e)}),400
        finally:
            conn.close()

    @app.get("/api/compras/conceptos/disponibles")
    @usuario_required
    def listar_conceptos_compra_disponibles():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            rows=conn.execute("""SELECT c.*, cc.codigo AS cuenta_codigo, cc.nombre AS cuenta_nombre
                FROM conceptos_compra c
                LEFT JOIN cuentas_contables cc ON cc.id=c.cuenta_contable_id
                WHERE c.cliente_id=? AND c.activo=1
                  AND c.cuenta_contable_id IS NOT NULL
                  AND COALESCE(TRIM(c.concepto_presupuestario),'')<>''
                ORDER BY c.codigo""",(cid,)).fetchall()
            return jsonify([dict(x) for x in rows])
        finally: conn.close()

    @app.post("/api/compras/conceptos")
    @staff_required
    def crear_concepto_compra():
        d=parse_json(); conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            descripcion=(d.get("descripcion") or d.get("nombre") or "").strip()
            unidad_id=d.get("unidad_medida_id")
            unidad=""
            if unidad_id not in (None,"","null"):
                try: unidad_id=int(unidad_id)
                except (TypeError,ValueError): return jsonify({"error":"Unidad de medida inválida."}),400
                unidad_row=conn.execute(
                    "SELECT id,nombre,activo FROM unidades_medida WHERE id=? AND cliente_id=?",
                    (unidad_id,cid)
                ).fetchone()
                if not unidad_row:return jsonify({"error":"La unidad de medida no existe para este cliente."}),400
                if int(unidad_row["activo"])==0:return jsonify({"error":"La unidad de medida seleccionada está inactiva."}),400
                unidad=unidad_row["nombre"]
            else:
                # Compatibilidad con clientes que todavía envíen el texto antiguo.
                unidad_texto=(d.get("unidad_medida") or "").strip()
                if unidad_texto:
                    unidad_row=conn.execute(
                        "SELECT id,nombre FROM unidades_medida WHERE cliente_id=? AND activo=1 "
                        "AND (UPPER(codigo)=UPPER(?) OR UPPER(nombre)=UPPER(?)) LIMIT 1",
                        (cid,unidad_texto,unidad_texto)
                    ).fetchone()
                    if unidad_row:
                        unidad_id=unidad_row["id"];unidad=unidad_row["nombre"]
            if not descripcion:return jsonify({"error":"La descripción es obligatoria."}),400
            if unidad_id in (None,"","null") or not unidad:return jsonify({"error":"Seleccioná una unidad de medida válida."}),400
            try: stock=float(d.get("stock_minimo") or 0)
            except (TypeError,ValueError): return jsonify({"error":"El stock mínimo debe ser numérico."}),400
            if stock<0:return jsonify({"error":"El stock mínimo no puede ser negativo."}),400
            try: iva=float(d.get("tasa_iva"))
            except (TypeError,ValueError): return jsonify({"error":"El tipo IVA es obligatorio."}),400
            if iva not in (0,5,10):return jsonify({"error":"El tipo IVA debe ser 0%, 5% o 10%."}),400
            rid=insertar_id(conn,"INSERT INTO conceptos_compra(cliente_id,codigo,nombre,descripcion,tipo,unidad_medida,unidad_medida_id,stock_minimo,cuenta_contable_id,concepto_presupuestario,tasa_iva,activo) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid,"0",descripcion,descripcion,(d.get("tipo") or "bien").strip(),unidad,int(unidad_id),stock,None,None,iva,1))
            codigo=str(rid)
            conn.execute("UPDATE conceptos_compra SET codigo=? WHERE id=? AND cliente_id=?",(codigo,rid,cid))
            conn.commit();return jsonify({"ok":True,"id":rid,"codigo":codigo}),201
        except Exception as e:
            conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.put("/api/compras/conceptos/<int:item_id>")
    @staff_required
    def editar_concepto_compra(item_id):
        d=parse_json(); conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            row=conn.execute("SELECT * FROM conceptos_compra WHERE id=? AND cliente_id=?",(item_id,cid)).fetchone()
            if not row:return jsonify({"error":"Ítem no encontrado."}),404
            descripcion=(d.get("descripcion") or "").strip()
            unidad_id=d.get("unidad_medida_id")
            if unidad_id in (None,"","null"):
                unidad_id=row["unidad_medida_id"]
            try: unidad_id=int(unidad_id)
            except (TypeError,ValueError): return jsonify({"error":"Unidad de medida inválida."}),400
            unidad_row=conn.execute(
                "SELECT id,nombre,activo FROM unidades_medida WHERE id=? AND cliente_id=?",
                (unidad_id,cid)
            ).fetchone()
            if not unidad_row:return jsonify({"error":"La unidad de medida no existe para este cliente."}),400
            if int(unidad_row["activo"])==0:return jsonify({"error":"La unidad de medida seleccionada está inactiva."}),400
            unidad=unidad_row["nombre"]
            if not descripcion:return jsonify({"error":"La descripción es obligatoria."}),400
            try: stock=float(d.get("stock_minimo") or 0)
            except (TypeError,ValueError):return jsonify({"error":"El stock mínimo debe ser numérico."}),400
            if stock<0:return jsonify({"error":"El stock mínimo no puede ser negativo."}),400
            try: iva=float(d.get("tasa_iva"))
            except (TypeError,ValueError):return jsonify({"error":"El tipo IVA es obligatorio."}),400
            if iva not in (0,5,10):return jsonify({"error":"El tipo IVA debe ser 0%, 5% o 10%."}),400
            activo=1 if str(d.get("estado","activo")).lower() in ("activo","1","true","on") else 0
            conn.execute("UPDATE conceptos_compra SET nombre=?,descripcion=?,unidad_medida=?,unidad_medida_id=?,stock_minimo=?,tasa_iva=?,activo=? WHERE id=? AND cliente_id=?",
                (descripcion,descripcion,unidad,int(unidad_id),stock,iva,activo,item_id,cid))
            conn.commit();return jsonify({"ok":True})
        except Exception as e:
            conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.post("/api/compras/conceptos/<int:item_id>/estado")
    @staff_required
    def cambiar_estado_concepto_compra(item_id):
        d=parse_json(); conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            row=conn.execute("SELECT id FROM conceptos_compra WHERE id=? AND cliente_id=?",(item_id,cid)).fetchone()
            if not row:return jsonify({"error":"Ítem no encontrado."}),404
            activo=1 if bool(d.get("activo")) else 0
            conn.execute("UPDATE conceptos_compra SET activo=? WHERE id=? AND cliente_id=?",(activo,item_id,cid))
            conn.commit();return jsonify({"ok":True})
        except Exception as e:
            conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.delete("/api/compras/conceptos/<int:item_id>")
    @staff_required
    def eliminar_concepto_compra(item_id):
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            row=conn.execute("SELECT id FROM conceptos_compra WHERE id=? AND cliente_id=?",(item_id,cid)).fetchone()
            if not row:return jsonify({"error":"Ítem no encontrado."}),404
            usado=conn.execute(
                "SELECT 1 FROM ordenes_compra_detalle WHERE concepto_id=? LIMIT 1",
                (item_id,)
            ).fetchone() or conn.execute(
                "SELECT 1 FROM comprobantes_compra_detalle WHERE concepto_id=? LIMIT 1",
                (item_id,)
            ).fetchone()
            if usado:
                return jsonify({"error":"No se puede eliminar este ítem porque ya fue utilizado en una operación. Podés inactivarlo para conservar la trazabilidad."}),409
            conn.execute("DELETE FROM conceptos_compra WHERE id=? AND cliente_id=?",(item_id,cid))
            conn.commit();return jsonify({"ok":True})
        except Exception as e:
            conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.put("/api/contabilidad/cuentas-articulos/<int:item_id>")
    @staff_required
    def asignar_contabilidad_articulo(item_id):
        d=parse_json(); conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            row=conn.execute("SELECT id FROM conceptos_compra WHERE id=? AND cliente_id=?",(item_id,cid)).fetchone()
            if not row:return jsonify({"error":"Ítem no encontrado."}),404
            concepto=(d.get("concepto_presupuestario") or "").strip()
            cuenta=d.get("cuenta_contable_id")
            if not concepto:return jsonify({"error":"El concepto presupuestario es obligatorio."}),400
            if cuenta in (None,"","null"):return jsonify({"error":"La cuenta contable es obligatoria."}),400
            try: cuenta=int(cuenta)
            except (TypeError,ValueError):return jsonify({"error":"Cuenta contable inválida."}),400
            ok=conn.execute("SELECT id FROM cuentas_contables WHERE id=? AND (cliente_id=? OR cliente_id IS NULL) AND activa=1 AND imputable=1",(cuenta,cid)).fetchone()
            if not ok:return jsonify({"error":"La cuenta contable seleccionada no existe, no pertenece al cliente o no es imputable/activa."}),400
            conn.execute("UPDATE conceptos_compra SET concepto_presupuestario=?,cuenta_contable_id=? WHERE id=? AND cliente_id=?",(concepto,cuenta,item_id,cid))
            conn.commit();return jsonify({"ok":True})
        except Exception as e:
            conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.get("/api/contabilidad/cuentas-articulos")
    @usuario_required
    def listar_cuentas_articulos():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            rows=conn.execute("""SELECT c.*, cc.codigo AS cuenta_codigo, cc.nombre AS cuenta_nombre
                FROM conceptos_compra c
                LEFT JOIN cuentas_contables cc ON cc.id=c.cuenta_contable_id
                WHERE c.cliente_id=? ORDER BY c.codigo""",(cid,)).fetchall()
            return jsonify([dict(x) for x in rows])
        finally:conn.close()

    @app.put("/api/compras/formas_pago_compra/<int:item_id>")
    @staff_required
    def editar_forma_pago_compra(item_id):
        d=parse_json(); conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            row=conn.execute("SELECT * FROM formas_pago_compra WHERE id=? AND cliente_id=?",(item_id,cid)).fetchone()
            if not row:return jsonify({"error":"Forma de pago no encontrada."}),404
            codigo=(d.get("codigo") or "").strip(); nombre=(d.get("nombre") or "").strip(); tipo=(d.get("tipo") or "contado").strip()
            if not codigo or not nombre:return jsonify({"error":"Código y nombre son obligatorios."}),400
            cuenta=d.get("cuenta_contable_id")
            if cuenta not in (None,"","null"):
                cuenta=int(cuenta)
                ok=conn.execute("SELECT id FROM cuentas_contables WHERE id=? AND (cliente_id=? OR cliente_id IS NULL)",(cuenta,cid)).fetchone()
                if not ok:return jsonify({"error":"La cuenta contable seleccionada no existe o no pertenece al cliente."}),400
            else: cuenta=None
            conn.execute("UPDATE formas_pago_compra SET codigo=?,nombre=?,tipo=?,cuenta_contable_id=?,activo=? WHERE id=? AND cliente_id=?",(codigo,nombre,tipo,cuenta,1 if d.get("activo",1) else 0,item_id,cid))
            conn.commit();return jsonify({"ok":True})
        except Exception as e:
            conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    for _table in ("tipos_comprobante_compra","condiciones_compra"):
        @app.put(f"/api/compras/{_table}/<int:item_id>", endpoint="compras_edit_"+_table)
        @staff_required
        def editar_catalogo_compra(item_id, _table=_table):
            d=parse_json(); conn=get_db()
            try:
                cid,err=_cliente_id(conn)
                if err:return jsonify({"error":err}),401
                row=conn.execute(f"SELECT * FROM {_table} WHERE id=? AND cliente_id=?",(item_id,cid)).fetchone()
                if not row:return jsonify({"error":"Registro no encontrado."}),404
                if not d.get("codigo") or not d.get("nombre"):return jsonify({"error":"Código y nombre son obligatorios."}),400
                if _table=="tipos_comprobante_compra":
                    conn.execute("UPDATE tipos_comprobante_compra SET codigo=?,nombre=?,activo=? WHERE id=? AND cliente_id=?",(d["codigo"].strip(),d["nombre"].strip(),1 if d.get("activo",1) else 0,item_id,cid))
                else:
                    tipo=(d.get("tipo") or "dias").strip().lower(); dias=int(d.get("dias_credito") or 0); cuotas=int(d.get("cuotas") or 1)
                    if tipo not in ("dias","cuotas"):return jsonify({"error":"Tipo de condición inválido."}),400
                    if tipo=="dias" and dias<0:return jsonify({"error":"Los días no pueden ser negativos."}),400
                    if tipo=="cuotas" and cuotas<1:return jsonify({"error":"La cantidad de cuotas debe ser al menos 1."}),400
                    conn.execute("UPDATE condiciones_compra SET codigo=?,nombre=?,tipo=?,dias_credito=?,cuotas=?,activo=? WHERE id=? AND cliente_id=?",(d["codigo"].strip(),d["nombre"].strip(),tipo,dias if tipo=="dias" else 0,cuotas if tipo=="cuotas" else 1,1 if d.get("activo",1) else 0,item_id,cid))
                conn.commit();return jsonify({"ok":True})
            except Exception as e:
                conn.rollback();return jsonify({"error":str(e)}),400
            finally:conn.close()

        @app.delete(f"/api/compras/{_table}/<int:item_id>", endpoint="compras_delete_"+_table)
        @staff_required
        def eliminar_catalogo_compra(item_id, _table=_table):
            conn=get_db()
            try:
                cid,err=_cliente_id(conn)
                if err:return jsonify({"error":err}),401
                row=conn.execute(f"SELECT id FROM {_table} WHERE id=? AND cliente_id=?",(item_id,cid)).fetchone()
                if not row:return jsonify({"error":"Registro no encontrado."}),404
                conn.execute(f"DELETE FROM {_table} WHERE id=? AND cliente_id=?",(item_id,cid));conn.commit()
                return jsonify({"ok":True})
            except Exception as e:
                conn.rollback();return jsonify({"error":str(e)}),400
            finally:conn.close()

    @app.get("/api/compras/cuotas/<int:comprobante_id>")
    @usuario_required
    def listar_cuotas_compra(comprobante_id):
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            rows=conn.execute("SELECT * FROM cuotas_compras WHERE comprobante_id=? AND cliente_id=? ORDER BY numero_cuota",(comprobante_id,cid)).fetchall()
            return jsonify([dict(x) for x in rows])
        finally:conn.close()

    @app.post("/api/compras/proveedores/<int:proveedor_id>")
    @staff_required
    def editar_proveedor(proveedor_id):
        d=parse_json(); conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            row=conn.execute("SELECT * FROM proveedores WHERE id=? AND cliente_id=?",(proveedor_id,cid)).fetchone()
            if not row:return jsonify({"error":"Proveedor no encontrado."}),404

            campos=["ruc","razon_social","nombre_comercial","documento","correo","telefono","direccion"]
            updates=[]; vals=[]
            for campo in campos:
                if campo in d:
                    updates.append(campo+"=?")
                    vals.append(d.get(campo))

            if not updates:
                return jsonify({"error":"No se recibieron datos para actualizar."}),400

            ruc=(d.get("ruc") if "ruc" in d else row["ruc"]) or ""
            ruc=str(ruc).strip().upper()
            if not ruc:
                return jsonify({"error":"El RUC es obligatorio."}),400

            existente=conn.execute(
                "SELECT id FROM proveedores WHERE cliente_id=? AND UPPER(ruc)=? AND id<>?",
                (cid,ruc,proveedor_id)
            ).fetchone()
            if existente:
                return jsonify({"error":"Ya existe otro proveedor con ese RUC."}),409

            if "ruc" in d:
                for i,cambio in enumerate(updates):
                    if cambio=="ruc=?":
                        vals[i]=ruc
                        break

            vals += [cid,proveedor_id]
            conn.execute(
                "UPDATE proveedores SET "+", ".join(updates)+", actualizado_en=CAST(CURRENT_TIMESTAMP AS TEXT) WHERE cliente_id=? AND id=?",
                vals
            )
            conn.commit()
            return jsonify({"ok":True})
        except Exception as e:
            conn.rollback()
            return jsonify({"error":str(e)}),400
        finally:
            conn.close()

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
            cols=["cliente_id","proveedor_id","tipo_comprobante_id","timbrado_id","numero","cdc","fecha","condicion_id","forma_pago_id","estado","moneda","gravado_10","gravado_5","exento","iva_10","iva_5","total","orden_compra_id","origen","observacion","creado_por"]
            timbrado,terr=_validar_timbrado(conn,cid,int(d["proveedor_id"]),d.get("tipo_comprobante_id"),d["numero"],d["fecha"],d.get("timbrado_id"))
            if terr:return jsonify({"error":terr}),400
            vals=[cid,d["proveedor_id"],d.get("tipo_comprobante_id"),timbrado["id"],d["numero"],d.get("cdc",""),d["fecha"],d.get("condicion_id"),d.get("forma_pago_id"),d.get("estado","registrado"),d.get("moneda","PYG"),float(d.get("gravado_10",0) or 0),float(d.get("gravado_5",0) or 0),float(d.get("exento",0) or 0),float(d.get("iva_10",0) or 0),float(d.get("iva_5",0) or 0),float(d.get("total",0) or 0),d.get("orden_compra_id"),d.get("origen","MANUAL"),d.get("observacion",""),None]
            cidc=insertar_id(conn, "INSERT INTO comprobantes_compra("+",".join(cols)+") VALUES("+",".join(["?"]*len(cols))+")", vals)
            if d.get("condicion_id"):
                condicion=conn.execute("SELECT * FROM condiciones_compra WHERE id=? AND cliente_id=? AND activo=1",(int(d["condicion_id"]),cid)).fetchone()
                if not condicion:return jsonify({"error":"Condición de compra inválida."}),400
                from datetime import date,timedelta
                fecha_base=date.fromisoformat(str(d["fecha"])[:10])
                tipo_cond=(condicion["tipo"] or "dias").lower()
                cantidad=int(condicion["cuotas"] or 1) if tipo_cond=="cuotas" else 1
                fechas=[fecha_base+timedelta(days=30*(i+1)) for i in range(cantidad)] if tipo_cond=="cuotas" else [fecha_base+timedelta(days=int(condicion["dias_credito"] or 0))]
                total=float(d.get("total",0) or 0); base=round(total/cantidad,2)
                for i,venc in enumerate(fechas,1):
                    importe=base if i<cantidad else round(total-base*(cantidad-1),2)
                    conn.execute("INSERT INTO cuotas_compras(cliente_id,comprobante_id,numero_cuota,fecha_vencimiento,importe,saldo,estado) VALUES(?,?,?,?,?,?,?)",(cid,cidc,i,venc.isoformat(),importe,importe,"pendiente"))
            for item in d.get("detalle",[]):
                concepto_id=item.get("concepto_id")
                iva_tasa=float(item.get("iva_tasa",10) or 0)
                cuenta_detalle=item.get("cuenta_contable_id")
                if concepto_id not in (None,"","null"):
                    concepto=conn.execute("""SELECT * FROM conceptos_compra
                        WHERE id=? AND cliente_id=? AND activo=1
                          AND cuenta_contable_id IS NOT NULL
                          AND COALESCE(TRIM(concepto_presupuestario),'')<>''""",(int(concepto_id),cid)).fetchone()
                    if not concepto:
                        return jsonify({"error":"El ítem seleccionado todavía no está habilitado por Contabilidad."}),400
                    iva_tasa=float(concepto["tasa_iva"] or 0)
                    cuenta_detalle=concepto["cuenta_contable_id"]
                cantidad_item=float(item.get("cantidad",1) or 1)
                precio_item=float(item.get("precio_unitario",0) or 0)
                subtotal_item=float(item.get("subtotal",0) or 0)
                conn.execute("""INSERT INTO comprobantes_compra_detalle(comprobante_id,concepto_id,descripcion,cantidad,precio_unitario,iva_tasa,subtotal,cuenta_contable_id) VALUES(?,?,?,?,?,?,?,?)""",
                    (cidc,concepto_id,item.get("descripcion",""),cantidad_item,precio_item,iva_tasa,subtotal_item,cuenta_detalle))
                if concepto_id not in (None,"","null") and d.get("estado","registrado") not in ("borrador","anulado"):
                    registrar_ingreso_compra(conn,cid,int(cidc),{
                        "concepto_id":concepto_id,"cantidad":cantidad_item,"precio_unitario":precio_item
                    },usuario_id=None,fecha=d.get("fecha"))
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
            conn.execute("UPDATE comprobantes_compra SET estado=?, actualizado_en=CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id=? AND cliente_id=?",(estado,comp_id,cid)); conn.commit()
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
