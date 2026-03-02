"""Thin MCP JSON-RPC HTTP client.

Calls MCP tools directly via HTTP — no LLM involved.
Handles OAuth 2.1 session initialization and token refresh.
"""

import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

logger = logging.getLogger("mcp-client")

# Persist tokens across restarts
TOKEN_CACHE_DIR = Path(os.environ.get("MCP_TOKEN_DIR", Path.home() / ".cache" / "mcp-agent"))


class MCPClient:
    """Thin wrapper that calls MCP tools via JSON-RPC over HTTP."""

    def __init__(self, base_url: str, token_name: str = "default"):
        self.base_url = base_url.rstrip("/")
        self.mcp_endpoint = f"{self.base_url}/mcp"
        self._session_id: str | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: float = 0
        self._client_id: str | None = None
        self._client: httpx.Client = httpx.Client(timeout=30.0)
        self._msg_id = 0
        self._token_file = TOKEN_CACHE_DIR / f"{token_name}.json"
        self._load_cached_token()

    def _load_cached_token(self):
        if self._token_file.exists():
            try:
                data = json.loads(self._token_file.read_text())
                self._access_token = data.get("access_token")
                self._refresh_token = data.get("refresh_token")
                self._token_expires_at = data.get("expires_at", 0)
                self._client_id = data.get("client_id")
                self._session_id = data.get("session_id")
            except (json.JSONDecodeError, OSError):
                pass

    def _save_cached_token(self):
        TOKEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._token_file.write_text(json.dumps({
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._token_expires_at,
            "client_id": self._client_id,
            "session_id": self._session_id,
        }))
        self._token_file.chmod(0o600)

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _ensure_auth(self):
        """Run OAuth 2.1 flow if we don't have a valid token."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return

        # Try refresh first
        if self._refresh_token and self._client_id:
            if self._try_refresh():
                return

        # Full OAuth flow
        self._oauth_flow()

    def _get_oauth_metadata(self) -> dict:
        resp = self._client.get(f"{self.base_url}/.well-known/oauth-authorization-server")
        resp.raise_for_status()
        return resp.json()

    def _try_refresh(self) -> bool:
        try:
            meta = self._get_oauth_metadata()
            resp = self._client.post(
                meta["token_endpoint"],
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                },
            )
            if resp.status_code != 200:
                return False
            token_data = resp.json()
            self._access_token = token_data["access_token"]
            self._refresh_token = token_data.get("refresh_token", self._refresh_token)
            self._token_expires_at = time.time() + token_data.get("expires_in", 3600)
            self._save_cached_token()
            return True
        except Exception:
            return False

    def _oauth_flow(self):
        """Full OAuth 2.1 authorization code flow with PKCE."""
        meta = self._get_oauth_metadata()

        # Register client
        reg_resp = self._client.post(
            meta["registration_endpoint"],
            json={
                "client_name": "mcp-agent-daemon",
                "redirect_uris": ["http://localhost:19876/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": "memory",
            },
        )
        reg_resp.raise_for_status()
        reg = reg_resp.json()
        self._client_id = reg["client_id"]

        # PKCE
        code_verifier = secrets.token_urlsafe(64)
        import base64
        challenge_bytes = hashlib.sha256(code_verifier.encode()).digest()
        code_challenge = base64.urlsafe_b64encode(challenge_bytes).rstrip(b"=").decode()

        # Authorize — the server auto-approves and redirects
        auth_params = urlencode({
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": "http://localhost:19876/callback",
            "scope": "memory",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "resource": f"{self.base_url}/mcp",
        })
        auth_resp = self._client.get(
            f"{meta['authorization_endpoint']}?{auth_params}",
            follow_redirects=False,
        )

        # Extract code from redirect Location header
        location = auth_resp.headers.get("location", "")
        parsed = urlparse(location)
        qs = parse_qs(parsed.query)
        code = qs.get("code", [None])[0]
        if not code:
            raise RuntimeError(f"OAuth authorize failed: no code in redirect. Status={auth_resp.status_code}, Location={location}")

        # Exchange code for token
        token_resp = self._client.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://localhost:19876/callback",
                "client_id": self._client_id,
                "code_verifier": code_verifier,
            },
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()

        self._access_token = token_data["access_token"]
        self._refresh_token = token_data.get("refresh_token")
        self._token_expires_at = time.time() + token_data.get("expires_in", 3600)
        self._save_cached_token()
        logger.info("OAuth flow complete, token acquired")

    def _init_session(self):
        """Initialize an MCP session if we don't have one."""
        if self._session_id:
            return
        self._ensure_auth()
        resp = self._client.post(
            self.mcp_endpoint,
            json={
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-agent-daemon", "version": "0.1.0"},
                },
            },
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )

        if resp.status_code in (401, 404):
            # Token/session stale (e.g. server restarted), re-auth and retry
            logger.info("Session init got %d, re-authenticating", resp.status_code)
            self._access_token = None
            self._refresh_token = None
            self._ensure_auth()
            resp = self._client.post(
                self.mcp_endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "mcp-agent-daemon", "version": "0.1.0"},
                    },
                },
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )

        resp.raise_for_status()

        self._session_id = resp.headers.get("mcp-session-id")

        # Parse SSE response to get the JSON-RPC result
        self._parse_sse_result(resp.text)

        # Send initialized notification
        self._client.post(
            self.mcp_endpoint,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
            headers=self._headers(),
        )

    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _parse_sse_result(self, text: str) -> dict | None:
        """Parse SSE response body and extract JSON-RPC result."""
        for line in text.splitlines():
            if line.startswith("data: "):
                data = line[6:]
                try:
                    parsed = json.loads(data)
                    return parsed.get("result")
                except json.JSONDecodeError:
                    continue
        return None

    def _call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call an MCP tool and return the text result."""
        self._ensure_auth()
        self._init_session()

        resp = self._client.post(
            self.mcp_endpoint,
            json={
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments,
                },
            },
            headers=self._headers(),
        )

        if resp.status_code in (401, 404):
            # Token/session expired (404 = server lost session), re-auth and retry
            self._access_token = None
            self._session_id = None
            self._ensure_auth()
            self._init_session()
            resp = self._client.post(
                self.mcp_endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                },
                headers=self._headers(),
            )

        resp.raise_for_status()
        result = self._parse_sse_result(resp.text)
        if result is None:
            raise RuntimeError(f"No result in response: {resp.text[:500]}")

        # MCP tool results are in result.content[0].text
        is_error = result.get("isError", False)
        content = result.get("content", [])
        text = content[0].get("text", "") if content and isinstance(content, list) else str(result)
        if is_error:
            raise RuntimeError(f"MCP tool '{tool_name}' returned error: {text}")
        return text

    # ── Convenience methods ──────────────────────────────────────────

    def list(self, prefix: str = "") -> str:
        return self._call_tool("list_memories", {"prefix": prefix})

    def read(self, filename: str) -> str:
        return self._call_tool("read_memory", {"filename": filename})

    def write(self, filename: str, content: str) -> str:
        return self._call_tool("write_memory", {"filename": filename, "content": content})

    def edit(self, filename: str, old_text: str, new_text: str) -> str:
        return self._call_tool("edit_memory", {"filename": filename, "old_text": old_text, "new_text": new_text})

    def search(self, query: str) -> str:
        return self._call_tool("search_memories", {"query": query})

    def delete(self, filename: str) -> str:
        return self._call_tool("delete_memory", {"filename": filename})

    def create_task(self, target: str, request: str, **kwargs) -> str:
        args = {"target": target, "request": request, **kwargs}
        return self._call_tool("create_task", args)

    def notify_agent(self, agent_id: str, task_id: str) -> str:
        return self._call_tool("notify_agent", {"agent_id": agent_id, "task_id": task_id})

    def await_task(self, task_id: str, timeout: int = 120) -> str:
        return self._call_tool("await_task", {"task_id": task_id, "timeout": timeout})

    def close(self):
        self._client.close()
