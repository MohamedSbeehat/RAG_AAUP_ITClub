"""Authenticated organization boundaries: separate files, SQLite databases, and Qdrant namespaces."""

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from rag_aaup.academic.sync import AcademicReferences
from rag_aaup.config import Settings

COOKIE = "campusiq_session"


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    tenant_id: str
    company_name: str
    knowledge_mode: str


class Accounts:
    def __init__(self, path: Path, session_hours: int = 12):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.session_hours = path, session_hours
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tenants(
                    tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL, knowledge_mode TEXT NOT NULL,
                    academic_enabled INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS users(
                    user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
                    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id));
                CREATE TABLE IF NOT EXISTS sessions(
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id), expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_log(
                    event_id INTEGER PRIMARY KEY, tenant_id TEXT, user_id TEXT, action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at REAL NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _password(password: str, salt: bytes) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000).hex()

    def register(self, username: str, password: str, company: str, mode: str = "organization") -> Principal:
        tenant_id, user_id = str(uuid4()), str(uuid4())
        salt = secrets.token_bytes(16)
        encoded = salt.hex() + "$" + self._password(password, salt)
        with self.connect() as db:
            db.execute(
                "INSERT INTO tenants(tenant_id,name,knowledge_mode,created_at) VALUES(?,?,?,?)",
                (tenant_id, company, mode, time.time()),
            )
            db.execute(
                "INSERT INTO users VALUES(?,?,?,?)", (user_id, username.casefold(), encoded, tenant_id)
            )
        principal = Principal(user_id, username.casefold(), tenant_id, company, mode)
        self.audit(principal, "company_created", "")
        return principal

    def login(self, username: str, password: str) -> Principal | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT u.*,t.name,t.knowledge_mode FROM users u JOIN tenants t USING(tenant_id) WHERE username=?",
                (username.casefold(),),
            ).fetchone()
        encoded = row["password_hash"] if row else "00" * 16 + "$" + "00" * 32
        salt, expected = encoded.split("$", 1)
        valid = hmac.compare_digest(self._password(password, bytes.fromhex(salt)), expected)
        if row and valid:
            return Principal(
                row["user_id"], row["username"], row["tenant_id"], row["name"], row["knowledge_mode"]
            )
        return None

    def session(self, principal: Principal) -> str:
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    principal.user_id,
                    time.time() + self.session_hours * 3600,
                ),
            )
        return token

    def authenticate(self, token: str) -> Principal | None:
        if not token or len(token) > 200:
            return None
        with self.connect() as db:
            row = db.execute(
                """SELECT u.user_id,u.username,t.tenant_id,t.name,t.knowledge_mode
                FROM sessions s JOIN users u USING(user_id) JOIN tenants t USING(tenant_id)
                WHERE s.token_hash=? AND s.expires_at>?""",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        return (
            Principal(row["user_id"], row["username"], row["tenant_id"], row["name"], row["knowledge_mode"])
            if row
            else None
        )

    def logout(self, token: str) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)
            )

    def tenant(self, tenant_id: str):
        with self.connect() as db:
            return db.execute("SELECT * FROM tenants WHERE tenant_id=?", (tenant_id,)).fetchone()

    def set_academic(self, tenant_id: str, enabled: bool) -> None:
        with self.connect() as db:
            db.execute("UPDATE tenants SET academic_enabled=? WHERE tenant_id=?", (int(enabled), tenant_id))

    def audit(self, principal: Principal, action: str, detail: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit_log(tenant_id,user_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                (principal.tenant_id, principal.user_id, action, detail[:1000], time.time()),
            )


class TenantRegistry:
    def __init__(self, settings: Settings, factory):
        self.settings, self.factory = settings, factory
        self.accounts = Accounts(settings.workspace_root / "accounts.sqlite3", settings.session_hours)
        self.references = AcademicReferences()
        self._runtimes = {}
        self._lock = threading.RLock()
        self._auth_attempts: dict[str, list[float]] = {}

    def allow_auth(self, address: str) -> bool:
        return self._allow("auth:" + address, 12)

    def allow_query(self, tenant_id: str) -> bool:
        return self._allow("query:" + tenant_id, 30)

    def _allow(self, key: str, limit: int) -> bool:
        now = time.monotonic()
        with self._lock:
            attempts = [when for when in self._auth_attempts.get(key, []) if now - when < 60]
            if len(attempts) >= limit:
                return False
            self._auth_attempts[key] = attempts + [now]
            return True

    def runtime(self, tenant_id: str):
        with self._lock:
            if tenant_id not in self._runtimes:
                row = self.accounts.tenant(tenant_id)
                if row is None:
                    raise ValueError("Unknown company")
                folder = self.settings.workspace_root.resolve() / tenant_id
                settings = self.settings.model_copy(
                    update={
                        "raw_dir": folder / "documents",
                        "database_path": folder / "rag.sqlite3",
                        "qdrant_path": folder / "qdrant",
                        "app_password": None,
                        "organization_name": row["name"],
                        "knowledge_mode": row["knowledge_mode"],
                        "assistant_name": "مساعد مستندات " + row["name"],
                    }
                )
                runtime = self.factory(settings)
                runtime.indexer.sync()
                self._runtimes[tenant_id] = runtime
            return self._runtimes[tenant_id]

    def sync_all(self):
        with self._lock:
            items = list(self._runtimes.items())
        if any(self.accounts.tenant(key)["academic_enabled"] for key, _ in items):
            self.references.sync()
        for tenant_id, runtime in items:
            with runtime.sync_guard:
                if self.accounts.tenant(tenant_id)["academic_enabled"]:
                    self.references.install(runtime.settings.raw_dir)
                runtime.indexer.sync()

    def close(self):
        with self._lock:
            for runtime in self._runtimes.values():
                runtime.database.close()
            self._runtimes.clear()
