import os
import tempfile
import unittest
from io import BytesIO
from datetime import datetime, timedelta

TEST_DIR = tempfile.mkdtemp(prefix="kakuaa-tests-")
os.environ["DB_PATH"] = os.path.join(TEST_DIR, "test.db")
os.environ["DOCS_DIR"] = os.path.join(TEST_DIR, "documentos")
os.environ["SUPERADMIN_USUARIO"] = "superadmin-test"
os.environ["SUPERADMIN_PASSWORD"] = "Super123!"
os.environ["SUPERADMIN_EMAIL"] = "superadmin@test.local"

from app import app, get_db, hash_password


class KakuaaApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app.test_client()
        cls.otp = "1234"
        import app as module
        module.generar_otp = lambda: cls.otp
        module.enviar_otp = lambda usuario, otp: True
        module.enviar_correo = lambda *args, **kwargs: True

    def setUp(self):
        conn = get_db()
        conn.execute("DELETE FROM login_otp")
        conn.execute("DELETE FROM documentos")
        conn.execute("DELETE FROM subcarpetas")
        conn.execute("DELETE FROM tickets_recuperacion")
        conn.execute("DELETE FROM usuarios WHERE rol != 'superadmin'")
        conn.execute("""UPDATE usuarios SET intentos_fallidos=0, bloqueo_hasta=NULL,
                        token_sesion=NULL, token_sesion_hash=NULL, token_expira_en=NULL,
                        debe_cambiar=0, activo=1
                        WHERE rol='superadmin'""")
        conn.commit()
        self.superadmin_id = conn.execute(
            "SELECT id FROM usuarios WHERE rol='superadmin'"
        ).fetchone()["id"]
        conn.close()

    def add_user(self, usuario, rol="contribuyente", password="Test123!", ruc=None):
        conn = get_db()
        cur = conn.execute(
            """INSERT INTO usuarios
               (ruc, correo, nombre, password_hash, usuario, rol, debe_cambiar)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (ruc or f"{usuario}-ruc", f"{usuario}@test.local", usuario.title(),
             hash_password(password), usuario, rol)
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        return user_id

    def _user_id(self, usuario):
        conn = get_db()
        row = conn.execute(
            "SELECT id FROM usuarios WHERE usuario=?", (usuario,)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        return row["id"]

    def login(self, usuario, password="Test123!"):
        response = self.client.post(
            "/api/login", json={"usuario": usuario, "password": password}
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["challenge"]

    def verify(self, challenge, otp="1234"):
        response = self.client.post(
            "/api/login/verify-otp", json={"challenge": challenge, "otp": otp}
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["token"]

    def auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_login_otp_and_session(self):
        self.add_user("contrib")
        challenge = self.login("contrib")
        bad = self.client.post(
            "/api/login/verify-otp", json={"challenge": challenge, "otp": "9999"}
        )
        self.assertEqual(bad.status_code, 401)
        good = self.client.post(
            "/api/login/verify-otp", json={"challenge": challenge, "otp": "1234"}
        )
        self.assertEqual(good.status_code, 200)
        token = good.get_json()["token"]
        self.assertTrue(token)
        self.assertEqual(
            self.client.get("/api/mis-documentos", headers=self.auth(token)).status_code,
            200,
        )

    def test_password_lockout_after_five_failures(self):
        self.add_user("locked")
        for attempt in range(1, 6):
            response = self.client.post(
                "/api/login", json={"usuario": "locked", "password": "Wrong123!"}
            )
            self.assertEqual(response.status_code, 429 if attempt == 5 else 401)
        response = self.client.post(
            "/api/login", json={"usuario": "locked", "password": "Test123!"}
        )
        self.assertEqual(response.status_code, 429)
        self.assertTrue(response.get_json()["bloqueado"])

    def test_otp_lockout_after_five_failures(self):
        self.add_user("otplocked")
        challenge = self.login("otplocked")
        for attempt in range(1, 6):
            response = self.client.post(
                "/api/login/verify-otp",
                json={"challenge": challenge, "otp": "9999"},
            )
            self.assertEqual(response.status_code, 429 if attempt == 5 else 401)

    def test_expired_otp_and_resend_limit(self):
        self.add_user("resend")
        challenge = self.login("resend")
        conn = get_db()
        conn.execute(
            "UPDATE login_otp SET expira_en=? WHERE challenge_token=?",
            ((datetime.utcnow() - timedelta(minutes=2)).isoformat(), challenge),
        )
        conn.commit()
        conn.close()
        expired = self.client.post(
            "/api/login/verify-otp", json={"challenge": challenge, "otp": "1234"}
        )
        self.assertEqual(expired.status_code, 401)
        self.assertTrue(expired.get_json()["vencido"])

        challenge = self.login("resend")
        for expected_remaining in [4, 3, 2, 1, 0]:
            response = self.client.post(
                "/api/login/resend-otp", json={"challenge": challenge}
            )
            self.assertEqual(response.status_code, 200, response.get_json())
            data = response.get_json()
            self.assertEqual(data["regeneraciones_restantes"], expected_remaining)
            challenge = data["challenge"]
        response = self.client.post(
            "/api/login/resend-otp", json={"challenge": challenge}
        )
        self.assertEqual(response.status_code, 429)

    def test_logout_and_expired_session(self):
        self.add_user("session")
        token = self.verify(self.login("session"))
        self.assertEqual(
            self.client.post("/api/logout", headers=self.auth(token)).status_code, 200
        )
        self.assertEqual(
            self.client.get("/api/mis-documentos", headers=self.auth(token)).status_code,
            401,
        )

    def test_role_hierarchy(self):
        self.add_user("admin", "admin")
        self.add_user("operativo", "operativo")
        self.add_user("contrib2", "contribuyente")
        super_token = self.verify(self.login("superadmin-test", "Super123!"))

        response = self.client.post(
            "/api/admin/usuarios",
            headers=self.auth(super_token),
            json={"ruc": "nuevo-ruc", "correo": "nuevo@test.local", "nombre": "Nuevo",
                  "usuario": "nuevo", "contrasena": "Test123!", "rol": "superadmin"},
        )
        self.assertEqual(response.status_code, 400)

        admin_token = self.verify(self.login("admin"))
        response = self.client.post(
            "/api/admin/usuarios",
            headers=self.auth(admin_token),
            json={"ruc": "otro-ruc", "correo": "otro@test.local", "nombre": "Otro",
                  "usuario": "otro", "contrasena": "Test123!", "rol": "admin"},
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            "/api/admin/usuarios",
            headers=self.auth(admin_token),
            json={"ruc": "op2-ruc", "correo": "op2@test.local", "nombre": "Op2",
                  "usuario": "op2", "contrasena": "Test123!", "rol": "operativo"},
        )
        self.assertEqual(response.status_code, 201)

        operativo_token = self.verify(self.login("operativo"))
        response = self.client.post(
            "/api/admin/usuarios",
            headers=self.auth(operativo_token),
            json={"ruc": "c3-ruc", "correo": "c3@test.local", "nombre": "C3",
                  "usuario": "c3", "contrasena": "Test123!", "rol": "contribuyente"},
        )
        self.assertEqual(response.status_code, 201)

        response = self.client.post(
            "/api/admin/usuarios",
            headers=self.auth(operativo_token),
            json={"ruc": "op3-ruc", "correo": "op3@test.local", "nombre": "Op3",
                  "usuario": "op3", "contrasena": "Test123!", "rol": "operativo"},
        )
        self.assertEqual(response.status_code, 403)

    def test_documents_are_isolated_and_paths_are_constrained(self):
        self.add_user("admin2", "admin")
        user_a = self.add_user("usera")
        user_b = self.add_user("userb")
        admin_token = self.verify(self.login("admin2"))

        upload = self.client.post(
            f"/api/admin/usuarios/{user_a}/documentos",
            headers=self.auth(admin_token),
            data={
                "carpeta": "Facturas",
                "subcarpeta": "../fuera",
                "subcarpeta2": "2026",
                "archivo": (BytesIO(b"hola"), "../../factura.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 201, upload.get_json())

        conn = get_db()
        doc = conn.execute("SELECT * FROM documentos WHERE usuario_id=?", (user_a,)).fetchone()
        conn.close()
        self.assertTrue(doc)
        docs_real = os.path.realpath(os.environ["DOCS_DIR"])
        self.assertEqual(os.path.commonpath([docs_real, os.path.realpath(doc["ruta"])]), docs_real)

        user_a_token = self.verify(self.login("usera"))
        user_b_token = self.verify(self.login("userb"))
        self.assertEqual(
            self.client.get("/api/mis-documentos", headers=self.auth(user_a_token)).status_code,
            200,
        )
        forbidden = self.client.get(
            f"/api/mis-documentos/{doc['id']}/descargar",
            headers=self.auth(user_b_token),
        )
        self.assertEqual(forbidden.status_code, 403)

        outside = os.path.join(TEST_DIR, "outside.pdf")
        with open(outside, "wb") as fh:
            fh.write(b"outside")
        conn = get_db()
        conn.execute("UPDATE documentos SET ruta=? WHERE id=?", (outside, doc["id"]))
        conn.commit()
        conn.close()
        blocked = self.client.get(
            f"/api/mis-documentos/{doc['id']}/descargar",
            headers=self.auth(user_a_token),
        )
        self.assertEqual(blocked.status_code, 404)

    def test_session_expiration_is_enforced_server_side(self):
        self.add_user("expired-session")
        token = self.verify(self.login("expired-session"))
        conn = get_db()
        conn.execute(
            "UPDATE usuarios SET token_expira_en=? WHERE usuario=?",
            ((datetime.utcnow() - timedelta(minutes=1)).isoformat(), "expired-session"),
        )
        conn.commit()
        conn.close()
        response = self.client.get(
            "/api/mis-documentos", headers=self.auth(token)
        )
        self.assertEqual(response.status_code, 401)

    def test_document_delete_rejects_path_outside_docs(self):
        self.add_user("admin-delete", "admin")
        user_id = self.add_user("delete-target")
        admin_token = self.verify(self.login("admin-delete"))
        upload = self.client.post(
            f"/api/admin/usuarios/{user_id}/documentos",
            headers=self.auth(admin_token),
            data={
                "carpeta": "Facturas",
                "archivo": (BytesIO(b"hola"), "factura.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 201, upload.get_json())
        doc_id = upload.get_json()["id"]
        outside = os.path.join(TEST_DIR, "outside-delete.pdf")
        with open(outside, "wb") as fh:
            fh.write(b"outside")
        conn = get_db()
        conn.execute("UPDATE documentos SET ruta=? WHERE id=?", (outside, doc_id))
        conn.commit()
        conn.close()
        response = self.client.delete(
            f"/api/admin/documentos/{doc_id}",
            headers=self.auth(admin_token),
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(os.path.isfile(outside))

    def test_recovery_request_is_generic_for_unknown_ruc(self):
        known = self.add_user("recover")
        known_response = self.client.post(
            "/api/solicitar-reset", json={"ruc": "recover-ruc"}
        )
        unknown_response = self.client.post(
            "/api/solicitar-reset", json={"ruc": "does-not-exist"}
        )
        self.assertEqual(known_response.status_code, 200)
        self.assertEqual(unknown_response.status_code, 200)
        self.assertEqual(known_response.get_json()["message"], unknown_response.get_json()["message"])
        self.assertIsNotNone(known)

    def test_user_listing_respects_role_hierarchy(self):
        self.add_user("list-admin", "admin")
        self.add_user("list-operativo", "operativo")
        self.add_user("list-contrib", "contribuyente")

        super_token = self.verify(self.login("superadmin-test", "Super123!"))
        super_users = self.client.get("/api/admin/usuarios", headers=self.auth(super_token))
        self.assertEqual(super_users.status_code, 200)
        self.assertEqual({u["rol"] for u in super_users.get_json()}, {"superadmin", "admin", "operativo", "contribuyente"})

        admin_token = self.verify(self.login("list-admin"))
        admin_users = self.client.get("/api/admin/usuarios", headers=self.auth(admin_token))
        self.assertEqual(admin_users.status_code, 200)
        self.assertEqual({u["rol"] for u in admin_users.get_json()}, {"operativo", "contribuyente"})

        operativo_token = self.verify(self.login("list-operativo"))
        operativo_users = self.client.get("/api/admin/usuarios", headers=self.auth(operativo_token))
        self.assertEqual(operativo_users.status_code, 200)
        self.assertEqual({u["rol"] for u in operativo_users.get_json()}, {"contribuyente"})

    def test_disabling_user_revokes_existing_session(self):
        self.add_user("target-disable")
        self.add_user("admin-disable", "admin")
        target_token = self.verify(self.login("target-disable"))
        admin_token = self.verify(self.login("admin-disable"))
        target_id = self._user_id("target-disable")

        response = self.client.put(
            f"/api/admin/usuarios/{target_id}/estado",
            headers=self.auth(admin_token),
            json={"activo": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/mis-documentos", headers=self.auth(target_token)).status_code, 401)

        conn = get_db()
        row = conn.execute("SELECT activo, token_sesion, token_sesion_hash, token_expira_en FROM usuarios WHERE usuario=?", ("target-disable",)).fetchone()
        conn.close()
        self.assertEqual(row["activo"], 0)
        self.assertIsNone(row["token_sesion"])
        self.assertIsNone(row["token_sesion_hash"])
        self.assertIsNone(row["token_expira_en"])

        response = self.client.put(
            f"/api/admin/usuarios/{target_id}/estado",
            headers=self.auth(admin_token),
            json={"activo": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/mis-documentos", headers=self.auth(target_token)).status_code, 401)

    def test_superadmin_cannot_be_disabled(self):
        self.add_user("admin3", "admin")
        admin_token = self.verify(self.login("admin3"))
        response = self.client.put(
            f"/api/admin/usuarios/{self.superadmin_id}/estado",
            headers=self.auth(admin_token),
            json={"activo": False},
        )
        self.assertIn(response.status_code, (403, 401))



    def test_admin_cannot_manage_another_admin(self):
        self.add_user("admin-owner", "admin")
        target_id = self.add_user("admin-target", "admin")
        admin_token = self.verify(self.login("admin-owner"))

        response = self.client.put(
            f"/api/admin/usuarios/{target_id}",
            headers=self.auth(admin_token),
            json={"ruc": "new-ruc", "correo": "new@test.local", "nombre": "Nuevo"},
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            f"/api/admin/usuarios/{target_id}/reset-password",
            headers=self.auth(admin_token),
            json={"nueva_password": "New123!"},
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.put(
            f"/api/admin/usuarios/{target_id}/estado",
            headers=self.auth(admin_token),
            json={"activo": False},
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.put(
            f"/api/admin/usuarios/{target_id}/rol",
            headers=self.auth(admin_token),
            json={"rol": "operativo"},
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.get(
            f"/api/admin/usuarios/{target_id}/documentos",
            headers=self.auth(admin_token),
        )
        self.assertEqual(response.status_code, 403)


    def test_password_change_invalidates_existing_session(self):
        self.add_user("change-password")
        token = self.verify(self.login("change-password"))

        response = self.client.post(
            "/api/cambiar-password",
            headers=self.auth(token),
            json={"nueva_password": "Changed123!"},
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.get(
            "/api/mis-documentos",
            headers=self.auth(token),
        )
        self.assertEqual(response.status_code, 401)

        challenge = self.login("change-password", "Changed123!")
        self.assertTrue(challenge)


    def test_recovery_approval_sends_single_use_reset_link(self):
        self.add_user("recover-approved")
        self.add_user("recovery-admin", "admin")
        old_token = self.verify(self.login("recover-approved"))

        request_reset = self.client.post("/api/solicitar-reset", json={"ruc": "recover-approved-ruc"})
        self.assertEqual(request_reset.status_code, 200)

        conn = get_db()
        ticket = conn.execute(
            "SELECT id FROM tickets_recuperacion WHERE usuario_id = ?",
            (self._user_id("recover-approved"),),
        ).fetchone()
        conn.close()

        admin_token = self.verify(self.login("recovery-admin"))
        response = self.client.post(
            f"/api/admin/tickets/{ticket['id']}/aprobar",
            headers=self.auth(admin_token),
        )
        self.assertEqual(response.status_code, 200)

        self.assertEqual(
            self.client.get("/api/mis-documentos", headers=self.auth(old_token)).status_code,
            401,
        )

        conn = get_db()
        row = conn.execute(
            "SELECT estado, nueva_password FROM tickets_recuperacion WHERE id = ?",
            (ticket["id"],),
        ).fetchone()
        user = conn.execute(
            "SELECT reset_token_hash, reset_expira_en, debe_cambiar FROM usuarios WHERE usuario = ?",
            ("recover-approved",),
        ).fetchone()
        conn.close()

        self.assertEqual(row["estado"], "aprobado")
        self.assertIsNone(row["nueva_password"])
        self.assertTrue(user["reset_token_hash"])
        self.assertTrue(user["reset_expira_en"])
        self.assertEqual(user["debe_cambiar"], 0)
