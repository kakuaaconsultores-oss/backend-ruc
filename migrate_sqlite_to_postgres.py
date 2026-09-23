import os
import sqlite3

import psycopg
from psycopg.rows import dict_row


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SQLITE_PATH = os.environ.get("SQLITE_MIGRATION_PATH", "/var/data/usuarios.db")

TABLES = [
    "rate_limit_events",
    "superadmin_bootstrap",
    "usuarios",
    "login_otp",
    "documentos",
    "subcarpetas",
    "tickets_recuperacion",
    "articulos",
    "tarifas_articulos",
    "costos_persona",
    "facturas_clientes",
    "tareas",
    "sesiones_trabajo",
]


def table_columns_sqlite(conn, table):
    return [row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def table_columns_postgres(conn, table):
    rows = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    return [row["column_name"] for row in rows]


def reset_sequence(conn, table):
    if "id" not in table_columns_postgres(conn, table):
        return
    conn.execute(
        f"""
        SELECT setval(
            pg_get_serial_sequence(%s, 'id'),
            COALESCE(MAX(id), 1),
            MAX(id) IS NOT NULL
        )
        FROM "{table}"
        """,
        (table,),
    )


def main():
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL no está configurado.")
    if not os.path.exists(SQLITE_PATH):
        raise SystemExit(f"No existe la base SQLite a migrar: {SQLITE_PATH}")

    sqlite_conn = sqlite3.connect(f"file:{SQLITE_PATH}?mode=ro", uri=True)
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)

    try:
        pg_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS migration_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP::text
            )
            """
        )
        done = pg_conn.execute(
            "SELECT value FROM migration_meta WHERE key = 'sqlite_to_postgres_v1'"
        ).fetchone()
        if done:
            print("[MIGRATION] SQLite -> PostgreSQL ya fue ejecutada. No se modifica nada.")
            return

        non_empty = []
        for table in TABLES:
            row = pg_conn.execute(f'SELECT COUNT(*) AS total FROM "{table}"').fetchone()
            if int(row["total"]) > 0:
                non_empty.append(f"{table}={row['total']}")
        # superadmin_bootstrap is initialized with its mandatory id=1 row.
        non_empty = [x for x in non_empty if not x.startswith("superadmin_bootstrap=")]
        if non_empty:
            raise RuntimeError(
                "PostgreSQL no está vacío; se cancela la migración para evitar mezclar datos: "
                + ", ".join(non_empty)
            )

        print(f"[MIGRATION] Origen: {SQLITE_PATH}")
        print("[MIGRATION] Destino: PostgreSQL")

        for table in TABLES:
            sqlite_cols = table_columns_sqlite(sqlite_conn, table)
            pg_cols = table_columns_postgres(pg_conn, table)
            cols = [c for c in sqlite_cols if c in pg_cols]
            if not cols:
                continue

            rows = sqlite_conn.execute(
                f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{table}"'
            ).fetchall()
            if not rows:
                print(f"[MIGRATION] {table}: 0 filas")
                continue

            placeholders = ", ".join(["%s"] * len(cols))
            quoted_cols = ", ".join(chr(34) + c + chr(34) for c in cols)
            sql = f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({placeholders})'
            for row in rows:
                pg_conn.execute(sql, tuple(row[c] for c in cols))

            print(f"[MIGRATION] {table}: {len(rows)} filas")

        for table in TABLES:
            reset_sequence(pg_conn, table)

        pg_conn.execute(
            """
            INSERT INTO migration_meta (key, value)
            VALUES ('sqlite_to_postgres_v1', %s)
            """,
            ("completed",),
        )
        pg_conn.commit()
        print("[MIGRATION] COMPLETADA correctamente.")

    except Exception:
        pg_conn.rollback()
        raise
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    main()
