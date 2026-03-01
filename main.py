import asyncio
import json
import logging

from datetime import datetime, timezone
from pathlib import Path
from mcp_memory.config import DATA_DIR, PORT
from mcp_memory.server import (
    mcp, agent_subscribe, agent_unsubscribe,
    monitor_subscribe, monitor_unsubscribe,
    _agent_queues,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-memory")

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


# ── SSE endpoint for agent task notifications ───────────────────────

SSE_KEEPALIVE_SECONDS = 30
HEARTBEAT_STALE_SECONDS = 600  # 2x default heartbeat interval (300s)


async def handle_sse(scope, receive, send):
    """SSE endpoint: /events/{agent_id}

    Agents connect outbound to this endpoint. The server pushes task
    notifications down the connection. No inbound ports needed on agents.
    """
    path = scope["path"].rstrip("/")
    parts = path.split("/")
    # Expect /events/{agent_id}
    if len(parts) != 3 or parts[1] != "events" or not parts[2]:
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"Not Found"})
        return

    agent_id = parts[2]
    q = agent_subscribe(agent_id)

    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"text/event-stream"),
            (b"cache-control", b"no-cache"),
            (b"x-accel-buffering", b"no"),  # tell nginx not to buffer
        ],
    })
    await send({
        "type": "http.response.body",
        "body": f": connected as {agent_id}\n\n".encode(),
        "more_body": True,
    })

    # Watch for client disconnect
    async def wait_disconnect():
        while True:
            msg = await receive()
            if msg.get("type") == "http.disconnect":
                return

    disconnect_task = asyncio.create_task(wait_disconnect())

    try:
        while True:
            queue_task = asyncio.create_task(q.get())
            done, pending = await asyncio.wait(
                {disconnect_task, queue_task},
                timeout=SSE_KEEPALIVE_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if disconnect_task in done:
                queue_task.cancel()
                return

            if queue_task in done:
                task_id = queue_task.result()
                data = json.dumps({"task_id": task_id})
                await send({
                    "type": "http.response.body",
                    "body": f"event: task\ndata: {data}\n\n".encode(),
                    "more_body": True,
                })
            else:
                # Timeout — send keepalive so proxies don't drop the connection
                queue_task.cancel()
                await send({
                    "type": "http.response.body",
                    "body": b": keepalive\n\n",
                    "more_body": True,
                })
    finally:
        disconnect_task.cancel()
        agent_unsubscribe(agent_id, q)


# ── Monitor SSE endpoint ──────────────────────────────────────────────

async def handle_monitor_sse(scope, receive, send):
    """SSE endpoint: /monitor — broadcasts all server activity."""
    q = monitor_subscribe()

    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"text/event-stream"),
            (b"cache-control", b"no-cache"),
            (b"x-accel-buffering", b"no"),
        ],
    })
    await send({
        "type": "http.response.body",
        "body": b": connected to monitor\n\n",
        "more_body": True,
    })

    async def wait_disconnect():
        while True:
            msg = await receive()
            if msg.get("type") == "http.disconnect":
                return

    disconnect_task = asyncio.create_task(wait_disconnect())

    try:
        while True:
            queue_task = asyncio.create_task(q.get())
            done, pending = await asyncio.wait(
                {disconnect_task, queue_task},
                timeout=SSE_KEEPALIVE_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if disconnect_task in done:
                queue_task.cancel()
                return

            if queue_task in done:
                event = queue_task.result()
                event_type = event.get("type", "unknown")
                data = json.dumps(event)
                await send({
                    "type": "http.response.body",
                    "body": f"event: {event_type}\ndata: {data}\n\n".encode(),
                    "more_body": True,
                })
            else:
                queue_task.cancel()
                await send({
                    "type": "http.response.body",
                    "body": b": keepalive\n\n",
                    "more_body": True,
                })
    finally:
        disconnect_task.cancel()
        monitor_unsubscribe(q)


# ── Health endpoint ────────────────────────────────────────────────

HEALTH_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCP Nodes</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 2rem; }
  h1 { color: #58a6ff; margin-bottom: 1.5rem; font-size: 1.4rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; }
  .node { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1rem; }
  .node.online { border-color: #238636; }
  .node.offline { border-color: #da3633; opacity: 0.6; }
  .node-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem; }
  .node-name { font-size: 1.1rem; font-weight: 600; color: #f0f6fc; }
  .badge { font-size: 0.75rem; padding: 2px 8px; border-radius: 12px; font-weight: 500; }
  .badge.online { background: #238636; color: #fff; }
  .badge.offline { background: #da3633; color: #fff; }
  .node-detail { font-size: 0.85rem; color: #8b949e; line-height: 1.6; }
  .node-detail span { color: #c9d1d9; }
  .refresh { color: #8b949e; font-size: 0.8rem; margin-top: 1.5rem; }
  .refresh a { color: #58a6ff; text-decoration: none; }
</style>
</head>
<body>
<h1>MCP Nodes</h1>
<div class="grid">{{NODES}}</div>
<p class="refresh">Last checked: {{TIMESTAMP}} &middot; <a href="/health">Refresh</a></p>
</body>
</html>"""

NODE_CARD = """<div class="node {status_class}">
  <div class="node-header">
    <span class="node-name">{name}</span>
    <span class="badge {status_class}">{status}</span>
  </div>
  <div class="node-detail">
    {details}
  </div>
</div>"""


def _build_health_page() -> str:
    """Build the health status HTML page."""
    # Load devices
    devices_path = DATA_DIR / "devices.json"
    devices = {}
    if devices_path.exists():
        try:
            devices = json.loads(devices_path.read_text())
        except Exception:
            pass

    # Deduplicate by name (multiple client_ids can map to same device)
    seen = {}
    for cid, entry in devices.items():
        name = entry.get("name", cid[:8])
        if name not in seen or len(entry) > len(seen[name]):
            seen[name] = entry

    connected_agents = set(_agent_queues.keys())
    now = datetime.now(timezone.utc)

    cards = []
    for name in sorted(seen.keys()):
        entry = seen[name]
        has_sse = name in connected_agents

        # Get last_seen from agent registration
        last_seen = ""
        heartbeat_ago = None
        reg_path = DATA_DIR / f"agent-reg-{name}.md"
        if reg_path.exists():
            try:
                for line in reg_path.read_text().splitlines():
                    if "- last_seen:" in line:
                        ts = line.split("- last_seen:")[1].strip()
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            heartbeat_ago = (now - dt).total_seconds()
                            if heartbeat_ago < 60:
                                last_seen = f"{int(heartbeat_ago)}s ago"
                            elif heartbeat_ago < 3600:
                                last_seen = f"{int(heartbeat_ago/60)}m ago"
                            else:
                                last_seen = f"{int(heartbeat_ago/3600)}h ago"
                        except Exception:
                            last_seen = ts
                        break
            except Exception:
                pass

        # SSE connection can linger after sleep — verify with heartbeat
        online = has_sse and (heartbeat_ago is None or heartbeat_ago < HEARTBEAT_STALE_SECONDS)
        status = "online" if online else "offline"
        status_class = status

        details = []
        aliases = entry.get("aliases", "")
        if aliases:
            details.append(f"<b>{aliases}</b>")
        model = entry.get("model", "")
        if model:
            details.append(model)
        os_name = entry.get("os", "")
        env = entry.get("env", "")
        if os_name:
            platform = os_name
            if env and env != "bare metal":
                platform += f" ({env})"
            details.append(platform)
        cpu = entry.get("cpu", "")
        if cpu:
            details.append(f"CPU: <span>{cpu}</span>")
        ram = entry.get("ram", "")
        if ram:
            details.append(f"RAM: <span>{ram}</span>")
        gpu = entry.get("gpu", "")
        if gpu and "Virtio" not in gpu:
            short_gpu = gpu.split("(")[0].strip() if len(gpu) > 40 else gpu
            details.append(f"GPU: <span>{short_gpu}</span>")
        if last_seen:
            details.append(f"Last seen: <span>{last_seen}</span>")

        cards.append(NODE_CARD.format(
            name=f"@{name}",
            status=status,
            status_class=status_class,
            details="<br>".join(details),
        ))

    timestamp = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    return HEALTH_HTML.replace("{{NODES}}", "\n".join(cards)).replace("{{TIMESTAMP}}", timestamp)


async def handle_health(scope, receive, send):
    """GET /health — HTML dashboard of node status."""
    html = _build_health_page()
    body = html.encode()
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"text/html; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({
        "type": "http.response.body",
        "body": body,
    })


# ── ASGI app ─────────────────────────────────────────────────────────

async def app(scope, receive, send):
    """Root ASGI app. Routes SSE and monitor before MCP middleware."""
    if scope["type"] == "http":
        path = scope.get("path", "")
        if path.startswith("/events/"):
            await handle_sse(scope, receive, send)
            return
        if path.rstrip("/") == "/monitor":
            await handle_monitor_sse(scope, receive, send)
            return
        if path.rstrip("/") == "/health":
            await handle_health(scope, receive, send)
            return
    await patch_metadata_middleware(scope, receive, send)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)
