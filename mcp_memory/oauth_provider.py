import json
import logging
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mcp_memory.config import DATA_DIR

logger = logging.getLogger("mcp-oauth")

ACCESS_TOKEN_TTL = 3600  # 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days
AUTH_CODE_TTL = 300  # 5 minutes

DB_PATH = DATA_DIR / "oauth.db"


def _init_db(db_path: Path) -> sqlite3.Connection:
    """Create SQLite database and tables for persistent OAuth state."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            client_id TEXT PRIMARY KEY,
            data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS access_tokens (
            token TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            scopes TEXT NOT NULL,
            expires_at INTEGER,
            resource TEXT
        );
        CREATE TABLE IF NOT EXISTS refresh_tokens (
            token TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            scopes TEXT NOT NULL,
            expires_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS client_resources (
            client_id TEXT PRIMARY KEY,
            resource TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


class PersistentOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """SQLite-backed OAuth 2.1 provider. Tokens survive server restarts."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db = _init_db(db_path)
        self._auth_codes: dict[str, AuthorizationCode] = {}  # ephemeral, short-lived
        self._cleanup_expired()

    def _cleanup_expired(self):
        """Remove expired tokens on startup."""
        now = int(time.time())
        self._db.execute("DELETE FROM access_tokens WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
        self._db.execute("DELETE FROM refresh_tokens WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
        self._db.commit()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self._db.execute("SELECT data FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate_json(row[0])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO clients (client_id, data) VALUES (?, ?)",
            (client_info.client_id, client_info.model_dump_json()),
        )
        self._db.commit()

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )

        query = urlencode(
            {
                "code": code,
                **({"state": params.state} if params.state else {}),
            }
        )
        return f"{params.redirect_uri}?{query}"

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        ac = self._auth_codes.get(authorization_code)
        if ac is None:
            return None
        if ac.client_id != client.client_id:
            return None
        if time.time() > ac.expires_at:
            self._auth_codes.pop(authorization_code, None)
            return None
        return ac

    def _save_access_token(self, token: AccessToken):
        self._db.execute(
            "INSERT OR REPLACE INTO access_tokens (token, client_id, scopes, expires_at, resource) VALUES (?, ?, ?, ?, ?)",
            (token.token, token.client_id, json.dumps(token.scopes), token.expires_at, str(token.resource) if token.resource else None),
        )

    def _save_refresh_token(self, token: RefreshToken):
        self._db.execute(
            "INSERT OR REPLACE INTO refresh_tokens (token, client_id, scopes, expires_at) VALUES (?, ?, ?, ?)",
            (token.token, token.client_id, json.dumps(token.scopes), token.expires_at),
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)

        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = time.time()

        resource = authorization_code.resource
        if resource:
            self._db.execute(
                "INSERT OR REPLACE INTO client_resources (client_id, resource) VALUES (?, ?)",
                (client.client_id, str(resource)),
            )

        logger.debug("exchange_authorization_code: client=%s, token=%s...", client.client_id, access[:8])

        at = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(now) + ACCESS_TOKEN_TTL,
            resource=resource,
        )
        rt = RefreshToken(
            token=refresh,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(now) + REFRESH_TOKEN_TTL,
        )
        self._save_access_token(at)
        self._save_refresh_token(rt)
        self._db.commit()

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(authorization_code.scopes),
            refresh_token=refresh,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        row = self._db.execute(
            "SELECT token, client_id, scopes, expires_at FROM refresh_tokens WHERE token = ?",
            (refresh_token,),
        ).fetchone()
        if row is None:
            return None
        token, client_id, scopes_json, expires_at = row
        if client_id != client.client_id:
            return None
        if expires_at and time.time() > expires_at:
            self._db.execute("DELETE FROM refresh_tokens WHERE token = ?", (refresh_token,))
            self._db.commit()
            return None
        return RefreshToken(
            token=token,
            client_id=client_id,
            scopes=json.loads(scopes_json),
            expires_at=expires_at,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self._db.execute("DELETE FROM refresh_tokens WHERE token = ?", (refresh_token.token,))

        access = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        now = time.time()

        effective_scopes = scopes if scopes else refresh_token.scopes

        row = self._db.execute(
            "SELECT resource FROM client_resources WHERE client_id = ?",
            (client.client_id,),
        ).fetchone()
        resource = row[0] if row else None

        logger.debug("exchange_refresh_token: client=%s, token=%s...", client.client_id, access[:8])

        at = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=effective_scopes,
            expires_at=int(now) + ACCESS_TOKEN_TTL,
            resource=resource,
        )
        rt = RefreshToken(
            token=new_refresh,
            client_id=client.client_id,
            scopes=effective_scopes,
            expires_at=int(now) + REFRESH_TOKEN_TTL,
        )
        self._save_access_token(at)
        self._save_refresh_token(rt)
        self._db.commit()

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(effective_scopes),
            refresh_token=new_refresh,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._db.execute(
            "SELECT token, client_id, scopes, expires_at, resource FROM access_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        if row is not None:
            tok, client_id, scopes_json, expires_at, resource = row
            if expires_at and time.time() > expires_at:
                logger.warning("load_access_token: token=%s... EXPIRED", token[:8])
                self._db.execute("DELETE FROM access_tokens WHERE token = ?", (token,))
                self._db.commit()
                return None
            logger.debug("load_access_token: token=%s... OK", token[:8])
            return AccessToken(
                token=tok,
                client_id=client_id,
                scopes=json.loads(scopes_json),
                expires_at=expires_at,
                resource=resource,
            )

        # Auto-accept unknown tokens — single-user personal server.
        # Claude Code has a bug where it caches stale tokens and ignores
        # fresh ones from re-auth, so we accept any bearer token.
        if len(token) >= 20:
            logger.info("load_access_token: auto-accepted unknown token %s...", token[:8])
            now = int(time.time())
            at = AccessToken(
                token=token,
                client_id="auto-accepted",
                scopes=["memory"],
                expires_at=now + ACCESS_TOKEN_TTL,
                resource="https://mcp.howling.one/mcp",
            )
            self._save_access_token(at)
            self._db.commit()
            return at

        logger.warning("load_access_token: token=%s... REJECTED (too short)", token[:8])
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._db.execute("DELETE FROM access_tokens WHERE token = ?", (token.token,))
        else:
            self._db.execute("DELETE FROM refresh_tokens WHERE token = ?", (token.token,))
        self._db.commit()
