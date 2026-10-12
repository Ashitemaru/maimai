"""Master-key authentication, persistent sessions, and the public/static boundary."""

import asyncio
import json
import os
import stat
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.auth import (
    COOKIE_NAME,
    LOGIN_ATTEMPTS,
    AuthConfig,
    LoginBodyLimitMiddleware,
    hash_master_key,
)
from backend.store import Store
from backend.test_app import FakeUpdater
from tests.fixtures.catalog import write_catalog
from tests.support import create_app

MASTER_KEY = "test-only-master-key-not-a-production-secret"
MASTER_KEY_HASH = hash_master_key(MASTER_KEY)


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = write_catalog(self.root / "catalog")
        self.store = Store(runtime=self.root / "runtime", catalog_dir=self.catalog)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        (self.assets / "private.png").write_bytes(b"private cover")
        self.frontend = self.root / "frontend"
        (self.frontend / "assets").mkdir(parents=True)
        for name in ("index.html", "manifest.webmanifest", "icon.svg", "assets/bundle.js"):
            (self.frontend / name).write_text("public fixture")
        (self.frontend / "assets/secret.json").write_text("private fixture")
        (self.frontend / "assets/bundle.js.map").write_text("private source map")
        self.environment = patch.dict(
            os.environ,
            {
                "MAIMAI_ENV": "test",
                "MAIMAI_AUTH_DISABLED": "0",
                "MAIMAI_MASTER_KEY_HASH": MASTER_KEY_HASH,
                "MAIMAI_ASSETS_DIR": str(self.assets),
                "MAIMAI_FRONTEND_DIR": str(self.frontend),
            },
        )
        self.environment.start()
        self.app = create_app(self.store, updater=FakeUpdater())
        self.client = self.client_for()

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        self.temp.cleanup()

    def client_for(self, app=None, host="testserver", client_ip="203.0.113.20"):
        return TestClient(app or self.app, base_url=f"https://{host}", client=(client_ip, 12345))

    def login(self, key=MASTER_KEY, client=None, **kwargs):
        return (client or self.client).post("/api/auth/login", json={"master_key": key}, **kwargs)

    def test_venue_location_save_requires_session_and_same_origin(self):
        payload = {"name": "synthetic venue", "address": "synthetic address"}
        self.assertEqual(self.client.put("/api/venues/location", json=payload).status_code, 401)
        self.assertEqual(self.login().status_code, 200)
        response = self.client.put(
            "/api/venues/location", json=payload, headers={"Origin": "https://foreign.invalid"}
        )
        self.assertEqual(response.status_code, 403)
        with self.store.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM venue_locations").fetchone()[0], 0)

    def test_public_login_shell_and_exact_bundle_paths_do_not_unlock_private_routes(self):
        for path in ("/", "/index.html", "/assets/bundle.js", "/manifest.webmanifest", "/icon.svg"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        for path in (
            "/api/summary",
            "/api/venues/summary",
            "/api/venues/map",
            "/api/venues/charts?name=synthetic",
            "/api/venues/search?q=synthetic",
            "/api/catalog/data",
            "/api/health",
            "/api/ready",
            "/cover-assets/private.png",
            "/jackets/30.png",
            "/api/completion-exports/synthetic/download",
            "/assets/secret.json",
            "/assets/bundle.js.map",
            "/assets/%2e%2e/private.json",
            "/docs",
            "/openapi.json",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertNotIn("www-authenticate", response.headers)
        self.assertEqual(
            self.client.get("/api/auth/session").json(),
            {
                "authenticated": False,
                "auth_required": True,
            },
        )

    def test_valid_key_creates_secure_cookie_and_only_hashed_persistent_token(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"authenticated": True, "auth_required": True})
        cookie = response.headers["set-cookie"]
        for value in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=2592000"):
            self.assertIn(value, cookie)
        self.assertNotIn("Domain=", cookie)
        self.assertNotIn(MASTER_KEY, response.text)
        self.assertNotIn(MASTER_KEY_HASH, response.text)
        self.assertEqual(self.client.get("/api/summary").status_code, 200)
        self.assertEqual(self.client.get("/cover-assets/private.png").status_code, 200)
        database = self.app.state.auth.path
        self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
        contents = database.read_bytes()
        self.assertNotIn(self.client.cookies[COOKIE_NAME].encode(), contents)
        self.assertNotIn(MASTER_KEY.encode(), contents)
        self.assertNotIn(MASTER_KEY_HASH.encode(), contents)

    def test_wrong_key_and_malformed_payload_never_echo_secrets(self):
        rejected = "private-synthetic-invalid-key"
        response = self.login(rejected)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(rejected, response.text)
        for value in (123, [], {"private": rejected}, rejected * 100):
            response = self.client.post("/api/auth/login", json={"master_key": value})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(rejected, response.text)
        self.assertFalse(self.client.cookies)

    def test_expired_tampered_and_unknown_cookies_do_not_authenticate(self):
        self.login()
        token = self.client.cookies[COOKIE_NAME]
        self.client.cookies.clear()
        for token_value in (token[:-1] + ("a" if token[-1] != "a" else "b"), "x" * 43, "short"):
            response = self.client.get(
                "/api/summary", headers={"Cookie": f"{COOKIE_NAME}={token_value}"}
            )
            self.assertEqual(response.status_code, 401)
        with self.app.state.auth.database() as db:
            db.execute("UPDATE sessions SET expires_at = ?", (int(time.time()) - 1,))
        self.assertEqual(
            self.client.get(
                "/api/summary", headers={"Cookie": f"{COOKIE_NAME}={token}"}
            ).status_code,
            401,
        )

    def test_logout_revokes_token_and_replay_cannot_restore_it(self):
        self.login()
        token = self.client.cookies[COOKIE_NAME]
        response = self.client.post("/api/auth/logout")
        self.assertEqual(response.json(), {"authenticated": False, "auth_required": True})
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertEqual(
            self.client.get(
                "/api/summary", headers={"Cookie": f"{COOKIE_NAME}={token}"}
            ).status_code,
            401,
        )
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)

    def test_session_survives_application_restart_and_is_invalidated_by_key_rotation(self):
        self.login()
        token = self.client.cookies[COOKIE_NAME]
        restarted = create_app(self.store, updater=FakeUpdater())
        with self.client_for(restarted) as other:
            headers = {"Cookie": f"{COOKIE_NAME}={token}"}
            self.assertEqual(other.get("/api/summary", headers=headers).status_code, 200)
        with patch.dict(
            os.environ, {"MAIMAI_MASTER_KEY_HASH": hash_master_key("new-test-only-key")}
        ):
            rotated = create_app(self.store, updater=FakeUpdater())
            with self.client_for(rotated) as other:
                self.assertEqual(other.get("/api/summary", headers=headers).status_code, 401)
                self.assertEqual(self.login(client=other).status_code, 401)
                self.assertEqual(self.login("new-test-only-key", client=other).status_code, 200)

    def test_login_rotates_existing_token_without_revoking_other_devices(self):
        self.login()
        old = self.client.cookies[COOKIE_NAME]
        with self.client_for(client_ip="203.0.113.21") as phone:
            self.login(client=phone)
            self.login()
            self.assertNotEqual(old, self.client.cookies[COOKIE_NAME])
            self.assertEqual(phone.get("/api/summary").status_code, 200)
            self.assertEqual(
                phone.get("/api/summary", headers={"Cookie": f"{COOKIE_NAME}={old}"}).status_code,
                401,
            )

    def test_login_attempt_limit_is_persistent_and_ignores_forged_forwarded_headers(self):
        for _ in range(LOGIN_ATTEMPTS):
            self.assertEqual(self.login("wrong-key").status_code, 401)
        response = self.login(headers={"X-Forwarded-For": "203.0.113.30"})
        self.assertEqual(response.status_code, 429)
        self.assertLessEqual(int(response.headers["retry-after"]), 900)
        restarted = create_app(self.store, updater=FakeUpdater())
        with self.client_for(restarted) as other:
            self.assertEqual(self.login(client=other).status_code, 429)
        with self.client_for(client_ip="203.0.113.21") as other:
            self.assertEqual(self.login(client=other).status_code, 200)
        with self.app.state.auth.database() as db:
            db.execute("UPDATE login_attempts SET attempted_at = ?", (int(time.time()) - 901,))
        self.assertEqual(self.login().status_code, 200)

    def test_concurrent_guesses_cannot_race_past_attempt_budget(self):
        def guess(_):
            with self.client_for() as client:
                return self.login("wrong-key", client=client).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(guess, range(8)))
        self.assertEqual(statuses.count(401), LOGIN_ATTEMPTS)
        self.assertEqual(statuses.count(429), 8 - LOGIN_ATTEMPTS)

    def test_login_logout_and_mutations_reject_cross_origin_or_cross_site_requests(self):
        for headers in (
            {"Origin": "https://foreign.example"},
            {"Origin": "http://testserver"},
            {"Origin": "null"},
            {"Origin": "https://[malformed"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.login(headers=headers).status_code, 403)
                self.assertEqual(
                    self.client.post("/api/auth/logout", headers=headers).status_code, 403
                )
        self.assertEqual(self.login(headers={"Origin": "https://testserver"}).status_code, 200)
        self.assertEqual(
            self.client.put(
                "/api/settings/preferences",
                json={"default_constant_min": 13.0, "default_constant_max": 13.9},
                headers={"Sec-Fetch-Site": "cross-site"},
            ).status_code,
            403,
        )
        self.assertTrue(self.client.get("/api/auth/session").json()["authenticated"])

    def test_unauthenticated_cors_preflight_cannot_open_private_api(self):
        response = self.client.options(
            "/api/summary",
            headers={
                "Origin": "https://foreign.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_only_direct_loopback_with_local_host_can_read_deployment_probes(self):
        with self.client_for(host="127.0.0.1", client_ip="127.0.0.1") as local:
            self.assertEqual(local.get("/api/health").status_code, 200)
            self.assertEqual(local.get("/api/ready").status_code, 200)
            for name in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto"):
                self.assertEqual(
                    local.get("/api/health", headers={name: "synthetic"}).status_code, 401
                )
            self.assertEqual(local.get("/api/summary").status_code, 401)
        with self.client_for(client_ip="127.0.0.1") as wrong_host:
            self.assertEqual(wrong_host.get("/api/ready").status_code, 401)
        with self.client_for(host="127.0.0.1") as remote:
            self.assertEqual(remote.get("/api/ready").status_code, 401)

    def test_oversized_login_with_and_without_content_length_is_rejected(self):
        body = json.dumps({"master_key": "x" * 9000})
        response = self.client.post(
            "/api/auth/login", content=body, headers={"Content-Type": "application/json"}
        )
        self.assertEqual(response.status_code, 413)
        response = self.client.post(
            "/api/auth/login", content=iter([body[:4500].encode(), body[4500:].encode()])
        )
        self.assertEqual(response.status_code, 413)
        self.assertNotIn("x" * 10, response.text)

    def test_body_limit_stops_reading_before_downstream_or_additional_chunks(self):
        async def run():
            messages = []
            reads = 0

            async def downstream(*args):
                self.fail("Oversized login reached request parser")

            async def receive():
                nonlocal reads
                reads += 1
                self.assertEqual(reads, 1)
                return {"type": "http.request", "body": b"x" * 8193, "more_body": True}

            async def send(message):
                messages.append(message)

            middleware = LoginBodyLimitMiddleware(downstream)
            await middleware(
                {"type": "http", "method": "POST", "path": "/api/auth/login"}, receive, send
            )
            self.assertEqual(messages[0]["status"], 413)

        asyncio.run(run())

    def test_production_cannot_disable_auth_or_start_with_missing_or_invalid_hash(self):
        for config in (
            {"MAIMAI_MASTER_KEY_HASH": ""},
            {"MAIMAI_AUTH_DISABLED": "1"},
            {"MAIMAI_MASTER_KEY_HASH": "scrypt$999999999$8$1$salt$hash"},
        ):
            with patch.dict(os.environ, {"MAIMAI_ENV": "production", **config}):
                with self.assertRaises(RuntimeError):
                    create_app(self.store, updater=FakeUpdater())
        with patch.dict(os.environ, {"MAIMAI_AUTH_DISABLED": "1"}):
            disabled = create_app(self.store, updater=FakeUpdater())
            with self.client_for(disabled) as client:
                self.assertEqual(client.get("/api/summary").status_code, 200)
                self.assertEqual(
                    client.get("/api/auth/session").json(),
                    {
                        "authenticated": True,
                        "auth_required": False,
                    },
                )

    def test_invalid_encoded_hashes_never_include_secret_config_in_error(self):
        for encoded in ("synthetic-secret", MASTER_KEY_HASH.replace("scrypt", "pbkdf2")):
            with patch.dict(os.environ, {"MAIMAI_MASTER_KEY_HASH": encoded}):
                with self.assertRaises(RuntimeError) as error:
                    AuthConfig.from_environment()
                self.assertNotIn(encoded, str(error.exception))


if __name__ == "__main__":
    unittest.main()
