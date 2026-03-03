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

def now_ts() -> int:
    return int(time.time())


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


def update_heartbeat():
    """POST heartbeat to server — single HTTP call, no MCP."""
    try:
        httpx.post(f"{MCP_URL}/heartbeat/{AGENT_ID}", timeout=5)
    except Exception as e:
        logger.warning("Heartbeat failed: %s", e)


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
    """Extract a field value from task content (JSON only)."""
    data = _try_parse_json(content)
    if data is None:
        return ""
    val = data.get(field)
    if val is None:
        aliases = {"assigned_to": "target", "target": "assigned_to"}
        val = data.get(aliases.get(field, ""))
    if val is None:
        return ""
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    return str(val)


def parse_allowed_commands(content: str) -> list[str]:
    """Extract allowed commands from task content (JSON only)."""
    data = _try_parse_json(content)
    if data is None:
        return []
    cmds = data.get("allowed_commands", [])
    return cmds if isinstance(cmds, list) else []


def parse_request(content: str) -> str:
    """Extract the request text from task content (JSON only)."""
    data = _try_parse_json(content)
    if data is None:
        return ""
    return data.get("request", "") or data.get("prompt", "")


def parse_files_list(content: str) -> list[str]:
    """Extract the files list from task content (JSON only)."""
    data = _try_parse_json(content)
    if data is None:
        return []
    files = data.get("files", [])
    if isinstance(files, list):
        return [f for f in files if f]
    return []


# ── File Locking (server-side via HTTP) ───────────────────────────────

def acquire_locks(files: list[str]) -> list[str]:
    """Acquire file locks via server HTTP endpoint."""
    acquired = []
    for filepath in files:
        try:
            resp = httpx.post(f"{MCP_URL}/lock/{AGENT_ID}/{filepath}", timeout=10)
            result = resp.json()
            if not result.get("ok"):
                logger.warning("Lock denied for %s: %s", filepath, result.get("msg"))
                release_locks(acquired)
                return []
            acquired.append(filepath)
            logger.info("Acquired lock: %s", filepath)
        except Exception as e:
            logger.error("Lock request failed for %s: %s", filepath, e)
            release_locks(acquired)
            return []
    return acquired


def release_locks(files: list[str]):
    """Release file locks via server HTTP endpoint."""
    for filepath in files:
        try:
            httpx.delete(f"{MCP_URL}/lock/{AGENT_ID}/{filepath}", timeout=5)
            logger.info("Released lock: %s", filepath)
        except Exception:
            pass


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


def _edit_status(mcp: MCPClient, task_id: str, old_status: str, new_status: str, content: str | None = None):
    """Update task status (JSON only)."""
    fresh = _try_parse_json(mcp.read(task_id))
    if fresh is None:
        logger.warning("Cannot parse task %s as JSON", task_id)
        return
    fresh["status"] = new_status
    mcp.write(task_id, json.dumps(fresh, indent=2))


def _edit_result(mcp: MCPClient, task_id: str, result_text: str, content: str | None = None):
    """Write result text (JSON only)."""
    fresh = _try_parse_json(mcp.read(task_id))
    if fresh is None:
        logger.warning("Cannot parse task %s as JSON", task_id)
        return
    fresh["result"] = result_text
    mcp.write(task_id, json.dumps(fresh, indent=2))


def append_log(mcp: MCPClient, task_id: str, agent: str, message: str, content: str | None = None):
    """Append a log entry to the task file (JSON only)."""
    fresh = _try_parse_json(mcp.read(task_id))
    if fresh is None:
        logger.warning("Cannot parse task %s as JSON", task_id)
        return
    if "log" not in fresh or not isinstance(fresh["log"], list):
        fresh["log"] = []
    fresh["log"].append({"ts": now_ts(), "agent": agent, "msg": message})
    mcp.write(task_id, json.dumps(fresh, indent=2))


def _set_running(mcp: MCPClient, task_id: str, data: dict | None = None) -> dict | None:
    """Transition task from claimed to running (single write for JSON)."""
    if data is not None:
        data["status"] = "running"
        data.setdefault("log", []).append({"ts": now_ts(), "agent": AGENT_ID, "msg": "Executing task"})
        mcp.write(task_id, json.dumps(data, indent=2))
        return data
    content = mcp.read(task_id)
    _edit_status(mcp, task_id, "claimed", "running", content=content)
    append_log(mcp, task_id, AGENT_ID, "Executing task")
    return None


def _complete_task(mcp: MCPClient, task_id: str, result_text: str, log_msg: str, data: dict | None = None) -> dict | None:
    """Set result + status=completed + log entry (single write for JSON)."""
    if data is not None:
        data["result"] = result_text
        data["status"] = "completed"
        data.setdefault("log", []).append({"ts": now_ts(), "agent": AGENT_ID, "msg": log_msg})
        mcp.write(task_id, json.dumps(data, indent=2))
        return data
    content = mcp.read(task_id)
    _edit_result(mcp, task_id, result_text, content=content)
    _edit_status(mcp, task_id, "running", "completed", content=content)
    append_log(mcp, task_id, AGENT_ID, log_msg)
    return None


def claim_task(mcp: MCPClient, task_id: str, content: str | None = None) -> dict | bool:
    """Attempt to claim a task. Returns data dict on success, False on failure."""
    try:
        if content is None:
            content = mcp.read(task_id)
    except Exception as e:
        logger.info("Could not read task %s: %s", task_id, e)
        return False
    try:
        data = _try_parse_json(content)
        if data is None:
            logger.info("Task %s is not valid JSON", task_id)
            return False
        if data.get("status", "pending") != "pending":
            logger.info("Could not find pending status in task %s", task_id)
            return False
        data["status"] = "claimed"
        data.setdefault("log", []).append({"ts": now_ts(), "agent": AGENT_ID, "msg": "Claimed task"})
        mcp.write(task_id, json.dumps(data, indent=2))
        return data
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
    if target and target not in (AGENT_ID, "here"):
        logger.info("Task %s is for '%s', not us ('%s'). Skipping.", task_id, target, AGENT_ID)
        return

    # Check status — treat missing/empty status as "pending"
    status = parse_task_field(content, "status") or "pending"
    if status != "pending":
        logger.info("Task %s status is '%s', not 'pending'. Skipping.", task_id, status)
        return

    # Claim — returns data dict on success, False on failure
    claim_result = claim_task(mcp, task_id, content=content)
    if not claim_result:
        return
    task_data = claim_result

    # Set running
    try:
        task_data = _set_running(mcp, task_id, data=task_data)
    except Exception as e:
        logger.error("Failed to set running status: %s", e)
        return

    # Parse task details
    request = parse_request(content)
    allowed_commands = parse_allowed_commands(content)
    timeout = int(parse_task_field(content, "timeout") or MAX_TASK_DURATION)
    task_type = parse_task_field(content, "type") or "query"
    files = parse_files_list(content)

    if task_type.lower() == "code-edit":
        _execute_code_edit(mcp, task_id, request, files, allowed_commands, timeout, content, task_data)
    else:
        _execute_query(mcp, task_id, request, allowed_commands, timeout, content, task_data)


def _build_system_prompt(request_type: str, allowed_commands: list[str]) -> str:
    """Build system prompt constraints for claude --print."""
    constraints = [
        f"You are executing a task on machine '{AGENT_ID}' ({AGENT_PLATFORM}).",
        "Do NOT create or dispatch tasks to other agents. If you need information from another node, say so in your response and let the user decide.",
    ]

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
    """Run claude --print with stream-json, forwarding text deltas as thinking."""
    cmd = [
        "claude", "--print",
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--allowedTools", allowed_tools,
        "--append-system-prompt", system_prompt,
    ]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_DIR,
        env=env,
    )
    proc.stdin.write(request)
    proc.stdin.close()

    result_text = ""
    success = False
    text_chunks = []
    lock = threading.Lock()
    last_flush = time.time()

    def flush_thinking():
        nonlocal last_flush
        with lock:
            pending = "".join(text_chunks)
            text_chunks.clear()
        if pending:
            _flush_thinking(AGENT_ID, task_id, pending)
            last_flush = time.time()

    def reader():
        nonlocal result_text, success
        for raw_line in proc.stdout:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                msg = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "stream_event":
                evt = msg.get("event", {})
                if evt.get("type") == "content_block_delta":
                    delta = evt.get("delta", {})
                    if delta.get("type") == "text_delta":
                        with lock:
                            text_chunks.append(delta["text"])
            elif mtype == "result":
                result_text = msg.get("result", "")
                success = not msg.get("is_error", False)

    read_thread = threading.Thread(target=reader, daemon=True)
    read_thread.start()

    deadline = time.time() + timeout
    while read_thread.is_alive():
        remaining = deadline - time.time()
        if remaining <= 0:
            proc.kill()
            proc.wait()
            read_thread.join(timeout=2)
            raise subprocess.TimeoutExpired(cmd, timeout)
        read_thread.join(timeout=min(1.0, remaining))
        if time.time() - last_flush >= 1.0:
            flush_thinking()

    flush_thinking()
    proc.wait()

    if not result_text and proc.returncode != 0:
        stderr = proc.stderr.read()
        result_text = f"Process exited {proc.returncode}"
        if stderr:
            result_text += f"\n\nSTDERR:\n{stderr.strip()}"

    return result_text, success


def _execute_query(mcp: MCPClient, task_id: str, request: str,
                   allowed_commands: list[str], timeout: int, content: str,
                   task_data: dict | None = None):
    """Execute a query task (check something, report output)."""
    system_prompt = _build_system_prompt("query", allowed_commands)

    logger.info("Invoking claude --print for query task %s", task_id)
    try:
        output, success = _invoke_claude(request, system_prompt, timeout, task_id=task_id)

        _complete_task(mcp, task_id, output or "_(no output)_", "Task completed", data=task_data)
        logger.info("Task %s completed", task_id)

    except subprocess.TimeoutExpired:
        _fail_task(mcp, task_id, f"Timed out after {timeout}s", data=task_data)
        logger.warning("Task %s timed out", task_id)

    except Exception as e:
        _fail_task(mcp, task_id, str(e), data=task_data)

    _notify_creator(mcp, task_id, content)


def _execute_code_edit(mcp: MCPClient, task_id: str, request: str,
                       files: list[str], allowed_commands: list[str],
                       timeout: int, content: str,
                       task_data: dict | None = None):
    """Execute a code-edit task: lock → edit → git commit → unlock."""
    if not files:
        _fail_task(mcp, task_id, "code-edit task requires '- files:' field listing files to edit", data=task_data)
        _notify_creator(mcp, task_id, content)
        return

    # 1. Acquire file locks
    logger.info("Acquiring locks for %s", files)
    append_log(mcp, task_id, AGENT_ID, f"Acquiring locks: {', '.join(files)}")
    locked = acquire_locks(files)
    if not locked:
        _fail_task(mcp, task_id, f"Could not acquire locks for: {', '.join(files)}. Another agent is editing.", data=task_data)
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
            _fail_task(mcp, task_id, f"Timed out after {timeout}s", data=task_data)
            return

        # 4. Git commit the changed files
        commit_msg = f"[{AGENT_ID}] {request[:72]}"
        git_result = git_commit_files(files, commit_msg)
        logger.info("Git: %s", git_result)

        # 5. Write result
        result_text = output or "_(no output)_"
        result_text += f"\n\n**Git:** {git_result}"
        _complete_task(mcp, task_id, result_text, f"Task completed. {git_result}", data=task_data)
        logger.info("Code-edit task %s completed", task_id)

    except Exception as e:
        _fail_task(mcp, task_id, str(e), data=task_data)

    finally:
        # 6. Always release locks
        release_locks(locked)

    _notify_creator(mcp, task_id, content)


def _fail_task(mcp: MCPClient, task_id: str, error: str, data: dict | None = None):
    """Mark a task as failed."""
    try:
        if data is None:
            data = _try_parse_json(mcp.read(task_id)) or {}
        data["status"] = "failed"
        data["result"] = f"_(error: {error})_"
        data.setdefault("log", []).append({"ts": now_ts(), "agent": AGENT_ID, "msg": f"Task failed: {error}"})
        mcp.write(task_id, json.dumps(data, indent=2))
    except Exception as e:
        logger.error("Failed to update task %s during failure handling: %s", task_id, e)
    logger.error("Task %s failed: %s", task_id, error)


def _notify_creator(mcp: MCPClient, task_id: str, content: str):
    """Best-effort notification to the task creator."""
    data = _try_parse_json(content)
    created_by = data.get("created_by", "") if data else ""
    if created_by and created_by != AGENT_ID:
        try:
            mcp.notify_agent(created_by, id=task_id)
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
    """Background thread that updates heartbeat and cleans up old tasks."""
    while True:
        time.sleep(interval)
        update_heartbeat()
        cleanup_old_tasks(mcp)
        logger.debug("Heartbeat sent")


# ── Cleanup old tasks ────────────────────────────────────────────────

def cleanup_old_tasks(mcp: MCPClient, max_age_hours: int = 24):
    """Delete completed/failed tasks older than max_age_hours."""
    try:
        file_list = mcp.list()
        if file_list == "No memory files found.":
            return
        for filename in file_list.splitlines():
            filename = filename.strip()
            if not filename:
                continue
            try:
                content = mcp.read(filename)
                status = parse_task_field(content, "status")
                if not status or status not in ("completed", "failed"):
                    continue
                created = parse_task_field(content, "created")
                if not created:
                    continue
                age_hours = (int(time.time()) - int(created)) / 3600
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
        file_list = mcp.list()
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
                if status == "pending" and (not target or target in (AGENT_ID, "here")):
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

    # Register device info and send initial heartbeat
    register_device_info(mcp)
    update_heartbeat()

    # Pick up any tasks that arrived while we were offline
    check_pending_tasks(mcp)

    # Start heartbeat thread
    heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        args=(mcp, HEARTBEAT_INTERVAL),
        daemon=True,
    )
    heartbeat_thread.start()

    # Graceful shutdown — server detects offline via SSE disconnect + stale heartbeat
    def shutdown(signum, frame):
        logger.info("Shutting down...")
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
