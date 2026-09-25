import os
import unicodedata
from datetime import datetime
from flask import request, jsonify

# Catálogo central de funcionalidades de KAKUAA.
MODULOS_DEFAULT = [
    ("INICIO", "Inicio", "Panel principal"),
    ("CLIENTES", "Clientes", "Gestión de clientes"),
    ("PROVEEDORES", "Proveedores", "Gestión de proveedores"),
    ("COMPRAS", "Compras", "Órdenes, comprobantes y cuentas por pagar"),
    ("VENTAS", "Ventas", "Ventas y documentos comerciales"),
    ("FACTURACION", "Facturación", "Emisión y gestión de comprobantes"),
    ("CONTABILIDAD", "Contabilidad", "Plan de cuentas, asientos y reportes"),
    ("TESORERIA", "Tesorería", "Caja, bancos y pagos"),
    ("INVENTARIO", "Inventario", "Existencias, movimientos y stock"),
    ("FACTURACION_ELECTRONICA", "Facturación Electrónica", "SIFEN y documentos electrónicos"),
]

PERMISOS_DEFAULT = [
    ("VER", "Ver"),
    ("CREAR", "Crear"),
    ("EDITAR", "Editar"),
    ("ANULAR", "Anular"),
    ("EXPORTAR", "Exportar"),
    ("CONFIGURAR", "Configurar"),
]

def _normalizar(texto):
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in texto if not unicodedata.combining(c)).upper().strip()

def _usuario_actual(conn, obtener_usuario_por_token):
    return obtener_usuario_por_token()

def _es_superadmin(usuario):
    return bool(usuario and str(usuario["rol"]).lower() == "superadmin")

def _membresia(conn, usuario_id, cliente_id):
    return conn.execute(
        """SELECT uc.*, c.razon_social, c.nombre_comercial, c.ruc, c.estado, c.es_tenant_maestro
           FROM usuario_clientes uc
           JOIN clientes c ON c.id = uc.cliente_id
           WHERE uc.usuario_id=? AND uc.cliente_id=? AND c.estado='activo'""",
        (usuario_id, cliente_id),
    ).fetchone()

def _puede_ver_empresa(conn, usuario, cliente_id):
    if _es_superadmin(usuario):
        return True
    if not usuario:
        return False
    fila = _membresia(conn, usuario["id"], cliente_id)
    if not fila:
        return False
    return True

def _seed_catalogo(conn):
    id_col = "BIGSERIAL" if os.environ.get("DATABASE_URL") else "INTEGER"
    conn.execute(f"""CREATE TABLE IF NOT EXISTS modulos (
        id {id_col} PRIMARY KEY,
        codigo TEXT UNIQUE NOT NULL,
        nombre TEXT NOT NULL,
        descripcion TEXT DEFAULT '',
        activo INTEGER NOT NULL DEFAULT 1,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS cliente_modulos (
        id {id_col} PRIMARY KEY,
        cliente_id INTEGER NOT NULL,
        modulo_id INTEGER NOT NULL,
        activo INTEGER NOT NULL DEFAULT 1,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
        actualizado_en TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(cliente_id, modulo_id)
    )""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS permisos (
        id {id_col} PRIMARY KEY,
        codigo TEXT UNIQUE NOT NULL,
        nombre TEXT NOT NULL,
        activo INTEGER NOT NULL DEFAULT 1
    )""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS usuario_cliente_permisos (
        id {id_col} PRIMARY KEY,
        usuario_cliente_id INTEGER NOT NULL,
        modulo_id INTEGER NOT NULL,
        permiso_id INTEGER NOT NULL,
        activo INTEGER NOT NULL DEFAULT 1,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(usuario_cliente_id, modulo_id, permiso_id)
    )""")

    # Evolución no destructiva de usuario_clientes.
    columnas = set()
    if os.environ.get("DATABASE_URL"):
        filas = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='usuario_clientes'"
        ).fetchall()
        columnas = {str(x["column_name"]) for x in filas}
    else:
        columnas = {str(x[1]) for x in conn.execute("PRAGMA table_info(usuario_clientes)").fetchall()}

    if "rol_empresa" not in columnas:
        conn.execute("ALTER TABLE usuario_clientes ADD COLUMN rol_empresa TEXT NOT NULL DEFAULT 'operativo'")
    if "activo" not in columnas:
        conn.execute("ALTER TABLE usuario_clientes ADD COLUMN activo INTEGER NOT NULL DEFAULT 1")

    for codigo, nombre, descripcion in MODULOS_DEFAULT:
        if os.environ.get("DATABASE_URL"):
            conn.execute(
                """INSERT INTO modulos(codigo,nombre,descripcion,activo)
                   VALUES(?,?,?,1) ON CONFLICT(codigo) DO UPDATE SET nombre=EXCLUDED.nombre, descripcion=EXCLUDED.descripcion""",
                (codigo, nombre, descripcion),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO modulos(codigo,nombre,descripcion,activo) VALUES(?,?,?,1)",
                (codigo, nombre, descripcion),
            )

    for codigo, nombre in PERMISOS_DEFAULT:
        if os.environ.get("DATABASE_URL"):
            conn.execute(
                """INSERT INTO permisos(codigo,nombre,activo) VALUES(?,?,1)
                   ON CONFLICT(codigo) DO UPDATE SET nombre=EXCLUDED.nombre""",
                (codigo, nombre),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO permisos(codigo,nombre,activo) VALUES(?,?,1)",
                (codigo, nombre),
            )

    # KAKUAA DEMO S.A. es el tenant maestro actual. No dependemos de un RUC
    # fijo: lo identificamos por razón social normalizada.
    try:
        conn.execute(
            "ALTER TABLE clientes ADD COLUMN es_tenant_maestro INTEGER NOT NULL DEFAULT 0"
        )
    except Exception:
        pass

    maestro = conn.execute(
        "SELECT id FROM clientes WHERE es_tenant_maestro=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if not maestro:
        maestro = conn.execute(
            "SELECT id FROM clientes WHERE UPPER(TRIM(razon_social))='KAKUAA DEMO S.A.' LIMIT 1"
        ).fetchone()

    if maestro:
        conn.execute("UPDATE clientes SET es_tenant_maestro=1 WHERE id=?", (maestro["id"],))
        modulos = conn.execute("SELECT id FROM modulos WHERE activo=1").fetchall()
        for m in modulos:
            if os.environ.get("DATABASE_URL"):
                conn.execute(
                    """INSERT INTO cliente_modulos(cliente_id,modulo_id,activo)
                       VALUES(?,?,1) ON CONFLICT(cliente_id,modulo_id)
                       DO UPDATE SET activo=1, actualizado_en=CURRENT_TIMESTAMP""",
                    (maestro["id"], m["id"]),
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO cliente_modulos(cliente_id,modulo_id,activo) VALUES(?,?,1)",
                    (maestro["id"], m["id"]),
                )
        # Los administradores existentes de KAKUAA conservan acceso al tenant maestro.
        admins = conn.execute(
            "SELECT id FROM usuarios WHERE rol IN ('admin','superadmin') AND activo=1"
        ).fetchall()
        for u in admins:
            if os.environ.get("DATABASE_URL"):
                conn.execute(
                    """INSERT INTO usuario_clientes(usuario_id,cliente_id,rol_empresa,activo)
                       VALUES(?,?,?,1) ON CONFLICT(usuario_id,cliente_id)
                       DO UPDATE SET activo=1, rol_empresa=EXCLUDED.rol_empresa""",
                    (u["id"], maestro["id"], "admin"),
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO usuario_clientes(usuario_id,cliente_id,rol_empresa,activo) VALUES(?,?,?,1)",
                    (u["id"], maestro["id"], "admin"),
                )
                conn.execute(
                    "UPDATE usuario_clientes SET activo=1,rol_empresa='admin' WHERE usuario_id=? AND cliente_id=?",
                    (u["id"], maestro["id"]),
                )

def register(app, get_db, staff_required, admin_required, obtener_usuario_por_token):
    conn = get_db()
    try:
        _seed_catalogo(conn)
        conn.commit()
    finally:
        conn.close()

    @app.get("/api/empresas/mis-empresas")
    @staff_required
    def mis_empresas():
        usuario = obtener_usuario_por_token()
        conn = get_db()
        try:
            if _es_superadmin(usuario):
                rows = conn.execute(
                    "SELECT * FROM clientes WHERE estado='activo' ORDER BY razon_social"
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT c.* FROM clientes c
                       JOIN usuario_clientes uc ON uc.cliente_id=c.id
                       WHERE uc.usuario_id=? AND uc.activo=1 AND c.estado='activo'
                       ORDER BY c.razon_social""",
                    (usuario["id"],),
                ).fetchall()
            return jsonify([dict(r) for r in rows])
        finally:
            conn.close()

    @app.get("/api/empresas/<int:cliente_id>/modulos")
    @staff_required
    def modulos_empresa(cliente_id):
        usuario = obtener_usuario_por_token()
        conn = get_db()
        try:
            if not _puede_ver_empresa(conn, usuario, cliente_id):
                return jsonify({"error": "No tenés acceso a esta empresa."}), 403
            rows = conn.execute(
                """SELECT m.codigo,m.nombre,m.descripcion,
                          CASE WHEN cm.activo=1 THEN 1 ELSE 0 END AS habilitado
                   FROM modulos m
                   LEFT JOIN cliente_modulos cm
                     ON cm.modulo_id=m.id AND cm.cliente_id=?
                   WHERE m.activo=1
                   ORDER BY m.id""",
                (cliente_id,),
            ).fetchall()
            return jsonify([dict(r) for r in rows])
        finally:
            conn.close()

    @app.put("/api/empresas/<int:cliente_id>/modulos")
    @app.post("/api/empresas/<int:cliente_id>/modulos")
    @admin_required
    def actualizar_modulos_empresa(cliente_id):
        usuario = obtener_usuario_por_token()
        if not _es_superadmin(usuario):
            return jsonify({"error": "Solo KAKUAA puede habilitar o deshabilitar módulos de una empresa."}), 403
        data = request.get_json() or {}
        modulos = data.get("modulos")
        if not isinstance(modulos, list):
            return jsonify({"error": "Enviá una lista de módulos."}), 400

        conn = get_db()
        try:
            empresa = conn.execute(
                "SELECT id,razon_social,es_tenant_maestro FROM clientes WHERE id=? AND estado='activo'",
                (cliente_id,),
            ).fetchone()
            if not empresa:
                return jsonify({"error": "Empresa no encontrada o inactiva."}), 404

            catalogo = {
                str(r["codigo"]): r["id"]
                for r in conn.execute("SELECT id,codigo FROM modulos WHERE activo=1").fetchall()
            }
            seleccion = {str(x).strip().upper() for x in modulos}
            desconocidos = sorted(seleccion - set(catalogo))
            if desconocidos:
                return jsonify({"error": "Módulos desconocidos: " + ", ".join(desconocidos)}), 400

            for codigo, modulo_id in catalogo.items():
                activo = 1 if codigo in seleccion else 0
                if os.environ.get("DATABASE_URL"):
                    conn.execute(
                        """INSERT INTO cliente_modulos(cliente_id,modulo_id,activo)
                           VALUES(?,?,?)
                           ON CONFLICT(cliente_id,modulo_id)
                           DO UPDATE SET activo=EXCLUDED.activo,actualizado_en=CURRENT_TIMESTAMP""",
                        (cliente_id, modulo_id, activo),
                    )
                else:
                    conn.execute(
                        """INSERT OR IGNORE INTO cliente_modulos(cliente_id,modulo_id,activo)
                           VALUES(?,?,?)""",
                        (cliente_id, modulo_id, activo),
                    )
                    conn.execute(
                        "UPDATE cliente_modulos SET activo=?,actualizado_en=datetime('now') WHERE cliente_id=? AND modulo_id=?",
                        (activo, cliente_id, modulo_id),
                    )

            # El tenant maestro nunca pierde módulos.
            if int(empresa["es_tenant_maestro"] or 0) == 1:
                conn.execute(
                    "UPDATE cliente_modulos SET activo=1,actualizado_en=CURRENT_TIMESTAMP WHERE cliente_id=?",
                    (cliente_id,),
                )

            conn.commit()
            return jsonify({"ok": True})
        except Exception as e:
            conn.rollback()
            return jsonify({"error": str(e)}), 400
        finally:
            conn.close()

    @app.get("/api/empresas/<int:cliente_id>/usuarios")
    @admin_required
    def usuarios_empresa(cliente_id):
        usuario = obtener_usuario_por_token()
        conn = get_db()
        try:
            if not _es_superadmin(usuario):
                membership = _membresia(conn, usuario["id"], cliente_id)
                if not membership or str(membership["rol_empresa"]).lower() != "admin":
                    return jsonify({"error": "Solo el ADMIN de la empresa puede administrar sus usuarios."}), 403
            rows = conn.execute(
                """SELECT u.id,u.usuario,u.nombre,u.correo,u.activo,
                          uc.rol_empresa,uc.activo AS membresia_activa
                   FROM usuario_clientes uc
                   JOIN usuarios u ON u.id=uc.usuario_id
                   WHERE uc.cliente_id=?
                   ORDER BY u.nombre""",
                (cliente_id,),
            ).fetchall()
            return jsonify([dict(r) for r in rows])
        finally:
            conn.close()
