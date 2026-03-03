import asyncio
import json
import logging
import re
import time
import uuid
from collections import deque
from pathlib import Path

from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from mcp_memory.config import DATA_DIR
from mcp_memory.oauth_provider import PersistentOAuthProvider

mcp = FastMCP(
    "memory",
    stateless_http=True,
    auth_server_provider=PersistentOAuthProvider(),
    auth=AuthSettings(
        issuer_url="https://mcp.howling.one",
        resource_server_url="https://mcp.howling.one/mcp",
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["memory"],
            default_scopes=["memory"],
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=["memory"],
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["mcp.howling.one", "127.0.0.1:8766", "localhost:8766"],
    ),
)

logger = logging.getLogger("mcp-memory")

FILENAME_RE = re.compile(r"^[a-zA-Z0-9_\-]+\.(md|json)$")
DEVICES_FILE = DATA_DIR / "devices.json"

def _next_event_id() -> str:
    return str(uuid.uuid4())



def _validate_filename(filename: str) -> Path:
    if not FILENAME_RE.match(filename):
        raise ValueError(
            f"Invalid filename '{filename}'. "
            "Must be alphanumeric/dashes/underscores with .md or .json extension."
        )
    path = (DATA_DIR / filename).resolve()
    if not str(path).startswith(str(DATA_DIR.resolve())):
        raise ValueError("Path traversal not allowed.")
    return path


# ── Device lookup ────────────────────────────────────────────────────

def _load_devices() -> dict:
    if DEVICES_FILE.exists():
        try:
            return json.loads(DEVICES_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_devices(devices: dict):
    DEVICES_FILE.write_text(json.dumps(devices, indent=2) + "\n")


def _resolve_device(client_id: str | None) -> str | None:
    if not client_id:
        return None
    devices = _load_devices()
    entry = devices.get(client_id)
    return entry.get("name") if entry else None


def _get_client_id(ctx: Context = None) -> str | None:
    token = get_access_token()
    if token:
        return token.client_id
    if ctx:
        return ctx.client_id
    return None


def _client_fields(ctx: Context) -> dict:
    cid = _get_client_id(ctx)
    fields = {}
    if cid:
        fields["client"] = cid
        devices = _load_devices()
        entry = devices.get(cid, {})
        if entry.get("name"):
            fields["device"] = entry["name"]
        # Determine if this is a daemon or user session
        # Daemon entries have a "daemon" field in devices.json
        fields["is_daemon"] = bool(entry.get("daemon"))
    return fields


# ── Thinking buffer ──────────────────────────────────────────────────

_thinking_buffer: dict[str, deque] = {}
THINKING_BUFFER_SIZE = 50


def store_thinking_chunk(agent_id: str, task_id: str, text: str):
    key = task_id
    if key not in _thinking_buffer:
        _thinking_buffer[key] = deque(maxlen=THINKING_BUFFER_SIZE)
    _thinking_buffer[key].append(text)
    emit_monitor_event({
        "action": "thinking",
        "agent": "@" + agent_id,
        "task": task_id,
        "content": text[:200],
    })


def get_thinking_lines(task_id: str) -> list[str]:
    buf = _thinking_buffer.get(task_id)
    if not buf:
        return []
    return list(buf)


def clear_thinking_buffer(task_id: str):
    _thinking_buffer.pop(task_id, None)


# ── Tools ────────────────────────────────────────────────────────────

@mcp.tool()
def list_memories(prefix: str = "", ctx: Context = None) -> str:
    """List all markdown memory files.

    Args:
        prefix: Optional prefix to filter filenames (e.g. "task-" or "agent-reg-")
    """
    files = sorted(
        p.name for ext in ("*.md", "*.json") for p in DATA_DIR.glob(ext)
    )
    if prefix:
        files = [f for f in files if f.startswith(prefix)]
    if not files:
        return "No memory files found."
    return "\n".join(files)


@mcp.tool()
def read_memory(filename: str, ctx: Context = None) -> str:
    """Read the contents of a memory file.

    Args:
        filename: Name of the .md file to read (e.g. "MEMORY.md")
    """
    path = _validate_filename(filename)
    if not path.exists():
        raise FileNotFoundError(f"Memory file '{filename}' not found.")
    return path.read_text()


@mcp.tool()
def write_memory(filename: str, content: str, ctx: Context = None) -> str:
    """Create or overwrite a memory file.

    Args:
        filename: Name of the .md file to write (e.g. "MEMORY.md")
        content: Full markdown content to write
    """
    path = _validate_filename(filename)
    path.write_text(content)
    if filename.endswith(".json"):
        # Emit monitor event for task files (have a "status" field)
        try:
            data = json.loads(content)
            if "status" in data:
                _emit_task_event(filename, content)
        except (json.JSONDecodeError, ValueError):
            pass
    return f"Wrote {len(content)} bytes to {filename}."


def _emit_task_event(filename: str, content: str):
    try:
        task = json.loads(content)
        evt = {"action": "task", "task": filename, "status": task.get("status", "")}
        if task.get("target"):
            evt["agent"] = task["target"]
        log = task.get("log", [])
        if log:
            evt["agent"] = log[-1].get("agent", evt.get("agent", ""))
        usage = task.get("usage")
        if usage and task.get("status") in ("completed", "failed"):
            evt["tokens_in"] = usage.get("input_tokens")
            evt["tokens_out"] = usage.get("output_tokens")
            evt["cost_usd"] = usage.get("cost_usd")
            evt["duration_ms"] = usage.get("duration_ms")
        emit_monitor_event(evt)
    except (json.JSONDecodeError, Exception):
        pass


@mcp.tool()
async def create_task(target: str, request: str, task_type: str = "query",
                      timeout: int = 120, allowed_commands: list[str] | None = None,
                      files: list[str] | None = None, ctx: Context = None) -> str:
    """Create a task, save it, and notify the target agent.

    Primary dispatch interface — generates a UUID filename, builds the JSON task,
    saves it, and sends an SSE notification to the target agent.

    Args:
        target: Agent ID to dispatch to (e.g. "arch", "power", "here" for broadcast)
        request: What the agent should do
        task_type: Task type — "query" (default) or "code-edit"
        timeout: Max seconds for execution (default 120)
        allowed_commands: Optional list of shell commands the agent may run
        files: Optional list of file paths (required for code-edit tasks)
    """
    task_id = f"{uuid.uuid4()}.json"
    now = int(time.time())

    cf = _client_fields(ctx)
    if cf.get("is_daemon"):
        created_by = cf.get("device", "daemon")
    else:
        created_by = "user"

    task = {
        "title": request[:80],
        "status": "pending",
        "type": task_type,
        "created": now,
        "created_by": created_by,
        "target": target,
        "timeout": timeout,
        "request": request,
        "allowed_commands": allowed_commands or [],
        "files": files or [],
        "result": None,
        "log": [{"ts": now, "agent": created_by, "msg": "Created task"}],
    }

    path = _validate_filename(task_id)
    content = json.dumps(task, indent=2)
    path.write_text(content)
    _emit_task_event(task_id, content)

    result = await notify_agent(target, task_id, ctx)
    return task_id


@mcp.tool()
def edit_memory(filename: str, old_text: str, new_text: str, ctx: Context = None) -> str:
    """Find and replace text in a memory file.

    Args:
        filename: Name of the .md file to edit
        old_text: Exact text to find
        new_text: Text to replace it with
    """
    path = _validate_filename(filename)
    if not path.exists():
        raise FileNotFoundError(f"Memory file '{filename}' not found.")
    content = path.read_text()
    if old_text not in content:
        raise ValueError(f"Text not found in '{filename}'.")
    new_content = content.replace(old_text, new_text, 1)
    path.write_text(new_content)
    return f"Replaced text in {filename}."


@mcp.tool()
def search_memories(query: str, ctx: Context = None) -> str:
    """Search across all memory files for matching text.

    Args:
        query: Text to search for (case-insensitive)
    """
    results = []
    for path in sorted(
        p for ext in ("*.md", "*.json") for p in DATA_DIR.glob(ext)
    ):
        content = path.read_text()
        lines = content.splitlines()
        matches = [
            (i + 1, line.strip())
            for i, line in enumerate(lines)
            if query.lower() in line.lower()
        ]
        if matches:
            result_lines = [f"## {path.name}"]
            for line_num, line in matches:
                result_lines.append(f"  L{line_num}: {line}")
            results.append("\n".join(result_lines))
    if not results:
        return f"No matches found for '{query}'."
    return "\n\n".join(results)


@mcp.tool()
def delete_memory(filename: str, ctx: Context = None) -> str:
    """Delete a memory file.

    Args:
        filename: Name of the .md file to delete (e.g. "old-notes.md")
    """
    path = _validate_filename(filename)
    if not path.exists():
        raise FileNotFoundError(f"Memory file '{filename}' not found.")
    path.unlink()
    return f"Deleted {filename}."


# ── SSE event queues ─────────────────────────────────────────────────

_agent_queues: dict[str, set[asyncio.Queue]] = {}

# ── In-memory heartbeat + lock state ─────────────────────────────────

_heartbeats: dict[str, int] = {}
_locks: dict[str, dict] = {}  # filename -> {"agent_id": str, "acquired": int}

HEARTBEAT_STALE_SECONDS = 600


def update_heartbeat(agent_id: str):
    _heartbeats[agent_id] = int(time.time())


def get_heartbeat(agent_id: str) -> int | None:
    return _heartbeats.get(agent_id)


def is_agent_alive(agent_id: str, stale_seconds: int = HEARTBEAT_STALE_SECONDS) -> bool:
    if agent_id not in _agent_queues:
        return False
    ts = _heartbeats.get(agent_id)
    if ts is None:
        return True  # connected via SSE but no heartbeat yet — assume alive
    return (int(time.time()) - ts) < stale_seconds


def acquire_lock(agent_id: str, filename: str) -> dict:
    existing = _locks.get(filename)
    if existing:
        if existing["agent_id"] == agent_id:
            return {"ok": True, "msg": "already held"}
        if is_agent_alive(existing["agent_id"]):
            return {"ok": False, "msg": f"locked by {existing['agent_id']}"}
        logger.info("Breaking stale lock on %s (held by %s)", filename, existing["agent_id"])
    _locks[filename] = {"agent_id": agent_id, "acquired": int(time.time())}
    return {"ok": True, "msg": "acquired"}


def release_lock(agent_id: str, filename: str) -> dict:
    existing = _locks.get(filename)
    if not existing:
        return {"ok": True, "msg": "not locked"}
    if existing["agent_id"] != agent_id:
        return {"ok": False, "msg": f"locked by {existing['agent_id']}"}
    del _locks[filename]
    return {"ok": True, "msg": "released"}


def release_all_locks(agent_id: str) -> int:
    to_delete = [f for f, v in _locks.items() if v["agent_id"] == agent_id]
    for f in to_delete:
        del _locks[f]
    return len(to_delete)


def agent_subscribe(agent_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _agent_queues.setdefault(agent_id, set()).add(q)
    logger.info("Agent '%s' subscribed to SSE (%d listeners)", agent_id, len(_agent_queues[agent_id]))
    emit_monitor_event({"action": "connect", "agent": "@" + agent_id})
    return q


def agent_unsubscribe(agent_id: str, q: asyncio.Queue):
    if agent_id in _agent_queues:
        _agent_queues[agent_id].discard(q)
        if not _agent_queues[agent_id]:
            del _agent_queues[agent_id]
    logger.info("Agent '%s' unsubscribed from SSE", agent_id)
    emit_monitor_event({"action": "disconnect", "agent": "@" + agent_id})


# ── Monitor event bus ─────────────────────────────────────────────────

_monitor_queues: set[asyncio.Queue] = set()


def monitor_subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _monitor_queues.add(q)
    logger.info("Monitor client subscribed (%d total)", len(_monitor_queues))
    return q


def monitor_unsubscribe(q: asyncio.Queue):
    _monitor_queues.discard(q)
    logger.info("Monitor client unsubscribed (%d remaining)", len(_monitor_queues))


def emit_monitor_event(event: dict):
    event["id"] = _next_event_id()
    event.setdefault("ts", int(time.time()))
    dead = []
    for q in _monitor_queues:
        try:
            q.put_nowait(event)
        except Exception:
            dead.append(q)
    for q in dead:
        _monitor_queues.discard(q)


@mcp.tool()
def _set_task_target(content: str, agent_id: str) -> str:
    """Set or replace the target field in a task file."""
    data = json.loads(content)
    data["target"] = agent_id
    return json.dumps(data, indent=2)


@mcp.tool()
async def notify_agent(agent_id: str, id: str, ctx: Context = None) -> str:
    """Send a notification to an agent about a new task.

    The agent must be connected via SSE to receive the notification.
    If the agent is offline, the task file remains pending and will be
    picked up when the agent reconnects.

    Use agent_id="here" to broadcast to all connected agents.

    Args:
        agent_id: Target agent ID (e.g. "thinkpad", "here" for all connected)
        id: Task filename (e.g. "ae4f2b.json")
    """
    task_id = id
    # @here: broadcast to all connected agents
    if agent_id == "here":
        if not _agent_queues:
            return "No agents are currently connected."
        connected = list(_agent_queues.keys())
        # Read the template task to clone per-agent
        task_path = DATA_DIR / task_id
        if not task_path.exists():
            raise FileNotFoundError(f"Task file '{task_id}' not found.")
        template = task_path.read_text()
        results = []
        for aid in connected:
            agent_task_id = f"{uuid.uuid4()}.json"
            data = json.loads(template)
            data["target"] = aid
            agent_content = json.dumps(data, indent=2)
            agent_path = DATA_DIR / agent_task_id
            agent_path.write_text(agent_content)
            result = await notify_agent(aid, agent_task_id, ctx)
            results.append(f"@{aid}: {result}")
        # Remove the template task
        task_path.unlink()
        return f"Broadcast to {len(connected)} agents:\n" + "\n".join(results)

    # Verify agent is known (has heartbeat or SSE connection)
    if agent_id not in _heartbeats and agent_id not in _agent_queues:
        # Check legacy reg file as fallback during migration
        reg_path = DATA_DIR / f"agent-reg-{agent_id}.md"
        if not reg_path.exists():
            raise FileNotFoundError(
                f"Agent '{agent_id}' is not registered. "
                f"No heartbeat or SSE connection found."
            )

    queues = _agent_queues.get(agent_id, set())
    online = bool(queues)
    cf = _client_fields(ctx)

    # Determine request vs response from task status
    action = "request"
    status = "pending"
    task_path = DATA_DIR / task_id
    query = ""
    if task_path.exists():
        try:
            content = task_path.read_text()
            data = json.loads(content)
            status = data.get("status", "pending")
            if status in ("completed", "failed"):
                action = "response"

            if status == "failed":
                raw = data.get("result", "")
                if isinstance(raw, str) and raw.startswith("_(") and raw.endswith(")_"):
                    raw = raw[2:-2]
                if raw:
                    errors = [e.strip() for e in str(raw).split(";") if e.strip()]
                else:
                    errors = []

            query = data.get("request", "") or data.get("prompt", "")
            if len(query) > 120:
                query = query[:117] + "..."
        except Exception:
            pass

    device_name = cf.get("device", cf.get("client", "unknown")[:8] if cf.get("client") else "unknown")
    if cf.get("is_daemon"):
        sender = "@" + device_name
    else:
        sender = "howlingfantods_"
    evt = {
        "action": action,
        "tool": "notify_agent",
        "task": task_id,
        "from": sender,
        "to": "@" + agent_id,
        "on": "@" + device_name if device_name else None,
        "status": status,
        "online": online,
    }
    if not evt["on"]:
        del evt["on"]
    if query:
        evt["query"] = query
    if action == "response":
        evt["errors"] = errors if "errors" in dir() else []
        created_ts = data.get("created") if data else None
        if isinstance(created_ts, (int, float)):
            evt["response_time_ms"] = int((time.time() - created_ts) * 1000)
    evt.update(cf)
    emit_monitor_event(evt)

    if not queues:
        return (
            f"Agent '{agent_id}' is not connected. "
            f"Task '{task_id}' is saved and will be picked up when it reconnects."
        )

    for q in queues:
        await q.put(task_id)

    return f"Notified agent '{agent_id}' about task '{task_id}'."


@mcp.tool()
def register_device(name: str, model: str = "", cpu: str = "", ram: str = "", gpu: str = "",
                    os: str = "", env: str = "", aliases: str = "", ctx: Context = None) -> str:
    """Register the calling client's device info for monitor display.

    Maps the caller's OAuth client_id to a human-readable device name with specs.
    Called automatically by the agent daemon at startup. Fields that are empty
    are preserved from the existing entry (so daemon auto-registration doesn't
    overwrite manually-set specs).

    Args:
        name: Short device name (e.g. "thinkpad", "power")
        model: Hardware model (e.g. "ThinkPad X1 Carbon Gen 11")
        cpu: CPU model
        ram: RAM spec
        gpu: GPU model
        os: Operating system (e.g. "Arch Linux", "Windows 11", "macOS Sequoia")
        env: Runtime environment (e.g. "bare metal", "WSL2", "VM")
        aliases: Comma-separated aliases (e.g. "@arch, @thinkpad")
    """
    cid = _get_client_id(ctx)
    if not cid:
        return "Cannot register: no client_id available in this session."

    devices = _load_devices()
    existing = devices.get(cid, {})
    incoming = {"name": name}
    for key, val in [("model", model), ("cpu", cpu), ("ram", ram), ("gpu", gpu),
                     ("os", os), ("env", env), ("aliases", aliases)]:
        if val:
            incoming[key] = val
    # Merge: incoming overwrites, but empty incoming fields keep existing values
    merged = {**existing, **incoming}
    devices[cid] = merged
    _save_devices(devices)

    emit_monitor_event({"action": "request", "tool": "register_device", "device": name, "client": cid})
    return f"Registered client {cid[:8]}... as '{name}'."


def _extract_result(data: dict) -> str:
    return data.get("result", "") if data else ""


@mcp.tool()
async def await_task(id: str, timeout: int = 120, ctx: Context = None) -> str:
    """Block until a task completes and return its result.

    Polls the task file every 3 seconds. On completion or failure, returns
    the result text. On timeout while still pending, raises an error. On
    timeout while in progress, returns recent thinking lines.

    Args:
        id: Task filename (e.g. "ae4f2b.json")
        timeout: Max seconds to wait (default 120)
    """
    task_id = id
    deadline = asyncio.get_event_loop().time() + timeout
    last_status = "unknown"

    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break

        path = _validate_filename(task_id)
        if not path.exists():
            await asyncio.sleep(min(3, remaining))
            continue

        content = path.read_text()
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            data = {}

        last_status = data.get("status", "pending")

        if last_status in ("completed", "failed"):
            result = _extract_result(data)
            clear_thinking_buffer(task_id)
            if last_status == "failed":
                return f"FAILED: {result}" if result else "FAILED: (no details)"
            return result or "(no result)"

        await asyncio.sleep(min(3, remaining))

    # Timed out
    if last_status in ("pending", "unknown"):
        raise TimeoutError(f"Task '{task_id}' was never picked up (status: {last_status}) after {timeout}s")

    # In progress — return thinking lines so caller knows what's happening
    lines = get_thinking_lines(task_id)
    if lines:
        preview = "\n".join(lines[-10:])
        return f"TIMEOUT (still {last_status}). Recent activity:\n{preview}"
    return f"TIMEOUT (still {last_status}). No thinking data available."
