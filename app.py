import os
import io
import sqlite3
import time
import smtplib
import secrets
import hashlib
import html
import shutil
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from functools import wraps
import bcrypt
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Render usa un filesystem efímero salvo que el servicio tenga un Persistent Disk.
# En producción configuramos PERSISTENT_DATA_DIR=/var/data; localmente se conserva BASE_DIR.
PERSISTENT_DATA_DIR = os.environ.get("PERSISTENT_DATA_DIR", BASE_DIR)
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_BACKEND = "postgres" if DATABASE_URL else "sqlite"
DB_INTEGRITY_ERROR = (sqlite3.IntegrityError, psycopg.IntegrityError) if psycopg is not None else (sqlite3.IntegrityError,)


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


if DB_BACKEND == "sqlite":
    _migrar_almacenamiento_persistente()

if DB_BACKEND == "sqlite":
    DB_PATH = os.path.abspath(os.environ.get("DB_PATH", os.path.join(PERSISTENT_DATA_DIR, "usuarios.db")))
else:
    DB_PATH = None

DOCS_DIR = os.path.abspath(os.environ.get("DOCS_DIR", os.path.join(PERSISTENT_DATA_DIR if PERSISTENT_DATA_DIR != BASE_DIR else BASE_DIR, "documentos")))

if DB_BACKEND == "sqlite":
    if os.path.abspath(PERSISTENT_DATA_DIR) != os.path.abspath(BASE_DIR):
        persistent_real = os.path.realpath(PERSISTENT_DATA_DIR)
        db_real_parent = os.path.realpath(os.path.dirname(DB_PATH))
        docs_real = os.path.realpath(DOCS_DIR)
        if os.path.commonpath([persistent_real, db_real_parent]) != persistent_real:
            raise RuntimeError(f"DB_PATH debe estar dentro de PERSISTENT_DATA_DIR en producción: {DB_PATH}")
        if os.path.commonpath([persistent_real, docs_real]) != persistent_real:
            raise RuntimeError(f"DOCS_DIR debe estar dentro de PERSISTENT_DATA_DIR en producción: {DOCS_DIR}")
    if os.path.abspath(PERSISTENT_DATA_DIR) == os.path.abspath("/var/data") and not os.path.exists(DB_PATH):
        if os.environ.get("ALLOW_EMPTY_PERSISTENT_STORAGE", "0") != "1":
            raise RuntimeError(f"No se encontró la base persistente en {DB_PATH}. Se evita crear una base vacía para proteger los datos existentes.")

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
def _adapt_postgres_sql(sql):
    sql = sql.replace("?", "%s")
    sql = sql.replace("BEGIN IMMEDIATE", "BEGIN")
    sql = sql.replace("datetime('now')", "CURRENT_TIMESTAMP::text")
    sql = sql.replace("date('now')", "CURRENT_DATE::text")
    sql = sql.replace("date(s.inicio)", "CAST(s.inicio AS DATE)")
    sql = sql.replace("date(s2.inicio)", "CAST(s2.inicio AS DATE)")
    sql = sql.replace("COLLATE NOCASE", "")
    sql = sql.replace("julianday(", "_julianday(")
    return sql

if DB_BACKEND == "postgres":
    if psycopg is None:
        raise RuntimeError("DATABASE_URL está configurado pero psycopg no está instalado.")
    class CompatPGConnection(psycopg.Connection):
        def execute(self, query, params=None, *, prepare=None, binary=False):
            return super().execute(_adapt_postgres_sql(query), params, prepare=prepare, binary=binary)
    def get_db():
        return CompatPGConnection.connect(DATABASE_URL, row_factory=dict_row)
else:
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

    conn.execute("""CREATE TABLE IF NOT EXISTS clientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ruc TEXT UNIQUE NOT NULL,
        dv TEXT DEFAULT '',
        razon_social TEXT NOT NULL,
        nombre_comercial TEXT DEFAULT '',
        tipo_persona TEXT NOT NULL DEFAULT 'juridica',
        documento TEXT DEFAULT '',
        correo TEXT DEFAULT '',
        telefono TEXT DEFAULT '',
        direccion TEXT DEFAULT '',
        estado TEXT NOT NULL DEFAULT 'activo',
        creado_por INTEGER,
        creado_en TEXT DEFAULT (datetime('now')),
        actualizado_en TEXT DEFAULT (datetime('now')),
        tipo_impuesto TEXT DEFAULT NULL,
        FOREIGN KEY (creado_por) REFERENCES usuarios(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clientes_estado ON clientes(estado)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clientes_razon_social ON clientes(razon_social)")
    try:
        conn.execute("ALTER TABLE clientes ADD COLUMN tipo_impuesto TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    conn.execute("""CREATE TABLE IF NOT EXISTS cliente_obligaciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER NOT NULL,
        codigo TEXT NOT NULL,
        activo INTEGER NOT NULL DEFAULT 1,
        creado_en TEXT DEFAULT (datetime('now')),
        UNIQUE(cliente_id, codigo),
        FOREIGN KEY (cliente_id) REFERENCES clientes(id) ON DELETE CASCADE
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cliente_obligaciones_cliente ON cliente_obligaciones(cliente_id, activo)")
    conn.execute("""CREATE TABLE IF NOT EXISTS usuario_clientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        cliente_id INTEGER NOT NULL,
        creado_en TEXT DEFAULT (datetime('now')),
        UNIQUE(usuario_id, cliente_id),
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE,
        FOREIGN KEY (cliente_id) REFERENCES clientes(id) ON DELETE CASCADE
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usuario_clientes_usuario ON usuario_clientes(usuario_id, cliente_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usuario_clientes_cliente ON usuario_clientes(cliente_id, usuario_id)")
    conn.commit()
    conn.close()

def init_db_postgres():
    conn = get_db()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS rate_limit_events (id BIGSERIAL PRIMARY KEY, ip TEXT NOT NULL, endpoint TEXT NOT NULL, creado_en DOUBLE PRECISION NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_limit_events ON rate_limit_events(ip, endpoint, creado_en)")
        conn.execute("""CREATE TABLE IF NOT EXISTS superadmin_bootstrap (id INTEGER PRIMARY KEY, usado INTEGER NOT NULL DEFAULT 0, usado_en TEXT DEFAULT NULL)""")
        conn.execute("INSERT INTO superadmin_bootstrap (id, usado) VALUES (1, 0) ON CONFLICT (id) DO NOTHING")
        conn.execute("""CREATE TABLE IF NOT EXISTS usuarios (id BIGSERIAL PRIMARY KEY, ruc TEXT UNIQUE NOT NULL, correo TEXT UNIQUE NOT NULL, nombre TEXT NOT NULL, password_hash TEXT NOT NULL, activo INTEGER DEFAULT 1, intentos_fallidos INTEGER DEFAULT 0, bloqueo_hasta TEXT DEFAULT NULL, token_sesion TEXT DEFAULT NULL, token_sesion_hash TEXT DEFAULT NULL, token_expira_en TEXT DEFAULT NULL, rol TEXT DEFAULT 'contribuyente', usuario TEXT, debe_cambiar INTEGER DEFAULT 0, creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text), csrf_token_hash TEXT DEFAULT NULL, reset_token_hash TEXT DEFAULT NULL, reset_expira_en TEXT DEFAULT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS login_otp (id BIGSERIAL PRIMARY KEY, usuario_id BIGINT NOT NULL REFERENCES usuarios(id), challenge_token TEXT UNIQUE NOT NULL, otp_hash TEXT NOT NULL, expira_en TEXT NOT NULL, intentos INTEGER DEFAULT 0, generaciones INTEGER DEFAULT 0, creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS documentos (id BIGSERIAL PRIMARY KEY, usuario_id BIGINT NOT NULL REFERENCES usuarios(id), nombre_archivo TEXT NOT NULL, ruta TEXT NOT NULL, carpeta TEXT NOT NULL, subcarpeta TEXT DEFAULT '', subcarpeta2 TEXT DEFAULT '', subido_en TEXT DEFAULT (CURRENT_TIMESTAMP::text))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS subcarpetas (id BIGSERIAL PRIMARY KEY, usuario_id BIGINT NOT NULL REFERENCES usuarios(id), carpeta TEXT NOT NULL, nombre TEXT NOT NULL, padre TEXT DEFAULT '')""")
        conn.execute("""CREATE TABLE IF NOT EXISTS tickets_recuperacion (id BIGSERIAL PRIMARY KEY, usuario_id BIGINT NOT NULL REFERENCES usuarios(id), ruc TEXT NOT NULL, estado TEXT DEFAULT 'pendiente', nueva_password TEXT DEFAULT NULL, creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text), resuelto_en TEXT DEFAULT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS facturas_clientes (id BIGSERIAL PRIMARY KEY, cliente_id BIGINT NOT NULL REFERENCES usuarios(id), numero TEXT NOT NULL, fecha TEXT NOT NULL DEFAULT (CURRENT_DATE::text), concepto TEXT NOT NULL, monto DOUBLE PRECISION NOT NULL DEFAULT 0, estado TEXT NOT NULL DEFAULT 'emitida', creado_por BIGINT REFERENCES usuarios(id), creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text), articulo_id BIGINT, tarifa_id BIGINT, cantidad DOUBLE PRECISION DEFAULT 1)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facturas_cliente ON facturas_clientes(cliente_id, fecha)")
        conn.execute("""CREATE TABLE IF NOT EXISTS articulos (id BIGSERIAL PRIMARY KEY, codigo TEXT UNIQUE NOT NULL, nombre TEXT NOT NULL, descripcion TEXT DEFAULT '', unidad TEXT NOT NULL DEFAULT 'servicio', activo INTEGER NOT NULL DEFAULT 1, creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS tarifas_articulos (id BIGSERIAL PRIMARY KEY, articulo_id BIGINT NOT NULL REFERENCES articulos(id), precio DOUBLE PRECISION NOT NULL DEFAULT 0, vigencia_desde TEXT NOT NULL, vigencia_hasta TEXT NOT NULL, ajuste_vencimiento DOUBLE PRECISION NOT NULL DEFAULT 0, activo INTEGER NOT NULL DEFAULT 1, creado_por BIGINT REFERENCES usuarios(id), creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tarifas_articulo_vigencia ON tarifas_articulos(articulo_id, vigencia_desde, vigencia_hasta)")
        conn.execute("""CREATE TABLE IF NOT EXISTS costos_persona (id BIGSERIAL PRIMARY KEY, usuario_id BIGINT NOT NULL REFERENCES usuarios(id), costo_hora DOUBLE PRECISION NOT NULL DEFAULT 0, vigencia_desde TEXT NOT NULL, vigencia_hasta TEXT DEFAULT NULL, activo INTEGER NOT NULL DEFAULT 1, creado_por BIGINT REFERENCES usuarios(id), creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_costos_persona_vigencia ON costos_persona(usuario_id, vigencia_desde, vigencia_hasta)")
        conn.execute("""CREATE TABLE IF NOT EXISTS tareas (id BIGSERIAL PRIMARY KEY, cliente_id BIGINT NOT NULL REFERENCES usuarios(id), titulo TEXT NOT NULL, descripcion TEXT DEFAULT '', prioridad TEXT NOT NULL DEFAULT 'media', estado TEXT NOT NULL DEFAULT 'pendiente', asignado_id BIGINT REFERENCES usuarios(id), creado_por BIGINT NOT NULL REFERENCES usuarios(id), fecha_limite TEXT DEFAULT NULL, creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text), iniciado_en TEXT DEFAULT NULL, completado_en TEXT DEFAULT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tareas_cliente ON tareas(cliente_id, estado)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tareas_asignado ON tareas(asignado_id, estado)")
        conn.execute("""CREATE TABLE IF NOT EXISTS sesiones_trabajo (id BIGSERIAL PRIMARY KEY, tarea_id BIGINT NOT NULL REFERENCES tareas(id), cliente_id BIGINT NOT NULL REFERENCES usuarios(id), usuario_id BIGINT NOT NULL REFERENCES usuarios(id), inicio TEXT NOT NULL, fin TEXT DEFAULT NULL, creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sesiones_cliente ON sesiones_trabajo(cliente_id, inicio)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sesiones_usuario_activa ON sesiones_trabajo(usuario_id, fin)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_usuarios_superadmin ON usuarios(rol) WHERE rol = 'superadmin'")
        conn.execute("""CREATE OR REPLACE FUNCTION _julianday(value TEXT) RETURNS DOUBLE PRECISION LANGUAGE SQL IMMUTABLE RETURNS NULL ON NULL INPUT AS 'SELECT EXTRACT(EPOCH FROM value::timestamp) / 86400.0'""")
        conn.execute("UPDATE usuarios SET usuario = ruc WHERE (usuario IS NULL OR TRIM(usuario) = '')")
        superadmin=conn.execute("SELECT id, usuario FROM usuarios WHERE rol = 'superadmin' LIMIT 1").fetchone()
        if superadmin:
            conn.execute("UPDATE usuarios SET rol = 'contribuyente' WHERE rol = 'superadmin' AND id != ?", (superadmin["id"],))
            usuario_configurado=os.environ.get("SUPERADMIN_USUARIO", "").strip() or "superadmin"
            placeholders={"el usuario que quieras conservar/crear", "superadmin"}
            if usuario_configurado.lower() in placeholders: usuario_configurado="superadmin"
            if superadmin["usuario"] in placeholders or not str(superadmin["usuario"] or "").strip():
                conflicto=conn.execute("SELECT id FROM usuarios WHERE usuario = ? AND id != ?", (usuario_configurado, superadmin["id"])).fetchone()
                if not conflicto: conn.execute("UPDATE usuarios SET usuario = ? WHERE id = ?", (usuario_configurado, superadmin["id"]))

        conn.execute("""CREATE TABLE IF NOT EXISTS clientes (
            id BIGSERIAL PRIMARY KEY,
            ruc TEXT UNIQUE NOT NULL,
            dv TEXT DEFAULT '',
            razon_social TEXT NOT NULL,
            nombre_comercial TEXT DEFAULT '',
            tipo_persona TEXT NOT NULL DEFAULT 'juridica',
            documento TEXT DEFAULT '',
            correo TEXT DEFAULT '',
            telefono TEXT DEFAULT '',
            direccion TEXT DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'activo',
            creado_por BIGINT REFERENCES usuarios(id),
            creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text),
            actualizado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text),
            tipo_impuesto TEXT DEFAULT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_clientes_estado ON clientes(estado)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_clientes_razon_social ON clientes(razon_social)")
        conn.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS tipo_impuesto TEXT DEFAULT NULL")
        conn.execute("""CREATE TABLE IF NOT EXISTS cliente_obligaciones (
            id BIGSERIAL PRIMARY KEY,
            cliente_id BIGINT NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
            codigo TEXT NOT NULL,
            activo INTEGER NOT NULL DEFAULT 1,
            creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text),
            UNIQUE(cliente_id, codigo)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cliente_obligaciones_cliente ON cliente_obligaciones(cliente_id, activo)")
        conn.execute("""CREATE TABLE IF NOT EXISTS usuario_clientes (
            id BIGSERIAL PRIMARY KEY,
            usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            cliente_id BIGINT NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
            creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text),
            UNIQUE(usuario_id, cliente_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usuario_clientes_usuario ON usuario_clientes(usuario_id, cliente_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usuario_clientes_cliente ON usuario_clientes(cliente_id, usuario_id)")
        # ==================== CONTABILIDAD ====================
        conn.execute("""CREATE TABLE IF NOT EXISTS cuentas_contables (
            id BIGSERIAL PRIMARY KEY,
            cliente_id BIGINT DEFAULT NULL REFERENCES clientes(id),
            codigo TEXT NOT NULL,
            nombre TEXT NOT NULL,
            descripcion TEXT DEFAULT '',
            tipo TEXT NOT NULL,
            naturaleza TEXT NOT NULL,
            nivel INTEGER NOT NULL DEFAULT 1,
            cuenta_padre_id BIGINT DEFAULT NULL,
            imputable INTEGER NOT NULL DEFAULT 1,
            activa INTEGER NOT NULL DEFAULT 1,
            creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text),
            actualizado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text),
            concepto_flujo_efectivo TEXT DEFAULT NULL,
            formulario_impuesto TEXT DEFAULT NULL,
            inciso_formulario TEXT DEFAULT NULL,
            FOREIGN KEY (cuenta_padre_id) REFERENCES cuentas_contables(id)
        )""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_cuentas_cliente_codigo ON cuentas_contables(cliente_id, codigo)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cuentas_cliente ON cuentas_contables(cliente_id, codigo)")
        conn.execute("ALTER TABLE cuentas_contables ADD COLUMN IF NOT EXISTS concepto_flujo_efectivo TEXT DEFAULT NULL")
        conn.execute("ALTER TABLE cuentas_contables ADD COLUMN IF NOT EXISTS formulario_impuesto TEXT DEFAULT NULL")
        conn.execute("ALTER TABLE cuentas_contables ADD COLUMN IF NOT EXISTS inciso_formulario TEXT DEFAULT NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_cuentas_global_codigo ON cuentas_contables(codigo) WHERE cliente_id IS NULL")
        conn.execute("""CREATE TABLE IF NOT EXISTS periodos_contables (
            id BIGSERIAL PRIMARY KEY,
            cliente_id BIGINT DEFAULT NULL REFERENCES clientes(id),
            anio INTEGER NOT NULL,
            mes INTEGER NOT NULL,
            fecha_inicio TEXT NOT NULL,
            fecha_fin TEXT NOT NULL,
            estado TEXT NOT NULL DEFAULT 'abierto',
            UNIQUE(cliente_id, anio, mes)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_periodos_cliente ON periodos_contables(cliente_id, anio, mes)")
        conn.execute("""CREATE TABLE IF NOT EXISTS asientos_contables (
            id BIGSERIAL PRIMARY KEY,
            cliente_id BIGINT DEFAULT NULL REFERENCES clientes(id),
            numero INTEGER,
            fecha TEXT NOT NULL,
            concepto TEXT NOT NULL,
            origen TEXT NOT NULL DEFAULT 'MANUAL',
            referencia_tipo TEXT DEFAULT NULL,
            referencia_id INTEGER DEFAULT NULL,
            estado TEXT NOT NULL DEFAULT 'borrador',
            usuario_creador_id BIGINT NOT NULL REFERENCES usuarios(id),
            usuario_contabilizador_id BIGINT DEFAULT NULL REFERENCES usuarios(id),
            contabilizado_en TEXT DEFAULT NULL,
            creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text),
            actualizado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_asientos_cliente ON asientos_contables(cliente_id, fecha, estado)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_asientos_origen ON asientos_contables(origen, referencia_tipo, referencia_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS detalle_asientos (
            id BIGSERIAL PRIMARY KEY,
            asiento_id BIGINT NOT NULL REFERENCES asientos_contables(id) ON DELETE CASCADE,
            cuenta_id BIGINT NOT NULL REFERENCES cuentas_contables(id),
            descripcion TEXT DEFAULT '',
            debe DOUBLE PRECISION NOT NULL DEFAULT 0,
            haber DOUBLE PRECISION NOT NULL DEFAULT 0,
            orden INTEGER NOT NULL DEFAULT 1
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_detalle_asiento ON detalle_asientos(asiento_id, orden)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_detalle_cuenta ON detalle_asientos(cuenta_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS reglas_contables (
            id BIGSERIAL PRIMARY KEY,
            cliente_id BIGINT DEFAULT NULL REFERENCES clientes(id),
            nombre TEXT NOT NULL,
            origen TEXT NOT NULL,
            cuenta_debe_id BIGINT DEFAULT NULL REFERENCES cuentas_contables(id),
            cuenta_haber_id BIGINT DEFAULT NULL REFERENCES cuentas_contables(id),
            activa INTEGER NOT NULL DEFAULT 1,
            creado_en TEXT DEFAULT (CURRENT_TIMESTAMP::text)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reglas_cliente ON reglas_contables(cliente_id, origen, activa)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

if DB_BACKEND == "postgres":
    init_db_postgres()
else:
    init_db()

def insertar_y_obtener_id(conn, sql, params=()):
    if DB_BACKEND == "postgres":
        row = conn.execute(_adapt_postgres_sql(sql).rstrip().rstrip(";") + " RETURNING id", params).fetchone()
        return row["id"]
    return conn.execute(sql, params).lastrowid

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


# ---------- CONTEXTO MULTI-CLIENTE ----------

CLIENTE_TIPOS = {"juridica", "fisica"}
CLIENTE_OBLIGACIONES_VALIDAS = {"IVA", "IRP", "IRE", "IDU"}
CLIENTE_IMPUESTOS_VALIDOS = {"IVA", "IRP-RSP", "IRP-RGC", "IRE SIMPLE", "IRE GENERAL"}
CLIENTE_FORMULARIOS = {"IVA": "120", "IRP-RSP": "515", "IRP-RGC": "516", "IRE SIMPLE": "501", "IRE GENERAL": "500"}

FLUJO_EFECTIVO_CLASIFICACIONES = {
    "1.01": "VENTAS NETAS (COBRO NETO)",
    "1.02": "PAGO A PROVEEDORES LOCALES (PAGO NETO)",
    "1.03": "PAGO A PROVEEDORES DEL EXTERIOR (PAGO NETO)",
    "1.04": "EFECTIVO PAGADO A EMPLEADOS",
    "1.05": "EFECTIVO GENERADO (USADO) POR OTRAS ACTIVIDADES OPERATIVAS",
    "1.06": "PAGO DE IMPUESTOS",
    "2.01": "AUMENTO/DISMINUCIÓN NETO/A DE INVERSIONES TEMPORARIAS",
    "2.02": "AUMENTO/DISMINUCIÓN NETO/A DE INVERSIONES A LARGO PLAZO",
    "2.03": "AUMENTO/DISMINUCIÓN NETO/A DE PROPIEDAD, PLANTA Y EQUIPO",
    "3.01": "APORTE DE CAPITAL",
    "3.02": "AUMENTO/DISMINUCIÓN NETO/A DE PRÉSTAMOS",
    "3.03": "DIVIDENDOS PAGADOS",
    "3.04": "AUMENTO/DISMINUCIÓN NETO/A DE INTERESES",
    "4": "EFECTO DE LAS GANANCIAS O PÉRDIDAS POR DIFERENCIAS DE TIPO DE CAMBIO",
}

FORMULARIO_RENTA_CASILLAS = {
    "500": {
        10: "Enajenación de bienes provenientes de la actividad comercial",
        11: "Prestación de servicios no personales",
        12: "Enajenación de bienes de producción industrial",
        13: "Enajenación de productos agrícolas, frutícolas y hortícolas",
        14: "Enajenación de bienes de producción animal o pecuaria",
        15: "Enajenación de bienes de actividad forestal, minera, pesquera y extractiva",
        16: "Intereses, comisiones, rendimientos y ganancias de capital",
        17: "Operaciones con instrumentos financieros derivados",
        18: "Otros ingresos gravados",
        19: "Ingresos exonerados",
        20: "Ingresos no gravados",
        21: "Total de ingresos",
        22: "Costo de bienes y servicios vendidos",
        23: "Gastos de personal",
        24: "Gastos de administración",
        25: "Gastos de comercialización",
        26: "Gastos financieros",
        27: "Otros gastos deducibles",
        28: "Total de egresos deducibles",
        29: "Renta neta real",
        30: "Renta neta fiscal",
    },
    "501": {
        10: "TOTAL DE INGRESOS DEL EJERCICIO",
        11: "TOTAL DE EGRESOS DEL EJERCICIO",
        12: "RENTA NETA REAL",
        13: "FACTURACIÓN BRUTA ANUAL DEL EJERCICIO",
        14: "RENTA NETA PRESUNTA",
        15: "SALDO A FAVOR DEL CONTRIBUYENTE DEL EJERCICIO ANTERIOR",
        16: "RETENCIONES",
        17: "PERCEPCIONES",
        18: "ANTICIPOS INGRESADOS",
        19: "SUBTOTAL A FAVOR DEL CONTRIBUYENTE",
        20: "SALDO A FAVOR DEL CONTRIBUYENTE",
        21: "BASE IMPONIBLE",
        22: "IMPUESTO DETERMINADO",
        23: "MULTA",
        24: "SUBTOTAL A FAVOR DEL FISCO",
        25: "SALDO A INGRESAR A FAVOR DEL FISCO",
        26: "IMPUESTO LIQUIDADO EN EL PRESENTE EJERCICIO",
        27: "RETENCIONES Y PERCEPCIONES COMPUTABLES",
        28: "ANTICIPOS A INGRESAR PARA EL SIGUIENTE EJERCICIO",
        29: "SALDO A FAVOR DEL CONTRIBUYENTE DEL EJERCICIO QUE SE LIQUIDA",
        30: "CUOTAS DE ANTICIPOS A INGRESAR",
        74: "IMPUESTO LIQUIDADO EN EL EJERCICIO ANTERIOR",
        75: "IMPUESTO LIQUIDADO EN EL EJERCICIO ANTERIOR AL SEÑALADO EN EL INCISO B",
        76: "PROMEDIO DEL IMPUESTO A LA RENTA LIQUIDADO",
    }
}


def formulario_por_impuesto(tipo_impuesto):
    return CLIENTE_FORMULARIOS.get(str(tipo_impuesto or "").strip().upper())

def _cliente_obligaciones(conn, cliente_id):
    filas = conn.execute(
        "SELECT codigo FROM cliente_obligaciones WHERE cliente_id = ? AND activo = 1 ORDER BY codigo",
        (cliente_id,)
    ).fetchall()
    return [str(f["codigo"]) for f in filas]

def _cliente_dict(conn, fila):
    obligaciones = _cliente_obligaciones(conn, fila["id"])
    tipo = str(fila["tipo_persona"] or "juridica").lower()
    tipo_impuesto = str(fila["tipo_impuesto"] or "").strip().upper()
    perfil = ("PERSONA_JURIDICA" if tipo == "juridica" else
              "PERSONA_FISICA_IVA_IRP" if tipo_impuesto == "IVA" else
              "PERSONA_FISICA_IRP" if tipo_impuesto in {"IRP-RSP", "IRP-RGC"} else
              "PERSONA_FISICA")
    return {
        "id": fila["id"],
        "ruc": fila["ruc"],
        "dv": fila["dv"] or "",
        "razon_social": fila["razon_social"],
        "nombre_comercial": fila["nombre_comercial"] or "",
        "tipo_persona": tipo,
        "documento": fila["documento"] or "",
        "correo": fila["correo"] or "",
        "telefono": fila["telefono"] or "",
        "direccion": fila["direccion"] or "",
        "estado": fila["estado"],
        "obligaciones": obligaciones,
        "tipo_impuesto": tipo_impuesto,
        "formulario_impuesto": formulario_por_impuesto(tipo_impuesto),
        "perfil": perfil,
        "creado_en": fila["creado_en"],
        "actualizado_en": fila["actualizado_en"],
    }

def _puede_acceder_cliente(conn, usuario, cliente_id):
    if usuario["rol"] in ("superadmin", "admin"):
        return True
    fila = conn.execute(
        """SELECT 1 FROM usuario_clientes
           WHERE usuario_id = ? AND cliente_id = ?""",
        (usuario["id"], cliente_id)
    ).fetchone()
    return bool(fila)

@app.route("/api/clientes", methods=["GET"])
@staff_required
def listar_clientes():
    usuario = obtener_usuario_por_token()
    conn = get_db()
    if usuario["rol"] in ("superadmin", "admin"):
        filas = conn.execute(
            "SELECT * FROM clientes WHERE estado = 'activo' ORDER BY razon_social"
        ).fetchall()
    else:
        filas = conn.execute(
            """SELECT c.* FROM clientes c
               INNER JOIN usuario_clientes uc ON uc.cliente_id = c.id
               WHERE uc.usuario_id = ? AND c.estado = 'activo'
               ORDER BY c.razon_social""",
            (usuario["id"],)
        ).fetchall()
    resultado = [_cliente_dict(conn, f) for f in filas]
    conn.close()
    return jsonify(resultado)

@app.route("/api/clientes/<int:cliente_id>", methods=["GET"])
@staff_required
def obtener_cliente(cliente_id):
    usuario = obtener_usuario_por_token()
    conn = get_db()
    fila = conn.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
    if not fila:
        conn.close()
        return jsonify({"error": "Cliente no encontrado."}), 404
    if not _puede_acceder_cliente(conn, usuario, cliente_id):
        conn.close()
        return jsonify({"error": "No tenés acceso a este cliente."}), 403
    resultado = _cliente_dict(conn, fila)
    conn.close()
    return jsonify(resultado)

@app.route("/api/clientes", methods=["POST"])
@admin_required
def crear_cliente():
    usuario = obtener_usuario_por_token()
    data = request.get_json() or {}
    ruc = str(data.get("ruc", "")).strip()
    dv = str(data.get("dv", "")).strip()
    razon = str(data.get("razon_social", "")).strip()
    nombre_comercial = str(data.get("nombre_comercial", "")).strip()
    tipo = str(data.get("tipo_persona", "juridica")).strip().lower()
    documento = str(data.get("documento", "")).strip()
    correo = str(data.get("correo", "")).strip()
    telefono = str(data.get("telefono", "")).strip()
    direccion = str(data.get("direccion", "")).strip()
    tipo_impuesto = str(data.get("tipo_impuesto", "")).strip().upper()
    obligaciones = data.get("obligaciones") or []

    if not ruc or not razon:
        return jsonify({"error": "RUC y razón social son obligatorios."}), 400
    if tipo not in CLIENTE_TIPOS:
        return jsonify({"error": "El tipo de persona no es válido."}), 400
    if tipo_impuesto not in CLIENTE_IMPUESTOS_VALIDOS:
        return jsonify({"error": "Seleccioná un tipo de impuesto válido."}), 400
    if not isinstance(obligaciones, list):
        obligaciones = []
    obligacion_principal = "IVA" if tipo_impuesto == "IVA" else "IRP" if tipo_impuesto.startswith("IRP-") else "IRE"
    obligaciones = sorted(set([str(x).strip().upper() for x in obligaciones if str(x).strip()] + [obligacion_principal]))
    invalidas = [x for x in obligaciones if x not in CLIENTE_OBLIGACIONES_VALIDAS]
    if invalidas:
        return jsonify({"error": "Obligación no válida: " + ", ".join(invalidas)}), 400

    conn = get_db()
    try:
        nuevo_id = insertar_y_obtener_id(
            conn,
            """INSERT INTO clientes
               (ruc, dv, razon_social, nombre_comercial, tipo_persona, documento, correo, telefono, direccion, estado, creado_por, tipo_impuesto)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'activo', ?, ?)""",
            (ruc, dv, razon, nombre_comercial, tipo, documento, correo, telefono, direccion, usuario["id"], tipo_impuesto)
        )
        for codigo in obligaciones:
            conn.execute(
                "INSERT INTO cliente_obligaciones (cliente_id, codigo, activo) VALUES (?, ?, 1)",
                (nuevo_id, codigo)
            )
        conn.execute(
            "INSERT INTO usuario_clientes (usuario_id, cliente_id) VALUES (?, ?) ON CONFLICT DO NOTHING"
            if DB_BACKEND == "postgres"
            else "INSERT OR IGNORE INTO usuario_clientes (usuario_id, cliente_id) VALUES (?, ?)",
            (usuario["id"], nuevo_id)
        )
        conn.commit()
        fila = conn.execute("SELECT * FROM clientes WHERE id = ?", (nuevo_id,)).fetchone()
        resultado = _cliente_dict(conn, fila)
        return jsonify(resultado), 201
    except DB_INTEGRITY_ERROR:
        conn.rollback()
        return jsonify({"error": "Ya existe un cliente con ese RUC."}), 409
    finally:
        conn.close()

@app.route("/api/clientes/<int:cliente_id>", methods=["PUT"])
@admin_required
def actualizar_cliente(cliente_id):
    data = request.get_json() or {}
    razon = str(data.get("razon_social", "")).strip()
    ruc = str(data.get("ruc", "")).strip()
    if not razon or not ruc:
        return jsonify({"error": "RUC y razón social son obligatorios."}), 400
    tipo = str(data.get("tipo_persona", "juridica")).strip().lower()
    if tipo not in CLIENTE_TIPOS:
        return jsonify({"error": "El tipo de persona no es válido."}), 400
    tipo_impuesto = str(data.get("tipo_impuesto", "")).strip().upper()
    if tipo_impuesto not in CLIENTE_IMPUESTOS_VALIDOS:
        return jsonify({"error": "Seleccioná un tipo de impuesto válido."}), 400
    obligaciones = data.get("obligaciones") or []
    if not isinstance(obligaciones, list):
        obligaciones = []
    obligacion_principal = "IVA" if tipo_impuesto == "IVA" else "IRP" if tipo_impuesto.startswith("IRP-") else "IRE"
    obligaciones = sorted(set([str(x).strip().upper() for x in obligaciones if str(x).strip()] + [obligacion_principal]))
    if any(x not in CLIENTE_OBLIGACIONES_VALIDAS for x in obligaciones):
        return jsonify({"error": "Hay una obligación no válida."}), 400

    conn = get_db()
    try:
        existe = conn.execute("SELECT id FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
        if not existe:
            return jsonify({"error": "Cliente no encontrado."}), 404
        conn.execute(
            """UPDATE clientes SET ruc = ?, dv = ?, razon_social = ?, nombre_comercial = ?,
               tipo_persona = ?, documento = ?, correo = ?, telefono = ?, direccion = ?,
               tipo_impuesto = ?, actualizado_en = CURRENT_TIMESTAMP WHERE id = ?""",
            (ruc, str(data.get("dv", "")).strip(), razon, str(data.get("nombre_comercial", "")).strip(),
             tipo, str(data.get("documento", "")).strip(), str(data.get("correo", "")).strip(),
             str(data.get("telefono", "")).strip(), str(data.get("direccion", "")).strip(), tipo_impuesto, cliente_id)
        )
        conn.execute("DELETE FROM cliente_obligaciones WHERE cliente_id = ?", (cliente_id,))
        for codigo in obligaciones:
            conn.execute("INSERT INTO cliente_obligaciones (cliente_id, codigo, activo) VALUES (?, ?, 1)", (cliente_id, codigo))
        conn.commit()
        fila = conn.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
        resultado = _cliente_dict(conn, fila)
        return jsonify(resultado)
    except DB_INTEGRITY_ERROR:
        conn.rollback()
        return jsonify({"error": "Ya existe otro cliente con ese RUC."}), 409
    finally:
        conn.close()

@app.route("/api/clientes/<int:cliente_id>", methods=["DELETE"])
@admin_required
def desactivar_cliente(cliente_id):
    conn = get_db()
    try:
        existe = conn.execute("SELECT id FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
        if not existe:
            return jsonify({"error": "Cliente no encontrado."}), 404
        conn.execute(
            "UPDATE clientes SET estado = 'inactivo', actualizado_en = CURRENT_TIMESTAMP WHERE id = ?",
            (cliente_id,)
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()

@app.route("/api/clientes/contexto/<int:cliente_id>", methods=["GET"])
@staff_required
def seleccionar_cliente_contexto(cliente_id):
    usuario = obtener_usuario_por_token()
    conn = get_db()
    fila = conn.execute("SELECT * FROM clientes WHERE id = ? AND estado = 'activo'", (cliente_id,)).fetchone()
    if not fila:
        conn.close()
        return jsonify({"error": "Cliente no encontrado o inactivo."}), 404
    if not _puede_acceder_cliente(conn, usuario, cliente_id):
        conn.close()
        return jsonify({"error": "No tenés acceso a este cliente."}), 403
    resultado = _cliente_dict(conn, fila)
    conn.close()
    return jsonify({"ok": True, "cliente": resultado})


# ---------- RUTAS ----------

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
                    raise ValueError("El usuario superadmin ya pertenece a otra cuenta.")
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
            usuario_id = insertar_y_obtener_id(
                conn,
                """INSERT INTO usuarios
                   (ruc, correo, nombre, password_hash, activo, usuario, rol, debe_cambiar)
                   VALUES (?, ?, 'SUPERADMIN', ?, 1, ?, 'superadmin', 0)""",
                (ruc_superadmin, correo, hash_password(nueva_password), usuario),
            )
            accion = "superadmin_created"

        conn.execute(
            "UPDATE superadmin_bootstrap SET usado = 1, usado_en = datetime('now') WHERE id = 1"
        )
        conn.commit()
    except DB_INTEGRITY_ERROR as exc:
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
@app.route("/api/sesion", methods=["GET"])
@usuario_required
def sesion_actual():
    """Devuelve la identidad de la sesión activa usando la cookie HttpOnly."""
    u = obtener_usuario_por_token()
    if not u:
        return jsonify({"error": "No autorizado"}), 401
    return jsonify({
        "ok": True,
        "usuario": {
            "id": u["id"],
            "usuario": u["usuario"],
            "ruc": u["ruc"],
            "nombre": u["nombre"],
            "correo": u["correo"],
            "rol": u["rol"],
            "debe_cambiar": bool(u["debe_cambiar"]),
        }
    })


@app.route("/api/logout", methods=["POST"])
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
        usuario_id = insertar_y_obtener_id(conn, "INSERT INTO usuarios (ruc, correo, nombre, password_hash, usuario, rol, debe_cambiar) VALUES (?, ?, ?, ?, ?, ?, 1)",
                                           (ruc, correo, nombre, hashed, usuario_nuevo, rol_nuevo))
        conn.commit()
        return jsonify({"ok": True, "id": usuario_id}), 201
    except DB_INTEGRITY_ERROR:
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
    except DB_INTEGRITY_ERROR:
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
    documento_id = insertar_y_obtener_id(conn, "INSERT INTO documentos (usuario_id, nombre_archivo, ruta, carpeta, subcarpeta, subcarpeta2) VALUES (?, ?, ?, ?, ?, ?)",
                                         (usuario_id, nombre_archivo, ruta, carpeta, subcarpeta, subcarpeta2))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": documento_id}), 201

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
        aid=insertar_y_obtener_id(conn,"INSERT INTO articulos(codigo,nombre,descripcion,unidad) VALUES(?,?,?,?)",(codigo,nombre,descripcion,unidad))
        conn.commit()
    except DB_INTEGRITY_ERROR:
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
    except DB_INTEGRITY_ERROR:
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
    tid=insertar_y_obtener_id(conn,"""
        INSERT INTO tarifas_articulos(articulo_id,precio,vigencia_desde,vigencia_hasta,ajuste_vencimiento,creado_por)
        VALUES(?,?,?,?,?,?)
    """,(articulo_id,precio,desde,hasta,ajuste,obtener_usuario_por_token()["id"]))
    conn.commit(); conn.close()
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
    tid=insertar_y_obtener_id(conn,"""
        INSERT INTO tarifas_articulos(articulo_id,precio,vigencia_desde,vigencia_hasta,ajuste_vencimiento,creado_por)
        VALUES(?,?,?,?,?,?)
    """,(anterior["articulo_id"],precio,desde,hasta,ajuste,obtener_usuario_por_token()["id"]))
    conn.commit(); conn.close()
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
    cid=insertar_y_obtener_id(conn,"""
        INSERT INTO costos_persona(usuario_id,costo_hora,vigencia_desde,vigencia_hasta,creado_por)
        VALUES(?,?,?,?,?)
    """,(usuario_id,costo,desde,hasta,obtener_usuario_por_token()["id"]))
    conn.commit(); conn.close()
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
        factura_id = insertar_y_obtener_id(conn,"""
            INSERT INTO facturas_clientes
            (cliente_id, numero, fecha, concepto, monto, estado, creado_por, articulo_id, tarifa_id, cantidad)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (cliente_id, numero, fecha, concepto, monto, estado, obtener_usuario_por_token()["id"], articulo_id, tarifa_id, cantidad))
        conn.commit()
    except DB_INTEGRITY_ERROR:
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
    tarea_id = insertar_y_obtener_id(conn,"""
        INSERT INTO tareas
        (cliente_id, titulo, descripcion, prioridad, asignado_id, creado_por, fecha_limite)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (cliente_id, titulo, descripcion, prioridad, asignado_id, actual["id"], fecha_limite))
    conn.commit()
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



    # ==================== CONTABILIDAD ====================
    conn.execute("""CREATE TABLE IF NOT EXISTS cuentas_contables (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER DEFAULT NULL,
        codigo TEXT NOT NULL,
        nombre TEXT NOT NULL,
        descripcion TEXT DEFAULT '',
        tipo TEXT NOT NULL,
        naturaleza TEXT NOT NULL,
        nivel INTEGER NOT NULL DEFAULT 1,
        cuenta_padre_id INTEGER DEFAULT NULL,
        imputable INTEGER NOT NULL DEFAULT 1,
        activa INTEGER NOT NULL DEFAULT 1,
        creado_en TEXT DEFAULT (datetime('now')),
        actualizado_en TEXT DEFAULT (datetime('now')),
        concepto_flujo_efectivo TEXT DEFAULT NULL,
        formulario_impuesto TEXT DEFAULT NULL,
        inciso_formulario TEXT DEFAULT NULL,
        FOREIGN KEY (cuenta_padre_id) REFERENCES cuentas_contables(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cuentas_codigo ON cuentas_contables(codigo)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cuentas_padre ON cuentas_contables(cuenta_padre_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_cuentas_global_codigo ON cuentas_contables(codigo) WHERE cliente_id IS NULL")
    for col, definition in [
        ("concepto_flujo_efectivo", "TEXT DEFAULT NULL"),
        ("formulario_impuesto", "TEXT DEFAULT NULL"),
        ("inciso_formulario", "TEXT DEFAULT NULL")
    ]:
        try:
            conn.execute(f"ALTER TABLE cuentas_contables ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass

    conn.execute("""CREATE TABLE IF NOT EXISTS periodos_contables (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        anio INTEGER NOT NULL,
        mes INTEGER NOT NULL,
        fecha_inicio TEXT NOT NULL,
        fecha_fin TEXT NOT NULL,
        estado TEXT NOT NULL DEFAULT 'abierto',
        cliente_id INTEGER DEFAULT NULL,
        UNIQUE(cliente_id, anio, mes)
    )""")

    conn.execute("""CREATE TABLE IF NOT EXISTS asientos_contables (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER DEFAULT NULL,
        numero INTEGER,
        fecha TEXT NOT NULL,
        concepto TEXT NOT NULL,
        origen TEXT NOT NULL DEFAULT 'MANUAL',
        referencia_tipo TEXT DEFAULT NULL,
        referencia_id INTEGER DEFAULT NULL,
        estado TEXT NOT NULL DEFAULT 'borrador',
        usuario_creador_id INTEGER NOT NULL,
        usuario_contabilizador_id INTEGER DEFAULT NULL,
        contabilizado_en TEXT DEFAULT NULL,
        creado_en TEXT DEFAULT (datetime('now')),
        actualizado_en TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (usuario_creador_id) REFERENCES usuarios(id),
        FOREIGN KEY (usuario_contabilizador_id) REFERENCES usuarios(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_asientos_fecha ON asientos_contables(fecha, estado)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_asientos_origen ON asientos_contables(origen, referencia_tipo, referencia_id)")

    conn.execute("""CREATE TABLE IF NOT EXISTS detalle_asientos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asiento_id INTEGER NOT NULL,
        cuenta_id INTEGER NOT NULL,
        descripcion TEXT DEFAULT '',
        debe REAL NOT NULL DEFAULT 0,
        haber REAL NOT NULL DEFAULT 0,
        orden INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (asiento_id) REFERENCES asientos_contables(id) ON DELETE CASCADE,
        FOREIGN KEY (cuenta_id) REFERENCES cuentas_contables(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_detalle_asiento ON detalle_asientos(asiento_id, orden)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_detalle_cuenta ON detalle_asientos(cuenta_id)")

    conn.execute("""CREATE TABLE IF NOT EXISTS reglas_contables (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER DEFAULT NULL,
        nombre TEXT NOT NULL,
        origen TEXT NOT NULL,
        cuenta_debe_id INTEGER DEFAULT NULL,
        cuenta_haber_id INTEGER DEFAULT NULL,
        activa INTEGER NOT NULL DEFAULT 1,
        creado_en TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (cuenta_debe_id) REFERENCES cuentas_contables(id),
        FOREIGN KEY (cuenta_haber_id) REFERENCES cuentas_contables(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reglas_origen ON reglas_contables(origen, activa)")


# ==================== CONTEXTO CONTABLE ====================

def obtener_cliente_contable():
    """Obtiene y valida el cliente activo enviado por el ERP."""
    usuario = obtener_usuario_por_token()
    if not usuario:
        return None, (jsonify({"error": "No autorizado"}), 401)
    raw = request.headers.get("X-Cliente-ID", "").strip()
    if not raw:
        return None, (jsonify({"error": "Seleccioná un cliente antes de usar Contabilidad."}), 400)
    try:
        cliente_id = int(raw)
    except ValueError:
        return None, (jsonify({"error": "El cliente seleccionado no es válido."}), 400)
    conn = get_db()
    fila = conn.execute("SELECT * FROM clientes WHERE id = ? AND estado = 'activo'", (cliente_id,)).fetchone()
    if not fila:
        conn.close()
        return None, (jsonify({"error": "El cliente seleccionado no existe o está inactivo."}), 404)
    if not _puede_acceder_cliente(conn, usuario, cliente_id):
        conn.close()
        return None, (jsonify({"error": "No tenés acceso a este cliente."}), 403)
    conn.close()
    return cliente_id, None

# ==================== API CONTABILIDAD ====================

def obtener_contexto_cuenta():
    """Sin cliente activo: catálogo maestro de Kakuaa Consultores.
    Con cliente activo: catálogo exclusivo del cliente seleccionado.
    """
    usuario = obtener_usuario_por_token()
    if not usuario:
        return None, (jsonify({"error": "No autorizado"}), 401)
    raw = request.headers.get("X-Cliente-ID", "").strip()
    if not raw:
        return None, None
    try:
        cliente_id = int(raw)
    except ValueError:
        return None, (jsonify({"error": "El cliente seleccionado no es válido."}), 400)
    conn = get_db()
    fila = conn.execute("SELECT id FROM clientes WHERE id = ? AND estado = 'activo'", (cliente_id,)).fetchone()
    if not fila:
        conn.close()
        return None, (jsonify({"error": "El cliente seleccionado no existe o está inactivo."}), 404)
    if not _puede_acceder_cliente(conn, usuario, cliente_id):
        conn.close()
        return None, (jsonify({"error": "No tenés acceso a este cliente."}), 403)
    conn.close()
    return cliente_id, None

def _validar_cuenta_contable(data, conn, cliente_id, cuenta_id=None):
    codigo = str(data.get("codigo", "")).strip()
    nombre = str(data.get("nombre", "")).strip()
    tipo = str(data.get("tipo", "")).strip().lower()
    naturaleza = str(data.get("naturaleza", "")).strip().lower()
    concepto_flujo = str(data.get("concepto_flujo_efectivo", "")).strip().lower()
    formulario_impuesto = str(data.get("formulario_impuesto", "")).strip().upper()
    inciso_formulario = str(data.get("inciso_formulario", "")).strip()
    try:
        nivel = int(data.get("nivel", 1))
    except (TypeError, ValueError):
        return "El nivel debe ser numérico."
    if not codigo or not nombre:
        return "Código y nombre son obligatorios."
    if tipo not in {"activo", "pasivo", "patrimonio", "ingreso", "costo", "gasto"}:
        return "El tipo de cuenta no es válido."
    if naturaleza not in {"deudora", "acreedora"}:
        return "La naturaleza debe ser deudora o acreedora."
    if nivel < 1:
        return "El nivel debe ser mayor o igual a 1."
    if concepto_flujo not in set(FLUJO_EFECTIVO_CLASIFICACIONES):
        return "El concepto de Estado de Flujo de Efectivo no es válido."

    formulario_esperado = "NO_APLICA"
    if cliente_id is not None:
        fila_cliente = conn.execute("SELECT tipo_impuesto FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
        if not fila_cliente:
            return "El cliente seleccionado no existe."
        tipo_impuesto_cliente = str(fila_cliente["tipo_impuesto"] or "").strip().upper()
        if tipo_impuesto_cliente not in CLIENTE_IMPUESTOS_VALIDOS:
            return "El cliente seleccionado todavía no tiene un tipo de impuesto definido."
        formulario_esperado = formulario_por_impuesto(tipo_impuesto_cliente) or "NO_APLICA"
    if formulario_impuesto != formulario_esperado:
        return f"El formulario debe corresponder al tipo de impuesto del contexto: {formulario_esperado}."
    if formulario_esperado in {"500", "501"}:
        if not bool(data.get("imputable", True)) and inciso_formulario:
            return "El inciso/casilla del Formulario 500/501 solo puede asignarse a cuentas imputables."
        if bool(data.get("imputable", True)) and inciso_formulario:
            try:
                inciso = int(inciso_formulario)
            except ValueError:
                return "El inciso/casilla del Formulario 500/501 debe ser numérico."
            if inciso <= 0:
                return "El inciso/casilla del Formulario 500/501 debe ser mayor que cero."
            if inciso not in FORMULARIO_RENTA_CASILLAS[formulario_esperado]:
                return f"La casilla {inciso} no está definida para el Formulario {formulario_esperado}."
    elif inciso_formulario:
        return "El inciso/casilla de renta solo corresponde a los Formularios 500 y 501."

    scope = "cliente_id IS NULL" if cliente_id is None else "cliente_id = ?"
    scope_params = () if cliente_id is None else (cliente_id,)
    if cuenta_id is not None:
        existe = conn.execute(f"SELECT id FROM cuentas_contables WHERE {scope} AND codigo = ? AND id <> ?", scope_params + (codigo, cuenta_id)).fetchone()
    else:
        existe = conn.execute(f"SELECT id FROM cuentas_contables WHERE {scope} AND codigo = ?", scope_params + (codigo,)).fetchone()
    if existe:
        return "Ya existe una cuenta con ese código."

    imputable = bool(data.get("imputable", True))
    if cuenta_id is not None:
        hijos = conn.execute(f"SELECT COUNT(*) AS n FROM cuentas_contables WHERE {scope} AND cuenta_padre_id = ?", scope_params + (cuenta_id,)).fetchone()["n"]
        movimiento_scope = "a.cliente_id IS NULL" if cliente_id is None else "a.cliente_id = ?"
        movimiento_params = (cuenta_id,) if cliente_id is None else (cuenta_id, cliente_id)
        movimientos = conn.execute(f"""SELECT COUNT(*) AS n FROM detalle_asientos d
                                      JOIN asientos_contables a ON a.id = d.asiento_id
                                      WHERE d.cuenta_id = ? AND {movimiento_scope}""", movimiento_params).fetchone()["n"]
        if imputable and hijos:
            return "La cuenta tiene subcuentas y no puede ser imputable. Primero desactivá la imputabilidad."
        if not imputable and movimientos:
            return "La cuenta tiene movimientos y no puede convertirse en no imputable."

    padre = data.get("cuenta_padre_id")
    if nivel > 1 and padre in (None, "", "null"):
        return "Las cuentas de nivel 2 o superior deben tener una cuenta padre."
    if padre not in (None, "", "null"):
        try:
            padre = int(padre)
        except (TypeError, ValueError):
            return "La cuenta padre no es válida."
        if cuenta_id is not None and padre == cuenta_id:
            return "Una cuenta no puede ser su propia cuenta padre."
        padre_fila = conn.execute(f"SELECT id, imputable FROM cuentas_contables WHERE id = ? AND {scope}", (padre,) + scope_params).fetchone()
        if not padre_fila:
            return "La cuenta padre no existe."
        if padre_fila["imputable"]:
            return "La cuenta padre debe ser no imputable."
    return None

@app.route("/api/contabilidad/cuentas", methods=["GET"])
@admin_required
def listar_cuentas_contables():
    cliente_id, error_ctx = obtener_contexto_cuenta()
    if error_ctx: return error_ctx
    conn=get_db()
    if cliente_id is None:
        filas=conn.execute("""SELECT c.*, p.codigo AS padre_codigo, p.nombre AS padre_nombre
                              FROM cuentas_contables c
                              LEFT JOIN cuentas_contables p ON p.id=c.cuenta_padre_id AND p.cliente_id IS NULL
                              WHERE c.cliente_id IS NULL
                              ORDER BY c.codigo""").fetchall()
    else:
        filas=conn.execute("""SELECT c.*, p.codigo AS padre_codigo, p.nombre AS padre_nombre
                              FROM cuentas_contables c
                              LEFT JOIN cuentas_contables p ON p.id=c.cuenta_padre_id AND p.cliente_id=c.cliente_id
                              WHERE c.cliente_id = ?
                              ORDER BY c.codigo""",(cliente_id,)).fetchall()
    conn.close()
    return jsonify([dict(f) for f in filas])

@app.route("/api/contabilidad/cuentas", methods=["POST"])
@admin_required
def crear_cuenta_contable():
    cliente_id, error_ctx = obtener_contexto_cuenta()
    if error_ctx: return error_ctx
    data=request.get_json(silent=True) or {}
    conn=get_db()
    error=_validar_cuenta_contable(data,conn,cliente_id)
    if error:
        conn.close(); return jsonify({"error":error}),400
    padre=data.get("cuenta_padre_id")
    padre=int(padre) if padre not in (None,"","null") else None
    nivel=int(data.get("nivel",1))
    try:
        conn.execute("""INSERT INTO cuentas_contables
            (cliente_id,codigo,nombre,descripcion,tipo,naturaleza,nivel,cuenta_padre_id,imputable,activa,concepto_flujo_efectivo,formulario_impuesto,inciso_formulario)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cliente_id,str(data["codigo"]).strip(),str(data["nombre"]).strip(),str(data.get("descripcion","")).strip(),
             str(data["tipo"]).lower(),str(data["naturaleza"]).lower(),nivel,padre,
             1 if data.get("imputable",True) else 0,1 if data.get("activa",True) else 0,
             str(data["concepto_flujo_efectivo"]).strip(),str(data["formulario_impuesto"]).strip().upper(),(str(data.get("inciso_formulario","")).strip() if data.get("imputable",True) else None)))
        fila=(conn.execute("SELECT * FROM cuentas_contables WHERE cliente_id IS NULL AND codigo = ?",(str(data["codigo"]).strip(),)).fetchone()
              if cliente_id is None else
              conn.execute("SELECT * FROM cuentas_contables WHERE cliente_id = ? AND codigo = ?",(cliente_id,str(data["codigo"]).strip())).fetchone())
        conn.commit(); conn.close()
        return jsonify({"ok":True,"cuenta":dict(fila)}),201
    except DB_INTEGRITY_ERROR:
        conn.rollback(); conn.close()
        return jsonify({"error":"Ya existe una cuenta con ese código en este contexto."}),409

@app.route("/api/contabilidad/cuentas/<int:cuenta_id>", methods=["PUT"])
@admin_required
def actualizar_cuenta_contable(cuenta_id):
    cliente_id, error_ctx = obtener_contexto_cuenta()
    if error_ctx: return error_ctx
    data=request.get_json(silent=True) or {}
    conn=get_db()
    existe=(conn.execute("SELECT id FROM cuentas_contables WHERE id = ? AND cliente_id IS NULL",(cuenta_id,)).fetchone()
            if cliente_id is None else
            conn.execute("SELECT id FROM cuentas_contables WHERE id = ? AND cliente_id = ?",(cuenta_id,cliente_id)).fetchone())
    if not existe:
        conn.close(); return jsonify({"error":"Cuenta no encontrada en el contexto seleccionado."}),404
    error=_validar_cuenta_contable(data,conn,cliente_id,cuenta_id)
    if error:
        conn.close(); return jsonify({"error":error}),400
    padre=data.get("cuenta_padre_id")
    padre=int(padre) if padre not in (None,"","null") else None
    values=(str(data["codigo"]).strip(),str(data["nombre"]).strip(),str(data.get("descripcion","")).strip(),
            str(data["tipo"]).lower(),str(data["naturaleza"]).lower(),int(data.get("nivel",1)),padre,
            1 if data.get("imputable",True) else 0,1 if data.get("activa",True) else 0,
            str(data["concepto_flujo_efectivo"]).strip(),str(data["formulario_impuesto"]).strip().upper(),(str(data.get("inciso_formulario","")).strip() if data.get("imputable",True) else None),cuenta_id)
    try:
        if cliente_id is None:
            conn.execute("""UPDATE cuentas_contables SET codigo=?,nombre=?,descripcion=?,tipo=?,naturaleza=?,
                            nivel=?,cuenta_padre_id=?,imputable=?,activa=?,concepto_flujo_efectivo=?,formulario_impuesto=?,inciso_formulario=?,actualizado_en=CURRENT_TIMESTAMP
                            WHERE id=? AND cliente_id IS NULL""",values)
            fila=conn.execute("SELECT * FROM cuentas_contables WHERE id=? AND cliente_id IS NULL",(cuenta_id,)).fetchone()
        else:
            conn.execute("""UPDATE cuentas_contables SET codigo=?,nombre=?,descripcion=?,tipo=?,naturaleza=?,
                            nivel=?,cuenta_padre_id=?,imputable=?,activa=?,concepto_flujo_efectivo=?,formulario_impuesto=?,inciso_formulario=?,actualizado_en=CURRENT_TIMESTAMP
                            WHERE id=? AND cliente_id=?""",values+(cliente_id,))
            fila=conn.execute("SELECT * FROM cuentas_contables WHERE id=? AND cliente_id=?",(cuenta_id,cliente_id)).fetchone()
        conn.commit(); conn.close()
        return jsonify({"ok":True,"cuenta":dict(fila)})
    except DB_INTEGRITY_ERROR:
        conn.rollback(); conn.close()
        return jsonify({"error":"Ya existe otra cuenta para ese código en este contexto."}),409

@app.route("/api/contabilidad/cuentas/<int:cuenta_id>", methods=["DELETE"])
@admin_required
def eliminar_cuenta_contable(cuenta_id):
    cliente_id, error_ctx = obtener_contexto_cuenta()
    if error_ctx: return error_ctx
    conn=get_db()
    if cliente_id is None:
        movimientos=conn.execute("""SELECT COUNT(*) AS n FROM detalle_asientos d
                                    JOIN asientos_contables a ON a.id=d.asiento_id
                                    WHERE d.cuenta_id=? AND a.cliente_id IS NULL""",(cuenta_id,)).fetchone()["n"]
        hijos=conn.execute("SELECT COUNT(*) AS n FROM cuentas_contables WHERE cliente_id IS NULL AND cuenta_padre_id=?",(cuenta_id,)).fetchone()["n"]
        cur=conn.execute("DELETE FROM cuentas_contables WHERE id=? AND cliente_id IS NULL",(cuenta_id,))
    else:
        movimientos=conn.execute("""SELECT COUNT(*) AS n FROM detalle_asientos d
                                    JOIN asientos_contables a ON a.id=d.asiento_id
                                    WHERE d.cuenta_id=? AND a.cliente_id=?""",(cuenta_id,cliente_id)).fetchone()["n"]
        hijos=conn.execute("SELECT COUNT(*) AS n FROM cuentas_contables WHERE cliente_id=? AND cuenta_padre_id=?",(cliente_id,cuenta_id)).fetchone()["n"]
        cur=conn.execute("DELETE FROM cuentas_contables WHERE id=? AND cliente_id=?",(cuenta_id,cliente_id))
    if movimientos or hijos:
        conn.rollback(); conn.close(); return jsonify({"error":"La cuenta tiene movimientos o subcuentas y no puede eliminarse. Desactivala en su lugar."}),409
    conn.commit(); conn.close()
    if cur.rowcount == 0: return jsonify({"error":"Cuenta no encontrada"}),404
    return jsonify({"ok":True})

@app.route("/api/contabilidad/asientos", methods=["GET"])
@admin_required
def listar_asientos_contables():
    cliente_id, error_ctx = obtener_cliente_contable()
    if error_ctx: return error_ctx
    conn=get_db()
    filas=conn.execute("""SELECT a.*, u.nombre AS creador_nombre,
                          uc.nombre AS contabilizador_nombre,
                          COALESCE(SUM(d.debe),0) AS total_debe,
                          COALESCE(SUM(d.haber),0) AS total_haber
                          FROM asientos_contables a
                          JOIN usuarios u ON u.id=a.usuario_creador_id
                          LEFT JOIN usuarios uc ON uc.id=a.usuario_contabilizador_id
                          LEFT JOIN detalle_asientos d ON d.asiento_id=a.id
                          WHERE a.cliente_id = ?
                          GROUP BY a.id ORDER BY a.fecha DESC,a.id DESC""",(cliente_id,)).fetchall()
    conn.close()
    return jsonify([dict(f) for f in filas])

@app.route("/api/contabilidad/asientos", methods=["POST"])
@admin_required
def crear_asiento_contable():
    cliente_id, error_ctx = obtener_cliente_contable()
    if error_ctx: return error_ctx
    data=request.get_json(silent=True) or {}
    fecha=str(data.get("fecha","")).strip()
    concepto=str(data.get("concepto","")).strip()
    origen=str(data.get("origen","MANUAL")).strip().upper()
    detalles=data.get("detalles") or []
    if not fecha or not concepto or not isinstance(detalles,list) or len(detalles)<2:
        return jsonify({"error":"Fecha, concepto y al menos dos líneas son obligatorios."}),400
    conn=get_db()
    debe=haber=0.0
    for i,d in enumerate(detalles,1):
        try:
            cuenta_id=int(d.get("cuenta_id"))
            debe_i=float(d.get("debe",0) or 0)
            haber_i=float(d.get("haber",0) or 0)
        except (TypeError,ValueError):
            conn.close(); return jsonify({"error":f"Línea {i} inválida."}),400
        if debe_i < 0 or haber_i < 0 or (debe_i > 0 and haber_i > 0) or (debe_i == 0 and haber_i == 0):
            conn.close(); return jsonify({"error":f"Línea {i}: una línea debe tener débito o crédito, no ambos."}),400
        if not conn.execute("SELECT id FROM cuentas_contables WHERE id=? AND cliente_id=? AND activa=1",(cuenta_id,cliente_id)).fetchone():
            conn.close(); return jsonify({"error":f"La cuenta de la línea {i} no existe o está inactiva."}),400
        debe += debe_i; haber += haber_i
    if round(debe,2) != round(haber,2):
        conn.close(); return jsonify({"error":"El asiento no está cuadrado: el total del debe debe coincidir con el haber."}),400
    u=obtener_usuario_por_token()
    conn.execute("""INSERT INTO asientos_contables
        (cliente_id,fecha,concepto,origen,estado,usuario_creador_id) VALUES (?,?,?,?,?,?)""",
        (cliente_id,fecha,concepto,origen,"borrador",u["id"]))
    asiento=conn.execute("SELECT id FROM asientos_contables WHERE cliente_id=? ORDER BY id DESC LIMIT 1",(cliente_id,)).fetchone()
    asiento_id=asiento["id"]
    for i,d in enumerate(detalles,1):
        conn.execute("""INSERT INTO detalle_asientos
            (asiento_id,cuenta_id,descripcion,debe,haber,orden) VALUES (?,?,?,?,?,?)""",
            (asiento_id,int(d["cuenta_id"]),str(d.get("descripcion","")).strip(),
             float(d.get("debe",0) or 0),float(d.get("haber",0) or 0),i))
    conn.commit()
    fila=conn.execute("SELECT * FROM asientos_contables WHERE id=?",(asiento_id,)).fetchone()
    conn.close()
    return jsonify({"ok":True,"asiento":dict(fila)}),201

@app.route("/api/contabilidad/asientos/<int:asiento_id>/contabilizar", methods=["POST"])
@admin_required
def contabilizar_asiento_contable(asiento_id):
    cliente_id, error_ctx = obtener_cliente_contable()
    if error_ctx: return error_ctx
    conn=get_db()
    a=conn.execute("SELECT * FROM asientos_contables WHERE id=? AND cliente_id=?",(asiento_id,cliente_id)).fetchone()
    if not a:
        conn.close(); return jsonify({"error":"Asiento no encontrado."}),404
    if a["estado"] != "borrador":
        conn.close(); return jsonify({"error":"Solo se pueden contabilizar asientos en borrador."}),409
    tot=conn.execute("SELECT COALESCE(SUM(debe),0) AS debe,COALESCE(SUM(haber),0) AS haber FROM detalle_asientos WHERE asiento_id=?",(asiento_id,)).fetchone()
    if round(float(tot["debe"]),2) != round(float(tot["haber"]),2) or float(tot["debe"]) <= 0:
        conn.close(); return jsonify({"error":"El asiento debe estar cuadrado y tener importe antes de contabilizar."}),409
    u=obtener_usuario_por_token()
    numero=conn.execute("SELECT COALESCE(MAX(numero),0)+1 AS n FROM asientos_contables WHERE cliente_id=? AND estado='contabilizado'",(cliente_id,)).fetchone()["n"]
    conn.execute("""UPDATE asientos_contables SET estado='contabilizado',numero=?,
                    usuario_contabilizador_id=?,contabilizado_en=CURRENT_TIMESTAMP,
                    actualizado_en=CURRENT_TIMESTAMP WHERE id=?""",(numero,u["id"],asiento_id))
    conn.commit(); conn.close()
    return jsonify({"ok":True,"numero":numero})

@app.route("/api/contabilidad/asientos/<int:asiento_id>/anular", methods=["POST"])
@admin_required
def anular_asiento_contable(asiento_id):
    cliente_id, error_ctx = obtener_cliente_contable()
    if error_ctx: return error_ctx
    conn=get_db()
    a=conn.execute("SELECT * FROM asientos_contables WHERE id=? AND cliente_id=?",(asiento_id,cliente_id)).fetchone()
    if not a:
        conn.close(); return jsonify({"error":"Asiento no encontrado."}),404
    if a["estado"] not in ("borrador","contabilizado"):
        conn.close(); return jsonify({"error":"El asiento ya está anulado."}),409
    conn.execute("UPDATE asientos_contables SET estado='anulado',actualizado_en=CURRENT_TIMESTAMP WHERE id=?",(asiento_id,))
    conn.commit(); conn.close()
    return jsonify({"ok":True})

# ==================== REPORTES CONTABLES ====================

def _renta_casillas(conn, cliente_id, formulario, desde, hasta):
    filas = conn.execute("""
        SELECT c.inciso_formulario AS inciso,
               COALESCE(SUM(d.debe - d.haber), 0) AS importe
        FROM detalle_asientos d
        JOIN asientos_contables a ON a.id = d.asiento_id
        JOIN cuentas_contables c ON c.id = d.cuenta_id
        WHERE a.cliente_id = ?
          AND a.estado = 'contabilizado'
          AND a.fecha >= ?
          AND a.fecha <= ?
          AND c.cliente_id = ?
          AND c.imputable = 1
          AND c.formulario_impuesto = ?
          AND c.inciso_formulario IS NOT NULL
          AND TRIM(c.inciso_formulario) <> ''
        GROUP BY c.inciso_formulario
    """, (cliente_id, desde, hasta, cliente_id, formulario)).fetchall()
    return {int(r["inciso"]): float(r["importe"] or 0) for r in filas if str(r["inciso"]).isdigit()}

def _construir_formulario_renta(conn, cliente_id, desde, hasta):
    cliente = conn.execute("SELECT * FROM clientes WHERE id = ? AND estado = 'activo'", (cliente_id,)).fetchone()
    if not cliente:
        raise ValueError("Cliente no encontrado.")
    formulario = formulario_por_impuesto(str(cliente["tipo_impuesto"] or "").upper())
    if formulario not in {"500", "501"}:
        raise ValueError("El cliente seleccionado no utiliza el Formulario 500 o 501.")
    valores = _renta_casillas(conn, cliente_id, formulario, desde, hasta)
    definiciones = FORMULARIO_RENTA_CASILLAS[formulario]
    if formulario == "501":
        valores[12] = max(0, valores.get(10, 0) - valores.get(11, 0))
        valores[14] = valores.get(13, 0) * 0.30
        valores[21] = min(valores.get(12, 0), valores.get(14, 0))
        valores[22] = valores[21] * 0.10
        valores[19] = sum(valores.get(i, 0) for i in (15, 16, 17, 18))
        valores[24] = valores.get(22, 0) + valores.get(23, 0)
        valores[20] = max(0, valores[19] - valores[24])
        valores[25] = max(0, valores[24] - valores[19])
        valores[26] = valores.get(22, 0)
        valores[27] = valores.get(16, 0) + valores.get(17, 0)
        valores[28] = max(0, valores.get(76, valores.get(26, 0)) - valores.get(27, 0))
        valores[29] = min(valores.get(20, 0), valores.get(28, 0))
        valores[30] = max(0, valores.get(28, 0) - valores.get(29, 0)) * 0.25
    return {"cliente": dict(cliente), "formulario": formulario, "version": "3" if formulario == "500" else "2",
            "desde": desde, "hasta": hasta,
            "casillas": [{"numero": n, "descripcion": d, "importe": round(float(valores.get(n, 0)), 2)} for n, d in definiciones.items()],
            "nota": "Preliquidación generada desde la contabilidad de Kakuaa ERP. Debe ser revisada antes de su presentación en Marangatu."}

@app.route("/api/contabilidad/reportes/formulario-renta", methods=["GET"])
@admin_required
def reporte_formulario_renta():
    cliente_id, error_ctx = obtener_cliente_contable()
    if error_ctx: return error_ctx
    desde = str(request.args.get("desde", "")).strip() or datetime.utcnow().strftime("%Y") + "-01-01"
    hasta = str(request.args.get("hasta", "")).strip() or datetime.utcnow().strftime("%Y") + "-12-31"
    try:
        datetime.strptime(desde, "%Y-%m-%d"); datetime.strptime(hasta, "%Y-%m-%d")
    except ValueError: return jsonify({"error": "Las fechas deben tener formato AAAA-MM-DD."}), 400
    if desde > hasta: return jsonify({"error": "La fecha desde no puede ser posterior a la fecha hasta."}), 400
    conn=get_db()
    try: return jsonify(_construir_formulario_renta(conn, cliente_id, desde, hasta))
    except ValueError as e: return jsonify({"error": str(e)}), 400
    finally: conn.close()

@app.route("/api/contabilidad/reportes/formulario-renta/<formato>", methods=["GET"])
@admin_required
def descargar_formulario_renta(formato):
    cliente_id, error_ctx = obtener_cliente_contable()
    if error_ctx: return error_ctx
    formato = str(formato).strip().lower()
    if formato not in {"xlsx", "pdf"}: return jsonify({"error": "Formato no válido. Usá xlsx o pdf."}), 400
    desde = str(request.args.get("desde", "")).strip() or datetime.utcnow().strftime("%Y") + "-01-01"
    hasta = str(request.args.get("hasta", "")).strip() or datetime.utcnow().strftime("%Y") + "-12-31"
    try:
        datetime.strptime(desde, "%Y-%m-%d"); datetime.strptime(hasta, "%Y-%m-%d")
    except ValueError: return jsonify({"error": "Las fechas deben tener formato AAAA-MM-DD."}), 400
    conn=get_db()
    try: reporte=_construir_formulario_renta(conn, cliente_id, desde, hasta)
    except ValueError as e: conn.close(); return jsonify({"error": str(e)}), 400
    finally:
        try: conn.close()
        except Exception: pass
    razon=str(reporte["cliente"]["razon_social"]).replace("/", "-")
    nombre=f"Formulario_{reporte['formulario']}_{razon}_{desde}_{hasta}"
    if formato == "xlsx":
        wb=Workbook(); ws=wb.active; ws.title=f"Form {reporte['formulario']}"
        ws["A1"]=f"FORMULARIO N° {reporte['formulario']} — IRE {'GENERAL' if reporte['formulario']=='500' else 'SIMPLE'}"
        ws.merge_cells("A1:C1"); ws["A1"].font=Font(bold=True,size=14); ws["A1"].alignment=Alignment(horizontal="center")
        ws.append(["RUC",reporte["cliente"]["ruc"],"DV",reporte["cliente"].get("dv","")])
        ws.append(["Razón Social",reporte["cliente"]["razon_social"]]); ws.append(["Período",f"{desde} al {hasta}"]); ws.append([])
        ws.append(["INC./CASILLA","DESCRIPCIÓN","IMPORTE"])
        thin=Side(style="thin",color="B7B7B7")
        for cell in ws[6]: cell.font=Font(bold=True); cell.border=Border(bottom=thin)
        for item in reporte["casillas"]: ws.append([item["numero"],item["descripcion"],item["importe"]])
        for row in ws.iter_rows(min_row=7,min_col=3,max_col=3): row[0].number_format='#,##0'
        ws.column_dimensions["A"].width=16; ws.column_dimensions["B"].width=72; ws.column_dimensions["C"].width=18; ws.freeze_panes="A7"
        ws.cell(ws.max_row+2,1,reporte["nota"]); ws.merge_cells(start_row=ws.max_row+2,start_column=1,end_row=ws.max_row+2,end_column=3)
        output=io.BytesIO(); wb.save(output); output.seek(0)
        from flask import send_file
        return send_file(output,as_attachment=True,download_name=nombre+".xlsx",mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    styles=getSampleStyleSheet(); output=io.BytesIO()
    doc=SimpleDocTemplate(output,pagesize=A4,rightMargin=28,leftMargin=28,topMargin=28,bottomMargin=28)
    story=[Paragraph(f"<b>FORMULARIO N° {reporte['formulario']} — IRE {'GENERAL' if reporte['formulario']=='500' else 'SIMPLE'}</b>",styles["Title"]),
           Paragraph(f"RUC: {html.escape(str(reporte['cliente']['ruc']))} &nbsp;&nbsp; DV: {html.escape(str(reporte['cliente'].get('dv','')))}",styles["Normal"]),
           Paragraph(f"Razón Social: {html.escape(str(reporte['cliente']['razon_social']))}",styles["Normal"]),
           Paragraph(f"Período: {desde} al {hasta}",styles["Normal"]),Spacer(1,10)]
    data=[["INC./CASILLA","DESCRIPCIÓN","IMPORTE"]]+[[str(x["numero"]),x["descripcion"],f"{x['importe']:,.0f}"] for x in reporte["casillas"]]
    table=Table(data,colWidths=[65,370,90],repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#1a2a5e")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("GRID",(0,0),(-1,-1),0.4,colors.grey),("ALIGN",(2,1),(2,-1),"RIGHT"),("VALIGN",(0,0),(-1,-1),"TOP"),("FONTSIZE",(0,0),(-1,-1),8)]))
    story += [table,Spacer(1,10),Paragraph(html.escape(reporte["nota"]),styles["Normal"])]
    doc.build(story); output.seek(0)
    from flask import send_file
    return send_file(output,as_attachment=True,download_name=nombre+".pdf",mimetype="application/pdf")

# Consulta pública de RUC mediante la API de integración de TuRuc.
# El backend actúa como proxy para que el frontend de Kakuaa no dependa
# directamente de la API externa ni tenga problemas de CORS.
@app.route("/api/ruc", methods=["GET"])
def consultar_ruc():
    ruc = str(request.args.get("ruc", "")).strip()
    if not ruc:
        return jsonify({"error": "El RUC es obligatorio."}), 400

    try:
        respuesta = requests.get(
            f"https://turuc.com.py/api/contribuyente/{ruc}",
            timeout=10,
            headers={"Accept": "application/json", "User-Agent": "Kakuaa-Consultores/1.0"},
        )
        try:
            datos = respuesta.json()
        except ValueError:
            return jsonify({"error": "TuRuc devolvió una respuesta no válida."}), 502

        if respuesta.status_code >= 400:
            mensaje = datos.get("message") if isinstance(datos, dict) else None
            return jsonify({"error": mensaje or "No se pudo consultar el RUC."}), respuesta.status_code

        return jsonify(datos), 200
    except requests.RequestException:
        return jsonify({"error": "No se pudo conectar con el servicio de consulta de RUC."}), 502

# Búsqueda pública de contribuyentes por nombre, apellido, razón social o documento.
# TuRuc permite búsquedas flexibles y paginadas desde 3 caracteres.
@app.route("/api/ruc/search", methods=["GET"])
def buscar_ruc():
    termino = str(request.args.get("search", "")).strip()
    pagina = str(request.args.get("page", "0")).strip() or "0"

    if len(termino) < 3:
        return jsonify({"error": "Ingresá al menos 3 caracteres para buscar."}), 400

    try:
        pagina_num = int(pagina)
        if pagina_num < 0:
            raise ValueError
    except ValueError:
        return jsonify({"error": "La página indicada no es válida."}), 400

    try:
        respuesta = requests.get(
            "https://turuc.com.py/api/contribuyente/search",
            params={"search": termino, "page": pagina_num},
            timeout=10,
            headers={"Accept": "application/json", "User-Agent": "Kakuaa-Consultores/1.0"},
        )
        try:
            datos = respuesta.json()
        except ValueError:
            return jsonify({"error": "TuRuc devolvió una respuesta no válida."}), 502

        if respuesta.status_code >= 400:
            mensaje = datos.get("message") if isinstance(datos, dict) else None
            return jsonify({"error": mensaje or "No se pudo realizar la búsqueda."}), respuesta.status_code

        return jsonify(datos), 200
    except requests.RequestException:
        return jsonify({"error": "No se pudo conectar con el servicio de búsqueda de RUC."}), 502


if __name__ == "__main__":

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))