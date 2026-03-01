import asyncio
import logging
import re
from pathlib import Path

from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
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

FILENAME_RE = re.compile(r"^[a-zA-Z0-9_\-]+\.md$")


def _validate_filename(filename: str) -> Path:
    if not FILENAME_RE.match(filename):
        raise ValueError(
            f"Invalid filename '{filename}'. "
            "Must be alphanumeric/dashes/underscores with .md extension."
        )
    path = (DATA_DIR / filename).resolve()
    if not str(path).startswith(str(DATA_DIR.resolve())):
        raise ValueError("Path traversal not allowed.")
    return path


@mcp.tool()
def list_memories(prefix: str = "") -> str:
    """List all markdown memory files.

    Args:
        prefix: Optional prefix to filter filenames (e.g. "task-" or "agent-reg-")
    """
    files = sorted(p.name for p in DATA_DIR.glob("*.md"))
    if prefix:
        files = [f for f in files if f.startswith(prefix)]
    if not files:
        return "No memory files found."
    return "\n".join(files)


@mcp.tool()
def read_memory(filename: str) -> str:
    """Read the contents of a memory file.

    Args:
        filename: Name of the .md file to read (e.g. "MEMORY.md")
    """
    path = _validate_filename(filename)
    if not path.exists():
        raise FileNotFoundError(f"Memory file '{filename}' not found.")
    return path.read_text()


@mcp.tool()
def write_memory(filename: str, content: str) -> str:
    """Create or overwrite a memory file.

    Args:
        filename: Name of the .md file to write (e.g. "MEMORY.md")
        content: Full markdown content to write
    """
    path = _validate_filename(filename)
    path.write_text(content)
    return f"Wrote {len(content)} bytes to {filename}."


@mcp.tool()
def edit_memory(filename: str, old_text: str, new_text: str) -> str:
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
def search_memories(query: str) -> str:
    """Search across all memory files for matching text.

    Args:
        query: Text to search for (case-insensitive)
    """
    results = []
    for path in sorted(DATA_DIR.glob("*.md")):
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
def delete_memory(filename: str) -> str:
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
# Each connected agent holds an asyncio.Queue. notify_agent pushes to it,
# the SSE endpoint in main.py streams from it. No outbound HTTP needed.

_agent_queues: dict[str, set[asyncio.Queue]] = {}


def agent_subscribe(agent_id: str) -> asyncio.Queue:
    """Register an SSE listener for an agent. Called from the SSE endpoint."""
    q: asyncio.Queue = asyncio.Queue()
    _agent_queues.setdefault(agent_id, set()).add(q)
    logger.info("Agent '%s' subscribed to SSE (%d listeners)", agent_id, len(_agent_queues[agent_id]))
    return q


def agent_unsubscribe(agent_id: str, q: asyncio.Queue):
    """Remove an SSE listener. Called when the SSE connection drops."""
    if agent_id in _agent_queues:
        _agent_queues[agent_id].discard(q)
        if not _agent_queues[agent_id]:
            del _agent_queues[agent_id]
    logger.info("Agent '%s' unsubscribed from SSE", agent_id)


@mcp.tool()
async def notify_agent(agent_id: str, task_id: str) -> str:
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
    if not queues:
        return (
            f"Agent '{agent_id}' is not connected. "
            f"Task '{task_id}' is saved and will be picked up when it reconnects."
        )

    for q in queues:
        await q.put(task_id)

    return f"Notified agent '{agent_id}' about task '{task_id}'."
