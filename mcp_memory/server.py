import asyncio
import json
import logging
import re
from datetime import datetime, timezone
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
        device = _resolve_device(cid)
        if device:
            fields["device"] = device
    return fields


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
    evt = {"type": "tool", "tool": "list_memories"}
    if prefix:
        evt["prefix"] = prefix
    evt.update(_client_fields(ctx))
    emit_monitor_event(evt)
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
    evt = {"type": "tool", "tool": "read_memory", "file": filename}
    evt.update(_client_fields(ctx))
    emit_monitor_event(evt)
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
    evt = {"type": "tool", "tool": "write_memory", "file": filename, "bytes": len(content)}
    evt.update(_client_fields(ctx))
    emit_monitor_event(evt)
    if filename.startswith("task-") and filename.endswith(".json"):
        _emit_task_event(filename, content)
    return f"Wrote {len(content)} bytes to {filename}."


def _emit_task_event(filename: str, content: str):
    try:
        task = json.loads(content)
        evt = {"type": "task", "task": filename, "status": task.get("status", "")}
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
    evt = {"type": "tool", "tool": "edit_memory", "file": filename}
    evt.update(_client_fields(ctx))
    emit_monitor_event(evt)
    return f"Replaced text in {filename}."


@mcp.tool()
def search_memories(query: str, ctx: Context = None) -> str:
    """Search across all memory files for matching text.

    Args:
        query: Text to search for (case-insensitive)
    """
    evt = {"type": "tool", "tool": "search_memories", "query": query}
    evt.update(_client_fields(ctx))
    emit_monitor_event(evt)
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
    evt = {"type": "tool", "tool": "delete_memory", "file": filename}
    evt.update(_client_fields(ctx))
    emit_monitor_event(evt)
    return f"Deleted {filename}."


# ── SSE event queues ─────────────────────────────────────────────────

_agent_queues: dict[str, set[asyncio.Queue]] = {}


def agent_subscribe(agent_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _agent_queues.setdefault(agent_id, set()).add(q)
    logger.info("Agent '%s' subscribed to SSE (%d listeners)", agent_id, len(_agent_queues[agent_id]))
    emit_monitor_event({"type": "connect", "agent": agent_id})
    return q


def agent_unsubscribe(agent_id: str, q: asyncio.Queue):
    if agent_id in _agent_queues:
        _agent_queues[agent_id].discard(q)
        if not _agent_queues[agent_id]:
            del _agent_queues[agent_id]
    logger.info("Agent '%s' unsubscribed from SSE", agent_id)
    emit_monitor_event({"type": "disconnect", "agent": agent_id})


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
    event.setdefault("ts", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    dead = []
    for q in _monitor_queues:
        try:
            q.put_nowait(event)
        except Exception:
            dead.append(q)
    for q in dead:
        _monitor_queues.discard(q)


@mcp.tool()
async def notify_agent(agent_id: str, task_id: str, ctx: Context = None) -> str:
    """Send a notification to an agent about a new task.

    The agent must be connected via SSE to receive the notification.
    If the agent is offline, the task file remains pending and will be
    picked up when the agent reconnects.

    Args:
        agent_id: Target agent ID (e.g. "legion", "thinkpad")
        task_id: Task filename to notify about (e.g. "task-20260301-1430-obs.md")
    """
    reg_filename = f"agent-reg-{agent_id}.md"
    reg_path = DATA_DIR / reg_filename
    if not reg_path.exists():
        raise FileNotFoundError(
            f"Agent registry '{reg_filename}' not found. "
            f"Is agent '{agent_id}' registered?"
        )

    queues = _agent_queues.get(agent_id, set())
    online = bool(queues)
    evt = {"type": "notify", "agent": agent_id, "task": task_id, "online": online}
    evt.update(_client_fields(ctx))
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
def register_device(name: str, model: str = "", cpu: str = "", ram: str = "", gpu: str = "", ctx: Context = None) -> str:
    """Register the calling client's device info for monitor display.

    Maps the caller's OAuth client_id to a human-readable device name with specs.
    Call this once per device to populate the lookup table.

    Args:
        name: Short device name (e.g. "thinkpad", "power")
        model: Hardware model (e.g. "ThinkPad X1 Carbon Gen 11")
        cpu: CPU model
        ram: RAM spec
        gpu: GPU model
    """
    cid = _get_client_id(ctx)
    if not cid:
        return "Cannot register: no client_id available in this session."

    devices = _load_devices()
    entry = {"name": name}
    if model:
        entry["model"] = model
    if cpu:
        entry["cpu"] = cpu
    if ram:
        entry["ram"] = ram
    if gpu:
        entry["gpu"] = gpu
    devices[cid] = entry
    _save_devices(devices)

    emit_monitor_event({"type": "tool", "tool": "register_device", "device": name, "client": cid})
    return f"Registered client {cid[:8]}... as '{name}'."
