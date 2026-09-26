import os
import hashlib
from datetime import datetime, date
from flask import request, jsonify

def _id_col():
    return "BIGSERIAL" if os.environ.get("DATABASE_URL") else "INTEGER"

def _cliente_id(conn):
    raw = request.headers.get("X-Cliente-ID") or request.args.get("cliente_id")
    if not raw:
        return None, "Seleccioná un cliente activo."
    try:
        cid = int(raw)
    except (TypeError, ValueError):
        return None, "Cliente inválido."
    token = request.cookies.get("__Host-kakuaa_session", "")
    if not token:
        return None, "No autorizado."
    uid = conn.execute("SELECT id, rol FROM usuarios WHERE token_sesion_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if not uid:
        return None, "No autorizado."
    ok = conn.execute("SELECT 1 FROM usuario_clientes WHERE usuario_id=? AND cliente_id=? AND activo=1", (uid["id"], cid)).fetchone()
    if not ok and uid["rol"] not in ("superadmin","admin"):
        return None, "No autorizado para este cliente."
    exists = conn.execute("SELECT 1 FROM clientes WHERE id=? AND estado='activo'", (cid,)).fetchone()
    return (cid, None) if exists else (None, "Cliente no encontrado o inactivo.")

def _usuario_id(conn):
    token = request.cookies.get("__Host-kakuaa_session", "")
    if not token:
        return None
    row = conn.execute("SELECT id FROM usuarios WHERE token_sesion_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    return row["id"] if row else None

def _insert_id(conn, sql, params=()):
    if os.environ.get("DATABASE_URL"):
        row = conn.execute(sql.rstrip().rstrip(";") + " RETURNING id", params).fetchone()
        return row["id"]
    return conn.execute(sql, params).lastrowid

def _ensure_account(conn, cliente_id, codigo, nombre, naturaleza="DEUDORA", tipo="ACTIVO"):
    row = conn.execute("SELECT id FROM cuentas_contables WHERE cliente_id=? AND codigo=? LIMIT 1", (cliente_id,codigo)).fetchone()
    if row:
        return row["id"]
    return _insert_id(conn, """INSERT INTO cuentas_contables(cliente_id,codigo,nombre,tipo,naturaleza,nivel,imputable,activa)
        VALUES(?,?,?,?,?,?,1,1)""", (cliente_id,codigo,nombre,tipo,naturaleza,1))

def init_erp(get_db):
    conn=get_db()
    idc=_id_col()
    try:
        conn.execute(f"""CREATE TABLE IF NOT EXISTS depositos_inventario(
            id {idc} PRIMARY KEY, cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL, nombre TEXT NOT NULL,
            ubicacion TEXT DEFAULT '', activo INTEGER NOT NULL DEFAULT 1, creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(cliente_id,codigo))""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS inventario_items(
            id {idc} PRIMARY KEY, cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL, nombre TEXT NOT NULL,
            concepto_compra_id INTEGER DEFAULT NULL, articulo_venta_id INTEGER DEFAULT NULL,
            unidad_medida_id INTEGER DEFAULT NULL, inventariable INTEGER NOT NULL DEFAULT 1,
            stock_minimo REAL NOT NULL DEFAULT 0, costo_promedio REAL NOT NULL DEFAULT 0,
            cuenta_inventario_id INTEGER DEFAULT NULL, cuenta_costo_id INTEGER DEFAULT NULL,
            cuenta_venta_id INTEGER DEFAULT NULL, activo INTEGER NOT NULL DEFAULT 1,
            creado_en TEXT DEFAULT CURRENT_TIMESTAMP, actualizado_en TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(cliente_id,codigo))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_items_cliente ON inventario_items(cliente_id,activo,codigo)")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS inventario_stock(
            id {idc} PRIMARY KEY, item_id INTEGER NOT NULL, deposito_id INTEGER NOT NULL,
            existencia REAL NOT NULL DEFAULT 0, reservado REAL NOT NULL DEFAULT 0,
            actualizado_en TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(item_id,deposito_id))""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS inventario_movimientos(
            id {idc} PRIMARY KEY, cliente_id INTEGER NOT NULL, item_id INTEGER NOT NULL, deposito_id INTEGER NOT NULL,
            fecha TEXT NOT NULL, tipo TEXT NOT NULL, cantidad REAL NOT NULL, costo_unitario REAL NOT NULL DEFAULT 0,
            referencia_tipo TEXT DEFAULT NULL, referencia_id INTEGER DEFAULT NULL, observacion TEXT DEFAULT '',
            creado_por INTEGER, creado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_mov_item ON inventario_movimientos(cliente_id,item_id,fecha,id)")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS activos_fijos(
            id {idc} PRIMARY KEY, cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL, descripcion TEXT NOT NULL,
            categoria TEXT NOT NULL DEFAULT 'General', fecha_adquisicion TEXT NOT NULL, fecha_inicio_uso TEXT NOT NULL,
            valor_costo REAL NOT NULL DEFAULT 0, valor_residual REAL NOT NULL DEFAULT 0,
            vida_util_meses INTEGER NOT NULL DEFAULT 60, metodo TEXT NOT NULL DEFAULT 'lineal',
            cuenta_activo_id INTEGER DEFAULT NULL, cuenta_depreciacion_id INTEGER DEFAULT NULL,
            cuenta_gasto_id INTEGER DEFAULT NULL, ubicacion TEXT DEFAULT '', responsable TEXT DEFAULT '',
            numero_serie TEXT DEFAULT '', proveedor TEXT DEFAULT '', factura_referencia TEXT DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'activo', creado_por INTEGER, creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
            actualizado_en TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(cliente_id,codigo))""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS depreciacion_activos(
            id {idc} PRIMARY KEY, activo_id INTEGER NOT NULL, periodo TEXT NOT NULL, fecha TEXT NOT NULL,
            valor_inicial REAL NOT NULL, depreciacion REAL NOT NULL, depreciacion_acumulada REAL NOT NULL,
            valor_libro REAL NOT NULL, asiento_id INTEGER DEFAULT NULL, contabilizado INTEGER NOT NULL DEFAULT 0,
            UNIQUE(activo_id,periodo))""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS personas(
            id {idc} PRIMARY KEY, cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL, nombres TEXT NOT NULL,
            apellidos TEXT NOT NULL, documento TEXT DEFAULT '', fecha_nacimiento TEXT DEFAULT NULL,
            sexo TEXT DEFAULT '', correo TEXT DEFAULT '', telefono TEXT DEFAULT '', direccion TEXT DEFAULT '',
            cargo TEXT DEFAULT '', departamento TEXT DEFAULT '', fecha_ingreso TEXT DEFAULT NULL,
            fecha_salida TEXT DEFAULT NULL, tipo_contrato TEXT DEFAULT 'indefinido',
            salario_base REAL NOT NULL DEFAULT 0, estado TEXT NOT NULL DEFAULT 'activo',
            usuario_id INTEGER DEFAULT NULL, banco TEXT DEFAULT '', cuenta_bancaria TEXT DEFAULT '',
            creado_en TEXT DEFAULT CURRENT_TIMESTAMP, actualizado_en TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(cliente_id,codigo))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_personas_cliente ON personas(cliente_id,estado,apellidos,nombres)")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS personas_asistencia(
            id {idc} PRIMARY KEY, persona_id INTEGER NOT NULL, fecha TEXT NOT NULL,
            entrada TEXT DEFAULT NULL, salida TEXT DEFAULT NULL, estado TEXT NOT NULL DEFAULT 'presente',
            observacion TEXT DEFAULT '', UNIQUE(persona_id,fecha))""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS personas_permisos(
            id {idc} PRIMARY KEY, persona_id INTEGER NOT NULL, tipo TEXT NOT NULL,
            desde TEXT NOT NULL, hasta TEXT NOT NULL, dias REAL NOT NULL DEFAULT 0,
            estado TEXT NOT NULL DEFAULT 'pendiente', motivo TEXT DEFAULT '', aprobado_por INTEGER DEFAULT NULL,
            creado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS personas_contratos(
            id {idc} PRIMARY KEY, persona_id INTEGER NOT NULL, tipo TEXT NOT NULL DEFAULT 'indefinido',
            fecha_inicio TEXT NOT NULL, fecha_fin TEXT DEFAULT NULL, salario REAL NOT NULL DEFAULT 0,
            cargo TEXT DEFAULT '', documento_referencia TEXT DEFAULT '', estado TEXT NOT NULL DEFAULT 'vigente',
            creado_en TEXT DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS personas_nomina(
            id {idc} PRIMARY KEY, persona_id INTEGER NOT NULL, periodo TEXT NOT NULL,
            salario_base REAL NOT NULL DEFAULT 0, adicionales REAL NOT NULL DEFAULT 0,
            descuentos REAL NOT NULL DEFAULT 0, neto REAL NOT NULL DEFAULT 0,
            estado TEXT NOT NULL DEFAULT 'borrador', asiento_id INTEGER DEFAULT NULL,
            creado_en TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(persona_id,periodo))""")
        # Cada cliente recibe un depósito principal.
        clientes=conn.execute("SELECT id FROM clientes").fetchall()
        for row in clientes:
            cid=row["id"]
            conn.execute("""INSERT INTO depositos_inventario(cliente_id,codigo,nombre,activo)
                VALUES(?,?,?,1) ON CONFLICT (cliente_id,codigo) DO NOTHING""",(cid,"PRINCIPAL","Depósito Principal"))
        conn.commit()
    finally:
        conn.close()

def _get_principal_deposito(conn, cliente_id):
    row=conn.execute("SELECT id FROM depositos_inventario WHERE cliente_id=? AND activo=1 ORDER BY id LIMIT 1",(cliente_id,)).fetchone()
    if row: return row["id"]
    return _insert_id(conn,"INSERT INTO depositos_inventario(cliente_id,codigo,nombre,activo) VALUES(?,?,?,1)",(cliente_id,"PRINCIPAL","Depósito Principal"))

def _find_inventory_item(conn, cliente_id, articulo_id=None, concepto_id=None):
    if articulo_id:
        row=conn.execute("SELECT * FROM inventario_items WHERE cliente_id=? AND articulo_venta_id=? AND activo=1 LIMIT 1",(cliente_id,articulo_id)).fetchone()
        if row:return row
    if concepto_id:
        row=conn.execute("SELECT * FROM inventario_items WHERE cliente_id=? AND concepto_compra_id=? AND activo=1 LIMIT 1",(cliente_id,concepto_id)).fetchone()
        if row:return row
    return None

def stock_suficiente_para_venta(conn, cliente_id, articulo_id, cantidad):
    """Devuelve None si no se debe controlar stock; devuelve error si falta stock."""
    try:
        item=_find_inventory_item(conn,cliente_id,articulo_id=articulo_id)
        if not item or not int(item["inventariable"] or 0):
            return None
        dep=_get_principal_deposito(conn,cliente_id)
        row=conn.execute("SELECT existencia,reservado FROM inventario_stock WHERE item_id=? AND deposito_id=?",(item["id"],dep)).fetchone()
        disponible=float(row["existencia"] or 0)-float(row["reservado"] or 0) if row else 0
        if disponible < float(cantidad):
            return f"Stock insuficiente para {item['nombre']}. Disponible: {disponible:g}; solicitado: {float(cantidad):g}."
    except Exception:
        return None
    return None

def registrar_salida_venta(conn, cliente_id, articulo_id, cantidad, factura_id, usuario_id, fecha):
    item=_find_inventory_item(conn,cliente_id,articulo_id=articulo_id)
    if not item or not int(item["inventariable"] or 0):
        return
    dep=_get_principal_deposito(conn,cliente_id)
    stock=conn.execute("SELECT existencia FROM inventario_stock WHERE item_id=? AND deposito_id=?",(item["id"],dep)).fetchone()
    existencia=float(stock["existencia"] or 0) if stock else 0
    if existencia < float(cantidad):
        raise ValueError(f"Stock insuficiente para {item['nombre']}.")
    costo=float(item["costo_promedio"] or 0)
    conn.execute("UPDATE inventario_stock SET existencia=existencia-?, actualizado_en=CURRENT_TIMESTAMP WHERE item_id=? AND deposito_id=?",(cantidad,item["id"],dep))
    conn.execute("""INSERT INTO inventario_movimientos(cliente_id,item_id,deposito_id,fecha,tipo,cantidad,costo_unitario,referencia_tipo,referencia_id,creado_por)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",(cliente_id,item["id"],dep,fecha,"SALIDA_VENTA",-float(cantidad),costo,"FACTURA_VENTA",factura_id,usuario_id))

def registrar_ingreso_compra(conn, cliente_id, comprobante_id, detalle, usuario_id=None, fecha=None):
    """Entrada automática de stock desde una factura de compra. Solo procesa conceptos vinculados/inventariables."""
    fecha=fecha or datetime.utcnow().strftime("%Y-%m-%d")
    concepto_id=detalle.get("concepto_id")
    if not concepto_id:return
    if conn.execute("SELECT 1 FROM inventario_movimientos WHERE referencia_tipo='COMPROBANTE_COMPRA' AND referencia_id=? AND item_id IN (SELECT id FROM inventario_items WHERE concepto_compra_id=?) LIMIT 1",(comprobante_id,int(concepto_id))).fetchone():
        return
    item=_find_inventory_item(conn,cliente_id,concepto_id=int(concepto_id))
    if not item:
        concepto=conn.execute("SELECT * FROM conceptos_compra WHERE id=? AND cliente_id=? AND activo=1",(int(concepto_id),cliente_id)).fetchone()
        if not concepto or str(concepto["tipo"] or "").lower() in ("servicio","gasto"): return
        codigo=str(concepto["codigo"])
        item_id=_insert_id(conn,"""INSERT INTO inventario_items(cliente_id,codigo,nombre,concepto_compra_id,unidad_medida_id,inventariable,stock_minimo,activo)
            VALUES(?,?,?,?,?,1,?,1)""",(cliente_id,codigo,concepto["nombre"],concepto["id"],concepto["unidad_medida_id"],float(concepto["stock_minimo"] or 0)))
        item=conn.execute("SELECT * FROM inventario_items WHERE id=?",(item_id,)).fetchone()
        # Vinculación automática por código con el artículo de ventas, si existe.
        venta=conn.execute("SELECT id FROM articulos WHERE codigo=? LIMIT 1",(codigo,)).fetchone()
        if venta:
            conn.execute("UPDATE inventario_items SET articulo_venta_id=? WHERE id=?",(venta["id"],item_id))
    dep=_get_principal_deposito(conn,cliente_id)
    cantidad=float(detalle.get("cantidad") or 0)
    costo=float(detalle.get("precio_unitario") or 0)
    if cantidad<=0:return
    old=conn.execute("SELECT existencia FROM inventario_stock WHERE item_id=? AND deposito_id=?",(item["id"],dep)).fetchone()
    existencia=float(old["existencia"] or 0) if old else 0
    new=existencia+cantidad
    oldcost=float(item["costo_promedio"] or 0)
    avg=((existencia*oldcost)+(cantidad*costo))/new if new else costo
    conn.execute("""INSERT INTO inventario_stock(item_id,deposito_id,existencia,reservado)
        VALUES(?,?,?,0) ON CONFLICT (item_id,deposito_id) DO UPDATE SET existencia=excluded.existencia, actualizado_en=CURRENT_TIMESTAMP""",(item["id"],dep,new))
    conn.execute("UPDATE inventario_items SET costo_promedio=?, actualizado_en=CURRENT_TIMESTAMP WHERE id=?",(avg,item["id"]))
    conn.execute("""INSERT INTO inventario_movimientos(cliente_id,item_id,deposito_id,fecha,tipo,cantidad,costo_unitario,referencia_tipo,referencia_id,creado_por)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",(cliente_id,item["id"],dep,fecha,"ENTRADA_COMPRA",cantidad,costo,"COMPROBANTE_COMPRA",comprobante_id,usuario_id))

def register(app,get_db,staff_required,usuario_required,admin_required):
    init_erp(get_db)

    @app.get("/api/inventarios/dashboard")
    @usuario_required
    def inv_dashboard():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            rows=conn.execute("""SELECT i.id,i.codigo,i.nombre,i.stock_minimo,i.costo_promedio,
                COALESCE(SUM(s.existencia),0) existencia,COALESCE(SUM(s.reservado),0) reservado
                FROM inventario_items i LEFT JOIN inventario_stock s ON s.item_id=i.id
                WHERE i.cliente_id=? AND i.activo=1 GROUP BY i.id ORDER BY i.nombre""",(cid,)).fetchall()
            data=[dict(x) for x in rows]
            for x in data:x["disponible"]=float(x["existencia"] or 0)-float(x["reservado"] or 0);x["alerta"]=x["disponible"]<=float(x["stock_minimo"] or 0)
            return jsonify({"items":data,"total_items":len(data),"alertas":sum(1 for x in data if x["alerta"])})
        finally:conn.close()

    @app.get("/api/inventarios/items")
    @usuario_required
    def inv_items():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            rows=conn.execute("""SELECT i.*,COALESCE(SUM(s.existencia),0) existencia,COALESCE(SUM(s.reservado),0) reservado
                FROM inventario_items i LEFT JOIN inventario_stock s ON s.item_id=i.id
                WHERE i.cliente_id=? GROUP BY i.id ORDER BY i.codigo""",(cid,)).fetchall()
            return jsonify([dict(x) for x in rows])
        finally:conn.close()

    @app.post("/api/inventarios/items")
    @staff_required
    def inv_create_item():
        d=request.get_json() or {};conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            codigo=str(d.get("codigo","")).strip();nombre=str(d.get("nombre","")).strip()
            if not codigo or not nombre:return jsonify({"error":"Código y nombre son obligatorios."}),400
            item_id=_insert_id(conn,"""INSERT INTO inventario_items(cliente_id,codigo,nombre,concepto_compra_id,articulo_venta_id,unidad_medida_id,inventariable,stock_minimo,cuenta_inventario_id,cuenta_costo_id,cuenta_venta_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(cid,codigo,nombre,d.get("concepto_compra_id") or None,d.get("articulo_venta_id") or None,d.get("unidad_medida_id") or None,int(bool(d.get("inventariable",1))),float(d.get("stock_minimo") or 0),d.get("cuenta_inventario_id") or None,d.get("cuenta_costo_id") or None,d.get("cuenta_venta_id") or None))
            conn.commit();return jsonify({"ok":True,"id":item_id}),201
        except Exception as e:conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.post("/api/inventarios/movimientos")
    @staff_required
    def inv_move():
        d=request.get_json() or {};conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            item=conn.execute("SELECT * FROM inventario_items WHERE id=? AND cliente_id=? AND activo=1",(d.get("item_id"),cid)).fetchone()
            if not item:return jsonify({"error":"Ítem de inventario inválido."}),400
            dep=int(d.get("deposito_id") or _get_principal_deposito(conn,cid)); qty=float(d.get("cantidad") or 0)
            if qty<=0:return jsonify({"error":"La cantidad debe ser mayor a cero."}),400
            tipo=str(d.get("tipo","AJUSTE_ENTRADA")).upper()
            signo=1 if tipo in ("AJUSTE_ENTRADA","ENTRADA","DEVOLUCION_COMPRA") else -1
            row=conn.execute("SELECT existencia FROM inventario_stock WHERE item_id=? AND deposito_id=?",(item["id"],dep)).fetchone()
            existencia=float(row["existencia"] or 0) if row else 0
            nueva=existencia+(qty*signo)
            if nueva < 0:return jsonify({"error":"El movimiento dejaría stock negativo."}),409
            conn.execute("""INSERT INTO inventario_stock(item_id,deposito_id,existencia,reservado) VALUES(?,?,?,0)
                ON CONFLICT(item_id,deposito_id) DO UPDATE SET existencia=excluded.existencia,actualizado_en=CURRENT_TIMESTAMP""",(item["id"],dep,nueva))
            conn.execute("""INSERT INTO inventario_movimientos(cliente_id,item_id,deposito_id,fecha,tipo,cantidad,costo_unitario,observacion,creado_por)
                VALUES(?,?,?,?,?,?,?,?,?)""",(cid,item["id"],dep,d.get("fecha") or datetime.utcnow().strftime("%Y-%m-%d"),tipo,qty*signo,float(d.get("costo_unitario") or item["costo_promedio"] or 0),str(d.get("observacion","")), _usuario_id(conn)))
            conn.commit();return jsonify({"ok":True,"existencia":nueva})
        except Exception as e:conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.get("/api/inventarios/movimientos")
    @usuario_required
    def inv_movimientos():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            rows=conn.execute("""SELECT m.*,i.codigo,i.nombre item_nombre,d.nombre deposito_nombre
                FROM inventario_movimientos m JOIN inventario_items i ON i.id=m.item_id
                JOIN depositos_inventario d ON d.id=m.deposito_id WHERE m.cliente_id=?
                ORDER BY m.fecha DESC,m.id DESC LIMIT 300""",(cid,)).fetchall()
            return jsonify([dict(x) for x in rows])
        finally:conn.close()

    @app.get("/api/inventarios/depositos")
    @usuario_required
    def inv_depositos():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            return jsonify([dict(x) for x in conn.execute("SELECT * FROM depositos_inventario WHERE cliente_id=? ORDER BY nombre",(cid,)).fetchall()])
        finally:conn.close()

    @app.post("/api/inventarios/depositos")
    @admin_required
    def inv_deposito_create():
        d=request.get_json() or {};conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            dep_id=_insert_id(conn,"INSERT INTO depositos_inventario(cliente_id,codigo,nombre,ubicacion) VALUES(?,?,?,?)",(cid,str(d.get("codigo","")).strip(),str(d.get("nombre","")).strip(),str(d.get("ubicacion",""))))
            conn.commit();return jsonify({"ok":True,"id":dep_id}),201
        except Exception as e:conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.get("/api/activos-fijos")
    @usuario_required
    def af_list():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            rows=conn.execute("SELECT * FROM activos_fijos WHERE cliente_id=? ORDER BY estado,descripcion",(cid,)).fetchall()
            return jsonify([dict(x) for x in rows])
        finally:conn.close()

    @app.post("/api/activos-fijos")
    @staff_required
    def af_create():
        d=request.get_json() or {};conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            codigo=str(d.get("codigo","")).strip();desc=str(d.get("descripcion","")).strip()
            costo=float(d.get("valor_costo") or 0);res=float(d.get("valor_residual") or 0);vida=int(d.get("vida_util_meses") or 60)
            if not codigo or not desc or costo<=0 or vida<=0:return jsonify({"error":"Código, descripción, costo y vida útil son obligatorios."}),400
            if res<0 or res>=costo:return jsonify({"error":"El valor residual debe ser menor que el costo."}),400
            aid=_insert_id(conn,"""INSERT INTO activos_fijos(cliente_id,codigo,descripcion,categoria,fecha_adquisicion,fecha_inicio_uso,valor_costo,valor_residual,vida_util_meses,metodo,cuenta_activo_id,cuenta_depreciacion_id,cuenta_gasto_id,ubicacion,responsable,numero_serie,proveedor,factura_referencia,creado_por)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(cid,codigo,desc,d.get("categoria","General"),d.get("fecha_adquisicion"),d.get("fecha_inicio_uso") or d.get("fecha_adquisicion"),costo,res,vida,d.get("metodo","lineal"),d.get("cuenta_activo_id") or None,d.get("cuenta_depreciacion_id") or None,d.get("cuenta_gasto_id") or None,d.get("ubicacion",""),d.get("responsable",""),d.get("numero_serie",""),d.get("proveedor",""),d.get("factura_referencia",""),_usuario_id(conn)))
            conn.commit();return jsonify({"ok":True,"id":aid}),201
        except Exception as e:conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.post("/api/activos-fijos/<int:activo_id>/depreciacion")
    @staff_required
    def af_depreciacion(activo_id):
        d=request.get_json() or {};periodo=str(d.get("periodo") or datetime.utcnow().strftime("%Y-%m"))
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            a=conn.execute("SELECT * FROM activos_fijos WHERE id=? AND cliente_id=? AND estado='activo'",(activo_id,cid)).fetchone()
            if not a:return jsonify({"error":"Activo fijo no encontrado o inactivo."}),404
            costo=float(a["valor_costo"]);res=float(a["valor_residual"]);vida=int(a["vida_util_meses"]);cuota=(costo-res)/vida
            prev=conn.execute("SELECT COALESCE(MAX(depreciacion_acumulada),0) acumulada FROM depreciacion_activos WHERE activo_id=?",(activo_id,)).fetchone()
            acum=float(prev["acumulada"] or 0);saldo=max(costo-res-acum,0);dep=round(min(cuota,saldo),2)
            inicial=round(costo-acum,2);nuevo=round(acum+dep,2);libro=round(costo-nuevo,2)
            conn.execute("""INSERT INTO depreciacion_activos(activo_id,periodo,fecha,valor_inicial,depreciacion,depreciacion_acumulada,valor_libro)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(activo_id,periodo) DO UPDATE SET depreciacion=excluded.depreciacion,depreciacion_acumulada=excluded.depreciacion_acumulada,valor_libro=excluded.valor_libro""",(activo_id,periodo,periodo+"-01",inicial,dep,nuevo,libro))
            conn.commit()
            return jsonify({"ok":True,"activo":dict(a),"periodo":periodo,"depreciacion":dep,"depreciacion_acumulada":nuevo,"valor_libro":libro})
        except Exception as e:conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.get("/api/activos-fijos/<int:activo_id>/cuadro")
    @usuario_required
    def af_cuadro(activo_id):
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            a=conn.execute("SELECT * FROM activos_fijos WHERE id=? AND cliente_id=?",(activo_id,cid)).fetchone()
            if not a:return jsonify({"error":"Activo no encontrado."}),404
            rows=conn.execute("SELECT * FROM depreciacion_activos WHERE activo_id=? ORDER BY periodo",(activo_id,)).fetchall()
            return jsonify({"activo":dict(a),"cuadro":[dict(x) for x in rows]})
        finally:conn.close()

    @app.post("/api/activos-fijos/<int:activo_id>/asiento-depreciacion")
    @staff_required
    def af_asiento(activo_id):
        d=request.get_json() or {};periodo=str(d.get("periodo") or datetime.utcnow().strftime("%Y-%m"));conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            row=conn.execute("""SELECT d.*,a.* FROM depreciacion_activos d JOIN activos_fijos a ON a.id=d.activo_id
                WHERE a.id=? AND a.cliente_id=? AND d.periodo=?""",(activo_id,cid,periodo)).fetchone()
            if not row:return jsonify({"error":"Primero generá la depreciación del período."}),400
            if row["asiento_id"]:return jsonify({"ok":True,"asiento_id":row["asiento_id"],"mensaje":"El asiento ya fue generado."})
            cuenta_debe=row["cuenta_gasto_id"] or _ensure_account(conn,cid,"6.01.01","Gasto por depreciación","DEUDORA","GASTO")
            cuenta_haber=row["cuenta_depreciacion_id"] or _ensure_account(conn,cid,"1.99.01","Depreciación acumulada","ACREEDORA","ACTIVO")
            uid=_usuario_id(conn)
            asiento_id=_insert_id(conn,"""INSERT INTO asientos_contables(cliente_id,fecha,concepto,origen,referencia_tipo,referencia_id,estado,usuario_creador_id)
                VALUES(?,?,?,?,?,?,?,?)""",(cid,periodo+"-01","Depreciación "+row["descripcion"],"ACTIVO_FIJO","ACTIVO_FIJO",activo_id,"borrador",uid))
            conn.execute("""INSERT INTO detalle_asientos(asiento_id,cuenta_id,descripcion,debe,haber,orden) VALUES(?,?,?,?,?,1)""",(asiento_id,cuenta_debe,"Depreciación "+row["descripcion"],float(row["depreciacion"]),0))
            conn.execute("""INSERT INTO detalle_asientos(asiento_id,cuenta_id,descripcion,debe,haber,orden) VALUES(?,?,?,?,?,2)""",(asiento_id,cuenta_haber,"Depreciación acumulada "+row["descripcion"],0,float(row["depreciacion"])))
            conn.execute("UPDATE depreciacion_activos SET asiento_id=?,contabilizado=1 WHERE id=?",(asiento_id,row["id"]))
            conn.commit();return jsonify({"ok":True,"asiento_id":asiento_id})
        except Exception as e:conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.get("/api/personas")
    @usuario_required
    def personas_list():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            return jsonify([dict(x) for x in conn.execute("SELECT * FROM personas WHERE cliente_id=? ORDER BY estado,apellidos,nombres",(cid,)).fetchall()])
        finally:conn.close()

    @app.post("/api/personas")
    @staff_required
    def personas_create():
        d=request.get_json() or {};conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            nombres=str(d.get("nombres","")).strip();apellidos=str(d.get("apellidos","")).strip();codigo=str(d.get("codigo","")).strip()
            if not nombres or not apellidos or not codigo:return jsonify({"error":"Código, nombres y apellidos son obligatorios."}),400
            pid=_insert_id(conn,"""INSERT INTO personas(cliente_id,codigo,nombres,apellidos,documento,fecha_nacimiento,sexo,correo,telefono,direccion,cargo,departamento,fecha_ingreso,fecha_salida,tipo_contrato,salario_base,estado,usuario_id,banco,cuenta_bancaria)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(cid,codigo,nombres,apellidos,d.get("documento",""),d.get("fecha_nacimiento"),d.get("sexo",""),d.get("correo",""),d.get("telefono",""),d.get("direccion",""),d.get("cargo",""),d.get("departamento",""),d.get("fecha_ingreso"),d.get("fecha_salida"),d.get("tipo_contrato","indefinido"),float(d.get("salario_base") or 0),d.get("estado","activo"),d.get("usuario_id") or None,d.get("banco",""),d.get("cuenta_bancaria","")))
            conn.commit();return jsonify({"ok":True,"id":pid}),201
        except Exception as e:conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.patch("/api/personas/<int:persona_id>")
    @staff_required
    def personas_update(persona_id):
        d=request.get_json() or {};conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            campos=["nombres","apellidos","documento","fecha_nacimiento","sexo","correo","telefono","direccion","cargo","departamento","fecha_ingreso","fecha_salida","tipo_contrato","salario_base","estado","banco","cuenta_bancaria"]
            vals=[];setters=[]
            for c in campos:
                if c in d:
                    setters.append(c+"=?");vals.append(float(d[c]) if c=="salario_base" else d[c])
            if not setters:return jsonify({"error":"No hay cambios."}),400
            vals += [persona_id,cid];conn.execute("UPDATE personas SET "+",".join(setters)+",actualizado_en=CURRENT_TIMESTAMP WHERE id=? AND cliente_id=?",vals);conn.commit()
            return jsonify({"ok":True})
        except Exception as e:conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.get("/api/personas/<int:persona_id>/asistencia")
    @usuario_required
    def personas_attendance(persona_id):
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            if not conn.execute("SELECT 1 FROM personas WHERE id=? AND cliente_id=?",(persona_id,cid)).fetchone():return jsonify({"error":"Persona no encontrada."}),404
            return jsonify([dict(x) for x in conn.execute("SELECT * FROM personas_asistencia WHERE persona_id=? ORDER BY fecha DESC LIMIT 200",(persona_id,)).fetchall()])
        finally:conn.close()

    @app.post("/api/personas/<int:persona_id>/asistencia")
    @staff_required
    def personas_attendance_save(persona_id):
        d=request.get_json() or {};conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            if not conn.execute("SELECT 1 FROM personas WHERE id=? AND cliente_id=?",(persona_id,cid)).fetchone():return jsonify({"error":"Persona no encontrada."}),404
            conn.execute("""INSERT INTO personas_asistencia(persona_id,fecha,entrada,salida,estado,observacion)
                VALUES(?,?,?,?,?,?) ON CONFLICT(persona_id,fecha) DO UPDATE SET entrada=excluded.entrada,salida=excluded.salida,estado=excluded.estado,observacion=excluded.observacion""",(persona_id,d.get("fecha") or datetime.utcnow().strftime("%Y-%m-%d"),d.get("entrada"),d.get("salida"),d.get("estado","presente"),d.get("observacion","")))
            conn.commit();return jsonify({"ok":True})
        except Exception as e:conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.get("/api/personas/<int:persona_id>/permisos")
    @usuario_required
    def personas_leave(persona_id):
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            return jsonify([dict(x) for x in conn.execute("SELECT * FROM personas_permisos WHERE persona_id=? ORDER BY desde DESC",(persona_id,)).fetchall()])
        finally:conn.close()

    @app.post("/api/personas/<int:persona_id>/permisos")
    @staff_required
    def personas_leave_create(persona_id):
        d=request.get_json() or {};conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            pid=persona_id
            if not conn.execute("SELECT 1 FROM personas WHERE id=? AND cliente_id=?",(pid,cid)).fetchone():return jsonify({"error":"Persona no encontrada."}),404
            leave_id=_insert_id(conn,"INSERT INTO personas_permisos(persona_id,tipo,desde,hasta,dias,estado,motivo) VALUES(?,?,?,?,?,?,?)",(pid,d.get("tipo","vacaciones"),d.get("desde"),d.get("hasta"),float(d.get("dias") or 0),d.get("estado","pendiente"),d.get("motivo","")))
            conn.commit();return jsonify({"ok":True,"id":leave_id}),201
        except Exception as e:conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()

    @app.get("/api/personas/<int:persona_id>/nomina")
    @usuario_required
    def personas_payroll(persona_id):
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            return jsonify([dict(x) for x in conn.execute("SELECT * FROM personas_nomina WHERE persona_id=? ORDER BY periodo DESC",(persona_id,)).fetchall()])
        finally:conn.close()

    @app.post("/api/personas/<int:persona_id>/nomina")
    @staff_required
    def personas_payroll_save(persona_id):
        d=request.get_json() or {};conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            p=conn.execute("SELECT * FROM personas WHERE id=? AND cliente_id=?",(persona_id,cid)).fetchone()
            if not p:return jsonify({"error":"Persona no encontrada."}),404
            base=float(d.get("salario_base",p["salario_base"]) or 0);add=float(d.get("adicionales") or 0);desc=float(d.get("descuentos") or 0);neto=base+add-desc
            nom_id=_insert_id(conn,"""INSERT INTO personas_nomina(persona_id,periodo,salario_base,adicionales,descuentos,neto,estado)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(persona_id,periodo) DO UPDATE SET salario_base=excluded.salario_base,adicionales=excluded.adicionales,descuentos=excluded.descuentos,neto=excluded.neto""",(persona_id,d.get("periodo") or datetime.utcnow().strftime("%Y-%m"),base,add,desc,neto,d.get("estado","borrador")))
            conn.commit();return jsonify({"ok":True,"id":nom_id,"neto":neto})
        except Exception as e:conn.rollback();return jsonify({"error":str(e)}),400
        finally:conn.close()
