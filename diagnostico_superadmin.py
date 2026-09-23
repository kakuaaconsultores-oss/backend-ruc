import app

conn = app.get_db()
try:
    total = conn.execute("SELECT COUNT(*) AS total FROM usuarios WHERE rol = 'superadmin'").fetchone()["total"]
    row = conn.execute("SELECT usuario, activo, debe_cambiar FROM usuarios WHERE rol = 'superadmin' LIMIT 1").fetchone()
    bootstrap = conn.execute("SELECT usado FROM superadmin_bootstrap WHERE id = 1").fetchone()
    print("=== KAKUAA SUPERADMIN DIAGNOSTICO ===", flush=True)
    print(f"DB_BACKEND={app.DB_BACKEND}", flush=True)
    print(f"SUPERADMIN_COUNT={total}", flush=True)
    print(f"SUPERADMIN_USER={row['usuario'] if row else 'NONE'}", flush=True)
    print(f"SUPERADMIN_ACTIVE={row['activo'] if row else 'NONE'}", flush=True)
    print(f"SUPERADMIN_DEBE_CAMBIAR={row['debe_cambiar'] if row else 'NONE'}", flush=True)
    print(f"BOOTSTRAP_USED={bootstrap['usado'] if bootstrap else 'NONE'}", flush=True)
    print("=== FIN DIAGNOSTICO ===", flush=True)
finally:
    conn.close()
