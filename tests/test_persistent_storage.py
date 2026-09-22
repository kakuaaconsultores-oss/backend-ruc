import os
import sqlite3
import tempfile
import unittest

from app import _migrar_almacenamiento_persistente


class PersistentStorageMigrationTests(unittest.TestCase):
    def test_migrates_sqlite_documents_and_rewrites_document_paths(self):
        with tempfile.TemporaryDirectory(prefix="kakuaa-storage-test-") as legacy_dir, tempfile.TemporaryDirectory(prefix="kakuaa-disk-test-") as persistent_dir:
            legacy_db = os.path.join(legacy_dir, "usuarios.db")
            legacy_docs = os.path.join(legacy_dir, "documentos", "42", "Facturas")
            os.makedirs(legacy_docs, exist_ok=True)
            legacy_file = os.path.join(legacy_docs, "factura.pdf")
            with open(legacy_file, "wb") as fh:
                fh.write(b"PDF-DE-PRUEBA")

            conn = sqlite3.connect(legacy_db)
            conn.execute(
                "CREATE TABLE documentos (id INTEGER PRIMARY KEY, ruta TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO documentos (id, ruta) VALUES (?, ?)", (7, legacy_file))
            conn.commit()
            conn.close()

            _migrar_almacenamiento_persistente(legacy_dir, persistent_dir)

            persistent_db = os.path.join(persistent_dir, "usuarios.db")
            persistent_file = os.path.join(
                persistent_dir, "documentos", "42", "Facturas", "factura.pdf"
            )
            self.assertTrue(os.path.isfile(persistent_db))
            self.assertTrue(os.path.isfile(persistent_file))

            with open(persistent_file, "rb") as fh:
                self.assertEqual(fh.read(), b"PDF-DE-PRUEBA")

            conn = sqlite3.connect(persistent_db)
            row = conn.execute("SELECT ruta FROM documentos WHERE id = 7").fetchone()
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.close()

            self.assertEqual(row[0], persistent_file)
            self.assertEqual(integrity, "ok")

    def test_migration_is_idempotent_and_does_not_overwrite_persistent_files(self):
        with tempfile.TemporaryDirectory(prefix="kakuaa-storage-test-") as legacy_dir, tempfile.TemporaryDirectory(prefix="kakuaa-disk-test-") as persistent_dir:
            legacy_docs = os.path.join(legacy_dir, "documentos", "1")
            persistent_docs = os.path.join(persistent_dir, "documentos", "1")
            os.makedirs(legacy_docs, exist_ok=True)
            os.makedirs(persistent_docs, exist_ok=True)

            legacy_file = os.path.join(legacy_docs, "archivo.txt")
            persistent_file = os.path.join(persistent_docs, "archivo.txt")
            with open(legacy_file, "wb") as fh:
                fh.write(b"LEGACY")
            with open(persistent_file, "wb") as fh:
                fh.write(b"PERSISTENTE")

            legacy_db = os.path.join(legacy_dir, "usuarios.db")
            conn = sqlite3.connect(legacy_db)
            conn.execute("CREATE TABLE documentos (id INTEGER PRIMARY KEY, ruta TEXT NOT NULL)")
            conn.execute("INSERT INTO documentos (id, ruta) VALUES (?, ?)", (1, legacy_file))
            conn.commit()
            conn.close()

            _migrar_almacenamiento_persistente(legacy_dir, persistent_dir)
            _migrar_almacenamiento_persistente(legacy_dir, persistent_dir)

            with open(persistent_file, "rb") as fh:
                self.assertEqual(fh.read(), b"PERSISTENTE")

            conn = sqlite3.connect(os.path.join(persistent_dir, "usuarios.db"))
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            conn.close()


if __name__ == "__main__":
    unittest.main()
