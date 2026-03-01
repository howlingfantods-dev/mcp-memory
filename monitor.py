#!/usr/bin/env python3
"""Real-time monitor for MCP server activity.

Connects to the /monitor SSE endpoint and prints a formatted, color-coded
stream of all server events (tool calls, task state changes, agent
connections, notifications).

Usage:
    python3 monitor.py
    python3 monitor.py --no-color
    python3 monitor.py --filter action=task
    python3 monitor.py --filter agent=thinkpad
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import httpx

from mcp_client import MCPClient

MCP_URL = os.environ.get("MCP_URL", "https://mcp.howling.one")

# ANSI color codes
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

EVENT_COLORS = {
    "connect": GREEN,
    "disconnect": RED,
    "task": YELLOW,
    "request": YELLOW,
    "response": GREEN,
}


def format_tokens(tok_in, tok_out) -> str:
    def _fmt(n):
        if n >= 1000:
            return f"{n / 1000:.1f}k"
        return str(n)
    return f"{_fmt(tok_in)}/{_fmt(tok_out)} tok"


def _caller_tag(event: dict, use_color: bool) -> str:
    device = event.get("device")
    client = event.get("client")
    if device:
        tag = device
    elif client:
        tag = client[:8]
    else:
        return ""
    if use_color:
        return f" {DIM}[{tag}]{RESET}"
    return f" [{tag}]"


def format_event(event: dict, use_color: bool) -> str:
    etype = event.get("action", "unknown")
    ts = event.get("ts", "")
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
            ts = dt.strftime("%H:%M:%S")
        except Exception:
            ts = ts[-8:]
    else:
        ts = datetime.now().strftime("%H:%M:%S")

    color = EVENT_COLORS.get(etype, "") if use_color else ""
    reset = RESET if use_color else ""

    label = etype.upper().ljust(10)
    caller = _caller_tag(event, use_color)

    if etype in ("connect", "disconnect"):
        detail = event.get("agent", "")
        return f"{ts} {color}{label}{reset} {detail}"

    if etype == "request" and "tool" in event:
        eid = event.get("id", "")
        tool = event.get("tool", "").replace("_memory", "")
        sender = event.get("from", "")
        f = event.get("resource", "")
        b = event.get("bytes")
        id_tag = f"{DIM}#{eid}{RESET} " if use_color and eid else f"#{eid} " if eid else ""
        from_tag = f"{BOLD}@{sender}{RESET} " if use_color and sender else f"@{sender} " if sender else ""
        parts = [f"{ts} {id_tag}{color}{label}{reset} {from_tag}{tool.ljust(14)} {f}"]
        if b is not None:
            parts.append(format_bytes(b))
        prefix = event.get("prefix")
        if prefix:
            parts.append(f"prefix={prefix}")
        query = event.get("query")
        if query:
            parts.append(f"q=\"{query}\"")
        return "  ".join(p for p in parts if p)

    if etype in ("request", "response"):
        eid = event.get("id", "")
        sender = event.get("from", "")
        receiver = event.get("to", "")
        task = event.get("task", "")
        status = event.get("status", "")
        query = event.get("query", "")
        online = "online" if event.get("online") else "offline"
        id_tag = f"{DIM}#{eid}{RESET} " if use_color and eid else f"#{eid} " if eid else ""
        if use_color:
            arrow = f"{BOLD}@{sender}{RESET} -> {BOLD}@{receiver}{RESET}"
        else:
            arrow = f"@{sender} -> @{receiver}"
        parts = [f"{ts} {id_tag}{color}{label}{reset} {arrow}  {status}  {online}"]
        if query:
            q = query if len(query) <= 80 else query[:77] + "..."
            parts.append(f"\"{q}\"")
        errors = event.get("errors", [])
        for err in errors:
            e = err if len(err) <= 80 else err[:77] + "..."
            if use_color:
                parts.append(f"{RED}error: {e}{RESET}")
            else:
                parts.append(f"error: {e}")
        return "  ".join(p for p in parts if p)

    if etype == "task":
        eid = event.get("id", "")
        task = event.get("task", "")
        status = event.get("status", "")
        agent = event.get("agent", "")
        id_tag = f"{DIM}#{eid}{RESET} " if use_color and eid else f"#{eid} " if eid else ""
        parts = [f"{ts} {id_tag}{color}{label}{reset} {task.ljust(28)} {status.ljust(12)} @{agent}"]
        if event.get("tokens_in") is not None:
            parts.append(format_tokens(event["tokens_in"], event["tokens_out"]))
        if event.get("cost_usd") is not None:
            parts.append(f"${event['cost_usd']:.4f}")
        if event.get("duration_ms") is not None:
            parts.append(f"{event['duration_ms'] / 1000:.1f}s")
        return "  ".join(parts)

    return f"{ts} {color}{label}{reset} {json.dumps(event)}"


def matches_filter(event: dict, filters: dict) -> bool:
    for key, val in filters.items():
        if str(event.get(key, "")) != val:
            return False
    return True


def parse_filters(filter_args: list[str] | None) -> dict:
    if not filter_args:
        return {}
    result = {}
    for f in filter_args:
        if "=" in f:
            k, v = f.split("=", 1)
            result[k] = v
    return result


def stream_monitor(url: str, use_color: bool, filters: dict):
    client = MCPClient(url, token_name="monitor")
    client._ensure_auth()
    token = client._access_token

    monitor_url = f"{url}/monitor"
    headers = {"Authorization": f"Bearer {token}"}

    print(f"Connecting to {monitor_url}...")
    with httpx.Client(timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10)) as http:
        while True:
            try:
                with http.stream("GET", monitor_url, headers=headers) as resp:
                    resp.raise_for_status()
                    if use_color:
                        print(f"{GREEN}Connected{RESET}\n")
                    else:
                        print("Connected\n")
                    buffer = ""
                    for chunk in resp.iter_text():
                        buffer += chunk
                        while "\n\n" in buffer:
                            block, buffer = buffer.split("\n\n", 1)
                            block = block.strip()
                            if not block or block.startswith(":"):
                                continue
                            data_line = None
                            for line in block.splitlines():
                                if line.startswith("data: "):
                                    data_line = line[6:]
                            if not data_line:
                                continue
                            try:
                                event = json.loads(data_line)
                            except json.JSONDecodeError:
                                continue
                            if not matches_filter(event, filters):
                                continue
                            print(format_event(event, use_color))
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    client._access_token = None
                    client._ensure_auth()
                    token = client._access_token
                    headers = {"Authorization": f"Bearer {token}"}
                    continue
                print(f"HTTP error: {e}", file=sys.stderr)
            except (httpx.RemoteProtocolError, httpx.ReadTimeout):
                pass
            except KeyboardInterrupt:
                print("\nDisconnected.")
                return

            import time
            time.sleep(2)
            if use_color:
                print(f"\n{DIM}Reconnecting...{RESET}")
            else:
                print("\nReconnecting...")


def main():
    parser = argparse.ArgumentParser(description="MCP server activity monitor")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    parser.add_argument("--filter", action="append", dest="filters",
                        metavar="KEY=VAL", help="Filter events (e.g. action=task, agent=thinkpad)")
    args = parser.parse_args()

    use_color = not args.no_color and sys.stdout.isatty()
    filters = parse_filters(args.filters)
    stream_monitor(MCP_URL, use_color, filters)


if __name__ == "__main__":
    main()
