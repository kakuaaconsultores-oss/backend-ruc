import os
import sqlite3
import time
import smtplib
import secrets
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from functools import wraps
import bcrypt
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "usuarios.db")
DOCS_DIR = os.path.join(BASE_DIR, "documentos")

MAX_INTENTOS = 5
BLOQUEO_MINUTOS = 30
OTP_MINUTOS = 1
MAX_REGENERACIONES_OTP = 5
SESION_HORAS = 8
MAX_UPLOAD_MB = 16
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp", "doc", "docx", "xls", "xlsx", "csv"}
MAX_CONTENT_BYTES = MAX_UPLOAD_MB * 1024 * 1024

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_BYTES
CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS",
    "https://kakuaaconsultores-oss.github.io,http://localhost:5500,http://127.0.0.1:5500"
).split(",") if o.strip()]
CORS(app, resources={r"/api/*": {"origins": CORS_ORIGINS}}, supports_credentials=False, expose_headers=["Content-Disposition"])

# Configuración SMTP (se lee de variables de entorno de Render)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)

# ---------- Base de datos ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
def init_db():
    conn = get_db()
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
        ("token_expira_en", "TEXT DEFAULT NULL")
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
    legacy = conn.execute("SELECT id FROM usuarios WHERE ruc = '80000000-0'").fetchone()
    superadmin = conn.execute("SELECT id FROM usuarios WHERE rol = 'superadmin' LIMIT 1").fetchone()
    if superadmin:
        conn.execute("UPDATE usuarios SET rol = 'contribuyente' WHERE rol = 'superadmin' AND id != ?", (superadmin["id"],))
    elif legacy:
        superadmin_usuario = os.environ.get("SUPERADMIN_USUARIO", "superadmin").strip() or "superadmin"
        conn.execute("UPDATE usuarios SET rol = 'superadmin', usuario = ? WHERE id = ?", (superadmin_usuario, legacy["id"]))
    else:
        usuario = os.environ.get("SUPERADMIN_USUARIO", "superadmin").strip() or "superadmin"
        correo = os.environ.get("SUPERADMIN_EMAIL", "kakuaaconsultores@gmail.com").strip() or "kakuaaconsultores@gmail.com"
        password = os.environ.get("SUPERADMIN_PASSWORD", "").strip()
        if not password:
            password = secrets.token_urlsafe(12)
            print("[SEGURIDAD] SUPERADMIN_PASSWORD no configurada; se generó una contraseña temporal.")
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        conn.execute(
            "INSERT INTO usuarios (ruc, correo, nombre, password_hash, usuario, rol, debe_cambiar) VALUES (?, ?, ?, ?, ?, 'superadmin', 1)",
            ("80000000-0", correo, "SUPERADMIN", hashed, usuario)
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
    """Obtiene el usuario autenticado y valida la expiración de su sesión."""
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip()
    if not token:
        return None
    conn = get_db()
    u = conn.execute("SELECT * FROM usuarios WHERE token_sesion = ?", (token,)).fetchone()
    if not u or not u["activo"]:
        conn.close()
        return None
    if u["token_expira_en"]:
        try:
            if datetime.utcnow() >= datetime.fromisoformat(u["token_expira_en"]):
                conn.execute("UPDATE usuarios SET token_sesion = NULL, token_expira_en = NULL WHERE id = ?", (u["id"],))
                conn.commit()
                conn.close()
                return None
        except ValueError:
            conn.execute("UPDATE usuarios SET token_sesion = NULL, token_expira_en = NULL WHERE id = ?", (u["id"],))
            conn.commit()
            conn.close()
            return None
    conn.close()
    return u

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
    <p>Hola <strong>{usuario['nombre']}</strong>.</p>
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

# ---------- RUTAS ----------

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})

# Login con límite de intentos y token de sesión
@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    usuario_login = data.get("usuario", "").strip()
    password = data.get("password", "")
    if not usuario_login or not password:
        return jsonify({"error": "Ingresá tu usuario y contraseña"}), 400
    conn = get_db()
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
    data = request.get_json() or {}
    challenge = data.get("challenge", "").strip(); otp = data.get("otp", "").strip()
    if not challenge or len(otp) != 4 or not otp.isdigit(): return jsonify({"error": "Ingresá el código de 4 dígitos."}), 400
    conn = get_db()
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
    conn.execute("DELETE FROM login_otp WHERE id = ?", (row["id"],))
    token_expira = datetime.utcnow() + timedelta(hours=SESION_HORAS)
    conn.execute("UPDATE usuarios SET token_sesion = ?, token_expira_en = ?, intentos_fallidos = 0, bloqueo_hasta = NULL WHERE id = ?", (token, token_expira.isoformat(), row["usuario_id"]))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "token": token, "debe_cambiar": bool(row["debe_cambiar"]), "usuario": {"id": row["usuario_id"], "usuario": row["usuario"], "ruc": row["ruc"], "nombre": row["nombre"], "correo": row["correo"], "rol": row["rol"]}})

@app.route("/api/login/resend-otp", methods=["POST"])
def reenviar_otp():
    data = request.get_json() or {}; challenge = data.get("challenge", "").strip()
    if not challenge: return jsonify({"error": "Desafío inválido"}), 400
    conn = get_db(); row = conn.execute("SELECT u.* FROM login_otp o JOIN usuarios u ON u.id = o.usuario_id WHERE o.challenge_token = ?", (challenge,)).fetchone()
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
def cambiar_password():
    u = obtener_usuario_por_token()
    data = request.get_json() or {}
    nueva_password = data.get("nueva_password", "")

    error = validar_politica_password(nueva_password)
    if error:
        return jsonify({"error": error}), 400

    nuevo_hash = hash_password(nueva_password)
    conn = get_db()
    conn.execute("UPDATE usuarios SET password_hash = ?, debe_cambiar = 0, token_sesion = NULL, token_expira_en = NULL WHERE id = ?", (nuevo_hash, u["id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "Contraseña actualizada correctamente. Volvé a iniciar sesión."})

# Logout (invalida el token)
@app.route("/api/logout", methods=["POST"])
def logout():
    u = obtener_usuario_por_token()
    if u:
        conn = get_db()
        conn.execute("UPDATE usuarios SET token_sesion = NULL, token_expira_en = NULL WHERE id = ?", (u["id"],))
        conn.commit()
        conn.close()
    return jsonify({"ok": True})

# Solicitar reset (crea ticket)
@app.route("/api/solicitar-reset", methods=["POST"])
def solicitar_reset():
    data = request.get_json() or {}
    ruc = data.get("ruc", "").strip()
    if not ruc:
        return jsonify({"error": "Ingresá tu RUC"}), 400
    conn = get_db()
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

# Admin: aprobar ticket (cambia contraseña, fuerza cambio, invalida token viejo y envía correo)
@app.route("/api/admin/tickets/<int:ticket_id>/aprobar", methods=["POST"])
@admin_required
def aprobar_ticket(ticket_id):
    data = request.get_json() or {}
    nueva_password = data.get("nueva_password", "")
    if not nueva_password or len(nueva_password) < 6:
        return jsonify({"error": "La contraseña debe tener al menos 6 caracteres"}), 400
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
    hashed = hash_password(nueva_password)
    conn.execute("UPDATE usuarios SET password_hash = ?, intentos_fallidos = 0, bloqueo_hasta = NULL, token_sesion = NULL, token_expira_en = NULL, debe_cambiar = 1 WHERE id = ?", (hashed, t["usuario_id"]))
    conn.execute("UPDATE tickets_recuperacion SET estado = 'aprobado', nueva_password = NULL, resuelto_en = datetime('now') WHERE id = ?", (ticket_id,))
    conn.commit()
    u = conn.execute("SELECT * FROM usuarios WHERE id = ?", (t["usuario_id"],)).fetchone()
    conn.close()
    if u and u["correo"]:
        cuerpo = f"""
        <h2>Kakuaa Consultores</h2>
        <p>Hola <strong>{u['nombre']}</strong>,</p>
        <p>Tu contraseña fue restablecida por el administrador.</p>
        <p><strong>Tu nueva contraseña es:</strong> <code>{nueva_password}</code></p>
        <p>Al ingresar, el sistema te pedirá que la cambies por una nueva.</p>
        <p>Saludos,<br>Equipo Kakuaa Consultores</p>
        """
        enviar_correo(u["correo"], "Tu contraseña fue restablecida", cuerpo)
    return jsonify({"ok": True, "message": "Contraseña actualizada y correo enviado"})

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
@admin_required
def listar_usuarios():
    conn = get_db()
    usuarios = conn.execute("SELECT id, usuario, ruc, correo, nombre, activo, rol, debe_cambiar FROM usuarios ORDER BY CASE rol WHEN 'superadmin' THEN 0 WHEN 'admin' THEN 1 WHEN 'operativo' THEN 2 ELSE 3 END, id").fetchall()
    conn.close()
    return jsonify([dict(u) for u in usuarios])

# Admin: crear usuario (marca debe_cambiar para forzar cambio en primer ingreso)
@app.route("/api/admin/usuarios", methods=["POST"])
@admin_required
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
@admin_required
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

# Admin: resetear contraseña manual (fuerza cambio, invalida token viejo)
@app.route("/api/admin/usuarios/<int:usuario_id>/reset-password", methods=["POST"])
@admin_required
def resetear_password(usuario_id):
    data = request.get_json() or {}
    nueva_password = data.get("nueva_password", "")
    if not nueva_password or len(nueva_password) < 6:
        return jsonify({"error": "La contraseña debe tener al menos 6 caracteres"}), 400
    conn = get_db()
    u = conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if not u:
        conn.close()
        return jsonify({"error": "Usuario no encontrado"}), 404
    u_actual = obtener_usuario_por_token()
    if u["rol"] == "superadmin" or not puede_gestionar(u_actual["rol"], u["rol"]):
        conn.close()
        return jsonify({"error": "No tenés permisos para resetear este usuario."}), 403
    hashed = hash_password(nueva_password)
    conn.execute("UPDATE usuarios SET password_hash = ?, intentos_fallidos = 0, bloqueo_hasta = NULL, token_sesion = NULL, token_expira_en = NULL, debe_cambiar = 1 WHERE id = ?", (hashed, usuario_id))
    conn.commit()
    conn.close()
    if u["correo"]:
        cuerpo = f"""
        <h2>Kakuaa Consultores</h2>
        <p>Hola <strong>{u['nombre']}</strong>,</p>
        <p>Tu contraseña fue restablecida por el administrador.</p>
        <p><strong>Tu nueva contraseña es:</strong> <code>{nueva_password}</code></p>
        <p>Al ingresar, el sistema te pedirá que la cambies por una nueva.</p>
        <p>Saludos,<br>Equipo Kakuaa Consultores</p>
        """
        enviar_correo(u["correo"], "Tu contraseña fue restablecida", cuerpo)
    return jsonify({"ok": True, "message": "Contraseña actualizada y correo enviado"})

# Admin: cambiar estado (habilitar/deshabilitar, invalida token si deshabilitas)
@app.route("/api/admin/usuarios/<int:usuario_id>/estado", methods=["PUT"])
@admin_required
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
    conn.execute("UPDATE usuarios SET activo = ?, token_sesion = CASE WHEN ? = 0 THEN NULL ELSE token_sesion END, token_expira_en = CASE WHEN ? = 0 THEN NULL ELSE token_expira_en END WHERE id = ?", (1 if activo else 0, 1 if activo else 0, 1 if activo else 0, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# Admin: asignar rol
@app.route("/api/admin/usuarios/<int:usuario_id>/rol", methods=["PUT"])
@admin_required
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
    conn.execute("UPDATE usuarios SET rol = ? WHERE id = ?", (nuevo_rol, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "Rol actualizado"})

@app.route("/api/admin/usuarios/<int:usuario_id>/documentos", methods=["GET"])
@admin_required
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
@admin_required
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
    dir_usuario = os.path.join(DOCS_DIR, str(usuario_id))
    dir_carpeta = os.path.join(dir_usuario, carpeta)
    if subcarpeta:
        dir_carpeta = os.path.join(dir_carpeta, subcarpeta)
    if subcarpeta2:
        dir_carpeta = os.path.join(dir_carpeta, subcarpeta2)
    os.makedirs(dir_carpeta, exist_ok=True)
    ruta = os.path.join(dir_carpeta, nombre_archivo)
    archivo.save(ruta)
    conn = get_db()
    cur = conn.execute("INSERT INTO documentos (usuario_id, nombre_archivo, ruta, carpeta, subcarpeta, subcarpeta2) VALUES (?, ?, ?, ?, ?, ?)",
                       (usuario_id, nombre_archivo, ruta, carpeta, subcarpeta, subcarpeta2))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": cur.lastrowid}), 201

# Admin: eliminar documento
@app.route("/api/admin/documentos/<int:doc_id>", methods=["DELETE"])
@admin_required
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
    if os.path.exists(d["ruta"]):
        os.remove(d["ruta"])
    conn.execute("DELETE FROM documentos WHERE id = ?", (doc_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# Admin: subcarpetas de un usuario
@app.route("/api/admin/usuarios/<int:usuario_id>/subcarpetas", methods=["GET"])
@admin_required
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
@admin_required
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
@admin_required
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
    return send_from_directory(os.path.dirname(d["ruta"]), os.path.basename(d["ruta"]), as_attachment=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))