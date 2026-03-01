import logging
import re
from pathlib import Path

import httpx

from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from mcp_memory.config import DATA_DIR
from mcp_memory.oauth_provider import MemoryOAuthProvider

mcp = FastMCP(
    "memory",
    auth_server_provider=MemoryOAuthProvider(),
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


@mcp.tool()
async def notify_agent(agent_id: str, task_id: str) -> str:
    """Send a webhook notification to an agent about a new task.

    Reads the agent's registration file to find its webhook URL,
    then POSTs a notification with the task ID.

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

    content = reg_path.read_text()

    # Parse webhook URL from "- webhook: http://host:port/path" line
    webhook_url = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- webhook:"):
            webhook_url = stripped[len("- webhook:"):].strip()
            break

    if not webhook_url:
        raise ValueError(
            f"No webhook URL found in agent registry for '{agent_id}'."
        )

    # Check agent status
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- status:"):
            status = stripped[len("- status:"):].strip()
            if status != "online":
                logger.warning("Agent '%s' status is '%s', sending anyway", agent_id, status)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                webhook_url,
                json={"task_id": task_id},
            )
            resp.raise_for_status()
    except httpx.ConnectError:
        return f"Failed to reach agent '{agent_id}' at {webhook_url} (connection refused). Is the daemon running?"
    except httpx.TimeoutException:
        return f"Timeout notifying agent '{agent_id}' at {webhook_url}."
    except httpx.HTTPStatusError as e:
        return f"Agent '{agent_id}' returned HTTP {e.response.status_code}: {e.response.text}"

    return f"Notified agent '{agent_id}' about task '{task_id}'."
