# Módulo de operaciones simplificadas para contribuyentes Persona Física.
#
# Mantiene separado el circuito PF del ERP empresarial:
# Ingresos/Egresos -> Cobros/Pagos -> impuestos/reportes.
# No crea banca, presupuestos ni flujo de fondos.

def registrar_modulo_persona_fisica(app, get_db, obtener_cliente_contable, admin_required, db_backend='sqlite'):
    idc = 'BIGSERIAL PRIMARY KEY' if db_backend == 'postgres' else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    def _cliente_pf():
        cliente_id, error = obtener_cliente_contable()
        if error:
            return None, error
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT id, tipo_persona, impuestos FROM clientes WHERE id=?",
                (cliente_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None, ({"error": "Cliente no encontrado."}, 404)
        tipo = str(row["tipo_persona"] or "").lower()
        if tipo not in ("fisica", "persona_fisica", "persona física"):
            return None, ({"error": "Este módulo está disponible únicamente para Persona Física."}, 409)
        return cliente_id, None

    def _init(conn):
        conn.execute(f"""CREATE TABLE IF NOT EXISTS pf_medios_pago(
            id {idc},
            cliente_id INTEGER NOT NULL,
            nombre TEXT NOT NULL,
            tipo TEXT NOT NULL DEFAULT 'OTRO',
            activo INTEGER NOT NULL DEFAULT 1,
            creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(cliente_id,nombre)
        )""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS pf_categorias(
            id {idc},
            cliente_id INTEGER NOT NULL,
            nombre TEXT NOT NULL,
            tipo TEXT NOT NULL,
            activo INTEGER NOT NULL DEFAULT 1,
            creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(cliente_id,nombre,tipo)
        )""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS pf_operaciones(
            id {idc},
            cliente_id INTEGER NOT NULL,
            tipo TEXT NOT NULL,
            fecha TEXT NOT NULL,
            fecha_vencimiento TEXT,
            comprobante_tipo TEXT NOT NULL DEFAULT 'OTRO',
            comprobante_numero TEXT DEFAULT '',
            tercero TEXT DEFAULT '',
            tercero_ruc TEXT DEFAULT '',
            concepto TEXT NOT NULL,
            categoria_id INTEGER,
            monto NUMERIC NOT NULL DEFAULT 0,
            moneda TEXT NOT NULL DEFAULT 'PYG',
            estado TEXT NOT NULL DEFAULT 'PENDIENTE',
            origen TEXT NOT NULL DEFAULT 'MANUAL',
            origen_id INTEGER,
            medio_pago_id INTEGER,
            observacion TEXT DEFAULT '',
            creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
            actualizado_en TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS pf_movimientos_pago(
            id {idc},
            operacion_id INTEGER NOT NULL,
            cliente_id INTEGER NOT NULL,
            tipo TEXT NOT NULL,
            fecha TEXT NOT NULL,
            monto NUMERIC NOT NULL,
            medio_pago_id INTEGER,
            referencia TEXT DEFAULT '',
            observacion TEXT DEFAULT '',
            creado_en TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        for nombre,tipo in (
            ("Efectivo","EFECTIVO"),("Transferencia","TRANSFERENCIA"),
            ("Cheque","CHEQUE"),("Tarjeta","TARJETA"),("Otro","OTRO")
        ):
            conn.execute(
                "INSERT INTO pf_medios_pago(cliente_id,nombre,tipo) VALUES(?,?,?) ON CONFLICT(cliente_id,nombre) DO NOTHING",
                (cliente_id,nombre,tipo)
            )
        conn.execute(
            "INSERT INTO pf_categorias(cliente_id,nombre,tipo) VALUES(?,?,?) ON CONFLICT(cliente_id,nombre,tipo) DO NOTHING",
            (cliente_id,"General","INGRESO")
        )
        conn.execute(
            "INSERT INTO pf_categorias(cliente_id,nombre,tipo) VALUES(?,?,?) ON CONFLICT(cliente_id,nombre,tipo) DO NOTHING",
            (cliente_id,"General","EGRESO")
        )

    def _ensure(conn, cliente_id):
        _init(conn)
        conn.commit()

    # Responder explícitamente al preflight CORS del módulo PF.\n    # Flask-CORS también agrega los headers, pero esta ruta evita que un OPTIONS\n    # llegue a un 404 cuando el navegador envía X-Cliente-ID/CSRF u otros headers.\n    @app.route("/api/persona-fisica/<path:_ruta>", methods=["OPTIONS"])\n    def pf_preflight(_ruta):\n        return ("", 204)\n\n    # PostgreSQL y SQLite comparten la misma API; las tablas se crean al primer uso del módulo.\n    @app.get("/api/persona-fisica/resumen")
    @admin_required
    def pf_resumen():
        cid, error = _cliente_pf()
        if error:
            return jsonify(error[0]), error[1]
        conn = get_db()
        try:
            _ensure(conn,cid)
            total = conn.execute("""SELECT
                COALESCE(SUM(CASE WHEN tipo='INGRESO' THEN monto ELSE 0 END),0) ingresos,
                COALESCE(SUM(CASE WHEN tipo='EGRESO' THEN monto ELSE 0 END),0) egresos,
                COALESCE(SUM(CASE WHEN tipo='INGRESO' AND estado IN ('PENDIENTE','PARCIAL','VENCIDO') THEN monto ELSE 0 END),0) por_cobrar,
                COALESCE(SUM(CASE WHEN tipo='EGRESO' AND estado IN ('PENDIENTE','PARCIAL','VENCIDO') THEN monto ELSE 0 END),0) por_pagar
                FROM pf_operaciones WHERE cliente_id=?""",(cid,)).fetchone()
            vencidos = conn.execute("""SELECT COUNT(*) cantidad FROM pf_operaciones
                WHERE cliente_id=? AND estado IN ('PENDIENTE','PARCIAL')
                AND fecha_vencimiento IS NOT NULL AND fecha_vencimiento < CURRENT_DATE""",(cid,)).fetchone()
            return jsonify({
                "ingresos": float(total["ingresos"] or 0),
                "egresos": float(total["egresos"] or 0),
                "por_cobrar": float(total["por_cobrar"] or 0),
                "por_pagar": float(total["por_pagar"] or 0),
                "vencidos": int(vencidos["cantidad"] or 0)
            })
        finally:
            conn.close()

    @app.get("/api/persona-fisica/medios-pago")
    @admin_required
    def pf_medios():
        cid,error=_cliente_pf()
        if error:return jsonify(error[0]),error[1]
        conn=get_db()
        try:
            _ensure(conn,cid)
            rows=conn.execute("SELECT * FROM pf_medios_pago WHERE cliente_id=? AND activo=1 ORDER BY nombre",(cid,)).fetchall()
            return jsonify([dict(r) for r in rows])
        finally:conn.close()

    @app.get("/api/persona-fisica/categorias")
    @admin_required
    def pf_categorias():
        cid,error=_cliente_pf()
        if error:return jsonify(error[0]),error[1]
        conn=get_db()
        try:
            _ensure(conn,cid)
            rows=conn.execute("SELECT * FROM pf_categorias WHERE cliente_id=? AND activo=1 ORDER BY tipo,nombre",(cid,)).fetchall()
            return jsonify([dict(r) for r in rows])
        finally:conn.close()

    @app.post("/api/persona-fisica/categorias")
    @admin_required
    def pf_categoria_create():
        cid,error=_cliente_pf()
        if error:return jsonify(error[0]),error[1]
        d=request.get_json(silent=True) or {}
        nombre=str(d.get("nombre") or "").strip()
        tipo=str(d.get("tipo") or "").upper().strip()
        if not nombre or tipo not in ("INGRESO","EGRESO"):
            return jsonify({"error":"Nombre y tipo de categoría son obligatorios."}),400
        conn=get_db()
        try:
            _ensure(conn,cid)
            conn.execute("INSERT INTO pf_categorias(cliente_id,nombre,tipo) VALUES(?,?,?)",(cid,nombre,tipo))
            conn.commit()
            return jsonify({"ok":True})
        except Exception as exc:
            conn.rollback()
            return jsonify({"error":"La categoría ya existe o no pudo guardarse.","detalle":str(exc)}),409
        finally:conn.close()

    @app.get("/api/persona-fisica/operaciones")
    @admin_required
    def pf_operaciones():
        cid,error=_cliente_pf()
        if error:return jsonify(error[0]),error[1]
        tipo=str(request.args.get("tipo") or "").upper().strip()
        estado=str(request.args.get("estado") or "").upper().strip()
        conn=get_db()
        try:
            _ensure(conn,cid)
            where=["o.cliente_id=?"]; args=[cid]
            if tipo in ("INGRESO","EGRESO"):where.append("o.tipo=?");args.append(tipo)
            if estado:where.append("o.estado=?");args.append(estado)
            rows=conn.execute("""SELECT o.*, c.nombre categoria_nombre,
                COALESCE((SELECT SUM(monto) FROM pf_movimientos_pago m WHERE m.operacion_id=o.id),0) pagado
                FROM pf_operaciones o LEFT JOIN pf_categorias c ON c.id=o.categoria_id
                WHERE """+" AND ".join(where)+""" ORDER BY o.fecha DESC,o.id DESC""",args).fetchall()
            result=[]
            for r in rows:
                x=dict(r); x["monto"]=float(x["monto"] or 0); x["pagado"]=float(x["pagado"] or 0)
                x["saldo"]=max(0,x["monto"]-x["pagado"])
                result.append(x)
            return jsonify(result)
        finally:conn.close()

    @app.post("/api/persona-fisica/operaciones")
    @admin_required
    def pf_operacion_create():
        cid,error=_cliente_pf()
        if error:return jsonify(error[0]),error[1]
        d=request.get_json(silent=True) or {}
        tipo=str(d.get("tipo") or "").upper().strip()
        concepto=str(d.get("concepto") or "").strip()
        try:monto=float(d.get("monto") or 0)
        except (TypeError,ValueError):monto=-1
        if tipo not in ("INGRESO","EGRESO") or not concepto or monto<=0:
            return jsonify({"error":"Tipo, concepto y monto válido son obligatorios."}),400
        estado=str(d.get("estado") or "PENDIENTE").upper()
        if estado not in ("PENDIENTE","PARCIAL","COBRADO","PAGADO","VENCIDO","ANULADO"):
            return jsonify({"error":"Estado inválido."}),400
        fecha=str(d.get("fecha") or datetime.utcnow().strftime("%Y-%m-%d"))
        conn=get_db()
        try:
            _ensure(conn,cid)
            cur=conn.execute("""INSERT INTO pf_operaciones
                (cliente_id,tipo,fecha,fecha_vencimiento,comprobante_tipo,comprobante_numero,tercero,tercero_ruc,concepto,categoria_id,monto,moneda,estado,origen,origen_id,medio_pago_id,observacion)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid,tipo,fecha,d.get("fecha_vencimiento"),d.get("comprobante_tipo","OTRO"),str(d.get("comprobante_numero") or ""),
                 str(d.get("tercero") or ""),str(d.get("tercero_ruc") or ""),concepto,d.get("categoria_id") or None,monto,
                 str(d.get("moneda") or "PYG").upper(),estado,str(d.get("origen") or "MANUAL"),d.get("origen_id") or None,
                 d.get("medio_pago_id") or None,str(d.get("observacion") or "")))
            op_id=conn.execute("SELECT id FROM pf_operaciones WHERE cliente_id=? ORDER BY id DESC LIMIT 1",(cid,)).fetchone()["id"]
            conn.commit()
            return jsonify({"ok":True,"id":op_id})
        except Exception as exc:
            conn.rollback()
            return jsonify({"error":"No se pudo registrar la operación.","detalle":str(exc)}),400
        finally:conn.close()

    @app.post("/api/persona-fisica/operaciones/<int:operacion_id>/movimiento")
    @admin_required
    def pf_movimiento_create(operacion_id):
        cid,error=_cliente_pf()
        if error:return jsonify(error[0]),error[1]
        d=request.get_json(silent=True) or {}
        try:monto=float(d.get("monto") or 0)
        except (TypeError,ValueError):monto=0
        if monto<=0:return jsonify({"error":"El monto debe ser mayor que cero."}),400
        fecha=str(d.get("fecha") or datetime.utcnow().strftime("%Y-%m-%d"))
        conn=get_db()
        try:
            _ensure(conn,cid)
            op=conn.execute("SELECT * FROM pf_operaciones WHERE id=? AND cliente_id=?",(operacion_id,cid)).fetchone()
            if not op:return jsonify({"error":"Operación no encontrada."}),404
            total=conn.execute("SELECT COALESCE(SUM(monto),0) total FROM pf_movimientos_pago WHERE operacion_id=?",(operacion_id,)).fetchone()
            saldo=float(op["monto"] or 0)-float(total["total"] or 0)
            if monto>saldo+0.01:return jsonify({"error":"El movimiento supera el saldo pendiente.","saldo":round(saldo,2)}),409
            movimiento_tipo="COBRO" if op["tipo"]=="INGRESO" else "PAGO"
            conn.execute("""INSERT INTO pf_movimientos_pago
                (operacion_id,cliente_id,tipo,fecha,monto,medio_pago_id,referencia,observacion)
                VALUES(?,?,?,?,?,?,?,?)""",
                (operacion_id,cid,movimiento_tipo,fecha,monto,d.get("medio_pago_id") or None,str(d.get("referencia") or ""),str(d.get("observacion") or "")))
            nuevo=saldo-monto
            estado="COBRADO" if op["tipo"]=="INGRESO" and nuevo<=0.01 else ("PAGADO" if op["tipo"]=="EGRESO" and nuevo<=0.01 else "PARCIAL")
            conn.execute("UPDATE pf_operaciones SET estado=?,medio_pago_id=COALESCE(?,medio_pago_id),actualizado_en=CURRENT_TIMESTAMP WHERE id=? AND cliente_id=?",(estado,d.get("medio_pago_id") or None,operacion_id,cid))
            conn.commit()
            return jsonify({"ok":True,"saldo":max(0,nuevo),"estado":estado})
        finally:conn.close()

    @app.get("/api/persona-fisica/reportes")
    @admin_required
    def pf_reportes():
        cid,error=_cliente_pf()
        if error:return jsonify(error[0]),error[1]
        conn=get_db()
        try:
            _ensure(conn,cid)
            filas=conn.execute("""SELECT tipo,estado,COALESCE(SUM(monto),0) total,COUNT(*) cantidad
                FROM pf_operaciones WHERE cliente_id=? GROUP BY tipo,estado ORDER BY tipo,estado""",(cid,)).fetchall()
            return jsonify([dict(r) for r in filas])
        finally:conn.close()
