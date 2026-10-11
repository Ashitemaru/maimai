"""Single master key and revocable sessions; no accounts or reusable browser secrets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE_NAME = "__Host-mai_session"
SESSION_SECONDS = 30 * 24 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_ATTEMPTS = 5
GLOBAL_LOGIN_ATTEMPTS = 100
SCRYPT_PARAMETERS = (16384, 8, 1)
PUBLIC_FILES = {
    "/",
    "/index.html",
    "/icon.svg",
    "/apple-touch-icon.png",
    "/icon-192.png",
    "/icon-512.png",
    "/manifest.webmanifest",
}
PUBLIC_ASSET_SUFFIXES = {".js", ".css", ".woff", ".woff2", ".ttf", ".png", ".svg", ".webp"}


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def hash_master_key(key: str) -> str:
    if not 16 <= len(key) <= 1024:
        raise ValueError("Master key must contain 16 to 1024 characters.")
    salt = secrets.token_bytes(16)
    n, r, p = SCRYPT_PARAMETERS
    digest = hashlib.scrypt(key.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${_encode(salt)}${_encode(digest)}"


@dataclass(frozen=True)
class AuthConfig:
    encoded_hash: str = ""
    salt: bytes = b""
    digest: bytes = b""

    @property
    def required(self) -> bool:
        return bool(self.encoded_hash)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.encoded_hash.encode("ascii")).hexdigest()

    @classmethod
    def from_environment(cls) -> AuthConfig:
        production = os.environ.get("MAIMAI_ENV") == "production"
        disabled = os.environ.get("MAIMAI_AUTH_DISABLED") == "1"
        encoded_hash = os.environ.get("MAIMAI_MASTER_KEY_HASH", "")
        if production and (disabled or not encoded_hash):
            raise RuntimeError("Production requires MAIMAI_MASTER_KEY_HASH and authentication.")
        if disabled or not encoded_hash:
            return cls()
        try:
            algorithm, n, r, p, salt, digest = encoded_hash.split("$")
            if algorithm != "scrypt" or (int(n), int(r), int(p)) != SCRYPT_PARAMETERS:
                raise ValueError("Unsupported key hash")
            salt_bytes, digest_bytes = _decode(salt), _decode(digest)
            if len(salt_bytes) != 16 or len(digest_bytes) != 32:
                raise ValueError("Invalid key hash")
            encoded_hash.encode("ascii")
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("MAIMAI_MASTER_KEY_HASH is not a valid scrypt hash.") from exc
        return cls(encoded_hash, salt_bytes, digest_bytes)

    def verify(self, key: str) -> bool:
        n, r, p = SCRYPT_PARAMETERS
        digest = hashlib.scrypt(key.encode("utf-8"), salt=self.salt, n=n, r=r, p=p, dklen=32)
        return hmac.compare_digest(self.digest, digest)


class MasterKeyInput(BaseModel):
    model_config = {"extra": "forbid"}
    master_key: str = Field(min_length=1, max_length=1024, strict=True)


class AuthStore:
    def __init__(self, config: AuthConfig, runtime: Path):
        self.config = config
        self.path = runtime / "auth.sqlite3"
        if not config.required:
            return
        # Create privately before sqlite opens the file; the service also uses umask 0077.
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        with self.database() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    key_fingerprint TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_attempts (
                    id INTEGER PRIMARY KEY,
                    client_hash TEXT NOT NULL,
                    attempted_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS login_attempt_client
                    ON login_attempts(client_hash, attempted_at);
            """)

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def authenticated(self, request: Request) -> bool:
        if not self.config.required:
            return True
        token = request.cookies.get(COOKIE_NAME, "")
        if len(token) != 43:
            return False
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.database() as db:
            row = db.execute(
                "SELECT key_fingerprint, expires_at FROM sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return bool(row and row[0] == self.config.fingerprint and row[1] > int(time.time()))

    def reserve_attempt(self, client: str) -> int:
        """Reserve before running scrypt so parallel guesses share one persistent budget."""
        timestamp = int(time.time())
        client_hash = hashlib.sha256(client.encode("utf-8")).hexdigest()
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM login_attempts WHERE attempted_at <= ?",
                (timestamp - LOGIN_WINDOW_SECONDS,),
            )
            for where, values, limit in (
                ("WHERE client_hash = ?", (client_hash,), LOGIN_ATTEMPTS),
                ("", (), GLOBAL_LOGIN_ATTEMPTS),
            ):
                count, earliest = db.execute(
                    f"SELECT COUNT(*), MIN(attempted_at) FROM login_attempts {where}", values
                ).fetchone()
                if count >= limit:
                    return max(1, earliest + LOGIN_WINDOW_SECONDS - timestamp)
            db.execute(
                "INSERT INTO login_attempts(client_hash, attempted_at) VALUES (?, ?)",
                (client_hash, timestamp),
            )
        return 0

    def create_session(self, client: str, old_token: str) -> str:
        token = _encode(secrets.token_bytes(32))
        timestamp = int(time.time())
        with self.database() as db:
            db.execute(
                "DELETE FROM sessions WHERE expires_at <= ? OR key_fingerprint != ?",
                (timestamp, self.config.fingerprint),
            )
            db.execute(
                "DELETE FROM sessions WHERE token_hash = ?",
                (hashlib.sha256(old_token.encode("utf-8")).hexdigest(),),
            )
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (
                    hashlib.sha256(token.encode("ascii")).hexdigest(),
                    self.config.fingerprint,
                    timestamp + SESSION_SECONDS,
                ),
            )
            db.execute(
                "DELETE FROM login_attempts WHERE client_hash = ?",
                (hashlib.sha256(client.encode("utf-8")).hexdigest(),),
            )
        return token

    def revoke(self, token: str):
        if self.config.required:
            with self.database() as db:
                db.execute(
                    "DELETE FROM sessions WHERE token_hash = ?",
                    (hashlib.sha256(token.encode("utf-8")).hexdigest(),),
                )


class LoginBodyLimitMiddleware:
    """Bound login bodies even when transfer encoding provides no Content-Length."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if not (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"] == "/api/auth/login"
        ):
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > 8192:
                response = JSONResponse(
                    {"detail": "登录请求过大。"},
                    status_code=413,
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def public_paths(frontend: Path) -> frozenset[str]:
    """Freeze the exact compiled bundle paths; no broad static URL prefix bypass."""
    paths = set(PUBLIC_FILES)
    asset_directory = frontend / "assets"
    if asset_directory.is_dir():
        root = frontend.resolve()
        for path in asset_directory.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.suffix in PUBLIC_ASSET_SUFFIXES:
                if path.resolve().is_relative_to(root):
                    paths.add("/" + path.relative_to(frontend).as_posix())
    return frozenset(paths)


def local_probe(request: Request) -> bool:
    return (
        request.method in ("GET", "HEAD")
        and request.url.path in ("/api/health", "/api/ready")
        and request.client is not None
        and request.client.host in ("127.0.0.1", "::1")
        and request.url.hostname in ("127.0.0.1", "localhost", "::1")
        and not any(
            name.lower().startswith(("forwarded", "x-forwarded-")) for name in request.headers
        )
    )


def auth_router(auth: AuthStore) -> APIRouter:
    router = APIRouter(prefix="/api/auth")

    def state(authenticated: bool):
        return {"authenticated": authenticated, "auth_required": auth.config.required}

    @router.get("/session")
    def session(request: Request):
        return state(auth.authenticated(request))

    @router.post("/login")
    def login(value: MasterKeyInput, request: Request):
        if not auth.config.required:
            return state(True)
        client = request.client.host if request.client else "unknown"
        retry_after = auth.reserve_attempt(client)
        if retry_after:
            return JSONResponse(
                {"detail": "尝试次数过多，请稍后再试。"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        if not auth.config.verify(value.master_key):
            return JSONResponse({"detail": "Master key 不正确。"}, status_code=401)
        token = auth.create_session(client, request.cookies.get(COOKIE_NAME, ""))
        response = JSONResponse(state(True))
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    @router.post("/logout")
    def logout(request: Request):
        auth.revoke(request.cookies.get(COOKIE_NAME, ""))
        response = JSONResponse(state(not auth.config.required))
        response.delete_cookie(COOKIE_NAME, httponly=True, secure=True, samesite="lax", path="/")
        return response

    return router


def main():
    if sys.argv[1:] != ["hash"]:
        raise SystemExit("Usage: python -m backend.auth hash < private-key-file")
    key = sys.stdin.read(4097).rstrip("\r\n")
    print(hash_master_key(key))


if __name__ == "__main__":
    main()
