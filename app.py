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

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=False, expose_headers=["Content-Disposition"])

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "usuarios.db")
DOCS_DIR = os.path.join(BASE_DIR, "documentos")

# Configuración SMTP (se lee de variables de entorno de Render)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)

MAX_INTENTOS = 5
BLOQUEO_MINUTOS = 30

# ---------- Base de datos ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ruc TEXT UNIQUE NOT NULL,
            correo TEXT UNIQUE NOT NULL,
            nombre TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            activo INTEGER DEFAULT 1,
            intentos_fallidos INTEGER DEFAULT 0,
            bloqueo_hasta TEXT DEFAULT NULL,
            token_sesion TEXT DEFAULT NULL,
            debe_cambiar INTEGER DEFAULT 0,
            creado_en TEXT DEFAULT (datetime('now'))
        )
    """)
    # Agrega la columna debe_cambiar si la base ya existía sin ella
    try:
        conn.execute("ALTER TABLE usuarios ADD COLUMN debe_cambiar INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # Ya existe la columna
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL,
            nombre_archivo TEXT NOT NULL,
            ruta TEXT NOT NULL,
            carpeta TEXT NOT NULL,
            subcarpeta TEXT DEFAULT '',
            subcarpeta2 TEXT DEFAULT '',
            subido_en TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subcarpetas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL,
            carpeta TEXT NOT NULL,
            nombre TEXT NOT NULL,
            padre TEXT DEFAULT '',
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tickets_recuperacion (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL,
            ruc TEXT NOT NULL,
            estado TEXT DEFAULT 'pendiente',
            nueva_password TEXT DEFAULT NULL,
            creado_en TEXT DEFAULT (datetime('now')),
            resuelto_en TEXT DEFAULT NULL,
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
        )
    """)
    # Admin inicial
    cur = conn.execute("SELECT COUNT(*) AS c FROM usuarios WHERE ruc = '80000000-0'")
    if cur.fetchone()["c"] == 0:
        hashed = bcrypt.hashpw("admin123".encode(), bcrypt.gensalt()).decode()
        conn.execute("INSERT INTO usuarios (ruc, correo, nombre, password_hash) VALUES (?, ?, ?, ?)",
                     ("80000000-0", "admin@kakuaa.com", "Administrador", hashed))
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
    """Obtiene el usuario autenticado desde el header Authorization (token de sesión)."""
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip()
    if not token:
        return None
    conn = get_db()
    u = conn.execute("SELECT * FROM usuarios WHERE token_sesion = ?", (token,)).fetchone()
    conn.close()
    if not u or not u["activo"]:
        return None
    return u

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = obtener_usuario_por_token()
        if not u or u["ruc"] != "80000000-0":
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

# ---------- RUTAS ----------

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})

# Login con límite de intentos y token de sesión
@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    ruc = data.get("ruc", "").strip()
    password = data.get("password", "")
    conn = get_db()
    u = conn.execute("SELECT * FROM usuarios WHERE ruc = ?", (ruc,)).fetchone()
    if not u:
        return jsonify({"error": "Usuario o contraseña incorrectos"}), 401
    if not u["activo"]:
        return jsonify({"error": "Usuario deshabilitado"}), 403
    if u["bloqueo_hasta"]:
        bloqueo = datetime.fromisoformat(u["bloqueo_hasta"])
        if datetime.utcnow() < bloqueo:
            restante = (bloqueo - datetime.utcnow()).seconds // 60
            return jsonify({"error": f"Demasiados intentos. Esperá {restante} minutos o usá '¿Olvidó su contraseña?'", "bloqueado": True}), 429
        else:
            conn.execute("UPDATE usuarios SET intentos_fallidos = 0, bloqueo_hasta = NULL WHERE id = ?", (u["id"],))
            conn.commit()
    if check_password(password, u["password_hash"]):
        token = generar_token()
        conn.execute("UPDATE usuarios SET intentos_fallidos = 0, bloqueo_hasta = NULL, token_sesion = ? WHERE id = ?", (token, u["id"]))
        conn.commit()
        conn.close()
        # Devuelve debe_cambiar para que el frontend redirija si es necesario
        return jsonify({
            "ok": True,
            "token": token,
            "debe_cambiar": bool(u["debe_cambiar"]),
            "usuario": {"id": u["id"], "ruc": u["ruc"], "nombre": u["nombre"], "correo": u["correo"]}
        })
    else:
        intentos = u["intentos_fallidos"] + 1
        if intentos >= MAX_INTENTOS:
            bloqueo_hasta = (datetime.utcnow() + timedelta(minutes=BLOQUEO_MINUTOS)).isoformat()
            conn.execute("UPDATE usuarios SET intentos_fallidos = ?, bloqueo_hasta = ? WHERE id = ?", (0, bloqueo_hasta, u["id"]))
            conn.commit()
            conn.close()
            return jsonify({"error": f"Demasiados intentos. Esperá {BLOQUEO_MINUTOS} minutos o usá '¿Olvidó su contraseña?'", "bloqueado": True}), 429
        else:
            conn.execute("UPDATE usuarios SET intentos_fallidos = ? WHERE id = ?", (intentos, u["id"]))
            conn.commit()
            conn.close()
            return jsonify({"error": "Usuario o contraseña incorrectos", "intentos_restantes": MAX_INTENTOS - intentos}), 401

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
    conn.execute("UPDATE usuarios SET password_hash = ?, debe_cambiar = 0, token_sesion = NULL WHERE id = ?", (nuevo_hash, u["id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "Contraseña actualizada correctamente. Volvé a iniciar sesión."})

# Logout (invalida el token)
@app.route("/api/logout", methods=["POST"])
def logout():
    u = obtener_usuario_por_token()
    if u:
        conn = get_db()
        conn.execute("UPDATE usuarios SET token_sesion = NULL WHERE id = ?", (u["id"],))
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
    if not u:
        return jsonify({"error": "El RUC no existe en el sistema"}), 404
    pendiente = conn.execute("SELECT * FROM tickets_recuperacion WHERE usuario_id = ? AND estado = 'pendiente'", (u["id"],)).fetchone()
    if pendiente:
        conn.close()
        return jsonify({"error": "Ya tenés un pedido de reset pendiente. Esperá a que el administrador lo apruebe."}), 409
    conn.execute("INSERT INTO tickets_recuperacion (usuario_id, ruc) VALUES (?, ?)", (u["id"], ruc))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "Solicitud enviada. El administrador la revisará."})

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
    hashed = hash_password(nueva_password)
    conn.execute("UPDATE usuarios SET password_hash = ?, intentos_fallidos = 0, bloqueo_hasta = NULL, token_sesion = NULL, debe_cambiar = 1 WHERE id = ?", (hashed, t["usuario_id"]))
    conn.execute("UPDATE tickets_recuperacion SET estado = 'aprobado', nueva_password = ?, resuelto_en = datetime('now') WHERE id = ?", (nueva_password, ticket_id))
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
    conn.execute("UPDATE tickets_recuperacion SET estado = 'rechazado', resuelto_en = datetime('now') WHERE id = ?", (ticket_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "Ticket rechazado"})

# Admin: listar usuarios
@app.route("/api/admin/usuarios", methods=["GET"])
@admin_required
def listar_usuarios():
    conn = get_db()
    usuarios = conn.execute("SELECT id, ruc, correo, nombre, activo FROM usuarios ORDER BY id").fetchall()
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
    contrasena = data.get("contrasena", "")
    if not ruc or not correo or not nombre or not contrasena:
        return jsonify({"error": "Faltan datos"}), 400
    conn = get_db()
    try:
        hashed = hash_password(contrasena)
        cur = conn.execute("INSERT INTO usuarios (ruc, correo, nombre, password_hash, debe_cambiar) VALUES (?, ?, ?, ?, 1)",
                           (ruc, correo, nombre, hashed))
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
    if u["ruc"] == "80000000-0":
        conn.close()
        return jsonify({"error": "No podés editar al administrador principal"}), 403
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
    if u["ruc"] == "80000000-0":
        conn.close()
        return jsonify({"error": "No podés resetear la contraseña del administrador principal"}), 403
    hashed = hash_password(nueva_password)
    conn.execute("UPDATE usuarios SET password_hash = ?, intentos_fallidos = 0, bloqueo_hasta = NULL, token_sesion = NULL, debe_cambiar = 1 WHERE id = ?", (hashed, usuario_id))
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
    activo = data.get("activo")
    conn = get_db()
    u = conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if not u:
        conn.close()
        return jsonify({"error": "Usuario no encontrado"}), 404
    if u["ruc"] == "80000000-0":
        conn.close()
        return jsonify({"error": "No podés deshabilitar al administrador principal"}), 403
    if not activo:
        conn.execute("UPDATE usuarios SET activo = 0, token_sesion = NULL WHERE id = ?", (usuario_id,))
    else:
        conn.execute("UPDATE usuarios SET activo = 1 WHERE id = ?", (usuario_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# Admin: listar documentos de un usuario
@app.route("/api/admin/usuarios/<int:usuario_id>/documentos", methods=["GET"])
@admin_required
def admin_documentos(usuario_id):
    conn = get_db()
    docs = conn.execute("SELECT * FROM documentos WHERE usuario_id = ? ORDER BY subido_en DESC", (usuario_id,)).fetchall()
    conn.close()
    return jsonify([dict(d) for d in docs])

# Admin: subir documento
@app.route("/api/admin/usuarios/<int:usuario_id>/documentos", methods=["POST"])
@admin_required
def admin_subir_documento(usuario_id):
    archivo = request.files.get("archivo")
    carpeta = request.form.get("carpeta", "")
    subcarpeta = request.form.get("subcarpeta", "")
    subcarpeta2 = request.form.get("subcarpeta2", "")
    if not archivo or not carpeta:
        return jsonify({"error": "Faltan datos"}), 400
    dir_usuario = os.path.join(DOCS_DIR, str(usuario_id))
    dir_carpeta = os.path.join(dir_usuario, carpeta)
    if subcarpeta:
        dir_carpeta = os.path.join(dir_carpeta, subcarpeta)
    if subcarpeta2:
        dir_carpeta = os.path.join(dir_carpeta, subcarpeta2)
    os.makedirs(dir_carpeta, exist_ok=True)
    nombre_archivo = archivo.filename
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
    conn = get_db()
    d = conn.execute("SELECT * FROM documentos WHERE id = ?", (doc_id,)).fetchone()
    if not d:
        conn.close()
        return jsonify({"error": "Documento no encontrado"}), 404
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
    conn = get_db()
    subs = conn.execute("SELECT * FROM subcarpetas WHERE usuario_id = ?", (usuario_id,)).fetchall()
    conn.close()
    return jsonify([dict(s) for s in subs])

# Admin: crear subcarpeta
@app.route("/api/admin/usuarios/<int:usuario_id>/subcarpetas", methods=["POST"])
@admin_required
def admin_crear_subcarpeta(usuario_id):
    data = request.get_json() or {}
    carpeta = data.get("carpeta", "")
    nombre = data.get("nombre", "").strip()
    padre = data.get("padre", "")
    if not carpeta or not nombre:
        return jsonify({"error": "Faltan datos"}), 400
    conn = get_db()
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
    conn = get_db()
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
