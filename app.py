import os
import sqlite3
import time
import smtplib
import secrets
import hashlib
import html
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
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "usuarios.db"))
DOCS_DIR = os.environ.get("DOCS_DIR", os.path.join(BASE_DIR, "documentos"))

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
    "superadmin_diagnostic": 5,
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

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})

@app.route("/api/superadmin/bootstrap-diagnostic", methods=["POST"])
def superadmin_bootstrap_diagnostic():
    """
    Diagnóstico temporal y no destructivo del estado de la cuenta SUPERADMIN.
    Requiere el mismo secreto de bootstrap, pero nunca devuelve ni registra
    secretos, contraseñas, hashes ni otros datos sensibles.
    """
    conn = get_db()
    if rate_limit_exceeded(conn, "superadmin_diagnostic"):
        conn.close()
        return rate_limit_response()

    bootstrap_secret = os.environ.get("SUPERADMIN_BOOTSTRAP_SECRET", "").strip()
    if not bootstrap_secret or len(bootstrap_secret) < 32:
        conn.close()
        return jsonify({"error": "Diagnóstico no habilitado."}), 503

    data = request.get_json(silent=True) or {}
    provided_secret = str(data.get("bootstrap_secret", "")).strip()
    if not provided_secret or not secrets.compare_digest(
        provided_secret.encode("utf-8"), bootstrap_secret.encode("utf-8")
    ):
        conn.close()
        return jsonify({"error": "Credencial inválida."}), 403

    sa = conn.execute(
        "SELECT usuario, rol, activo FROM usuarios WHERE rol = 'superadmin' LIMIT 1"
    ).fetchone()
    estado = conn.execute(
        "SELECT usado FROM superadmin_bootstrap WHERE id = 1"
    ).fetchone()
    conn.close()

    return jsonify({
        "ok": True,
        "superadmin_existe": bool(sa),
        "usuario": sa["usuario"] if sa else None,
        "rol": sa["rol"] if sa else None,
        "activo": bool(sa["activo"]) if sa else None,
        "bootstrap_usado": bool(estado["usado"]) if estado else None,
    })

# Login con límite de intentos y token de sesión
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

        if superadmin:
            usuario = superadmin["usuario"]
            usuario_id = superadmin["id"]
            correo = superadmin["correo"]
            conn.execute(
                """UPDATE usuarios
                   SET password_hash = ?, activo = 1, debe_cambiar = 0,
                       intentos_fallidos = 0, bloqueo_hasta = NULL,
                       token_sesion = NULL, token_sesion_hash = NULL,
                       csrf_token_hash = NULL, token_expira_en = NULL,
                       reset_token_hash = NULL, reset_expira_en = NULL
                   WHERE id = ?""",
                (hash_password(nueva_password), usuario_id),
            )
            accion = "password_reset"
        else:
            usuario = os.environ.get("SUPERADMIN_USUARIO", "superadmin").strip() or "superadmin"
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
    challenge, otp, _ = crear_desafio_otp(conn, u, 1)
    enviado = enviar_otp(u, otp); conn.close()
    if not enviado: return jsonify({"error": "No se pudo enviar el código de acceso. Intentá nuevamente."}), 503
    return jsonify({"ok": True, "requiere_otp": True, "challenge": challenge, "usuario": {"id": u["id"], "usuario": u["usuario"], "nombre": u["nombre"], "correo": u["correo"], "rol": u["rol"]}})

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
    new_challenge, otp, total_generaciones = crear_desafio_otp(conn, row, generaciones + 1)
    enviado = enviar_otp(row, otp)
    conn.close()
    if not enviado:
        return jsonify({"error": "No se pudo enviar el nuevo código."}), 503
    restantes = max(0, MAX_REGENERACIONES_OTP - (total_generaciones - 1))
    return jsonify({"ok": True, "challenge": new_challenge, "regeneraciones_restantes": restantes, "message": "Se envió un nuevo código. El anterior quedó invalidado."})
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))