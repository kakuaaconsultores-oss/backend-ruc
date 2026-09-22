import os
import sqlite3
import time
import smtplib
import secrets
import hashlib
import html
import shutil
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from functools import wraps
import bcrypt
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Render usa un filesystem efímero salvo que el servicio tenga un Persistent Disk.
# En producción configuramos PERSISTENT_DATA_DIR=/var/data; localmente se conserva BASE_DIR.
PERSISTENT_DATA_DIR = os.environ.get("PERSISTENT_DATA_DIR", BASE_DIR)


def _copiar_archivos_faltantes(origen, destino):
    """Copia solo archivos que todavía no existen en el almacenamiento persistente."""
    if not os.path.isdir(origen):
        return
    os.makedirs(destino, exist_ok=True)
    for raiz, directorios, archivos in os.walk(origen):
        relativa = os.path.relpath(raiz, origen)
        destino_raiz = destino if relativa == "." else os.path.join(destino, relativa)
        os.makedirs(destino_raiz, exist_ok=True)
        for nombre in archivos:
            origen_archivo = os.path.join(raiz, nombre)
            destino_archivo = os.path.join(destino_raiz, nombre)
            if not os.path.exists(destino_archivo):
                shutil.copy2(origen_archivo, destino_archivo)


def _migrar_almacenamiento_persistente(base_dir=None, persistent_dir=None):
    """Migra una instalación existente de /app al Persistent Disk una sola vez.

    Se ejecuta antes de abrir la base principal. La copia SQLite se realiza con
    sqlite3.backup() para obtener una base consistente y luego se corrigen las
    rutas absolutas de documentos que antes apuntaban al filesystem efímero.
    """
    base_dir = os.path.abspath(base_dir or BASE_DIR)
    persistent_dir = os.path.abspath(persistent_dir or PERSISTENT_DATA_DIR)
    if persistent_dir == base_dir:
        return

    legacy_db = os.path.join(base_dir, "usuarios.db")
    persistent_db = os.path.join(persistent_dir, "usuarios.db")
    legacy_docs = os.path.join(base_dir, "documentos")
    persistent_docs = os.path.join(persistent_dir, "documentos")
    os.makedirs(persistent_dir, exist_ok=True)

    if not os.path.exists(persistent_db) and os.path.exists(legacy_db):
        temporal_db = persistent_db + ".migrating"
        if os.path.exists(temporal_db):
            os.remove(temporal_db)
        origen = sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)
        destino = sqlite3.connect(temporal_db)
        try:
            origen.backup(destino)
            integridad = destino.execute("PRAGMA integrity_check").fetchone()[0]
            if integridad != "ok":
                raise RuntimeError(f"La copia SQLite no superó integrity_check: {integridad}")
            destino.commit()
        finally:
            destino.close()
            origen.close()
        os.replace(temporal_db, persistent_db)
        print(f"[STORAGE] Base SQLite migrada a {persistent_db}")

    if os.path.isdir(legacy_docs):
        _copiar_archivos_faltantes(legacy_docs, persistent_docs)

    if os.path.exists(persistent_db):
        conn = sqlite3.connect(persistent_db)
        try:
            docs = conn.execute("SELECT id, ruta FROM documentos WHERE ruta IS NOT NULL").fetchall()
            legacy_docs_real = os.path.realpath(legacy_docs)
            persistent_docs_real = os.path.realpath(persistent_docs)
            for doc_id, ruta in docs:
                ruta_real = os.path.realpath(str(ruta))
                if os.path.commonpath([legacy_docs_real, ruta_real]) != legacy_docs_real:
                    continue
                relativa = os.path.relpath(ruta_real, legacy_docs_real)
                nueva_ruta = os.path.join(persistent_docs_real, relativa)
                if nueva_ruta != ruta:
                    conn.execute("UPDATE documentos SET ruta = ? WHERE id = ?", (nueva_ruta, doc_id))
            integridad = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integridad != "ok":
                raise RuntimeError(f"El almacenamiento persistente no superó integrity_check: {integridad}")
            conn.commit()
        finally:
            conn.close()
        print(f"[STORAGE] Almacenamiento persistente listo en {persistent_dir}")


_migrar_almacenamiento_persistente()

# DB_PATH/DOCS_DIR explícitos tienen prioridad sobre PERSISTENT_DATA_DIR.
DB_PATH = os.environ.get("DB_PATH", os.path.join(PERSISTENT_DATA_DIR, "usuarios.db"))
DOCS_DIR = os.environ.get("DOCS_DIR", os.path.join(PERSISTENT_DATA_DIR, "documentos"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(DOCS_DIR, exist_ok=True)

MAX_INTENTOS = 5
BLOQUEO_MINUTOS = 30
OTP_MINUTOS = 1
MAX_REGENERACIONES_OTP = 5
SESION_HORAS = 8
RESET_TOKEN_HORAS = 1
MAX_UPLOAD_MB = 16
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp", "doc", "docx", "xls", "xlsx", "csv"}
MAX_CONTENT_BYTES = MAX_UPLOAD_MB * 1024 * 1024
SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "__Host-kakuaa_session")
CSRF_HEADER_NAME = "X-CSRF-Token"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") == "1"
COOKIE_SAMESITE = os.environ.get("COOKIE_SAMESITE", "None")

RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMITS = {
    "login": 10,
    "verify_otp": 10,
    "resend_otp": 6,
    "request_reset": 5,
    "reset_password": 5,
    "change_password": 5,
    "superadmin_bootstrap": 5,
}

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_BYTES
CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS",
    "https://kakuaaconsultores-oss.github.io,http://localhost:5500,http://127.0.0.1:5500"
).split(",") if o.strip()]
CORS(app, resources={r"/api/*": {"origins": CORS_ORIGINS}}, supports_credentials=True, expose_headers=["Content-Disposition"])

# Configuración SMTP (se lee de variables de entorno de Render)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
RESET_URL_BASE = os.environ.get("RESET_URL_BASE", "https://kakuaaconsultores-oss.github.io/restablecer-password.html")

# ---------- Base de datos ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn
def init_db():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS rate_limit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        creado_en REAL NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_limit_events ON rate_limit_events(ip, endpoint, creado_en)")
    conn.execute("""CREATE TABLE IF NOT EXISTS superadmin_bootstrap (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        usado INTEGER NOT NULL DEFAULT 0,
        usado_en TEXT DEFAULT NULL
    )""")
    conn.execute("INSERT OR IGNORE INTO superadmin_bootstrap (id, usado) VALUES (1, 0)")
    conn.execute("""CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ruc TEXT UNIQUE NOT NULL,
        correo TEXT UNIQUE NOT NULL,
        nombre TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        activo INTEGER DEFAULT 1,
        intentos_fallidos INTEGER DEFAULT 0,
        bloqueo_hasta TEXT DEFAULT NULL,
        token_sesion TEXT DEFAULT NULL,
        token_sesion_hash TEXT DEFAULT NULL,
        token_expira_en TEXT DEFAULT NULL,
        rol TEXT DEFAULT 'contribuyente',
        usuario TEXT,
        debe_cambiar INTEGER DEFAULT 0,
        creado_en TEXT DEFAULT (datetime('now'))
    )""")
    for col, definition in [
        ("rol", "TEXT DEFAULT 'contribuyente'"),
        ("usuario", "TEXT"),
        ("debe_cambiar", "INTEGER DEFAULT 0"),
        ("token_expira_en", "TEXT DEFAULT NULL"),
        ("token_sesion_hash", "TEXT DEFAULT NULL"),
        ("csrf_token_hash", "TEXT DEFAULT NULL"),
        ("reset_token_hash", "TEXT DEFAULT NULL"),
        ("reset_expira_en", "TEXT DEFAULT NULL")
    ]:
        try:
            conn.execute(f"ALTER TABLE usuarios ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass

    conn.execute("""CREATE TABLE IF NOT EXISTS login_otp (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        challenge_token TEXT UNIQUE NOT NULL,
        otp_hash TEXT NOT NULL,
        expira_en TEXT NOT NULL,
        intentos INTEGER DEFAULT 0,
        generaciones INTEGER DEFAULT 0,
        creado_en TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    )""")
    try:
        conn.execute("ALTER TABLE login_otp ADD COLUMN generaciones INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    conn.execute("""CREATE TABLE IF NOT EXISTS documentos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        nombre_archivo TEXT NOT NULL,
        ruta TEXT NOT NULL,
        carpeta TEXT NOT NULL,
        subcarpeta TEXT DEFAULT '',
        subcarpeta2 TEXT DEFAULT '',
        subido_en TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS subcarpetas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        carpeta TEXT NOT NULL,
        nombre TEXT NOT NULL,
        padre TEXT DEFAULT '',
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tickets_recuperacion (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        ruc TEXT NOT NULL,
        estado TEXT DEFAULT 'pendiente',
        nueva_password TEXT DEFAULT NULL,
        creado_en TEXT DEFAULT (datetime('now')),
        resuelto_en TEXT DEFAULT NULL,
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    )""")


    conn.execute("""CREATE TABLE IF NOT EXISTS facturas_clientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER NOT NULL,
        numero TEXT NOT NULL,
        fecha TEXT NOT NULL DEFAULT (date('now')),
        concepto TEXT NOT NULL,
        monto REAL NOT NULL DEFAULT 0,
        estado TEXT NOT NULL DEFAULT 'emitida',
        creado_por INTEGER,
        creado_en TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (cliente_id) REFERENCES usuarios(id),
        FOREIGN KEY (creado_por) REFERENCES usuarios(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_facturas_cliente ON facturas_clientes(cliente_id, fecha)")
    conn.execute("""CREATE TABLE IF NOT EXISTS articulos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        codigo TEXT UNIQUE NOT NULL,
        nombre TEXT NOT NULL,
        descripcion TEXT DEFAULT '',
        unidad TEXT NOT NULL DEFAULT 'servicio',
        activo INTEGER NOT NULL DEFAULT 1,
        creado_en TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tarifas_articulos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        articulo_id INTEGER NOT NULL,
        precio REAL NOT NULL DEFAULT 0,
        vigencia_desde TEXT NOT NULL,
        vigencia_hasta TEXT NOT NULL,
        ajuste_vencimiento REAL NOT NULL DEFAULT 0,
        activo INTEGER NOT NULL DEFAULT 1,
        creado_por INTEGER,
        creado_en TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (articulo_id) REFERENCES articulos(id),
        FOREIGN KEY (creado_por) REFERENCES usuarios(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tarifas_articulo_vigencia ON tarifas_articulos(articulo_id, vigencia_desde, vigencia_hasta)")
    conn.execute("""CREATE TABLE IF NOT EXISTS costos_persona (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        costo_hora REAL NOT NULL DEFAULT 0,
        vigencia_desde TEXT NOT NULL,
        vigencia_hasta TEXT DEFAULT NULL,
        activo INTEGER NOT NULL DEFAULT 1,
        creado_por INTEGER,
        creado_en TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id),
        FOREIGN KEY (creado_por) REFERENCES usuarios(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_costos_persona_vigencia ON costos_persona(usuario_id, vigencia_desde, vigencia_hasta)")
    for col, definition in [
        ("articulo_id", "INTEGER"),
        ("tarifa_id", "INTEGER"),
        ("cantidad", "REAL DEFAULT 1")
    ]:
        try:
            conn.execute(f"ALTER TABLE facturas_clientes ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass

    conn.execute("""CREATE TABLE IF NOT EXISTS tareas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER NOT NULL,
        titulo TEXT NOT NULL,
        descripcion TEXT DEFAULT '',
        prioridad TEXT NOT NULL DEFAULT 'media',
        estado TEXT NOT NULL DEFAULT 'pendiente',
        asignado_id INTEGER,
        creado_por INTEGER NOT NULL,
        fecha_limite TEXT DEFAULT NULL,
        creado_en TEXT DEFAULT (datetime('now')),
        iniciado_en TEXT DEFAULT NULL,
        completado_en TEXT DEFAULT NULL,
        FOREIGN KEY (cliente_id) REFERENCES usuarios(id),
        FOREIGN KEY (asignado_id) REFERENCES usuarios(id),
        FOREIGN KEY (creado_por) REFERENCES usuarios(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tareas_cliente ON tareas(cliente_id, estado)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tareas_asignado ON tareas(asignado_id, estado)")
    conn.execute("""CREATE TABLE IF NOT EXISTS sesiones_trabajo (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tarea_id INTEGER NOT NULL,
        cliente_id INTEGER NOT NULL,
        usuario_id INTEGER NOT NULL,
        inicio TEXT NOT NULL,
        fin TEXT DEFAULT NULL,
        creado_en TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (tarea_id) REFERENCES tareas(id),
        FOREIGN KEY (cliente_id) REFERENCES usuarios(id),
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sesiones_cliente ON sesiones_trabajo(cliente_id, inicio)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sesiones_usuario_activa ON sesiones_trabajo(usuario_id, fin)")

    conn.execute("UPDATE usuarios SET usuario = ruc WHERE (usuario IS NULL OR TRIM(usuario) = '')")
    # El ADMIN histórico 80000000-0 se conserva como ADMIN. La cuenta
    # SUPERADMIN es independiente y solo se crea mediante bootstrap.
    superadmin = conn.execute("SELECT id, usuario FROM usuarios WHERE rol = 'superadmin' LIMIT 1").fetchone()
    if superadmin:
        conn.execute("UPDATE usuarios SET rol = 'contribuyente' WHERE rol = 'superadmin' AND id != ?", (superadmin["id"],))
        # El bootstrap inicial se ejecutó con un placeholder accidental. Si la
        # cuenta aún conserva ese placeholder, normalizamos el usuario a un
        # identificador limpio y estable para producción.
        usuario_configurado = os.environ.get("SUPERADMIN_USUARIO", "").strip()
        placeholders = {"el usuario que quieras conservar/crear", "superadmin"}
        if not usuario_configurado or usuario_configurado.lower() in placeholders:
            usuario_configurado = "superadmin"
        if superadmin["usuario"] in placeholders or not str(superadmin["usuario"] or "").strip():
            conflicto = conn.execute(
                "SELECT id FROM usuarios WHERE usuario = ? AND id != ?",
                (usuario_configurado, superadmin["id"]),
            ).fetchone()
            if not conflicto:
                conn.execute(
                    "UPDATE usuarios SET usuario = ? WHERE id = ?",
                    (usuario_configurado, superadmin["id"]),
                )

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_usuarios_superadmin ON usuarios(rol) WHERE rol = 'superadmin'")
    conn.commit()
    conn.close()

init_db()

# ---------- Utilidades ----------
def hash_password(pw):
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def check_password(pw, hashed):
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False

def generar_token():
    return secrets.token_urlsafe(32)

def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def ruta_segura(base_dir, *partes):
    limpia = []
    for parte in partes:
        parte = str(parte or "")
        if os.path.isabs(parte) or parte in (".", "..") or ".." in parte.replace("\\", "/").split("/"):
            raise ValueError("Ruta inválida")
        segura = secure_filename(parte)
        if not segura:
            raise ValueError("Ruta inválida")
        limpia.append(segura)
    base_real = os.path.realpath(base_dir)
    destino = os.path.realpath(os.path.join(base_real, *limpia))
    if os.path.commonpath([base_real, destino]) != base_real:
        raise ValueError("Ruta fuera del directorio permitido")
    return destino

def validar_politica_password(pw):
    """Valida la política de seguridad de contraseñas."""
    if len(pw) < 6:
        return "La contraseña debe tener al menos 6 caracteres"
    if not any(c.isupper() for c in pw):
        return "Debe contener al menos una letra mayúscula"
    if not any(c.isdigit() for c in pw):
        return "Debe contener al menos un número"
    if not any(not c.isalnum() for c in pw):
        return "Debe contener al menos un carácter especial (!@#$%&*)"
    return None

def enviar_correo(destinatario, asunto, cuerpo_html):
    if not SMTP_USER or not SMTP_PASS:
        print(f"[SMTP] No configurado. No se envió correo a {destinatario}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = asunto
        msg["From"] = SMTP_FROM
        msg["To"] = destinatario
        msg.attach(MIMEText(cuerpo_html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [destinatario], msg.as_string())
        print(f"[SMTP] Correo enviado a {destinatario}")
        return True
    except Exception as e:
        print(f"[SMTP] Error al enviar: {e}")
        return False

def obtener_usuario_por_token():
    """Obtiene el usuario autenticado exclusivamente desde la cookie HttpOnly de sesión."""
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not token: return None
    conn = get_db()
    u = conn.execute("SELECT * FROM usuarios WHERE token_sesion_hash = ?", (hash_token(token),)).fetchone()
    if not u or not u["activo"]: conn.close(); return None
    if u["token_expira_en"]:
        try:
            if datetime.utcnow() >= datetime.fromisoformat(u["token_expira_en"]):
                conn.execute("UPDATE usuarios SET token_sesion = NULL, token_sesion_hash = NULL, csrf_token_hash = NULL, token_expira_en = NULL WHERE id = ?", (u["id"],))
                conn.commit(); conn.close(); return None
        except ValueError:
            conn.execute("UPDATE usuarios SET token_sesion = NULL, token_sesion_hash = NULL, csrf_token_hash = NULL, token_expira_en = NULL WHERE id = ?", (u["id"],))
            conn.commit(); conn.close(); return None
    conn.close(); return u

def csrf_valido():
    token = request.headers.get(CSRF_HEADER_NAME, "")
    if not token: return False
    u = obtener_usuario_por_token()
    return bool(u and u["csrf_token_hash"] and secrets.compare_digest(u["csrf_token_hash"], hash_token(token)))

def csrf_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not csrf_valido():
            return jsonify({"error": "Token CSRF inválido o ausente."}), 403
        return f(*args, **kwargs)
    return wrapper

ROLES = ['superadmin', 'admin', 'operativo', 'contribuyente']


def puede_gestionar(rol_actual, rol_objetivo):
    """Jerarquía: quién puede gestionar a quién."""
    jerarquia = {
        'superadmin': ['superadmin', 'admin', 'operativo', 'contribuyente'],
        'admin': ['operativo', 'contribuyente'],
        'operativo': ['contribuyente'],
        'contribuyente': []
    }
    return rol_objetivo in jerarquia.get(rol_actual, [])

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = obtener_usuario_por_token()
        if not u or u["rol"] not in ("superadmin", "admin"):
            return jsonify({"error": "No autorizado"}), 401
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not csrf_valido():
            return jsonify({"error": "Token CSRF inválido o ausente."}), 403
        return f(*args, **kwargs)
    return wrapper

def staff_required(f):
    """Permite operaciones de gestión a SUPERADMIN, ADMIN y OPERATIVO."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = obtener_usuario_por_token()
        if not u or u["rol"] not in ("superadmin", "admin", "operativo"):
            return jsonify({"error": "No autorizado"}), 401
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not csrf_valido():
            return jsonify({"error": "Token CSRF inválido o ausente."}), 403
        return f(*args, **kwargs)
    return wrapper

def usuario_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = obtener_usuario_por_token()
        if not u:
            return jsonify({"error": "No autorizado"}), 401
        return f(*args, **kwargs)
    return wrapper

def generar_otp():
    return f"{secrets.randbelow(10000):04d}"

def enviar_otp(usuario, otp):
    cuerpo = f"""
    <h2>Kakuaa Consultores</h2>
    <p>Hola <strong>{html.escape(str(usuario["nombre"]))}</strong>.</p>
    <p>Tu código de acceso es:</p>
    <p style="font-size:28px;font-weight:bold;letter-spacing:8px">{otp}</p>
    <p>Este código vence en {OTP_MINUTOS} minuto y solo el último código solicitado permanece válido.</p>
    """
    return enviar_correo(usuario["correo"], "Tu código de acceso - Kakuaa Consultores", cuerpo)

def crear_desafio_otp(conn, usuario, generaciones=1):
    conn.execute("DELETE FROM login_otp WHERE usuario_id = ?", (usuario["id"],))
    challenge = secrets.token_urlsafe(32)
    otp = generar_otp()
    expira = datetime.utcnow() + timedelta(minutes=OTP_MINUTOS)
    conn.execute(
        "INSERT INTO login_otp (usuario_id, challenge_token, otp_hash, expira_en, intentos, generaciones) VALUES (?, ?, ?, ?, 0, ?)",
        (usuario["id"], challenge, hash_password(otp), expira.isoformat(), generaciones)
    )
    conn.commit()
    return challenge, otp, generaciones

# ---------- Protección anti-abuso ----------
def client_ip():
    return request.remote_addr or "unknown"

def rate_limit_exceeded(conn, endpoint):
    """Registra la solicitud y devuelve True si supera el límite por IP."""
    limit = RATE_LIMITS[endpoint]
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    ip = client_ip()
    conn.execute("DELETE FROM rate_limit_events WHERE creado_en < ?", (window_start,))
    count = conn.execute(
        "SELECT COUNT(*) AS total FROM rate_limit_events WHERE ip = ? AND endpoint = ? AND creado_en >= ?",
        (ip, endpoint, window_start)
    ).fetchone()["total"]
    if count >= limit:
        conn.commit()
        return True
    conn.execute(
        "INSERT INTO rate_limit_events (ip, endpoint, creado_en) VALUES (?, ?, ?)",
        (ip, endpoint, now)
    )
    conn.commit()
    return False

def rate_limit_response():
    return jsonify({
        "error": "Demasiadas solicitudes. Esperá un momento e intentá nuevamente.",
        "rate_limited": True
    }), 429

# ---------- RUTAS ----------

@app.route("/api/superadmin/storage-diagnostic", methods=["POST"])
def superadmin_storage_diagnostic():
    """Diagnóstico temporal y protegido del almacenamiento de producción."""
    provided = str((request.get_json(silent=True) or {}).get("bootstrap_secret", "")).strip()
    expected = os.environ.get("SUPERADMIN_BOOTSTRAP_SECRET", "").strip()
    if not expected or len(expected) < 32 or not provided or not secrets.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    ):
        return jsonify({"error": "Credencial inválida."}), 403

    conn = get_db()
    try:
        db_exists = os.path.exists(DB_PATH)
        persistent_exists = os.path.isdir(PERSISTENT_DATA_DIR)
        superadmins = conn.execute(
            "SELECT id, usuario, rol, activo, debe_cambiar FROM usuarios WHERE rol = 'superadmin' ORDER BY id"
        ).fetchall()
        bootstrap = conn.execute(
            "SELECT usado, usado_en FROM superadmin_bootstrap WHERE id = 1"
        ).fetchone()
        usuarios_total = conn.execute("SELECT COUNT(*) AS total FROM usuarios").fetchone()["total"]
        return jsonify({
            "ok": True,
            "db_path": DB_PATH,
            "db_exists": db_exists,
            "persistent_data_dir": PERSISTENT_DATA_DIR,
            "persistent_dir_exists": persistent_exists,
            "usuarios_total": usuarios_total,
            "superadmins": [dict(row) for row in superadmins],
            "bootstrap": dict(bootstrap) if bootstrap else None,
        })
    finally:
        conn.close()

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})

@app.route("/api/superadmin/bootstrap", methods=["POST"])
def superadmin_bootstrap():
    """
    Recuperación/alta inicial del único SUPERADMIN mediante un secreto temporal
    configurado fuera del código (por ejemplo, en Render).

    El secreto nunca se devuelve ni se persiste. La operación queda marcada como
    usada en SQLite después de un reset/alta exitoso y no puede repetirse.
    """
    conn = get_db()
    if rate_limit_exceeded(conn, "superadmin_bootstrap"):
        conn.close()
        return rate_limit_response()

    bootstrap_secret = os.environ.get("SUPERADMIN_BOOTSTRAP_SECRET", "").strip()
    if not bootstrap_secret:
        conn.close()
        return jsonify({"error": "El procedimiento de bootstrap no está habilitado."}), 503
    if len(bootstrap_secret) < 32:
        conn.close()
        return jsonify({"error": "La configuración de bootstrap no cumple la longitud mínima de seguridad."}), 503

    data = request.get_json(silent=True) or {}
    provided_secret = str(data.get("bootstrap_secret", "")).strip()
    nueva_password = str(data.get("nueva_password", ""))
    if not provided_secret or not secrets.compare_digest(
        provided_secret.encode("utf-8"), bootstrap_secret.encode("utf-8")
    ):
        conn.close()
        return jsonify({"error": "Credencial de bootstrap inválida."}), 403

    error_password = validar_politica_password(nueva_password)
    if error_password:
        conn.close()
        return jsonify({"error": error_password}), 400

    try:
        conn.execute("BEGIN IMMEDIATE")
        estado = conn.execute(
            "SELECT usado FROM superadmin_bootstrap WHERE id = 1"
        ).fetchone()
        if estado and estado["usado"]:
            conn.rollback()
            conn.close()
            return jsonify({"error": "El bootstrap del SUPERADMIN ya fue utilizado."}), 409

        superadmin = conn.execute(
            "SELECT * FROM usuarios WHERE rol = 'superadmin' LIMIT 1"
        ).fetchone()

        usuario_configurado = os.environ.get("SUPERADMIN_USUARIO", "superadmin").strip() or "superadmin"
        placeholders = {"el usuario que quieras conservar/crear", "superadmin"}
        if not usuario_configurado or usuario_configurado.lower() in placeholders:
            usuario_configurado = "superadmin"

        if superadmin:
            usuario_id = superadmin["id"]
            correo = superadmin["correo"]
            # Si una instalación histórica creó el SUPERADMIN con el placeholder
            # accidental, el bootstrap lo corrige en la misma transacción.
            if superadmin["usuario"] in placeholders or not str(superadmin["usuario"] or "").strip():
                conflicto = conn.execute(
                    "SELECT id FROM usuarios WHERE usuario = ? AND id != ?",
                    (usuario_configurado, usuario_id),
                ).fetchone()
                if conflicto:
                    raise sqlite3.IntegrityError("El usuario 'superadmin' ya pertenece a otra cuenta.")
                usuario = usuario_configurado
            else:
                usuario = superadmin["usuario"]
            conn.execute(
                """UPDATE usuarios
                   SET usuario = ?, password_hash = ?, activo = 1, debe_cambiar = 0,
                       intentos_fallidos = 0, bloqueo_hasta = NULL,
                       token_sesion = NULL, token_sesion_hash = NULL,
                       csrf_token_hash = NULL, token_expira_en = NULL,
                       reset_token_hash = NULL, reset_expira_en = NULL
                   WHERE id = ?""",
                (usuario, hash_password(nueva_password), usuario_id),
            )
            accion = "password_reset"
        else:
            usuario = usuario_configurado
            correo = os.environ.get(
                "SUPERADMIN_EMAIL", "kakuaaconsultores@gmail.com"
            ).strip() or "kakuaaconsultores@gmail.com"
            # SUPERADMIN es una cuenta técnica independiente del ADMIN/RUC 80000000-0.
            # El RUC es NOT NULL + UNIQUE en el esquema histórico, por lo que usamos
            # un identificador interno reservado que no puede confundirse con un RUC real.
            ruc_superadmin = "SUPERADMIN-000000"
            cur = conn.execute(
                """INSERT INTO usuarios
                   (ruc, correo, nombre, password_hash, activo, usuario, rol, debe_cambiar)
                   VALUES (?, ?, 'SUPERADMIN', ?, 1, ?, 'superadmin', 0)""",
                (ruc_superadmin, correo, hash_password(nueva_password), usuario),
            )
            usuario_id = cur.lastrowid
            accion = "superadmin_created"

        conn.execute(
            "UPDATE superadmin_bootstrap SET usado = 1, usado_en = datetime('now') WHERE id = 1"
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        app.logger.exception("Error de integridad durante bootstrap del SUPERADMIN: %s", exc)
        conn.close()
        return jsonify({"error": "No se pudo completar el bootstrap del SUPERADMIN."}), 409
    except Exception as exc:
        conn.rollback()
        app.logger.exception("Error inesperado durante bootstrap del SUPERADMIN: %s", exc)
        conn.close()
        return jsonify({"error": "No se pudo completar el bootstrap del SUPERADMIN."}), 500

    conn.close()
    return jsonify({
        "ok": True,
        "accion": accion,
        "usuario": usuario,
        "correo": correo,
        "message": "SUPERADMIN listo. El secreto de bootstrap ya no puede volver a utilizarse."
    })

@app.route("/api/login", methods=["POST"])
def login():
    conn = get_db()
    if rate_limit_exceeded(conn, "login"):
        conn.close()
        return rate_limit_response()
    data = request.get_json() or {}
    usuario_login = data.get("usuario", "").strip()
    password = data.get("password", "")
    if not usuario_login or not password:
        return jsonify({"error": "Ingresá tu usuario y contraseña"}), 400
    u = conn.execute("SELECT * FROM usuarios WHERE usuario = ?", (usuario_login,)).fetchone()
    if not u:
        conn.close(); return jsonify({"error": "Usuario o contraseña incorrectos"}), 401
    if not u["activo"]:
        conn.close(); return jsonify({"error": "Usuario deshabilitado"}), 403
    if u["bloqueo_hasta"]:
        bloqueo = datetime.fromisoformat(u["bloqueo_hasta"])
        if datetime.utcnow() < bloqueo:
            restante = max(1, int((bloqueo - datetime.utcnow()).total_seconds() // 60) + 1)
            conn.close(); return jsonify({"error": f"Acceso bloqueado temporalmente. Intentá nuevamente en {restante} minutos.", "bloqueado": True}), 429
        conn.execute("UPDATE usuarios SET intentos_fallidos = 0, bloqueo_hasta = NULL WHERE id = ?", (u["id"],)); conn.commit()
    if not check_password(password, u["password_hash"]):
        intentos = u["intentos_fallidos"] + 1
        if intentos >= MAX_INTENTOS:
            bloqueo_hasta = datetime.utcnow() + timedelta(minutes=BLOQUEO_MINUTOS)
            conn.execute("UPDATE usuarios SET intentos_fallidos = 0, bloqueo_hasta = ? WHERE id = ?", (bloqueo_hasta.isoformat(), u["id"])); conn.commit(); conn.close()
            return jsonify({"error": f"Demasiados intentos. Esperá {BLOQUEO_MINUTOS} minutos.", "bloqueado": True}), 429
        conn.execute("UPDATE usuarios SET intentos_fallidos = ? WHERE id = ?", (intentos, u["id"])); conn.commit(); conn.close()
        restantes = MAX_INTENTOS - intentos
        return jsonify({"error": f"Usuario o contraseña incorrectos. Te quedan {restantes} intentos.", "intentos_restantes": restantes}), 401
    # El código de seguridad se muestra en la misma pantalla de login.
    # Se genera en el servidor y se almacena únicamente como hash.
    challenge, codigo, _ = crear_desafio_otp(conn, u, 1)
    conn.close()
    return jsonify({
        "ok": True,
        "requiere_otp": True,
        "challenge": challenge,
        "codigo": codigo,
        "usuario": {
            "id": u["id"], "usuario": u["usuario"], "nombre": u["nombre"],
            "correo": u["correo"], "rol": u["rol"]
        }
    })

@app.route("/api/login/verify-otp", methods=["POST"])
def verificar_otp():
    conn = get_db()
    if rate_limit_exceeded(conn, "verify_otp"):
        conn.close()
        return rate_limit_response()
    data = request.get_json() or {}
    challenge = data.get("challenge", "").strip(); otp = data.get("otp", "").strip()
    if not challenge or len(otp) != 4 or not otp.isdigit(): return jsonify({"error": "Ingresá el código de 4 dígitos."}), 400
    row = conn.execute("SELECT o.*, u.activo, u.nombre, u.usuario, u.correo, u.ruc, u.rol, u.debe_cambiar FROM login_otp o JOIN usuarios u ON u.id = o.usuario_id WHERE o.challenge_token = ?", (challenge,)).fetchone()
    if not row: conn.close(); return jsonify({"error": "El código ya no es válido. Solicitá uno nuevo."}), 401
    if datetime.utcnow() >= datetime.fromisoformat(row["expira_en"]):
        conn.execute("DELETE FROM login_otp WHERE id = ?", (row["id"],)); conn.commit(); conn.close(); return jsonify({"error": "El código venció. Solicitá uno nuevo.", "vencido": True}), 401
    if not row["activo"]: conn.close(); return jsonify({"error": "Usuario deshabilitado"}), 403
    if not check_password(otp, row["otp_hash"]):
        intentos = row["intentos"] + 1
        if intentos >= MAX_INTENTOS:
            bloqueo_hasta = datetime.utcnow() + timedelta(minutes=BLOQUEO_MINUTOS)
            conn.execute("UPDATE usuarios SET intentos_fallidos = 0, bloqueo_hasta = ? WHERE id = ?", (bloqueo_hasta.isoformat(), row["usuario_id"]))
            conn.execute("DELETE FROM login_otp WHERE id = ?", (row["id"],)); conn.commit(); conn.close()
            return jsonify({"error": f"Demasiados códigos incorrectos. Esperá {BLOQUEO_MINUTOS} minutos.", "bloqueado": True}), 429
        conn.execute("UPDATE login_otp SET intentos = ? WHERE id = ?", (intentos, row["id"])); conn.commit(); conn.close()
        restantes = MAX_INTENTOS - intentos
        return jsonify({"error": f"Código incorrecto. Te quedan {restantes} intentos.", "intentos_restantes": restantes}), 401
    token = generar_token()
    csrf_token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM login_otp WHERE id = ?", (row["id"],))
    token_expira = datetime.utcnow() + timedelta(hours=SESION_HORAS)
    conn.execute("UPDATE usuarios SET token_sesion = NULL, token_sesion_hash = ?, csrf_token_hash = ?, token_expira_en = ?, intentos_fallidos = 0, bloqueo_hasta = NULL WHERE id = ?", (hash_token(token), hash_token(csrf_token), token_expira.isoformat(), row["usuario_id"]))
    conn.commit(); conn.close()
    response = jsonify({"ok": True, "csrf_token": csrf_token, "debe_cambiar": bool(row["debe_cambiar"]), "usuario": {"id": row["usuario_id"], "usuario": row["usuario"], "ruc": row["ruc"], "nombre": row["nombre"], "correo": row["correo"], "rol": row["rol"]}})
    response.set_cookie(SESSION_COOKIE_NAME, token, max_age=SESION_HORAS * 3600, secure=COOKIE_SECURE, httponly=True, samesite=COOKIE_SAMESITE, path="/")
    return response

@app.route("/api/login/resend-otp", methods=["POST"])
def reenviar_otp():
    conn = get_db()
    if rate_limit_exceeded(conn, "resend_otp"):
        conn.close()
        return rate_limit_response()
    data = request.get_json() or {}; challenge = data.get("challenge", "").strip()
    if not challenge: return jsonify({"error": "Desafío inválido"}), 400
    row = conn.execute("SELECT u.* FROM login_otp o JOIN usuarios u ON u.id = o.usuario_id WHERE o.challenge_token = ?", (challenge,)).fetchone()
    if not row: conn.close(); return jsonify({"error": "La sesión de verificación ya no es válida. Volvé a iniciar sesión."}), 401
    current = conn.execute("SELECT generaciones FROM login_otp WHERE challenge_token = ?", (challenge,)).fetchone()
    generaciones = int(current["generaciones"] or 1)
    if generaciones >= MAX_REGENERACIONES_OTP + 1:
        conn.close()
        return jsonify({"error": "Alcanzaste el máximo de 5 solicitudes de nuevo código. Volvé a iniciar sesión."}), 429
    new_challenge, codigo, total_generaciones = crear_desafio_otp(conn, row, generaciones + 1)
    conn.close()
    restantes = max(0, MAX_REGENERACIONES_OTP - (total_generaciones - 1))
    return jsonify({
        "ok": True,
        "challenge": new_challenge,
        "codigo": codigo,
        "regeneraciones_restantes": restantes,
        "message": "Se generó un nuevo código. El anterior quedó invalidado."
    })
# Cambiar contraseña (obligatorio en primer ingreso o tras reset admin)
@app.route("/api/cambiar-password", methods=["POST"])
@usuario_required
@csrf_required
def cambiar_password():
    u = obtener_usuario_por_token()
    conn = get_db()
    if rate_limit_exceeded(conn, "change_password"):
        conn.close()
        return rate_limit_response()
    data = request.get_json() or {}
    password_actual = data.get("password_actual", "")
    nueva_password = data.get("nueva_password", "")

    if not password_actual or not check_password(password_actual, u["password_hash"]):
        conn.close()
        return jsonify({"error": "La contraseña actual es incorrecta."}), 401

    error = validar_politica_password(nueva_password)
    if error:
        conn.close()
        return jsonify({"error": error}), 400

    nuevo_hash = hash_password(nueva_password)
    conn.execute("UPDATE usuarios SET password_hash = ?, debe_cambiar = 0, reset_token_hash = NULL, reset_expira_en = NULL, token_sesion = NULL, token_sesion_hash = NULL, csrf_token_hash = NULL, token_expira_en = NULL WHERE id = ?", (nuevo_hash, u["id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "Contraseña actualizada correctamente. Volvé a iniciar sesión."})

# Logout (invalida el token)
@app.route("/api/logout", methods=["POST"])
@csrf_required
def logout():
    u = obtener_usuario_por_token()
    if u:
        conn = get_db()
        conn.execute("UPDATE usuarios SET token_sesion = NULL, token_sesion_hash = NULL, csrf_token_hash = NULL, token_expira_en = NULL WHERE id = ?", (u["id"],))
        conn.commit(); conn.close()
    response = jsonify({"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE)
    return response

# Solicitar reset (crea ticket)
@app.route("/api/solicitar-reset", methods=["POST"])
def solicitar_reset():
    conn = get_db()
    if rate_limit_exceeded(conn, "request_reset"):
        conn.close()
        return rate_limit_response()
    data = request.get_json() or {}
    ruc = data.get("ruc", "").strip()
    if not ruc:
        return jsonify({"error": "Ingresá tu RUC"}), 400
    u = conn.execute("SELECT * FROM usuarios WHERE ruc = ?", (ruc,)).fetchone()
    if u:
        pendiente = conn.execute(
            "SELECT id FROM tickets_recuperacion WHERE usuario_id = ? AND estado = 'pendiente'",
            (u["id"],)
        ).fetchone()
        if not pendiente:
            conn.execute(
                "INSERT INTO tickets_recuperacion (usuario_id, ruc) VALUES (?, ?)",
                (u["id"], ruc)
            )
            conn.commit()
    conn.close()
    # Respuesta genérica para no revelar si el RUC existe o si ya tiene un pedido pendiente.
    return jsonify({"ok": True, "message": "Si los datos corresponden a una cuenta, la solicitud fue registrada y será revisada por un administrador."})

# Admin: listar tickets
@app.route("/api/admin/tickets", methods=["GET"])
@admin_required
def listar_tickets():
    conn = get_db()
    tickets = conn.execute("""
        SELECT t.*, u.nombre, u.correo FROM tickets_recuperacion t
        JOIN usuarios u ON u.id = t.usuario_id
        ORDER BY CASE t.estado WHEN 'pendiente' THEN 0 ELSE 1 END, t.creado_en DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(t) for t in tickets])

# Admin: aprobar ticket y enviar enlace de restablecimiento de un solo uso
@app.route("/api/admin/tickets/<int:ticket_id>/aprobar", methods=["POST"])
@admin_required
def aprobar_ticket(ticket_id):
    conn = get_db()
    t = conn.execute("SELECT * FROM tickets_recuperacion WHERE id = ?", (ticket_id,)).fetchone()
    if not t:
        conn.close()
        return jsonify({"error": "Ticket no encontrado"}), 404
    if t["estado"] != "pendiente":
        conn.close()
        return jsonify({"error": "Este ticket ya fue resuelto"}), 400
    u_actual = obtener_usuario_por_token()
    objetivo = conn.execute("SELECT rol FROM usuarios WHERE id = ?", (t["usuario_id"],)).fetchone()
    if not objetivo:
        conn.close()
        return jsonify({"error": "Usuario asociado no encontrado"}), 404
    if not puede_gestionar(u_actual["rol"], objetivo["rol"]):
        conn.close()
        return jsonify({"error": "No tenés permisos para resolver este ticket."}), 403
    token = generar_token()
    token_hash = hash_token(token)
    expira = datetime.utcnow() + timedelta(hours=RESET_TOKEN_HORAS)
    conn.execute(
        "UPDATE usuarios SET reset_token_hash = ?, reset_expira_en = ?, intentos_fallidos = 0, bloqueo_hasta = NULL, token_sesion = NULL, token_sesion_hash = NULL, csrf_token_hash = NULL, token_expira_en = NULL WHERE id = ?",
        (token_hash, expira.isoformat(), t["usuario_id"])
    )
    conn.execute(
        "UPDATE tickets_recuperacion SET estado = 'aprobado', nueva_password = NULL, resuelto_en = datetime('now') WHERE id = ?",
        (ticket_id,)
    )
    conn.commit()
    u = conn.execute("SELECT * FROM usuarios WHERE id = ?", (t["usuario_id"],)).fetchone()
    conn.close()
    if not u or not u["correo"]:
        return jsonify({"error": "El usuario no tiene un correo de recuperación configurado."}), 400
    enlace = f"{RESET_URL_BASE}?token={token}"
    cuerpo = f"""
    <h2>Kakuaa Consultores</h2>
    <p>Hola <strong>{html.escape(str(u["nombre"]))}</strong>,</p>
    <p>Tu solicitud de recuperación fue aprobada.</p>
    <p>El siguiente enlace te permitirá establecer una nueva contraseña. Es de un solo uso y vence en {RESET_TOKEN_HORAS} hora.</p>
    <p><a href="{html.escape(enlace, quote=True)}">Restablecer mi contraseña</a></p>
    <p>Si no solicitaste este cambio, podés ignorar este correo.</p>
    <p>Saludos,<br>Equipo Kakuaa Consultores</p>
    """
    if not enviar_correo(u["correo"], "Restablecer contraseña - Kakuaa Consultores", cuerpo):
        return jsonify({"ok": True, "message": "Solicitud aprobada, pero no se pudo enviar el correo. Revisá la configuración SMTP."})
    return jsonify({"ok": True, "message": "Solicitud aprobada y enlace de recuperación enviado."})

# Restablecer contraseña mediante token de un solo uso
@app.route("/api/restablecer-password", methods=["POST"])
def restablecer_password():
    conn = get_db()
    if rate_limit_exceeded(conn, "reset_password"):
        conn.close()
        return rate_limit_response()
    data = request.get_json() or {}
    token = data.get("token", "").strip()
    nueva_password = data.get("nueva_password", "")
    confirmar_password = data.get("confirmar_password", "")
    if not token:
        return jsonify({"error": "Enlace de recuperación inválido."}), 400
    if nueva_password != confirmar_password:
        return jsonify({"error": "Las contraseñas no coinciden."}), 400
    error_password = validar_politica_password(nueva_password)
    if error_password:
        return jsonify({"error": error_password}), 400
    u = conn.execute(
        "SELECT * FROM usuarios WHERE reset_token_hash = ? AND reset_expira_en IS NOT NULL",
        (hash_token(token),)
    ).fetchone()
    if not u:
        conn.close()
        return jsonify({"error": "El enlace de recuperación no es válido o ya fue utilizado."}), 400
    try:
        if datetime.utcnow() >= datetime.fromisoformat(u["reset_expira_en"]):
            conn.execute("UPDATE usuarios SET reset_token_hash = NULL, reset_expira_en = NULL WHERE id = ?", (u["id"],))
            conn.commit()
            conn.close()
            return jsonify({"error": "El enlace de recuperación venció. Solicitá una nueva recuperación."}), 400
    except ValueError:
        conn.execute("UPDATE usuarios SET reset_token_hash = NULL, reset_expira_en = NULL WHERE id = ?", (u["id"],))
        conn.commit()
        conn.close()
        return jsonify({"error": "El enlace de recuperación no es válido."}), 400
    hashed = hash_password(nueva_password)
    conn.execute(
        "UPDATE usuarios SET password_hash = ?, debe_cambiar = 0, intentos_fallidos = 0, bloqueo_hasta = NULL, token_sesion = NULL, token_sesion_hash = NULL, token_expira_en = NULL, reset_token_hash = NULL, reset_expira_en = NULL WHERE id = ?",
        (hashed, u["id"])
    )
    conn.commit()
    conn.close()
    if u["correo"]:
        cuerpo = f"""
        <h2>Kakuaa Consultores</h2>
        <p>Hola <strong>{html.escape(str(u["nombre"]))}</strong>,</p>
        <p>Tu contraseña fue restablecida correctamente.</p>
        <p>Si no realizaste este cambio, contactá al administrador de Kakuaa Consultores.</p>
        """
        enviar_correo(u["correo"], "Contraseña restablecida - Kakuaa Consultores", cuerpo)
    return jsonify({"ok": True, "message": "Contraseña actualizada correctamente. Ya podés iniciar sesión."})

# Admin: rechazar ticket
@app.route("/api/admin/tickets/<int:ticket_id>/rechazar", methods=["POST"])
@admin_required
def rechazar_ticket(ticket_id):
    conn = get_db()
    t = conn.execute("SELECT * FROM tickets_recuperacion WHERE id = ?", (ticket_id,)).fetchone()
    if not t:
        conn.close()
        return jsonify({"error": "Ticket no encontrado"}), 404
    if t["estado"] != "pendiente":
        conn.close()
        return jsonify({"error": "Este ticket ya fue resuelto"}), 400
    u_actual = obtener_usuario_por_token()
    objetivo = conn.execute("SELECT rol FROM usuarios WHERE id = ?", (t["usuario_id"],)).fetchone()
    if not objetivo:
        conn.close()
        return jsonify({"error": "Usuario asociado no encontrado"}), 404
    if not puede_gestionar(u_actual["rol"], objetivo["rol"]):
        conn.close()
        return jsonify({"error": "No tenés permisos para resolver este ticket."}), 403
    conn.execute("UPDATE tickets_recuperacion SET estado = 'rechazado', resuelto_en = datetime('now') WHERE id = ?", (ticket_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "Ticket rechazado"})

# Admin: listar usuarios
@app.route("/api/admin/usuarios", methods=["GET"])
@staff_required
def listar_usuarios():
    u_actual = obtener_usuario_por_token()
    roles_visibles = [rol for rol in ROLES if puede_gestionar(u_actual["rol"], rol)]
    if not roles_visibles:
        return jsonify([])
    placeholders = ",".join("?" for _ in roles_visibles)
    conn = get_db()
    usuarios = conn.execute(
        f"SELECT id, usuario, ruc, correo, nombre, activo, rol, debe_cambiar FROM usuarios "
        f"WHERE rol IN ({placeholders}) "
        "ORDER BY CASE rol WHEN 'superadmin' THEN 0 WHEN 'admin' THEN 1 WHEN 'operativo' THEN 2 ELSE 3 END, id",
        roles_visibles,
    ).fetchall()
    conn.close()
    return jsonify([dict(u) for u in usuarios])

# Admin: crear usuario (marca debe_cambiar para forzar cambio en primer ingreso)
@app.route("/api/admin/usuarios", methods=["POST"])
@staff_required
def crear_usuario():
    data = request.get_json() or {}
    ruc = data.get("ruc", "").strip()
    correo = data.get("correo", "").strip()
    nombre = data.get("nombre", "").strip()
    usuario_nuevo = data.get("usuario", "").strip() or ruc
    contrasena = data.get("contrasena", "")
    rol_nuevo = data.get("rol", "contribuyente").strip()
    if not ruc or not correo or not nombre or not contrasena or not usuario_nuevo:
        return jsonify({"error": "Faltan datos"}), 400
    error_password = validar_politica_password(contrasena)
    if error_password:
        return jsonify({"error": error_password}), 400
    if rol_nuevo not in ("admin", "operativo", "contribuyente"):
        return jsonify({"error": "Rol inválido o no permitido"}), 400
    u_actual = obtener_usuario_por_token()
    if not puede_gestionar(u_actual["rol"], rol_nuevo):
        return jsonify({"error": "No tenés permisos para crear este rol."}), 403
    conn = get_db()
    try:
        hashed = hash_password(contrasena)
        cur = conn.execute("INSERT INTO usuarios (ruc, correo, nombre, password_hash, usuario, rol, debe_cambiar) VALUES (?, ?, ?, ?, ?, ?, 1)",
                           (ruc, correo, nombre, hashed, usuario_nuevo, rol_nuevo))
        conn.commit()
        return jsonify({"ok": True, "id": cur.lastrowid}), 201
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "El RUC o correo ya existe"}), 409

# Admin: editar usuario (invalida token si cambia RUC o desactiva)
@app.route("/api/admin/usuarios/<int:usuario_id>", methods=["PUT"])
@staff_required
def editar_usuario(usuario_id):
    data = request.get_json() or {}
    ruc = data.get("ruc", "").strip()
    correo = data.get("correo", "").strip()
    nombre = data.get("nombre", "").strip()
    conn = get_db()
    u = conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if not u:
        conn.close()
        return jsonify({"error": "Usuario no encontrado"}), 404
    u_actual = obtener_usuario_por_token()
    if u["rol"] == "superadmin" or not puede_gestionar(u_actual["rol"], u["rol"]):
        conn.close()
        return jsonify({"error": "No tenés permisos para editar este usuario."}), 403
    if not ruc or not correo or not nombre:
        conn.close()
        return jsonify({"error": "RUC, correo y nombre son obligatorios."}), 400
    try:
        conn.execute("UPDATE usuarios SET ruc = ?, correo = ?, nombre = ? WHERE id = ?", (ruc, correo, nombre, usuario_id))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "El RUC o correo ya existe"}), 409

# La recuperación administrativa usa exclusivamente tickets + enlaces de un solo uso.\n# No se permite establecer ni enviar contraseñas manualmente desde el panel.\n\n# Admin: cambiar estado (habilitar/deshabilitar, invalida token si deshabilitas)
@app.route("/api/admin/usuarios/<int:usuario_id>/estado", methods=["PUT"])
@staff_required
def cambiar_estado(usuario_id):
    data = request.get_json() or {}
    activo = bool(data.get("activo"))
    u_actual = obtener_usuario_por_token()
    conn = get_db()
    objetivo = conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if not objetivo:
        conn.close()
        return jsonify({"error": "Usuario no encontrado"}), 404
    if objetivo["rol"] == "superadmin":
        conn.close()
        return jsonify({"error": "No se puede deshabilitar al SUPERADMIN."}), 403
    if not puede_gestionar(u_actual["rol"], objetivo["rol"]):
        conn.close()
        return jsonify({"error": "No tenés permisos para esta operación."}), 403
    conn.execute(
        "UPDATE usuarios SET activo = ?, "
        "token_sesion = CASE WHEN ? = 0 THEN NULL ELSE token_sesion END, "
        "token_sesion_hash = CASE WHEN ? = 0 THEN NULL ELSE token_sesion_hash END, "
        "csrf_token_hash = CASE WHEN ? = 0 THEN NULL ELSE csrf_token_hash END, "
        "token_expira_en = CASE WHEN ? = 0 THEN NULL ELSE token_expira_en END "
        "WHERE id = ?",
        (1 if activo else 0, 1 if activo else 0, 1 if activo else 0, 1 if activo else 0, 1 if activo else 0, usuario_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# Admin: asignar rol
@app.route("/api/admin/usuarios/<int:usuario_id>/rol", methods=["PUT"])
@staff_required
def asignar_rol(usuario_id):
    data = request.get_json() or {}
    nuevo_rol = data.get("rol", "").strip()
    if nuevo_rol not in ROLES or nuevo_rol == "superadmin":
        return jsonify({"error": "Rol inválido o no permitido"}), 400
    u_actual = obtener_usuario_por_token()
    conn = get_db()
    objetivo = conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if not objetivo:
        conn.close()
        return jsonify({"error": "Usuario no encontrado"}), 404
    if objetivo["rol"] == "superadmin":
        conn.close()
        return jsonify({"error": "El SUPERADMIN es único y no puede ser modificado desde este módulo."}), 403
    if not puede_gestionar(u_actual["rol"], objetivo["rol"]) or not puede_gestionar(u_actual["rol"], nuevo_rol):
        conn.close()
        return jsonify({"error": "No tenés permisos para esta operación."}), 403
    conn.execute("UPDATE usuarios SET rol = ?, token_sesion = NULL, token_sesion_hash = NULL, csrf_token_hash = NULL, token_expira_en = NULL WHERE id = ?", (nuevo_rol, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "Rol actualizado"})

@app.route("/api/admin/usuarios/<int:usuario_id>/documentos", methods=["GET"])
@staff_required
def admin_documentos(usuario_id):
    u_actual = obtener_usuario_por_token()
    conn = get_db()
    objetivo = conn.execute("SELECT rol FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if not objetivo:
        conn.close()
        return jsonify({"error": "Usuario no encontrado"}), 404
    if not puede_gestionar(u_actual["rol"], objetivo["rol"]):
        conn.close()
        return jsonify({"error": "No tenés permisos para esta operación."}), 403
    docs = conn.execute("SELECT * FROM documentos WHERE usuario_id = ? ORDER BY subido_en DESC", (usuario_id,)).fetchall()
    conn.close()
    return jsonify([dict(d) for d in docs])

# Admin: subir documento
@app.route("/api/admin/usuarios/<int:usuario_id>/documentos", methods=["POST"])
@staff_required
def admin_subir_documento(usuario_id):
    u_actual = obtener_usuario_por_token()
    conn = get_db()
    objetivo = conn.execute("SELECT rol FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if not objetivo:
        conn.close()
        return jsonify({"error": "Usuario no encontrado"}), 404
    if not puede_gestionar(u_actual["rol"], objetivo["rol"]):
        conn.close()
        return jsonify({"error": "No tenés permisos para esta operación."}), 403
    conn.close()

    archivo = request.files.get("archivo")
    carpeta = request.form.get("carpeta", "")
    subcarpeta = request.form.get("subcarpeta", "")
    subcarpeta2 = request.form.get("subcarpeta2", "")
    if not archivo or not carpeta:
        return jsonify({"error": "Faltan datos"}), 400
    carpetas_permitidas = {"Declaraciones", "Balances", "Estados de Cuentas", "Facturas"}
    if carpeta not in carpetas_permitidas:
        return jsonify({"error": "Carpeta no permitida"}), 400
    nombre_archivo = secure_filename(archivo.filename or "")
    if not nombre_archivo:
        return jsonify({"error": "Nombre de archivo inválido"}), 400
    extension = nombre_archivo.rsplit(".", 1)[1].lower() if "." in nombre_archivo else ""
    if extension not in ALLOWED_EXTENSIONS:
        return jsonify({"error": "Tipo de archivo no permitido"}), 400
    subcarpeta = secure_filename(subcarpeta) if subcarpeta else ""
    subcarpeta2 = secure_filename(subcarpeta2) if subcarpeta2 else ""
    try:
        dir_usuario = ruta_segura(DOCS_DIR, str(usuario_id))
        dir_carpeta = ruta_segura(dir_usuario, carpeta)
        if subcarpeta:
            dir_carpeta = ruta_segura(dir_carpeta, subcarpeta)
        if subcarpeta2:
            dir_carpeta = ruta_segura(dir_carpeta, subcarpeta2)
        ruta = ruta_segura(dir_carpeta, nombre_archivo)
    except ValueError:
        return jsonify({"error": "Ruta de archivo inválida"}), 400
    os.makedirs(dir_carpeta, exist_ok=True)
    archivo.save(ruta)
    conn = get_db()
    cur = conn.execute("INSERT INTO documentos (usuario_id, nombre_archivo, ruta, carpeta, subcarpeta, subcarpeta2) VALUES (?, ?, ?, ?, ?, ?)",
                       (usuario_id, nombre_archivo, ruta, carpeta, subcarpeta, subcarpeta2))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": cur.lastrowid}), 201

# Admin: eliminar documento
@app.route("/api/admin/documentos/<int:doc_id>", methods=["DELETE"])
@staff_required
def admin_eliminar_documento(doc_id):
    u_actual = obtener_usuario_por_token()
    conn = get_db()
    d = conn.execute("SELECT * FROM documentos WHERE id = ?", (doc_id,)).fetchone()
    if not d:
        conn.close()
        return jsonify({"error": "Documento no encontrado"}), 404
    objetivo = conn.execute("SELECT rol FROM usuarios WHERE id = ?", (d["usuario_id"],)).fetchone()
    if not objetivo or not puede_gestionar(u_actual["rol"], objetivo["rol"]):
        conn.close()
        return jsonify({"error": "No tenés permisos para esta operación."}), 403
    try:
        ruta = os.path.realpath(d["ruta"])
        docs_real = os.path.realpath(DOCS_DIR)
        if os.path.commonpath([docs_real, ruta]) != docs_real:
            conn.close()
            return jsonify({"error": "Ruta de documento inválida"}), 400
        if os.path.exists(ruta):
            if not os.path.isfile(ruta):
                conn.close()
                return jsonify({"error": "Documento no disponible"}), 404
            os.remove(ruta)
    except ValueError:
        conn.close()
        return jsonify({"error": "Ruta de documento inválida"}), 400
    conn.execute("DELETE FROM documentos WHERE id = ?", (doc_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# Admin: subcarpetas de un usuario
@app.route("/api/admin/usuarios/<int:usuario_id>/subcarpetas", methods=["GET"])
@staff_required
def admin_subcarpetas(usuario_id):
    u_actual = obtener_usuario_por_token()
    conn = get_db()
    objetivo = conn.execute("SELECT rol FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if not objetivo:
        conn.close()
        return jsonify({"error": "Usuario no encontrado"}), 404
    if not puede_gestionar(u_actual["rol"], objetivo["rol"]):
        conn.close()
        return jsonify({"error": "No tenés permisos para esta operación."}), 403
    subs = conn.execute("SELECT * FROM subcarpetas WHERE usuario_id = ?", (usuario_id,)).fetchall()
    conn.close()
    return jsonify([dict(s) for s in subs])

# Admin: crear subcarpeta
@app.route("/api/admin/usuarios/<int:usuario_id>/subcarpetas", methods=["POST"])
@staff_required
def admin_crear_subcarpeta(usuario_id):
    data = request.get_json() or {}
    carpeta = data.get("carpeta", "")
    nombre = secure_filename(data.get("nombre", "").strip())
    padre = secure_filename(data.get("padre", "").strip()) if data.get("padre") else ""
    carpetas_permitidas = {"Declaraciones", "Balances", "Estados de Cuentas", "Facturas"}
    if carpeta not in carpetas_permitidas or not nombre:
        return jsonify({"error": "Datos de subcarpeta inválidos"}), 400
    conn = get_db()
    u_actual = obtener_usuario_por_token()
    objetivo = conn.execute("SELECT rol FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if not objetivo:
        conn.close()
        return jsonify({"error": "Usuario no encontrado"}), 404
    if not puede_gestionar(u_actual["rol"], objetivo["rol"]):
        conn.close()
        return jsonify({"error": "No tenés permisos para esta operación."}), 403
    if padre:
        parent = conn.execute(
            "SELECT id FROM subcarpetas WHERE usuario_id = ? AND carpeta = ? AND nombre = ?",
            (usuario_id, carpeta, padre)
        ).fetchone()
        if not parent:
            conn.close()
            return jsonify({"error": "Subcarpeta padre no encontrada"}), 404
    existe = conn.execute("SELECT * FROM subcarpetas WHERE usuario_id = ? AND carpeta = ? AND nombre = ? AND padre = ?",
                          (usuario_id, carpeta, nombre, padre)).fetchone()
    if existe:
        conn.close()
        return jsonify({"error": "Esa subcarpeta ya existe"}), 409
    conn.execute("INSERT INTO subcarpetas (usuario_id, carpeta, nombre, padre) VALUES (?, ?, ?, ?)",
                 (usuario_id, carpeta, nombre, padre))
    conn.commit()
    conn.close()
    return jsonify({"ok": True}), 201

# Admin: eliminar subcarpeta
@app.route("/api/admin/usuarios/<int:usuario_id>/subcarpetas/<path:nombre>", methods=["DELETE"])
@staff_required
def admin_eliminar_subcarpeta(usuario_id, nombre):
    u_actual = obtener_usuario_por_token()
    nombre = secure_filename(nombre)
    conn = get_db()
    objetivo = conn.execute("SELECT rol FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if not objetivo:
        conn.close()
        return jsonify({"error": "Usuario no encontrado"}), 404
    if not puede_gestionar(u_actual["rol"], objetivo["rol"]):
        conn.close()
        return jsonify({"error": "No tenés permisos para esta operación."}), 403
    s = conn.execute("SELECT * FROM subcarpetas WHERE usuario_id = ? AND nombre = ?", (usuario_id, nombre)).fetchone()
    if not s:
        conn.close()
        return jsonify({"error": "Subcarpeta no encontrada"}), 404
    conn.execute("DELETE FROM subcarpetas WHERE usuario_id = ? AND padre = ?", (usuario_id, nombre))
    conn.execute("DELETE FROM documentos WHERE usuario_id = ? AND subcarpeta = ?", (usuario_id, nombre))
    conn.execute("DELETE FROM subcarpetas WHERE id = ?", (s["id"],))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# Contribuyente: sus documentos
@app.route("/api/mis-documentos", methods=["GET"])
@usuario_required
def mis_documentos():
    u = obtener_usuario_por_token()
    conn = get_db()
    docs = conn.execute("SELECT * FROM documentos WHERE usuario_id = ?", (u["id"],)).fetchall()
    conn.close()
    return jsonify([dict(d) for d in docs])

# Contribuyente: descargar documento
@app.route("/api/mis-documentos/<int:doc_id>/descargar", methods=["GET"])
@usuario_required
def descargar_documento(doc_id):
    u = obtener_usuario_por_token()
    conn = get_db()
    d = conn.execute("SELECT * FROM documentos WHERE id = ? AND usuario_id = ?", (doc_id, u["id"])).fetchone()
    conn.close()
    if not d:
        return jsonify({"error": "No autorizado"}), 403
    try:
        ruta = os.path.realpath(d["ruta"])
        docs_real = os.path.realpath(DOCS_DIR)
        if os.path.commonpath([docs_real, ruta]) != docs_real or not os.path.isfile(ruta):
            return jsonify({"error": "Documento no disponible"}), 404
    except ValueError:
        return jsonify({"error": "Ruta de documento inválida"}), 400
    return send_from_directory(os.path.dirname(ruta), os.path.basename(ruta), as_attachment=True)


# ---------- SUPERADMIN: artículos, tarifas y costos ----------
def _tarifa_vigente(conn, articulo_id, fecha):
    return conn.execute("""
        SELECT * FROM tarifas_articulos
        WHERE articulo_id = ? AND activo = 1
          AND vigencia_desde <= ? AND vigencia_hasta >= ?
        ORDER BY vigencia_desde DESC, id DESC LIMIT 1
    """, (articulo_id, fecha, fecha)).fetchone()

@app.route("/api/admin/articulos", methods=["GET"])
@admin_required
def listar_articulos():
    conn=get_db()
    rows=conn.execute("""
        SELECT a.*, 
               (SELECT COUNT(*) FROM tarifas_articulos t WHERE t.articulo_id=a.id AND t.activo=1) AS tarifas,
               (SELECT t.precio FROM tarifas_articulos t WHERE t.articulo_id=a.id AND t.activo=1
                ORDER BY t.vigencia_hasta DESC, t.id DESC LIMIT 1) AS ultima_tarifa
        FROM articulos a ORDER BY a.activo DESC, a.nombre COLLATE NOCASE
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/articulos", methods=["POST"])
@admin_required
def crear_articulo():
    data=request.get_json() or {}
    codigo=str(data.get("codigo","")).strip()
    nombre=str(data.get("nombre","")).strip()
    descripcion=str(data.get("descripcion","")).strip()
    unidad=str(data.get("unidad","servicio")).strip() or "servicio"
    if not codigo or not nombre:
        return jsonify({"error":"Código y nombre son obligatorios."}),400
    conn=get_db()
    try:
        cur=conn.execute("INSERT INTO articulos(codigo,nombre,descripcion,unidad) VALUES(?,?,?,?)",(codigo,nombre,descripcion,unidad))
        conn.commit(); aid=cur.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback(); conn.close(); return jsonify({"error":"El código del artículo ya existe."}),409
    conn.close(); return jsonify({"ok":True,"id":aid})

@app.route("/api/admin/articulos/<int:articulo_id>", methods=["PATCH"])
@admin_required
def actualizar_articulo(articulo_id):
    data=request.get_json() or {}; cambios=[]; valores=[]
    for campo in ("codigo","nombre","descripcion","unidad"):
        if campo in data:
            valor=str(data[campo]).strip()
            if campo in ("codigo","nombre") and not valor:
                return jsonify({"error":"Código y nombre no pueden quedar vacíos."}),400
            cambios.append(campo+" = ?"); valores.append(valor)
    if "activo" in data:
        cambios.append("activo = ?"); valores.append(1 if data["activo"] else 0)
    if not cambios: return jsonify({"ok":True})
    valores.append(articulo_id); conn=get_db()
    try:
        conn.execute("UPDATE articulos SET "+", ".join(cambios)+" WHERE id=?",tuple(valores)); conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback(); conn.close(); return jsonify({"error":"El código ya está utilizado."}),409
    conn.close(); return jsonify({"ok":True})

@app.route("/api/admin/tarifas", methods=["GET"])
@admin_required
def listar_tarifas():
    conn=get_db()
    rows=conn.execute("""
        SELECT t.*, a.codigo, a.nombre AS articulo_nombre
        FROM tarifas_articulos t JOIN articulos a ON a.id=t.articulo_id
        ORDER BY t.vigencia_hasta DESC, a.nombre COLLATE NOCASE
    """).fetchall()
    conn.close(); return jsonify([dict(r) for r in rows])

@app.route("/api/admin/tarifas", methods=["POST"])
@admin_required
def crear_tarifa():
    data=request.get_json() or {}
    try: articulo_id=int(data.get("articulo_id")); precio=float(data.get("precio"))
    except (TypeError,ValueError): return jsonify({"error":"Artículo y precio son obligatorios."}),400
    desde=str(data.get("vigencia_desde","")).strip(); hasta=str(data.get("vigencia_hasta","")).strip()
    try: ajuste=float(data.get("ajuste_vencimiento",0))
    except (TypeError,ValueError): return jsonify({"error":"El factor de actualización debe ser numérico."}),400
    if precio < 0 or not desde or not hasta or hasta < desde or ajuste < -100:
        return jsonify({"error":"Vigencia, precio o factor de actualización inválidos."}),400
    conn=get_db()
    if not conn.execute("SELECT id FROM articulos WHERE id=? AND activo=1",(articulo_id,)).fetchone():
        conn.close(); return jsonify({"error":"Artículo no encontrado o inactivo."}),404
    cur=conn.execute("""
        INSERT INTO tarifas_articulos(articulo_id,precio,vigencia_desde,vigencia_hasta,ajuste_vencimiento,creado_por)
        VALUES(?,?,?,?,?,?)
    """,(articulo_id,precio,desde,hasta,ajuste,obtener_usuario_por_token()["id"]))
    conn.commit(); tid=cur.lastrowid; conn.close()
    return jsonify({"ok":True,"id":tid,"precio_sugerido_siguiente":round(precio*(1+ajuste/100),2)})

@app.route("/api/admin/tarifas/<int:tarifa_id>/renovar", methods=["POST"])
@admin_required
def renovar_tarifa(tarifa_id):
    data=request.get_json() or {}; desde=str(data.get("vigencia_desde","")).strip(); hasta=str(data.get("vigencia_hasta","")).strip()
    conn=get_db(); anterior=conn.execute("SELECT * FROM tarifas_articulos WHERE id=?",(tarifa_id,)).fetchone()
    if not anterior: conn.close(); return jsonify({"error":"Tarifa no encontrada."}),404
    try: ajuste=float(data.get("ajuste_vencimiento",anterior["ajuste_vencimiento"] or 0))
    except (TypeError,ValueError): conn.close(); return jsonify({"error":"Factor inválido."}),400
    if not desde or not hasta or hasta < desde:
        conn.close(); return jsonify({"error":"La vigencia es inválida."}),400
    precio=float(data.get("precio", anterior["precio"]*(1+ajuste/100)))
    if precio < 0: conn.close(); return jsonify({"error":"Precio inválido."}),400
    cur=conn.execute("""
        INSERT INTO tarifas_articulos(articulo_id,precio,vigencia_desde,vigencia_hasta,ajuste_vencimiento,creado_por)
        VALUES(?,?,?,?,?,?)
    """,(anterior["articulo_id"],precio,desde,hasta,ajuste,obtener_usuario_por_token()["id"]))
    conn.commit(); tid=cur.lastrowid; conn.close()
    return jsonify({"ok":True,"id":tid,"precio":round(precio,2),"precio_anterior":float(anterior["precio"]),"factor":ajuste})

@app.route("/api/admin/tarifas/vencidas", methods=["GET"])
@admin_required
def listar_tarifas_vencidas():
    hoy=datetime.utcnow().strftime("%Y-%m-%d"); conn=get_db()
    rows=conn.execute("""
        SELECT t.*,a.codigo,a.nombre AS articulo_nombre,
               ROUND(t.precio*(1+t.ajuste_vencimiento/100.0),2) AS precio_sugerido
        FROM tarifas_articulos t JOIN articulos a ON a.id=t.articulo_id
        WHERE t.activo=1 AND t.vigencia_hasta < ?
          AND NOT EXISTS (
            SELECT 1 FROM tarifas_articulos n
            WHERE n.articulo_id=t.articulo_id AND n.activo=1 AND n.vigencia_desde > t.vigencia_hasta
          )
        ORDER BY t.vigencia_hasta
    """,(hoy,)).fetchall()
    conn.close(); return jsonify([dict(r) for r in rows])

@app.route("/api/admin/costos-persona", methods=["GET"])
@admin_required
def listar_costos_persona():
    conn=get_db()
    rows=conn.execute("""
        SELECT c.*,u.nombre,u.usuario,u.rol
        FROM costos_persona c JOIN usuarios u ON u.id=c.usuario_id
        WHERE u.rol IN ('admin','operativo')
        ORDER BY u.nombre COLLATE NOCASE,c.vigencia_desde DESC
    """).fetchall()
    conn.close(); return jsonify([dict(r) for r in rows])

@app.route("/api/admin/costos-persona", methods=["POST"])
@admin_required
def crear_costo_persona():
    data=request.get_json() or {}
    try: usuario_id=int(data.get("usuario_id")); costo=float(data.get("costo_hora"))
    except (TypeError,ValueError): return jsonify({"error":"Persona y costo/hora son obligatorios."}),400
    desde=str(data.get("vigencia_desde","")).strip(); hasta=str(data.get("vigencia_hasta","")).strip() or None
    if costo<0 or not desde or (hasta and hasta<desde): return jsonify({"error":"Datos de costo inválidos."}),400
    conn=get_db()
    if not _usuario_trabajo_valido(conn,usuario_id):
        conn.close(); return jsonify({"error":"La persona no es ADMIN/OPERATIVO activo."}),400
    cur=conn.execute("""
        INSERT INTO costos_persona(usuario_id,costo_hora,vigencia_desde,vigencia_hasta,creado_por)
        VALUES(?,?,?,?,?)
    """,(usuario_id,costo,desde,hasta,obtener_usuario_por_token()["id"]))
    conn.commit(); cid=cur.lastrowid; conn.close()
    return jsonify({"ok":True,"id":cid})

@app.route("/api/admin/costos-persona/resumen", methods=["GET"])
@admin_required
def resumen_costos_persona():
    hoy=datetime.utcnow().strftime("%Y-%m-%d")
    conn=get_db()
    rows=conn.execute("""
        SELECT u.id,u.nombre,u.rol,
               COALESCE(SUM((julianday(COALESCE(s.fin,datetime('now')))-julianday(s.inicio))*24.0),0) AS horas,
               COALESCE((SELECT SUM(
                    (julianday(COALESCE(s2.fin,datetime('now')))-julianday(s2.inicio))*24.0 *
                    COALESCE((SELECT cp.costo_hora FROM costos_persona cp
                              WHERE cp.usuario_id=s2.usuario_id AND cp.vigencia_desde <= date(s2.inicio)
                                AND (cp.vigencia_hasta IS NULL OR cp.vigencia_hasta >= date(s2.inicio))
                              ORDER BY cp.vigencia_desde DESC,cp.id DESC LIMIT 1),0)
                ) FROM sesiones_trabajo s2 WHERE s2.usuario_id=u.id),0) AS costo_estimado
        FROM usuarios u LEFT JOIN sesiones_trabajo s ON s.usuario_id=u.id
        WHERE u.rol IN ('admin','operativo') GROUP BY u.id,u.nombre,u.rol
        ORDER BY costo_estimado DESC,u.nombre COLLATE NOCASE
    """).fetchall()
    conn.close(); return jsonify([dict(r) for r in rows])

# ---------- SUPERADMIN: dashboard, facturación, tareas y tracking ----------
PRIORIDADES_TAREA = {"urgente": 1, "alta": 2, "media": 3, "baja": 4}
ESTADOS_TAREA = {"pendiente", "en_progreso", "bloqueada", "completada", "cancelada"}
ESTADOS_FACTURA = {"emitida", "cobrada", "anulada"}

def _cliente_valido(conn, cliente_id):
    return conn.execute(
        "SELECT * FROM usuarios WHERE id = ? AND rol = 'contribuyente'",
        (cliente_id,),
    ).fetchone()

def _usuario_trabajo_valido(conn, usuario_id):
    return conn.execute(
        "SELECT * FROM usuarios WHERE id = ? AND rol IN ('admin','operativo') AND activo = 1",
        (usuario_id,),
    ).fetchone()

def _cerrar_sesion_activa(conn, usuario_id, fin=None):
    fin = fin or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE sesiones_trabajo SET fin = ? WHERE usuario_id = ? AND fin IS NULL",
        (fin, usuario_id),
    )

def _detener_tarea_si_corresponde(conn, tarea_id, usuario_id=None):
    query = "SELECT * FROM sesiones_trabajo WHERE tarea_id = ? AND fin IS NULL"
    params = [tarea_id]
    if usuario_id is not None:
        query += " AND usuario_id = ?"
        params.append(usuario_id)
    sesiones = conn.execute(query, tuple(params)).fetchall()
    ahora = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for sesion in sesiones:
        conn.execute("UPDATE sesiones_trabajo SET fin = ? WHERE id = ?", (ahora, sesion["id"]))

@app.route("/api/admin/dashboard", methods=["GET"])
@admin_required
def admin_dashboard():
    conn = get_db()
    clientes = conn.execute("""
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN activo = 1 THEN 1 ELSE 0 END),0) AS activos
        FROM usuarios WHERE rol = 'contribuyente'
    """).fetchone()
    facturacion = conn.execute("""
        SELECT COALESCE(SUM(monto),0) AS total
        FROM facturas_clientes
        WHERE estado <> 'anulada'
    """).fetchone()
    tickets = conn.execute("""
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN estado = 'pendiente' THEN 1 ELSE 0 END),0) AS pendientes,
               COALESCE(SUM(CASE WHEN estado IN ('aprobado','rechazado') THEN 1 ELSE 0 END),0) AS resueltos
        FROM tickets_recuperacion
    """).fetchone()
    tareas = conn.execute("""
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN estado = 'pendiente' THEN 1 ELSE 0 END),0) AS pendientes,
               COALESCE(SUM(CASE WHEN estado = 'en_progreso' THEN 1 ELSE 0 END),0) AS en_progreso,
               COALESCE(SUM(CASE WHEN estado = 'completada' THEN 1 ELSE 0 END),0) AS completadas,
               COALESCE(SUM(CASE WHEN estado = 'bloqueada' THEN 1 ELSE 0 END),0) AS bloqueadas
        FROM tareas
    """).fetchone()
    horas = conn.execute("""
        SELECT COALESCE(SUM((julianday(COALESCE(fin, datetime('now'))) - julianday(inicio)) * 24.0),0) AS horas
        FROM sesiones_trabajo
    """).fetchone()
    costo_total = conn.execute("""
        SELECT COALESCE(SUM(
            (julianday(COALESCE(s.fin,datetime('now')))-julianday(s.inicio))*24.0 *
            COALESCE((SELECT cp.costo_hora FROM costos_persona cp
                      WHERE cp.usuario_id=s.usuario_id AND cp.vigencia_desde <= date(s.inicio)
                        AND (cp.vigencia_hasta IS NULL OR cp.vigencia_hasta >= date(s.inicio))
                      ORDER BY cp.vigencia_desde DESC,cp.id DESC LIMIT 1),0)
        ),0) AS costo
        FROM sesiones_trabajo s
    """).fetchone()
    por_cliente = conn.execute("""
        SELECT u.id, u.nombre, u.ruc,
               COALESCE(SUM((julianday(COALESCE(s.fin, datetime('now'))) - julianday(s.inicio)) * 24.0),0) AS horas,
               COALESCE((SELECT SUM(f.monto) FROM facturas_clientes f
                         WHERE f.cliente_id = u.id AND f.estado <> 'anulada'),0) AS facturacion,
               COALESCE((SELECT COUNT(*) FROM tareas t WHERE t.cliente_id = u.id),0) AS tareas
        FROM usuarios u
        LEFT JOIN sesiones_trabajo s ON s.cliente_id = u.id
        WHERE u.rol = 'contribuyente'
        GROUP BY u.id, u.nombre, u.ruc
        ORDER BY horas DESC, u.nombre COLLATE NOCASE
    """).fetchall()
    ultimas_tareas = conn.execute("""
        SELECT t.*, c.nombre AS cliente_nombre, c.ruc AS cliente_ruc,
               a.nombre AS asignado_nombre
        FROM tareas t
        JOIN usuarios c ON c.id = t.cliente_id
        LEFT JOIN usuarios a ON a.id = t.asignado_id
        ORDER BY CASE t.prioridad
            WHEN 'urgente' THEN 1 WHEN 'alta' THEN 2 WHEN 'media' THEN 3 ELSE 4 END,
            CASE t.estado WHEN 'completada' THEN 3 WHEN 'cancelada' THEN 4 ELSE 1 END,
            t.creado_en DESC
        LIMIT 12
    """).fetchall()
    conn.close()
    return jsonify({
        "clientes": dict(clientes),
        "facturacion_total": round(float(facturacion["total"] or 0), 2),
        "costo_personal_total": round(float(costo_total["costo"] or 0), 2),
        "margen_estimado": round(float(facturacion["total"] or 0) - float(costo_total["costo"] or 0), 2),
        "horas_totales": round(float(horas["horas"] or 0), 2),
        "tickets": dict(tickets),
        "tareas": dict(tareas),
        "por_cliente": [dict(r) for r in por_cliente],
        "ultimas_tareas": [dict(r) for r in ultimas_tareas],
    })

@app.route("/api/admin/clientes", methods=["GET"])
@staff_required
def listar_clientes_operativos():
    conn = get_db()
    clientes = conn.execute("""
        SELECT id, ruc, correo, nombre, activo, creado_en
        FROM usuarios WHERE rol = 'contribuyente'
        ORDER BY activo DESC, nombre COLLATE NOCASE
    """).fetchall()
    conn.close()
    return jsonify([dict(c) for c in clientes])

@app.route("/api/admin/facturacion", methods=["GET"])
@admin_required
def listar_facturacion():
    conn = get_db()
    facturas = conn.execute("""
        SELECT f.*, c.nombre AS cliente_nombre, c.ruc AS cliente_ruc
        FROM facturas_clientes f
        JOIN usuarios c ON c.id = f.cliente_id
        ORDER BY f.fecha DESC, f.id DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(f) for f in facturas])

@app.route("/api/admin/facturacion", methods=["POST"])
@admin_required
def crear_factura_cliente():
    data = request.get_json() or {}
    try:
        cliente_id = int(data.get("cliente_id"))
        monto = float(data.get("monto"))
    except (TypeError, ValueError):
        return jsonify({"error": "Cliente y monto son obligatorios."}), 400
    numero = str(data.get("numero", "")).strip()
    concepto = str(data.get("concepto", "")).strip()
    fecha = str(data.get("fecha", "")).strip() or datetime.utcnow().strftime("%Y-%m-%d")
    estado = str(data.get("estado", "emitida")).strip().lower()
    articulo_raw = data.get("articulo_id")
    cantidad_raw = data.get("cantidad", 1)
    articulo_id = None
    try:
        cantidad = float(cantidad_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "Cantidad inválida."}), 400
    if articulo_raw not in (None, "", 0, "0"):
        try: articulo_id = int(articulo_raw)
        except (TypeError, ValueError): return jsonify({"error": "Artículo inválido."}), 400
    if not numero or not concepto or monto < 0 or cantidad <= 0 or estado not in ESTADOS_FACTURA:
        return jsonify({"error": "Datos de facturación inválidos."}), 400
    conn = get_db()
    if articulo_id is not None:
        articulo = conn.execute("SELECT * FROM articulos WHERE id = ? AND activo = 1", (articulo_id,)).fetchone()
        if not articulo:
            conn.close(); return jsonify({"error": "Artículo no encontrado o inactivo."}), 404
        tarifa = _tarifa_vigente(conn, articulo_id, fecha)
        if not tarifa:
            conn.close(); return jsonify({"error": "El artículo no tiene una tarifa vigente para esa fecha. Debés actualizar la tarifa antes de facturar."}), 409
        monto_esperado = round(float(tarifa["precio"]) * cantidad, 2)
        if abs(monto - monto_esperado) > 0.01:
            conn.close(); return jsonify({"error": "El monto no coincide con la tarifa vigente.", "monto_esperado": monto_esperado, "tarifa_id": tarifa["id"]}), 409
        tarifa_id = tarifa["id"]
    else:
        tarifa_id = None
    if not _cliente_valido(conn, cliente_id):
        conn.close()
        return jsonify({"error": "Cliente no encontrado."}), 404
    try:
        cur = conn.execute("""
            INSERT INTO facturas_clientes
            (cliente_id, numero, fecha, concepto, monto, estado, creado_por, articulo_id, tarifa_id, cantidad)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (cliente_id, numero, fecha, concepto, monto, estado, obtener_usuario_por_token()["id"], articulo_id, tarifa_id, cantidad))
        conn.commit()
        factura_id = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "No se pudo registrar la factura."}), 409
    conn.close()
    return jsonify({"ok": True, "id": factura_id})

@app.route("/api/admin/tareas", methods=["GET"])
@staff_required
def listar_tareas():
    u = obtener_usuario_por_token()
    conn = get_db()
    sql = """
        SELECT t.*, c.nombre AS cliente_nombre, c.ruc AS cliente_ruc,
               a.nombre AS asignado_nombre, a.rol AS asignado_rol
        FROM tareas t
        JOIN usuarios c ON c.id = t.cliente_id
        LEFT JOIN usuarios a ON a.id = t.asignado_id
    """
    params = []
    if u["rol"] == "operativo":
        sql += " WHERE t.asignado_id = ?"
        params.append(u["id"])
    sql += """ ORDER BY CASE t.estado WHEN 'completada' THEN 4 WHEN 'cancelada' THEN 5 ELSE 1 END,
               CASE t.prioridad WHEN 'urgente' THEN 1 WHEN 'alta' THEN 2 WHEN 'media' THEN 3 ELSE 4 END,
               t.fecha_limite IS NULL, t.fecha_limite, t.creado_en DESC"""
    tareas = conn.execute(sql, tuple(params)).fetchall()
    conn.close()
    return jsonify([dict(t) for t in tareas])

@app.route("/api/admin/tareas", methods=["POST"])
@admin_required
def crear_tarea():
    data = request.get_json() or {}
    try:
        cliente_id = int(data.get("cliente_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Seleccioná un cliente."}), 400
    titulo = str(data.get("titulo", "")).strip()
    descripcion = str(data.get("descripcion", "")).strip()
    prioridad = str(data.get("prioridad", "media")).strip().lower()
    fecha_limite = str(data.get("fecha_limite", "")).strip() or None
    asignado_raw = data.get("asignado_id")
    asignado_id = None
    if asignado_raw not in (None, "", 0, "0"):
        try:
            asignado_id = int(asignado_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "Responsable inválido."}), 400
    if not titulo or prioridad not in PRIORIDADES_TAREA:
        return jsonify({"error": "Título y prioridad son obligatorios."}), 400
    conn = get_db()
    actual = obtener_usuario_por_token()
    if not _cliente_valido(conn, cliente_id):
        conn.close()
        return jsonify({"error": "Cliente no encontrado."}), 404
    if asignado_id is not None:
        asignado = _usuario_trabajo_valido(conn, asignado_id)
        if not asignado or not puede_gestionar(actual["rol"], asignado["rol"]):
            conn.close()
            return jsonify({"error": "No tenés permisos para designar esa tarea."}), 403
    cur = conn.execute("""
        INSERT INTO tareas
        (cliente_id, titulo, descripcion, prioridad, asignado_id, creado_por, fecha_limite)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (cliente_id, titulo, descripcion, prioridad, asignado_id, actual["id"], fecha_limite))
    conn.commit()
    tarea_id = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": tarea_id})

@app.route("/api/admin/tareas/<int:tarea_id>", methods=["PATCH"])
@admin_required
def actualizar_tarea(tarea_id):
    data = request.get_json() or {}
    conn = get_db()
    actual = obtener_usuario_por_token()
    tarea = conn.execute("SELECT * FROM tareas WHERE id = ?", (tarea_id,)).fetchone()
    if not tarea:
        conn.close()
        return jsonify({"error": "Tarea no encontrada."}), 404
    cambios = []
    valores = []
    if "prioridad" in data:
        prioridad = str(data["prioridad"]).strip().lower()
        if prioridad not in PRIORIDADES_TAREA:
            conn.close()
            return jsonify({"error": "Prioridad inválida."}), 400
        cambios.append("prioridad = ?"); valores.append(prioridad)
    if "estado" in data:
        estado = str(data["estado"]).strip().lower()
        if estado not in ESTADOS_TAREA:
            conn.close()
            return jsonify({"error": "Estado inválido."}), 400
        cambios.append("estado = ?"); valores.append(estado)
        if estado == "en_progreso" and not tarea["iniciado_en"]:
            cambios.append("iniciado_en = ?"); valores.append(datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        if estado == "completada":
            cambios.append("completado_en = ?"); valores.append(datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
            _detener_tarea_si_corresponde(conn, tarea_id)
    if "asignado_id" in data:
        asignado_id = data["asignado_id"]
        if asignado_id in (None, "", 0, "0"):
            asignado_id = None
        else:
            try: asignado_id = int(asignado_id)
            except (TypeError, ValueError):
                conn.close()
                return jsonify({"error": "Responsable inválido."}), 400
            asignado = _usuario_trabajo_valido(conn, asignado_id)
            if not asignado or not puede_gestionar(actual["rol"], asignado["rol"]):
                conn.close()
                return jsonify({"error": "No tenés permisos para designar esa tarea."}), 403
        cambios.append("asignado_id = ?"); valores.append(asignado_id)
    if "fecha_limite" in data:
        cambios.append("fecha_limite = ?"); valores.append(str(data["fecha_limite"]).strip() or None)
    if "titulo" in data:
        titulo = str(data["titulo"]).strip()
        if not titulo:
            conn.close(); return jsonify({"error": "El título no puede quedar vacío."}), 400
        cambios.append("titulo = ?"); valores.append(titulo)
    if "descripcion" in data:
        cambios.append("descripcion = ?"); valores.append(str(data["descripcion"]).strip())
    if not cambios:
        conn.close()
        return jsonify({"ok": True})
    valores.append(tarea_id)
    conn.execute("UPDATE tareas SET " + ", ".join(cambios) + " WHERE id = ?", tuple(valores))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/admin/tareas/<int:tarea_id>/iniciar", methods=["POST"])
@staff_required
def iniciar_tarea(tarea_id):
    u = obtener_usuario_por_token()
    conn = get_db()
    tarea = conn.execute("SELECT * FROM tareas WHERE id = ?", (tarea_id,)).fetchone()
    if not tarea:
        conn.close(); return jsonify({"error": "Tarea no encontrada."}), 404
    if u["rol"] == "operativo" and tarea["asignado_id"] != u["id"]:
        conn.close(); return jsonify({"error": "Esta tarea no está asignada a vos."}), 403
    if tarea["estado"] in ("completada", "cancelada"):
        conn.close(); return jsonify({"error": "La tarea ya no puede iniciarse."}), 400
    _cerrar_sesion_activa(conn, u["id"])
    ahora = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE tareas SET estado = 'en_progreso', iniciado_en = COALESCE(iniciado_en, ?) WHERE id = ?", (ahora, tarea_id))
    conn.execute("""
        INSERT INTO sesiones_trabajo (tarea_id, cliente_id, usuario_id, inicio)
        VALUES (?, ?, ?, ?)
    """, (tarea_id, tarea["cliente_id"], u["id"], ahora))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "inicio": ahora, "tarea_id": tarea_id})

@app.route("/api/admin/tareas/<int:tarea_id>/detener", methods=["POST"])
@staff_required
def detener_tarea(tarea_id):
    u = obtener_usuario_por_token()
    conn = get_db()
    tarea = conn.execute("SELECT * FROM tareas WHERE id = ?", (tarea_id,)).fetchone()
    if not tarea:
        conn.close(); return jsonify({"error": "Tarea no encontrada."}), 404
    if u["rol"] == "operativo" and tarea["asignado_id"] != u["id"]:
        conn.close(); return jsonify({"error": "Esta tarea no está asignada a vos."}), 403
    sesion = conn.execute("""
        SELECT * FROM sesiones_trabajo
        WHERE tarea_id = ? AND usuario_id = ? AND fin IS NULL
        ORDER BY id DESC LIMIT 1
    """, (tarea_id, u["id"])).fetchone()
    if not sesion:
        conn.close(); return jsonify({"error": "No hay un cronómetro activo para esta tarea."}), 400
    fin = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE sesiones_trabajo SET fin = ? WHERE id = ?", (fin, sesion["id"]))
    conn.execute("UPDATE tareas SET estado = CASE WHEN estado = 'en_progreso' THEN 'pendiente' ELSE estado END WHERE id = ?", (tarea_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "fin": fin})

@app.route("/api/admin/tiempo/activo", methods=["GET"])
@staff_required
def tiempo_activo():
    u = obtener_usuario_por_token()
    conn = get_db()
    sesion = conn.execute("""
        SELECT s.*, t.titulo, c.nombre AS cliente_nombre
        FROM sesiones_trabajo s
        JOIN tareas t ON t.id = s.tarea_id
        JOIN usuarios c ON c.id = s.cliente_id
        WHERE s.usuario_id = ? AND s.fin IS NULL
        ORDER BY s.id DESC LIMIT 1
    """, (u["id"],)).fetchone()
    conn.close()
    return jsonify(dict(sesion) if sesion else None)

@app.route("/api/admin/tiempo/resumen", methods=["GET"])
@admin_required
def resumen_tiempo():
    conn = get_db()
    filas = conn.execute("""
        SELECT u.id, u.nombre, u.ruc,
               COALESCE(SUM((julianday(COALESCE(s.fin, datetime('now'))) - julianday(s.inicio)) * 24.0),0) AS horas
        FROM usuarios u
        LEFT JOIN sesiones_trabajo s ON s.cliente_id = u.id
        WHERE u.rol = 'contribuyente'
        GROUP BY u.id, u.nombre, u.ruc
        ORDER BY horas DESC, u.nombre COLLATE NOCASE
    """).fetchall()
    conn.close()
    return jsonify([dict(f) for f in filas])

@app.route("/api/admin/tiempo/<int:cliente_id>", methods=["GET"])
@admin_required
def detalle_tiempo_cliente(cliente_id):
    conn = get_db()
    if not _cliente_valido(conn, cliente_id):
        conn.close(); return jsonify({"error": "Cliente no encontrado."}), 404
    sesiones = conn.execute("""
        SELECT s.*, t.titulo, u.nombre AS usuario_nombre
        FROM sesiones_trabajo s
        JOIN tareas t ON t.id = s.tarea_id
        JOIN usuarios u ON u.id = s.usuario_id
        WHERE s.cliente_id = ?
        ORDER BY s.inicio DESC
    """, (cliente_id,)).fetchall()
    conn.close()
    return jsonify([dict(s) for s in sesiones])



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))