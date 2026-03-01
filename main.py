import json
import logging

from mcp_memory.config import PORT
from mcp_memory.server import mcp

logging.basicConfig(level=logging.INFO)

inner_app = mcp.streamable_http_app()


async def patch_metadata_middleware(scope, receive, send):
    """ASGI middleware that patches OAuth metadata responses.

    Fixes MCP SDK issues (https://github.com/modelcontextprotocol/python-sdk/issues/1919):
    1. Pydantic AnyHttpUrl adds trailing slash to issuer URL
    2. token_endpoint_auth_methods_supported missing "none" for public clients
    """
    if scope["type"] != "http":
        await inner_app(scope, receive, send)
        return

    path = scope.get("path", "")
    needs_patch = path in (
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource/mcp",
    )

    if not needs_patch:
        await inner_app(scope, receive, send)
        return

    response_headers = []
    response_status = 200
    body_parts = []

    async def patching_send(message):
        nonlocal response_headers, response_status
        if message["type"] == "http.response.start":
            response_status = message["status"]
            response_headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))
            if not message.get("more_body", False):
                full_body = b"".join(body_parts)
                try:
                    data = json.loads(full_body)
                    data = _patch_metadata(path, data)
                    patched = json.dumps(data).encode()
                except Exception:
                    patched = full_body

                new_headers = []
                for name, value in response_headers:
                    if name.lower() == b"content-length":
                        new_headers.append((name, str(len(patched)).encode()))
                    else:
                        new_headers.append((name, value))

                await send({
                    "type": "http.response.start",
                    "status": response_status,
                    "headers": new_headers,
                })
                await send({
                    "type": "http.response.body",
                    "body": patched,
                })

    await inner_app(scope, receive, patching_send)


def _patch_metadata(path: str, data: dict) -> dict:
    if path == "/.well-known/oauth-authorization-server":
        # Fix trailing slash on issuer (SDK issue #1919)
        if "issuer" in data and isinstance(data["issuer"], str):
            data["issuer"] = data["issuer"].rstrip("/")

        # Add "none" for public clients
        methods = data.get("token_endpoint_auth_methods_supported", [])
        if "none" not in methods:
            data["token_endpoint_auth_methods_supported"] = methods + ["none"]

        rev_methods = data.get("revocation_endpoint_auth_methods_supported", [])
        if rev_methods and "none" not in rev_methods:
            data["revocation_endpoint_auth_methods_supported"] = rev_methods + ["none"]

        # Fix trailing slash on endpoint URLs
        for key in ("authorization_endpoint", "token_endpoint",
                     "registration_endpoint", "revocation_endpoint"):
            if key in data and isinstance(data[key], str):
                data[key] = data[key].rstrip("/")

    elif path == "/.well-known/oauth-protected-resource/mcp":
        servers = data.get("authorization_servers", [])
        data["authorization_servers"] = [s.rstrip("/") for s in servers]

    return data


app = patch_metadata_middleware

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)
