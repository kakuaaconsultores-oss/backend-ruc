from flask import request, jsonify
from compras import _cliente_id
from erp_modulos import registrar_ingreso_compra

def register(app, get_db, staff_required, admin_required, insertar_id, obtener_usuario_por_token):
    @app.get("/api/compras/pendientes-contabilizacion")
    @admin_required
    def compras_pendientes_contabilizacion():
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            rows=conn.execute("""SELECT c.*,p.razon_social AS proveedor,p.ruc,
                    cc.codigo AS centro_costo_codigo,cc.nombre AS centro_costo_nombre,
                    tc.codigo AS tipo_codigo,tc.nombre AS tipo_nombre
                FROM comprobantes_compra c
                JOIN proveedores p ON p.id=c.proveedor_id
                LEFT JOIN centros_costos cc ON cc.id=c.centro_costo_id AND cc.cliente_id=c.cliente_id
                LEFT JOIN tipos_comprobante_compra tc ON tc.id=c.tipo_comprobante_id AND tc.cliente_id=c.cliente_id
                WHERE c.cliente_id=? AND c.estado IN ('pendiente_contabilizar','en_revision','rechazado')
                ORDER BY c.fecha DESC,c.id DESC""",(cid,)).fetchall()
            return jsonify([dict(x) for x in rows])
        finally: conn.close()

    @app.get("/api/compras/comprobantes/<int:comprobante_id>/contabilizacion")
    @admin_required
    def detalle_contabilizacion_compra(comprobante_id):
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            c=conn.execute("""SELECT c.*,p.razon_social proveedor,p.ruc,
                    p.cuenta_contable_id cuenta_proveedor_id,
                    tc.codigo tipo_codigo,tc.nombre tipo_nombre,
                    cc.codigo centro_costo_codigo,cc.nombre centro_costo_nombre
                FROM comprobantes_compra c
                JOIN proveedores p ON p.id=c.proveedor_id
                LEFT JOIN tipos_comprobante_compra tc ON tc.id=c.tipo_comprobante_id
                LEFT JOIN centros_costos cc ON cc.id=c.centro_costo_id
                WHERE c.id=? AND c.cliente_id=?""",(comprobante_id,cid)).fetchone()
            if not c:return jsonify({"error":"Comprobante no encontrado."}),404
            det=conn.execute("""SELECT d.*,cc.codigo cuenta_codigo,cc.nombre cuenta_nombre
                FROM comprobantes_compra_detalle d
                LEFT JOIN cuentas_contables cc ON cc.id=d.cuenta_contable_id
                WHERE d.comprobante_id=? ORDER BY d.id""",(comprobante_id,)).fetchall()
            return jsonify({"comprobante":dict(c),"detalle":[dict(x) for x in det]})
        finally: conn.close()

    @app.post("/api/compras/comprobantes/<int:comprobante_id>/aprobar-contabilizacion")
    @admin_required
    def aprobar_contabilizacion_compra(comprobante_id):
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            c=conn.execute("SELECT * FROM comprobantes_compra WHERE id=? AND cliente_id=?",(comprobante_id,cid)).fetchone()
            if not c:return jsonify({"error":"Comprobante no encontrado."}),404
            if c["estado"] not in ("pendiente_contabilizar","en_revision","rechazado"):
                return jsonify({"error":"El comprobante no está pendiente de contabilización."}),409
            proveedor=conn.execute("SELECT * FROM proveedores WHERE id=? AND cliente_id=?",(c["proveedor_id"],cid)).fetchone()
            if not proveedor or not proveedor["cuenta_contable_id"]:
                return jsonify({"error":"El proveedor no tiene Cuenta Contable asignada. Configurala antes de aprobar la carga."}),400
            det=conn.execute("SELECT * FROM comprobantes_compra_detalle WHERE comprobante_id=? ORDER BY id",(comprobante_id,)).fetchall()
            if not det:return jsonify({"error":"El comprobante no tiene detalle contable."}),400
            for d in det:
                if not d["cuenta_contable_id"]:
                    return jsonify({"error":"Todas las líneas deben tener Cuenta Contable asignada."}),400
            tc=conn.execute("SELECT codigo FROM tipos_comprobante_compra WHERE id=? AND cliente_id=?",(c["tipo_comprobante_id"],cid)).fetchone()
            es_nc=bool(tc and str(tc["codigo"]).upper()=="NOTA_CREDITO")
            signo=-1 if es_nc else 1
            total=float(c["total_gs"] or c["total"] or 0)
            if total<=0:return jsonify({"error":"El comprobante debe tener un total mayor que cero."}),400
            u=obtener_usuario_por_token()
            asiento_id=insertar_id(conn,"""INSERT INTO asientos_contables
                (cliente_id,fecha,concepto,origen,referencia_tipo,referencia_id,estado,usuario_creador_id)
                VALUES(?,?,?,?,?,?,?,?)""",
                (cid,c["fecha"],("Nota de Crédito " if es_nc else "Compra ")+str(c["numero"]),
                 "COMPRAS","COMPROBANTE_COMPRA",comprobante_id,"borrador",u["id"]))
            acumulado=0.0
            for idx,d in enumerate(det,1):
                bruto=float(d["subtotal"] or 0)
                if bruto<=0:continue
                importe=bruto if idx<len(det) else max(0,round(total-acumulado,2))
                acumulado=round(acumulado+importe,2)
                conn.execute("""INSERT INTO detalle_asientos
                    (asiento_id,cuenta_id,descripcion,debe,haber,orden)
                    VALUES(?,?,?,?,?,?)""",
                    (asiento_id,d["cuenta_contable_id"],d["descripcion"] or "Compra",
                     importe if signo>0 else 0,importe if signo<0 else 0,idx))
            conn.execute("""INSERT INTO detalle_asientos
                (asiento_id,cuenta_id,descripcion,debe,haber,orden)
                VALUES(?,?,?,?,?,?)""",
                (asiento_id,proveedor["cuenta_contable_id"],
                 "Proveedor "+str(proveedor["razon_social"]),
                 total if signo<0 else 0,total if signo>0 else 0,len(det)+1))
            numero=conn.execute("""SELECT COALESCE(MAX(numero),0)+1 AS n
                FROM asientos_contables WHERE cliente_id=? AND estado='contabilizado'""",(cid,)).fetchone()["n"]
            conn.execute("""UPDATE asientos_contables
                SET estado='contabilizado',numero=?,usuario_contabilizador_id=?,
                    contabilizado_en=CURRENT_TIMESTAMP,actualizado_en=CURRENT_TIMESTAMP
                WHERE id=?""",(numero,u["id"],asiento_id))
            conn.execute("""UPDATE comprobantes_compra
                SET estado='contabilizado',asiento_id=?,contabilizacion_usuario_id=?,
                    contabilizacion_en=CURRENT_TIMESTAMP,rechazo_motivo='',
                    rechazado_por=NULL,rechazado_en=NULL,actualizado_en=CURRENT_TIMESTAMP
                WHERE id=? AND cliente_id=?""",(asiento_id,u["id"],comprobante_id,cid))
            if not es_nc:
                for d in det:
                    if d["concepto_id"] not in (None,"") or d["item_id"]:
                        registrar_ingreso_compra(conn,cid,int(comprobante_id),{
                            "concepto_id":d["concepto_id"],"item_id":d["item_id"],
                            "deposito_id":d["deposito_id"],"cantidad":d["cantidad"],
                            "precio_unitario":d["precio_unitario"]
                        },usuario_id=u["id"],fecha=c["fecha"])
            conn.commit()
            return jsonify({"ok":True,"asiento_id":asiento_id,"numero_asiento":numero})
        except Exception as e:
            conn.rollback();return jsonify({"error":str(e)}),400
        finally: conn.close()

    @app.post("/api/compras/comprobantes/<int:comprobante_id>/rechazar-contabilizacion")
    @admin_required
    def rechazar_contabilizacion_compra(comprobante_id):
        d=request.get_json(silent=True) or {}
        motivo=str(d.get("motivo") or "").strip()
        if not motivo:return jsonify({"error":"El motivo del rechazo es obligatorio."}),400
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            c=conn.execute("SELECT id,estado FROM comprobantes_compra WHERE id=? AND cliente_id=?",(comprobante_id,cid)).fetchone()
            if not c:return jsonify({"error":"Comprobante no encontrado."}),404
            if c["estado"] not in ("pendiente_contabilizar","en_revision","rechazado"):
                return jsonify({"error":"El comprobante no está en la bandeja de aprobación."}),409
            u=obtener_usuario_por_token()
            conn.execute("""UPDATE comprobantes_compra
                SET estado='rechazado',rechazo_motivo=?,rechazado_por=?,
                    rechazado_en=CURRENT_TIMESTAMP,actualizado_en=CURRENT_TIMESTAMP
                WHERE id=? AND cliente_id=?""",(motivo,u["id"],comprobante_id,cid))
            conn.commit();return jsonify({"ok":True})
        finally: conn.close()

    @app.post("/api/compras/comprobantes/<int:comprobante_id>/reingresar-contabilizacion")
    @staff_required
    def reingresar_contabilizacion_compra(comprobante_id):
        conn=get_db()
        try:
            cid,err=_cliente_id(conn)
            if err:return jsonify({"error":err}),401
            c=conn.execute("SELECT id,estado FROM comprobantes_compra WHERE id=? AND cliente_id=?",(comprobante_id,cid)).fetchone()
            if not c:return jsonify({"error":"Comprobante no encontrado."}),404
            if c["estado"]!="rechazado":return jsonify({"error":"Solo una carga rechazada puede reingresarse."}),409
            conn.execute("""UPDATE comprobantes_compra
                SET estado='pendiente_contabilizar',rechazo_motivo='',rechazado_por=NULL,
                    rechazado_en=NULL,actualizado_en=CURRENT_TIMESTAMP
                WHERE id=? AND cliente_id=?""",(comprobante_id,cid))
            conn.commit();return jsonify({"ok":True})
        finally: conn.close()
