#!/usr/bin/env python3
"""Agent daemon — connects to MCP server via SSE, executes tasks on demand.

Opens an outbound SSE connection to the MCP server. When a task notification
arrives, reads/claims the task, invokes `claude --print` for execution, and
writes results back. Zero idle cost: no polling, no open ports, no LLM tokens
burned on coordination.

Usage:
    AGENT_ID=thinkpad MCP_URL=https://mcp.howling.one python3 agent_daemon.py

Environment variables:
    AGENT_ID           - Unique agent identifier (e.g. "thinkpad", "legion")
    MCP_URL            - Base URL of the MCP memory server
    PLATFORM           - Override platform detection (linux/darwin/win32)
    AGENT_ROLES        - Comma-separated roles (default: "general")
    HEARTBEAT_INTERVAL - Seconds between heartbeats (default: 300)
    MAX_TASK_DURATION  - Max seconds for task execution (default: 300)
    BLOCKED_PATHS      - Comma-separated paths to block (default: "/etc,/boot,~/.ssh")
"""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import httpx

from mcp_client import MCPClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("agent-daemon")

# ── Configuration ────────────────────────────────────────────────────

AGENT_ID = os.environ.get("AGENT_ID", socket.gethostname())
MCP_URL = os.environ.get("MCP_URL", "https://mcp.howling.one")
AGENT_PLATFORM = os.environ.get("PLATFORM", sys.platform)
AGENT_ROLES = os.environ.get("AGENT_ROLES", "general").split(",")
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "300"))
MAX_TASK_DURATION = int(os.environ.get("MAX_TASK_DURATION", "300"))
BLOCKED_PATHS = os.environ.get("BLOCKED_PATHS", "/etc,/boot,~/.ssh").split(",")
REPO_DIR = os.environ.get("REPO_DIR", os.getcwd())
SYNCTHING_SETTLE_SECONDS = int(os.environ.get("SYNCTHING_SETTLE", "5"))

REG_FILENAME = f"agent-reg-{AGENT_ID}.md"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def detect_gpu() -> str:
    """Best-effort GPU detection."""
    try:
        if AGENT_PLATFORM == "linux":
            result = subprocess.run(
                ["lspci"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if "VGA" in line or "3D" in line:
                    return line.split(": ", 1)[-1].strip()
        elif AGENT_PLATFORM == "darwin":
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if "Chipset" in line or "Chip" in line:
                    return line.split(": ", 1)[-1].strip()
        elif AGENT_PLATFORM in ("win32", "cygwin"):
            result = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                capture_output=True, text=True, timeout=5,
            )
            lines = [l.strip() for l in result.stdout.splitlines() if l.strip() and l.strip() != "Name"]
            if lines:
                return lines[0]
    except Exception:
        pass
    return "unknown"


def detect_shell() -> str:
    shell = os.environ.get("SHELL", "")
    if shell:
        return os.path.basename(shell)
    if AGENT_PLATFORM in ("win32", "cygwin"):
        return "powershell"
    return "bash"


# ── Agent Registration ───────────────────────────────────────────────

def build_registration() -> str:
    return f"""# Agent: {AGENT_ID}

## Identity
- hostname: {socket.gethostname()}
- platform: {AGENT_PLATFORM}
- registered: {now_iso()}

## Status
- connection: sse
- status: online
- last_seen: {now_iso()}

## Capabilities
- shell: {detect_shell()}
- gpu: {detect_gpu()}
- roles: {", ".join(AGENT_ROLES)}

## Restrictions
- max_task_duration: {MAX_TASK_DURATION}
- blocked_paths: {", ".join(BLOCKED_PATHS)}
"""


def register_agent(mcp: MCPClient):
    """Write or overwrite agent registration file."""
    content = build_registration()
    mcp.write(REG_FILENAME, content)
    logger.info("Registered agent '%s'", AGENT_ID)


def register_device_info(mcp: MCPClient):
    """Register this daemon's client_id → device name in the server lookup table."""
    try:
        gpu = detect_gpu()
        args = {"name": AGENT_ID}
        if gpu and gpu != "unknown":
            args["gpu"] = gpu
        result = mcp._call_tool("register_device", args)
        logger.info("Device registration: %s", result)
    except Exception as e:
        logger.warning("Device registration failed: %s", e)


def update_heartbeat(mcp: MCPClient):
    """Update last_seen timestamp — direct HTTP, no LLM."""
    try:
        content = mcp.read(REG_FILENAME)
        for line in content.splitlines():
            if line.strip().startswith("- last_seen:"):
                mcp.edit(REG_FILENAME, line.strip(), f"- last_seen: {now_iso()}")
                return
    except Exception as e:
        logger.warning("Heartbeat update failed: %s", e)


# ── Task Handling ────────────────────────────────────────────────────

def _try_parse_json(content: str) -> dict | None:
    """Try to parse content as JSON. Returns dict or None."""
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def parse_task_field(content: str, field: str) -> str:
    """Extract a field value from task content (JSON or markdown)."""
    # Try JSON first
    data = _try_parse_json(content)
    if data is not None:
        val = data.get(field)
        if val is None:
            # Also check common aliases
            aliases = {"assigned_to": "target", "target": "assigned_to"}
            val = data.get(aliases.get(field, ""))
        if val is None:
            return ""
        if isinstance(val, list):
            return ", ".join(str(v) for v in val)
        return str(val)

    # Markdown fallback
    lines = content.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Format 1: '- field: value'
        if stripped.startswith(f"- {field}:"):
            return stripped[len(f"- {field}:"):].strip()
        # Format 2: '## field' followed by value on next line
        if stripped.lower() == f"## {field}" and i + 1 < len(lines):
            val = lines[i + 1].strip()
            if val and not val.startswith("#"):
                return val
        # Format 3: bare 'field: value' (frontmatter style)
        if stripped.startswith(f"{field}:") and not stripped.startswith("- "):
            return stripped[len(f"{field}:"):].strip()
    return ""


def parse_allowed_commands(content: str) -> list[str]:
    """Extract allowed commands from task content (JSON or markdown)."""
    data = _try_parse_json(content)
    if data is not None:
        cmds = data.get("allowed_commands", [])
        return cmds if isinstance(cmds, list) else []

    commands = []
    in_section = False
    for line in content.splitlines():
        if line.strip() == "## Allowed Commands":
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            stripped = line.strip()
            if stripped.startswith("- "):
                commands.append(stripped[2:])
    return commands


def parse_request(content: str) -> str:
    """Extract the request text from task content (JSON or markdown)."""
    data = _try_parse_json(content)
    if data is not None:
        return data.get("request", "") or data.get("prompt", "")

    lines = []
    in_section = False
    for line in content.splitlines():
        header = line.strip().lower()
        if header in ("## request", "## prompt"):
            in_section = True
            continue
        if in_section:
            if line.startswith("## ") or line.startswith("# "):
                break
            lines.append(line)
    return "\n".join(lines).strip()


def parse_files_list(content: str) -> list[str]:
    """Extract the files list from task content (JSON or markdown)."""
    data = _try_parse_json(content)
    if data is not None:
        files = data.get("files", [])
        if isinstance(files, list):
            return [f for f in files if f]
        return []

    raw = parse_task_field(content, "files")
    if not raw:
        return []
    return [f.strip() for f in raw.split(",") if f.strip()]


def lock_filename(filepath: str) -> str:
    """Convert a filepath to a lock filename: agent_daemon.py → lock-agent-daemon-py.md"""
    safe = filepath.replace("/", "-").replace("\\", "-").replace(".", "-").replace("_", "-")
    return f"lock-{safe}.md"


# ── File Locking ─────────────────────────────────────────────────────

def acquire_locks(mcp: MCPClient, files: list[str]) -> list[str]:
    """Acquire MCP file locks for a list of files.

    Uses write-then-verify to avoid TOCTOU races: writes the lock, reads it
    back, and only proceeds if we're the holder. If another agent won the race,
    releases all acquired locks and returns empty list.
    """
    acquired = []
    for filepath in files:
        lf = lock_filename(filepath)

        # Check for existing lock first
        try:
            existing = mcp.read(lf)
            # Lock exists — check if it's ours (re-entrant) or stale
            if f"- holder: {AGENT_ID}" in existing:
                acquired.append(filepath)
                logger.info("Already hold lock: %s", filepath)
                continue
            if _is_stale_lock(mcp, existing):
                logger.info("Breaking stale lock on %s", filepath)
                mcp.delete(lf)
            else:
                logger.warning("File '%s' is locked by another agent", filepath)
                release_locks(mcp, acquired)
                return []
        except Exception:
            pass  # FileNotFoundError = not locked, proceed

        # Write our lock
        lock_content = (
            f"- holder: {AGENT_ID}\n"
            f"- acquired: {now_iso()}\n"
            f"- file: {filepath}\n"
        )
        mcp.write(lf, lock_content)

        # Verify we won the race — read it back
        try:
            readback = mcp.read(lf)
            if f"- holder: {AGENT_ID}" not in readback:
                logger.warning("Lost lock race on %s", filepath)
                release_locks(mcp, acquired)
                return []
        except Exception:
            logger.warning("Failed to verify lock on %s", filepath)
            release_locks(mcp, acquired)
            return []

        acquired.append(filepath)
        logger.info("Acquired lock: %s", filepath)

    return acquired


def release_locks(mcp: MCPClient, files: list[str]):
    """Release MCP file locks."""
    for filepath in files:
        lf = lock_filename(filepath)
        try:
            mcp.delete(lf)
            logger.info("Released lock: %s", filepath)
        except Exception:
            pass


def _is_stale_lock(mcp: MCPClient, lock_content: str) -> bool:
    """Check if a lock is stale (holder offline >10 min)."""
    holder = None
    for line in lock_content.splitlines():
        if line.strip().startswith("- holder:"):
            holder = line.strip()[len("- holder:"):].strip()
            break
    if not holder:
        return True
    try:
        reg = mcp.read(f"agent-reg-{holder}.md")
        for line in reg.splitlines():
            stripped = line.strip()
            if stripped.startswith("- status:") and "offline" in stripped:
                return True
            if stripped.startswith("- last_seen:"):
                last_seen_str = stripped[len("- last_seen:"):].strip()
                last_seen = datetime.fromisoformat(last_seen_str.replace("Z", "+00:00"))
                age_min = (datetime.now(timezone.utc) - last_seen).total_seconds() / 60
                if age_min > 10:
                    return True
    except Exception:
        return True  # Can't read registry = assume stale
    return False


def git_commit_files(files: list[str], message: str) -> str:
    """Stage and commit specific files. Returns commit output or error."""
    try:
        # Stage
        result = subprocess.run(
            ["git", "add"] + files,
            capture_output=True, text=True, timeout=10,
            cwd=REPO_DIR,
        )
        if result.returncode != 0:
            return f"git add failed: {result.stderr.strip()}"

        # Check if there's anything to commit
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            capture_output=True, timeout=10,
            cwd=REPO_DIR,
        )
        if status.returncode == 0:
            return "No changes to commit"

        # Commit
        result = subprocess.run(
            ["git", "commit", "-m", message],
            capture_output=True, text=True, timeout=10,
            cwd=REPO_DIR,
        )
        if result.returncode != 0:
            return f"git commit failed: {result.stderr.strip()}"

        return result.stdout.strip()
    except Exception as e:
        return f"git error: {e}"


def _edit_status(mcp: MCPClient, task_id: str, old_status: str, new_status: str):
    """Update task status (JSON or markdown)."""
    content = mcp.read(task_id)
    data = _try_parse_json(content)
    if data is not None:
        data["status"] = new_status
        mcp.write(task_id, json.dumps(data, indent=2))
        return
    if f"- status: {old_status}" in content:
        mcp.edit(task_id, f"- status: {old_status}", f"- status: {new_status}")
    elif f"## status\n{old_status}" in content:
        mcp.edit(task_id, f"## status\n{old_status}", f"## status\n{new_status}")
    elif f"\nstatus: {old_status}" in content:
        mcp.edit(task_id, f"status: {old_status}", f"status: {new_status}")
    else:
        mcp.edit(task_id, f"- status: {old_status}", f"- status: {new_status}")


def _edit_result(mcp: MCPClient, task_id: str, result_text: str):
    """Write result text (JSON or markdown)."""
    content = mcp.read(task_id)
    data = _try_parse_json(content)
    if data is not None:
        data["result"] = result_text
        mcp.write(task_id, json.dumps(data, indent=2))
        return
    if "_(pending)_" in content:
        mcp.edit(task_id, "_(pending)_", result_text)
    elif "## Result\n" in content:
        old_section = content.split("## Result\n", 1)[1].split("\n## ", 1)[0]
        mcp.edit(task_id, f"## Result\n{old_section}", f"## Result\n{result_text}\n")
    elif "## result\n" in content:
        old_section = content.split("## result\n", 1)[1].split("\n## ", 1)[0]
        mcp.edit(task_id, f"## result\n{old_section}", f"## result\n{result_text}\n")
    else:
        if "## status" in content:
            mcp.edit(task_id, "## status", f"## result\n{result_text}\n\n## status")
        elif "## Meta" in content:
            mcp.edit(task_id, "## Log", f"## Result\n{result_text}\n\n## Log")
        else:
            mcp.write(task_id, content + f"\n\n## Result\n{result_text}\n")


def append_log(mcp: MCPClient, task_id: str, agent: str, message: str):
    """Append a log entry to the task file (JSON or markdown)."""
    content = mcp.read(task_id)
    data = _try_parse_json(content)
    if data is not None:
        if "log" not in data or not isinstance(data["log"], list):
            data["log"] = []
        data["log"].append({"ts": now_iso(), "agent": agent, "msg": message})
        mcp.write(task_id, json.dumps(data, indent=2))
        return
    log_entry = f"\n- {now_iso()} [{agent}] {message}"
    if "## Log" in content:
        mcp.edit(task_id, "## Log", f"## Log{log_entry}")
    else:
        mcp.write(task_id, content + f"\n\n## Log{log_entry}")


def claim_task(mcp: MCPClient, task_id: str) -> bool:
    """Attempt to claim a task via optimistic locking."""
    try:
        content = mcp.read(task_id)
    except Exception as e:
        logger.info("Could not read task %s: %s", task_id, e)
        return False
    try:
        data = _try_parse_json(content)
        if data is not None:
            if data.get("status", "pending") != "pending":
                logger.info("Could not find pending status in task %s", task_id)
                return False
            data["status"] = "claimed"
            mcp.write(task_id, json.dumps(data, indent=2))
        elif "- status: pending" in content:
            mcp.edit(task_id, "- status: pending", "- status: claimed")
        elif "\n## status\npending" in content:
            mcp.edit(task_id, "## status\npending", "## status\nclaimed")
        elif "\n## status\n pending" in content:
            mcp.edit(task_id, "## status\n pending", "## status\nclaimed")
        elif "\nstatus: pending" in content:
            mcp.edit(task_id, "status: pending", "status: claimed")
        else:
            # No status field at all — insert one via append
            mcp.write(task_id, content.rstrip() + "\nstatus: claimed\n")
            logger.info("Inserted missing status field as 'claimed' in %s", task_id)
        append_log(mcp, task_id, AGENT_ID, "Claimed task")
        return True
    except Exception as e:
        logger.info("Could not claim task %s: %s", task_id, e)
        return False


def execute_task(mcp: MCPClient, task_id: str):
    """Read, claim, execute, and write results for a task."""
    logger.info("Processing task: %s", task_id)

    # Read task
    try:
        content = mcp.read(task_id)
    except Exception as e:
        logger.error("Failed to read task %s: %s", task_id, e)
        return

    # Verify it's for us (handle both 'target' and 'assigned_to')
    target = parse_task_field(content, "target") or parse_task_field(content, "assigned_to")
    target = target.lstrip("@")  # normalize @vps → vps
    if target and target != AGENT_ID:
        logger.info("Task %s is for '%s', not us ('%s'). Skipping.", task_id, target, AGENT_ID)
        return

    # Check status — treat missing/empty status as "pending"
    status = parse_task_field(content, "status") or "pending"
    if status != "pending":
        logger.info("Task %s status is '%s', not 'pending'. Skipping.", task_id, status)
        return

    # Claim
    if not claim_task(mcp, task_id):
        return

    # Set running
    try:
        _edit_status(mcp, task_id, "claimed", "running")
        append_log(mcp, task_id, AGENT_ID, "Executing task")
    except Exception as e:
        logger.error("Failed to set running status: %s", e)
        return

    # Parse task details
    request = parse_request(content)
    allowed_commands = parse_allowed_commands(content)
    timeout = int(parse_task_field(content, "timeout") or MAX_TASK_DURATION)
    task_type = parse_task_field(content, "type") or "query"
    files = parse_files_list(content)
    raw_depth = parse_task_field(content, "depth")
    created_by = parse_task_field(content, "created_by")

    # Only user-originated tasks can start at depth 0.
    # Agent-originated tasks must carry a depth field from their parent.
    # If an agent tries to create a task without depth, refuse it.
    if created_by and created_by != "howlingfantods_" and not raw_depth:
        _fail_task(mcp, task_id, "Agent-originated tasks must include a depth field. Only user requests can start new chains.")
        _notify_creator(mcp, task_id, content)
        return

    depth = int(raw_depth) if raw_depth else 0

    MAX_DEPTH = 5
    if depth >= MAX_DEPTH:
        _fail_task(mcp, task_id, f"Max dispatch depth ({MAX_DEPTH}) exceeded. Refusing to sub-dispatch further.")
        _notify_creator(mcp, task_id, content)
        return

    if task_type == "code-edit":
        _execute_code_edit(mcp, task_id, request, files, allowed_commands, timeout, content, depth)
    else:
        _execute_query(mcp, task_id, request, allowed_commands, timeout, content, depth)


def _build_system_prompt(request_type: str, allowed_commands: list[str]) -> str:
    """Build system prompt constraints for claude --print."""
    constraints = [
        f"You are executing a task on machine '{AGENT_ID}' ({AGENT_PLATFORM}).",
    ]
    # depth is passed via closure from execute_task

    if request_type == "code-edit":
        constraints.append("Edit the requested files. Do NOT commit — the daemon handles git.")
    else:
        constraints.append("Execute the requested task and provide the output.")

    if allowed_commands:
        cmd_list = "\n".join(f"  - {cmd}" for cmd in allowed_commands)
        constraints.append(f"You may ONLY run these commands:\n{cmd_list}")
        constraints.append("Do NOT run any commands not on this list.")
    if BLOCKED_PATHS:
        constraints.append(f"Do NOT access these paths: {', '.join(BLOCKED_PATHS)}")

    return "\n".join(constraints)


def _flush_thinking(agent_id: str, task_id: str, text: str):
    """Fire-and-forget POST of thinking text to the server."""
    if not task_id:
        return
    try:
        url = f"{MCP_URL}/thinking/{agent_id}/{task_id}"
        httpx.post(url, content=text, timeout=5)
    except Exception:
        pass


def _invoke_claude(request: str, system_prompt: str, timeout: int,
                   allowed_tools: str = "Bash(.*)",
                   task_id: str = "") -> tuple[str, bool]:
    """Run claude --print via Popen, streaming stdout to the thinking endpoint."""
    cmd = [
        "claude", "--print",
        "--dangerously-skip-permissions",
        "--allowedTools", allowed_tools,
        "--append-system-prompt", system_prompt,
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_DIR,
    )
    proc.stdin.write(request)
    proc.stdin.close()

    output_lines = []
    lock = threading.Lock()

    def reader():
        for line in proc.stdout:
            with lock:
                output_lines.append(line.rstrip("\n"))

    read_thread = threading.Thread(target=reader, daemon=True)
    read_thread.start()

    deadline = time.time() + timeout
    last_flush = time.time()
    flushed_count = 0

    while read_thread.is_alive():
        remaining = deadline - time.time()
        if remaining <= 0:
            proc.kill()
            proc.wait()
            read_thread.join(timeout=2)
            raise subprocess.TimeoutExpired(cmd, timeout)
        read_thread.join(timeout=min(1.0, remaining))
        # Flush new lines as thinking
        with lock:
            new_lines = output_lines[flushed_count:]
        if new_lines and (time.time() - last_flush >= 1.0 or not read_thread.is_alive()):
            _flush_thinking(AGENT_ID, task_id, "\n".join(new_lines))
            flushed_count += len(new_lines)
            last_flush = time.time()

    # Final flush of any remaining
    with lock:
        new_lines = output_lines[flushed_count:]
    if new_lines:
        _flush_thinking(AGENT_ID, task_id, "\n".join(new_lines))

    proc.wait()
    stderr = proc.stderr.read()
    output = "\n".join(output_lines).strip()
    if proc.returncode != 0 and stderr:
        output += f"\n\nSTDERR:\n{stderr.strip()}"
    return output, proc.returncode == 0


def _execute_query(mcp: MCPClient, task_id: str, request: str,
                   allowed_commands: list[str], timeout: int, content: str,
                   depth: int = 0):
    """Execute a query task (check something, report output)."""
    system_prompt = _build_system_prompt("query", allowed_commands)
    system_prompt += f"\nThis task is at dispatch depth {depth}/5. If you sub-dispatch to another agent, you MUST set depth to {depth + 1} in the task. Tasks without a depth field from agents will be rejected."

    logger.info("Invoking claude --print for query task %s", task_id)
    try:
        output, success = _invoke_claude(request, system_prompt, timeout, task_id=task_id)

        _edit_result(mcp, task_id, output or "_(no output)_")
        _edit_status(mcp, task_id, "running", "completed")
        append_log(mcp, task_id, AGENT_ID, "Task completed")
        logger.info("Task %s completed", task_id)

    except subprocess.TimeoutExpired:
        _edit_status(mcp, task_id, "running", "failed")
        _edit_result(mcp, task_id, f"_(timed out after {timeout}s)_")
        append_log(mcp, task_id, AGENT_ID, f"Task timed out after {timeout}s")
        logger.warning("Task %s timed out", task_id)

    except Exception as e:
        _fail_task(mcp, task_id, str(e))

    _notify_creator(mcp, task_id, content)


def _execute_code_edit(mcp: MCPClient, task_id: str, request: str,
                       files: list[str], allowed_commands: list[str],
                       timeout: int, content: str, depth: int = 0):
    """Execute a code-edit task: lock → edit → git commit → unlock."""
    if not files:
        _fail_task(mcp, task_id, "code-edit task requires '- files:' field listing files to edit")
        _notify_creator(mcp, task_id, content)
        return

    # 1. Acquire file locks
    logger.info("Acquiring locks for %s", files)
    append_log(mcp, task_id, AGENT_ID, f"Acquiring locks: {', '.join(files)}")
    locked = acquire_locks(mcp, files)
    if not locked:
        _fail_task(mcp, task_id, f"Could not acquire locks for: {', '.join(files)}. Another agent is editing.")
        _notify_creator(mcp, task_id, content)
        return

    try:
        # 2. Wait for Syncthing to settle (ensure we have latest files)
        if SYNCTHING_SETTLE_SECONDS > 0:
            logger.info("Waiting %ds for Syncthing to settle", SYNCTHING_SETTLE_SECONDS)
            time.sleep(SYNCTHING_SETTLE_SECONDS)

        # 3. Run claude --print with Edit + Read + Bash tools
        system_prompt = _build_system_prompt("code-edit", allowed_commands)
        allowed_tools = "Edit Read Bash(.*)"

        logger.info("Invoking claude --print for code-edit task %s", task_id)
        try:
            output, success = _invoke_claude(request, system_prompt, timeout, allowed_tools, task_id=task_id)
        except subprocess.TimeoutExpired:
            _edit_status(mcp, task_id, "running", "failed")
            _edit_result(mcp, task_id, f"_(timed out after {timeout}s)_")
            append_log(mcp, task_id, AGENT_ID, f"Task timed out after {timeout}s")
            return

        # 4. Git commit the changed files
        commit_msg = f"[{AGENT_ID}] {request[:72]}"
        git_result = git_commit_files(files, commit_msg)
        logger.info("Git: %s", git_result)

        # 5. Write result
        result_text = output or "_(no output)_"
        result_text += f"\n\n**Git:** {git_result}"
        _edit_result(mcp, task_id, result_text)
        _edit_status(mcp, task_id, "running", "completed")
        append_log(mcp, task_id, AGENT_ID, f"Task completed. {git_result}")
        logger.info("Code-edit task %s completed", task_id)

    except Exception as e:
        _fail_task(mcp, task_id, str(e))

    finally:
        # 6. Always release locks
        release_locks(mcp, locked)

    _notify_creator(mcp, task_id, content)


def _fail_task(mcp: MCPClient, task_id: str, error: str):
    """Mark a task as failed."""
    try:
        _edit_status(mcp, task_id, "running", "failed")
        _edit_result(mcp, task_id, f"_(error: {error})_")
        append_log(mcp, task_id, AGENT_ID, f"Task failed: {error}")
    except Exception:
        pass
    logger.error("Task %s failed: %s", task_id, error)


def _notify_creator(mcp: MCPClient, task_id: str, content: str):
    """Best-effort notification to the task creator."""
    data = _try_parse_json(content)
    created_by = data.get("created_by", "") if data else parse_task_field(content, "created_by")
    if created_by and created_by != AGENT_ID:
        try:
            mcp.notify_agent(created_by, task_id)
            logger.info("Notified '%s' about completed task %s", created_by, task_id)
        except Exception:
            pass


# ── SSE Client ───────────────────────────────────────────────────────

def parse_sse_event(text: str) -> str | None:
    """Parse an SSE event block and return task_id if it's a task event."""
    event_type = None
    data = None
    for line in text.strip().splitlines():
        if line.startswith("event: "):
            event_type = line[7:]
        elif line.startswith("data: "):
            data = line[6:]
    if event_type == "task" and data:
        try:
            return json.loads(data)["task_id"]
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def sse_listen(mcp: MCPClient, sse_url: str):
    """Connect to SSE endpoint and process task notifications. Reconnects on drop."""
    first_connect = True
    while True:
        if not first_connect:
            # On reconnect, check for upstream updates and restart if needed
            if check_self_update():
                logger.info("Code updated, exiting for restart...")
                sys.exit(0)
        first_connect = False
        try:
            logger.info("Connecting to SSE: %s", sse_url)
            with httpx.Client(timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10)) as client:
                with client.stream("GET", sse_url) as response:
                    response.raise_for_status()
                    logger.info("SSE connected")
                    buffer = ""
                    for chunk in response.iter_text():
                        buffer += chunk
                        while "\n\n" in buffer:
                            event_text, buffer = buffer.split("\n\n", 1)
                            event_text = event_text.strip()
                            if not event_text or event_text.startswith(":"):
                                continue  # comment/keepalive
                            task_id = parse_sse_event(event_text)
                            if task_id:
                                logger.info("Received task notification: %s", task_id)
                                thread = threading.Thread(
                                    target=execute_task,
                                    args=(mcp, task_id),
                                    daemon=True,
                                )
                                thread.start()
        except Exception as e:
            logger.warning("SSE connection lost: %s. Reconnecting in 5s...", e)
            time.sleep(5)


# ── Heartbeat ────────────────────────────────────────────────────────

def heartbeat_loop(mcp: MCPClient, interval: int):
    """Background thread that updates last_seen and cleans up old tasks."""
    while True:
        time.sleep(interval)
        update_heartbeat(mcp)
        cleanup_old_tasks(mcp)
        logger.debug("Heartbeat sent")


# ── Cleanup old tasks ────────────────────────────────────────────────

def cleanup_old_tasks(mcp: MCPClient, max_age_hours: int = 24):
    """Delete completed/failed tasks older than max_age_hours."""
    try:
        file_list = mcp.list(prefix="task-")
        if file_list == "No memory files found.":
            return
        for filename in file_list.splitlines():
            filename = filename.strip()
            if not filename:
                continue
            try:
                content = mcp.read(filename)
                status = parse_task_field(content, "status")
                if status not in ("completed", "failed"):
                    continue
                created = parse_task_field(content, "created")
                if not created:
                    continue
                created_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - created_time).total_seconds() / 3600
                if age_hours > max_age_hours:
                    mcp.delete(filename)
                    logger.info("Cleaned up old task: %s", filename)
            except Exception as e:
                logger.debug("Cleanup skip %s: %s", filename, e)
    except Exception as e:
        logger.debug("Cleanup failed: %s", e)


# ── Startup: check for pending tasks ─────────────────────────────────

def check_pending_tasks(mcp: MCPClient):
    """On startup, check for any pending tasks targeting this agent."""
    try:
        file_list = mcp.list(prefix="task-")
        if file_list == "No memory files found.":
            return
        for filename in file_list.splitlines():
            filename = filename.strip()
            if not filename:
                continue
            try:
                content = mcp.read(filename)
                status = parse_task_field(content, "status") or "pending"
                target = parse_task_field(content, "target").lstrip("@")
                if status == "pending" and (not target or target == AGENT_ID):
                    logger.info("Found pending task from before disconnect: %s", filename)
                    thread = threading.Thread(
                        target=execute_task,
                        args=(mcp, filename),
                        daemon=True,
                    )
                    thread.start()
            except Exception:
                pass
    except Exception as e:
        logger.debug("Pending task check failed: %s", e)


# ── Self-update on reconnect ──────────────────────────────────────────

def check_self_update() -> bool:
    """Check if the repo has upstream changes and pull + restart if so.

    Returns True if an update was applied (caller should exit so systemd restarts us).
    """
    try:
        # Fetch latest from origin
        result = subprocess.run(
            ["git", "fetch", "origin", "main"],
            capture_output=True, text=True, timeout=15,
            cwd=REPO_DIR,
        )
        if result.returncode != 0:
            logger.debug("git fetch failed: %s", result.stderr.strip())
            return False

        # Check if we're behind
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..origin/main"],
            capture_output=True, text=True, timeout=5,
            cwd=REPO_DIR,
        )
        behind = int(result.stdout.strip() or "0")
        if behind == 0:
            return False

        logger.info("Behind origin/main by %d commit(s), pulling...", behind)
        result = subprocess.run(
            ["git", "pull", "--ff-only", "origin", "main"],
            capture_output=True, text=True, timeout=30,
            cwd=REPO_DIR,
        )
        if result.returncode != 0:
            logger.warning("git pull failed: %s", result.stderr.strip())
            return False

        logger.info("Updated: %s", result.stdout.strip())
        return True

    except Exception as e:
        logger.debug("Self-update check failed: %s", e)
        return False


# ── Main ─────────────────────────────────────────────────────────────

def main():
    mcp = MCPClient(MCP_URL, token_name=AGENT_ID)

    # Register
    register_agent(mcp)
    register_device_info(mcp)

    # Pick up any tasks that arrived while we were offline
    check_pending_tasks(mcp)

    # Start heartbeat thread
    heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        args=(mcp, HEARTBEAT_INTERVAL),
        daemon=True,
    )
    heartbeat_thread.start()

    # Graceful shutdown
    def shutdown(signum, frame):
        logger.info("Shutting down...")
        try:
            mcp.edit(REG_FILENAME, "- status: online", "- status: offline")
        except Exception:
            pass
        mcp.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Connect to SSE and listen for tasks (blocks forever, reconnects on drop)
    sse_url = f"{MCP_URL}/events/{AGENT_ID}"
    logger.info("Agent daemon '%s' starting (SSE: %s)", AGENT_ID, sse_url)
    sse_listen(mcp, sse_url)


if __name__ == "__main__":
    main()
