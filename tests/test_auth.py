"""Master-key login, persistent sessions, and the public/private boundary."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.auth import COOKIE_NAME, LOGIN_ATTEMPTS, hash_master_key
from backend.store import Store
from tests.fixtures.catalog import write_catalog
from tests.support import FakeUpdater, create_app

MASTER_KEY = "test-only-master-key-not-a-production-secret"
MASTER_KEY_HASH = hash_master_key(MASTER_KEY)


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.store = Store(runtime=root / "runtime", catalog_dir=write_catalog(root / "catalog"))
        assets, frontend = root / "assets", root / "frontend"
        assets.mkdir()
        (assets / "private.png").write_bytes(b"private cover")
        (frontend / "assets").mkdir(parents=True)
        for name in ("index.html", "assets/bundle.js"):
            (frontend / name).write_text("public fixture")
        environment = {
            "MAIMAI_ENV": "test",
            "MAIMAI_MASTER_KEY_HASH": MASTER_KEY_HASH,
            "MAIMAI_ASSETS_DIR": str(assets),
            "MAIMAI_FRONTEND_DIR": str(frontend),
        }
        self.enterContext(patch.dict(os.environ, environment))
        self.app = create_app(self.store, updater=FakeUpdater())
        self.client = self.client_for(self.app)

    def client_for(self, app, client_ip="203.0.113.20"):
        client = TestClient(app, base_url="https://testserver", client=(client_ip, 12345))
        self.addCleanup(client.close)
        return client

    def login(self, key=MASTER_KEY, client=None, **kwargs):
        return (client or self.client).post("/api/auth/login", json={"master_key": key}, **kwargs)

    def test_private_routes_require_login_but_the_login_shell_is_public(self):
        for path in ("/", "/assets/bundle.js"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        for path in ("/api/summary", "/cover-assets/private.png", "/api/ready", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        put = self.client.put("/api/settings/divingfish", json={"token": "x"})
        self.assertEqual(put.status_code, 401)
        self.assertFalse(self.client.get("/api/auth/session").json()["authenticated"])

    def test_valid_key_sets_secure_session_cookie_until_logout(self):
        response = self.login()
        self.assertEqual(response.json(), {"authenticated": True, "auth_required": True})
        for value in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
            self.assertIn(value, response.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/summary").status_code, 200)
        token = self.client.cookies[COOKIE_NAME]
        stored = self.app.state.auth.path.read_bytes()
        self.assertNotIn(token.encode(), stored)
        self.assertNotIn(MASTER_KEY.encode(), stored)
        # The session survives an app restart.
        restarted = self.client_for(create_app(self.store, updater=FakeUpdater()))
        cookie = {"Cookie": f"{COOKIE_NAME}={token}"}
        self.assertEqual(restarted.get("/api/summary", headers=cookie).status_code, 200)
        self.client.post("/api/auth/logout")
        self.assertEqual(restarted.get("/api/summary", headers=cookie).status_code, 401)

    def test_wrong_tampered_or_expired_credentials_are_rejected(self):
        self.assertEqual(self.login("wrong-key").status_code, 401)
        self.assertFalse(self.client.cookies)
        self.login()
        token = self.client.cookies[COOKIE_NAME]
        self.client.cookies.clear()
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        for value in (tampered, "short"):
            headers = {"Cookie": f"{COOKIE_NAME}={value}"}
            self.assertEqual(self.client.get("/api/summary", headers=headers).status_code, 401)
        with self.app.state.auth.database() as db:
            db.execute("UPDATE sessions SET expires_at = ?", (int(time.time()) - 1,))
        headers = {"Cookie": f"{COOKIE_NAME}={token}"}
        self.assertEqual(self.client.get("/api/summary", headers=headers).status_code, 401)

    def test_guessing_is_rate_limited_per_client_and_ignores_forwarded_headers(self):
        for _ in range(LOGIN_ATTEMPTS):
            self.assertEqual(self.login("wrong-key").status_code, 401)
        limited = self.login(headers={"X-Forwarded-For": "203.0.113.30"})
        self.assertEqual(limited.status_code, 429)
        other = self.client_for(self.app, client_ip="203.0.113.21")
        self.assertEqual(self.login(client=other).status_code, 200)

    def test_cross_site_login_and_mutations_are_rejected(self):
        for headers in ({"Origin": "https://foreign.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.login(headers=headers).status_code, 403)
        self.login()
        response = self.client.put(
            "/api/settings/preferences",
            json={"default_constant_min": 13.0, "default_constant_max": 13.9},
            headers={"Origin": "https://foreign.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_production_refuses_to_start_without_a_valid_key_hash(self):
        for value in ("", "scrypt$999999999$8$1$salt$hash"):
            environment = {"MAIMAI_ENV": "production", "MAIMAI_MASTER_KEY_HASH": value}
            with patch.dict(os.environ, environment), self.assertRaises(RuntimeError):
                create_app(self.store, updater=FakeUpdater())


if __name__ == "__main__":
    unittest.main()
