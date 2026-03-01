#!/usr/bin/env python3
"""Agent daemon — lightweight HTTP server that executes tasks on demand.

Receives webhook notifications from the MCP server, reads/claims tasks,
invokes `claude --print` for execution, and writes results back.
Zero idle cost: no polling, no LLM tokens burned on coordination.

Usage:
    AGENT_ID=thinkpad MCP_URL=https://mcp.howling.one python3 agent_daemon.py

Environment variables:
    AGENT_ID        - Unique agent identifier (e.g. "thinkpad", "legion")
    MCP_URL         - Base URL of the MCP memory server
    DAEMON_PORT     - Webhook listener port (default: 9100)
    PLATFORM        - Override platform detection (linux/darwin/win32)
    AGENT_ROLES     - Comma-separated roles (default: "general")
    HEARTBEAT_INTERVAL - Seconds between heartbeats (default: 300)
    MAX_TASK_DURATION  - Max seconds for task execution (default: 300)
    BLOCKED_PATHS      - Comma-separated paths to block (default: "/etc,/boot,~/.ssh")
"""

import json
import logging
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

from mcp_client import MCPClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("agent-daemon")

# ── Configuration ────────────────────────────────────────────────────

AGENT_ID = os.environ.get("AGENT_ID", socket.gethostname())
MCP_URL = os.environ.get("MCP_URL", "https://mcp.howling.one")
DAEMON_PORT = int(os.environ.get("DAEMON_PORT", "9100"))
AGENT_PLATFORM = os.environ.get("PLATFORM", sys.platform)
AGENT_ROLES = os.environ.get("AGENT_ROLES", "general").split(",")
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "300"))
MAX_TASK_DURATION = int(os.environ.get("MAX_TASK_DURATION", "300"))
BLOCKED_PATHS = os.environ.get("BLOCKED_PATHS", "/etc,/boot,~/.ssh").split(",")

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

def build_registration(webhook_url: str) -> str:
    return f"""# Agent: {AGENT_ID}

## Identity
- hostname: {socket.gethostname()}
- platform: {AGENT_PLATFORM}
- registered: {now_iso()}

## Endpoint
- webhook: {webhook_url}
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


def register_agent(mcp: MCPClient, webhook_url: str):
    """Write or overwrite agent registration file."""
    content = build_registration(webhook_url)
    mcp.write(REG_FILENAME, content)
    logger.info("Registered agent '%s' with webhook %s", AGENT_ID, webhook_url)


def update_heartbeat(mcp: MCPClient):
    """Update last_seen timestamp — direct HTTP, no LLM."""
    try:
        mcp.edit(
            REG_FILENAME,
            old_text="- last_seen: ",  # Match prefix
            new_text=f"- last_seen: {now_iso()}",
        )
    except Exception:
        # If prefix match fails (edit_memory matches full line), read and replace
        try:
            content = mcp.read(REG_FILENAME)
            for line in content.splitlines():
                if line.strip().startswith("- last_seen:"):
                    mcp.edit(REG_FILENAME, line.strip(), f"- last_seen: {now_iso()}")
                    break
        except Exception as e:
            logger.warning("Heartbeat update failed: %s", e)


# ── Task Handling ────────────────────────────────────────────────────

def parse_task_field(content: str, field: str) -> str:
    """Extract a field value from task markdown."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"- {field}:"):
            return stripped[len(f"- {field}:"):].strip()
    return ""


def parse_allowed_commands(content: str) -> list[str]:
    """Extract allowed commands from the ## Allowed Commands section."""
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
    """Extract the request text from the ## Request section."""
    lines = []
    in_section = False
    for line in content.splitlines():
        if line.strip() == "## Request":
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            lines.append(line)
    return "\n".join(lines).strip()


def append_log(mcp: MCPClient, task_id: str, content: str, agent: str, message: str):
    """Append a log entry to the task file."""
    log_entry = f"\n- {now_iso()} [{agent}] {message}"
    mcp.edit(task_id, "## Log", f"## Log{log_entry}")


def claim_task(mcp: MCPClient, task_id: str) -> bool:
    """Attempt to claim a task via optimistic locking."""
    try:
        mcp.edit(task_id, "- status: pending", f"- status: claimed")
        append_log(mcp, task_id, "", AGENT_ID, "Claimed task")
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

    # Verify it's for us
    target = parse_task_field(content, "target")
    if target and target != AGENT_ID:
        logger.info("Task %s is for '%s', not us ('%s'). Skipping.", task_id, target, AGENT_ID)
        return

    # Check status
    status = parse_task_field(content, "status")
    if status != "pending":
        logger.info("Task %s status is '%s', not 'pending'. Skipping.", task_id, status)
        return

    # Claim
    if not claim_task(mcp, task_id):
        return

    # Set running
    try:
        mcp.edit(task_id, "- status: claimed", "- status: running")
        append_log(mcp, task_id, "", AGENT_ID, "Executing task")
    except Exception as e:
        logger.error("Failed to set running status: %s", e)
        return

    # Parse task details
    request = parse_request(content)
    allowed_commands = parse_allowed_commands(content)
    timeout = int(parse_task_field(content, "timeout") or MAX_TASK_DURATION)

    # Build system prompt for claude
    constraints = [
        f"You are executing a task on machine '{AGENT_ID}' ({AGENT_PLATFORM}).",
        "Execute the requested task and provide the output.",
    ]
    if allowed_commands:
        cmd_list = "\n".join(f"  - {cmd}" for cmd in allowed_commands)
        constraints.append(f"You may ONLY run these commands:\n{cmd_list}")
        constraints.append("Do NOT run any commands not on this list.")
    if BLOCKED_PATHS:
        constraints.append(f"Do NOT access these paths: {', '.join(BLOCKED_PATHS)}")

    system_prompt = "\n".join(constraints)

    # Invoke claude --print
    logger.info("Invoking claude --print for task %s", task_id)
    try:
        cmd = [
            "claude", "--print",
            "--allowedTools", "Bash(.*)",
            "--systemPrompt", system_prompt,
            request,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output += f"\n\nSTDERR:\n{result.stderr.strip()}"

        # Write result
        mcp.edit(task_id, "_(pending)_", output or "_(no output)_")
        mcp.edit(task_id, "- status: running", "- status: completed")
        append_log(mcp, task_id, "", AGENT_ID, "Task completed")
        logger.info("Task %s completed", task_id)

    except subprocess.TimeoutExpired:
        mcp.edit(task_id, "- status: running", "- status: failed")
        mcp.edit(task_id, "_(pending)_", f"_(timed out after {timeout}s)_")
        append_log(mcp, task_id, "", AGENT_ID, f"Task timed out after {timeout}s")
        logger.warning("Task %s timed out", task_id)

    except Exception as e:
        try:
            mcp.edit(task_id, "- status: running", "- status: failed")
            mcp.edit(task_id, "_(pending)_", f"_(error: {e})_")
            append_log(mcp, task_id, "", AGENT_ID, f"Task failed: {e}")
        except Exception:
            pass
        logger.error("Task %s failed: %s", task_id, e)

    # Notify creator that task is done (best-effort)
    created_by = parse_task_field(content, "created_by")
    if created_by and created_by != AGENT_ID:
        try:
            mcp.notify_agent(created_by, task_id)
            logger.info("Notified '%s' about completed task %s", created_by, task_id)
        except Exception:
            pass  # Best-effort


# ── Webhook HTTP Server ──────────────────────────────────────────────

class WebhookHandler(BaseHTTPRequestHandler):
    """Handles incoming webhook notifications."""

    mcp_client: MCPClient  # set on the class before serving

    def do_POST(self):
        if self.path != "/task":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        task_id = data.get("task_id")
        if not task_id:
            self.send_error(400, "Missing task_id")
            return

        # Respond immediately, process async
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "accepted"}).encode())

        # Execute in a thread so we don't block the server
        thread = threading.Thread(
            target=execute_task,
            args=(self.__class__.mcp_client, task_id),
            daemon=True,
        )
        thread.start()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "agent_id": AGENT_ID,
                "status": "online",
                "timestamp": now_iso(),
            }).encode())
            return
        self.send_error(404)

    def log_message(self, format, *args):
        logger.debug("HTTP %s", format % args)


# ── Heartbeat ────────────────────────────────────────────────────────

def heartbeat_loop(mcp: MCPClient, interval: int):
    """Background thread that updates last_seen periodically."""
    while True:
        time.sleep(interval)
        update_heartbeat(mcp)
        logger.debug("Heartbeat sent")


# ── Cleanup old tasks ────────────────────────────────────────────────

def cleanup_old_tasks(mcp: MCPClient, max_age_hours: int = 24):
    """Delete completed/failed tasks older than max_age_hours. Called during heartbeat."""
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
                # Check age from created timestamp
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


# ── Main ─────────────────────────────────────────────────────────────

def main():
    # Determine webhook URL
    # In production, this should be the Tailscale IP or a configured address
    webhook_host = os.environ.get("WEBHOOK_HOST", "")
    if not webhook_host:
        # Try to detect Tailscale IP
        try:
            result = subprocess.run(
                ["tailscale", "ip", "-4"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                webhook_host = result.stdout.strip()
        except Exception:
            pass
    if not webhook_host:
        webhook_host = "127.0.0.1"
        logger.warning("No Tailscale IP found, using localhost. Set WEBHOOK_HOST for remote access.")

    webhook_url = f"http://{webhook_host}:{DAEMON_PORT}/task"

    # Initialize MCP client
    mcp = MCPClient(MCP_URL, token_name=AGENT_ID)

    # Set mcp client on handler class
    WebhookHandler.mcp_client = mcp

    # Register
    register_agent(mcp, webhook_url)

    # Start heartbeat thread
    heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        args=(mcp, HEARTBEAT_INTERVAL),
        daemon=True,
    )
    heartbeat_thread.start()

    # Handle shutdown gracefully
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

    # Start HTTP server
    server = HTTPServer(("0.0.0.0", DAEMON_PORT), WebhookHandler)
    logger.info(
        "Agent daemon '%s' listening on :%d (webhook: %s)",
        AGENT_ID, DAEMON_PORT, webhook_url,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
