import os
import sqlite3
from datetime import datetime
from functools import wraps
import bcrypt
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
app = Flask(__name__)
from flask_cors import CORS, cross_origin
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=False, expose_headers=["Content-Disposition"])
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "usuarios.db")
DOCS_DIR = os.path.join(BASE_DIR, "documentos")
os.makedirs(DOCS_DIR, exist_ok=True)
# ---- Base de datos ----
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
            contrasena TEXT NOT NULL,
            es_admin INTEGER DEFAULT 0,
            activo INTEGER DEFAULT 1,
            creado_en TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL,
            nombre_archivo TEXT NOT NULL,
            tipo TEXT,
            subido_en TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (usuario_id) REFERENCES usuarios (id)
        )
    """)
    # Crear admin por defecto (RUC: 80000000-0, pass: admin123)
    admin_existe = conn.execute("SELECT id FROM usuarios WHERE es_admin=1").fetchone()
    if not admin_existe:
        hashed = bcrypt.hashpw("admin123".encode(), bcrypt.gensalt())
        conn.execute("INSERT INTO usuarios (ruc, correo, nombre, contrasena, es_admin) VALUES (?,?,?,?,1)",
                     ("80000000-0", "admin@kakuaa.com", "Administrador", hashed))
    conn.commit()
    conn.close()

init_db()

# ---- Utilidades ----
def crear_carpeta_usuario(usuario_id):
    carpeta = os.path.join(DOCS_DIR, str(usuario_id))
    os.makedirs(carpeta, exist_ok=True)
    return carpeta

def requiere_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "No autorizado"}), 401
        token = auth.replace("Bearer ", "")
        # Token simple: admin usa su RUC como token (demo)
        admin = get_db().execute("SELECT * FROM usuarios WHERE ruc=? AND es_admin=1", (token,)).fetchone()
        if not admin:
            return jsonify({"error": "No autorizado"}), 401
        return f(*args, **kwargs)
    return wrapper

def requiere_usuario(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "No autorizado"}), 401
        token = auth.replace("Bearer ", "")
        usuario = get_db().execute("SELECT * FROM usuarios WHERE ruc=? AND activo=1", (token,)).fetchone()
        if not usuario:
            return jsonify({"error": "No autorizado"}), 401
        return f(*args, **kwargs)
    return wrapper

# ---- Login y autenticación ----
@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    ruc = data.get("ruc", "").strip()
    contrasena = data.get("contrasena", "")
    conn = get_db()
    usuario = conn.execute("SELECT * FROM usuarios WHERE ruc=?", (ruc,)).fetchone()
    if not usuario:
        return jsonify({"error": "RUC o contraseña incorrectos"}), 401
    if not usuario["activo"]:
        return jsonify({"error": "Usuario deshabilitado. Contacte al administrador"}), 403
    try:
        contrasena_hash = usuario["contrasena"]
        if isinstance(contrasena_hash, str):
            contrasena_hash = contrasena_hash.encode()
        if not bcrypt.checkpw(contrasena.encode(), contrasena_hash):
            return jsonify({"error": "RUC o contraseña incorrectos"}), 401
    except Exception:
        return jsonify({"error": "RUC o contraseña incorrectos"}), 401
    return jsonify({
        "id": usuario["id"],
        "ruc": usuario["ruc"],
        "nombre": usuario["nombre"],
        "correo": usuario["correo"],
        "es_admin": bool(usuario["es_admin"])
    })
# ---- Admin: crear usuario ----
@app.route("/api/admin/usuarios", methods=["POST"])
@requiere_admin
def crear_usuario():
    data = request.get_json()
    ruc = data.get("ruc", "").strip()
    correo = data.get("correo", "").strip()
    nombre = data.get("nombre", "").strip()
    contrasena = data.get("contrasena", "")
    if not ruc or not correo or not nombre or not contrasena:
        return jsonify({"error": "Todos los campos son obligatorios"}), 400
    hashed = bcrypt.hashpw(contrasena.encode(), bcrypt.gensalt())
    conn = get_db()
    try:
        cur = conn.execute("INSERT INTO usuarios (ruc, correo, nombre, contrasena) VALUES (?,?,?,?)",
                            (ruc, correo, nombre, hashed))
        conn.commit()
        usuario_id = cur.lastrowid
        crear_carpeta_usuario(usuario_id)
        return jsonify({"mensaje": "Usuario creado", "id": usuario_id}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "El RUC o correo ya existe"}), 400
# ---- Admin: listar usuarios ----
@app.route("/api/admin/usuarios", methods=["GET"])
@requiere_admin
def listar_usuarios():
    conn = get_db()
    usuarios = conn.execute("SELECT id, ruc, correo, nombre, es_admin, activo, creado_en FROM usuarios").fetchall()
    return jsonify([dict(u) for u in usuarios])
# ---- Admin: deshabilitar / habilitar ----
@app.route("/api/admin/usuarios/<int:usuario_id>/estado", methods=["PUT"])
@requiere_admin
def cambiar_estado(usuario_id):
    data = request.get_json()
    activo = 1 if data.get("activo") else 0
    conn = get_db()
    conn.execute("UPDATE usuarios SET activo=? WHERE id=?", (activo, usuario_id))
    conn.commit()
    return jsonify({"mensaje": "Estado actualizado"})

# ---- Admin: dar permisos (es_admin) ----
@app.route("/api/admin/usuarios/<int:usuario_id>/permisos", methods=["PUT"])
@requiere_admin
def dar_permisos(usuario_id):
    data = request.get_json()
    es_admin = 1 if data.get("es_admin") else 0
    conn = get_db()
    conn.execute("UPDATE usuarios SET es_admin=? WHERE id=?", (es_admin, usuario_id))
    conn.commit()
    return jsonify({"mensaje": "Permisos actualizados"})

# ---- Admin: resetear contraseña ----
@app.route("/api/admin/usuarios/<int:usuario_id>/reset-password", methods=["PUT"])
@requiere_admin
def reset_password(usuario_id):
    data = request.get_json()
    nueva = data.get("nueva_contrasena", "")
    if not nueva:
        return jsonify({"error": "Ingresá la nueva contraseña"}), 400
    hashed = bcrypt.hashpw(nueva.encode(), bcrypt.gensalt())
    conn = get_db()
    conn.execute("UPDATE usuarios SET contrasena=? WHERE id=?", (hashed, usuario_id))
    conn.commit()
    return jsonify({"mensaje": "Contraseña reseteada"})

# ---- Admin: subir documento a usuario ----
@app.route("/api/admin/usuarios/<int:usuario_id>/documentos", methods=["POST"])
@requiere_admin
def subir_documento(usuario_id):
    if "archivo" not in request.files:
        return jsonify({"error": "No se subió ningún archivo"}), 400
    archivo = request.files["archivo"]
    if archivo.filename == "":
        return jsonify({"error": "Nombre de archivo vacío"}), 400
    carpeta = crear_carpeta_usuario(usuario_id)
    ruta = os.path.join(carpeta, archivo.filename)
    archivo.save(ruta)
    conn = get_db()
    conn.execute("INSERT INTO documentos (usuario_id, nombre_archivo, tipo) VALUES (?,?,?)",
                 (usuario_id, archivo.filename, request.form.get("tipo", "")))
    conn.commit()
    return jsonify({"mensaje": "Documento subido"}), 201

# ---- Admin: listar documentos de un usuario ----
@app.route("/api/admin/usuarios/<int:usuario_id>/documentos", methods=["GET"])
@requiere_admin
def listar_documentos_admin(usuario_id):
    conn = get_db()
    docs = conn.execute("SELECT * FROM documentos WHERE usuario_id=?", (usuario_id,)).fetchall()
    return jsonify([dict(d) for d in docs])

# ---- Usuario: ver sus propios documentos ----
@app.route("/api/mis-documentos", methods=["GET"])
@requiere_usuario
def mis_documentos():
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "")
    conn = get_db()
    usuario = conn.execute("SELECT * FROM usuarios WHERE ruc=?", (token,)).fetchone()
    docs = conn.execute("SELECT * FROM documentos WHERE usuario_id=?", (usuario["id"],)).fetchall()
    return jsonify([dict(d) for d in docs])

# ---- Usuario: descargar su documento ----
@app.route("/api/mis-documentos/<int:doc_id>/descargar", methods=["GET"])
@requiere_usuario
def descargar_documento(doc_id):
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "")
    conn = get_db()
    usuario = conn.execute("SELECT * FROM usuarios WHERE ruc=?", (token,)).fetchone()
    doc = conn.execute("SELECT * FROM documentos WHERE id=? AND usuario_id=?", (doc_id, usuario["id"])).fetchone()
    if not doc:
        return jsonify({"error": "Documento no encontrado"}), 404
    carpeta = os.path.join(DOCS_DIR, str(usuario["id"]))
    return send_from_directory(carpeta, doc["nombre_archivo"], as_attachment=True)

# ---- Endpoint RUC existente ----
@app.route("/api/ruc", methods=["GET"])
def consultar_ruc():
    ruc = request.args.get("ruc", "")
    if not ruc:
        return jsonify({"error": "Falta el parámetro ruc"}), 400
    try:
        import requests
        resp = requests.get(f"https://api.turuc.com.py/v1/ruc/{ruc}", timeout=15)
        if resp.status_code == 200:
            return jsonify(resp.json())
        return jsonify({"error": "No se encontró el RUC"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
