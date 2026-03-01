# Multi-Agent Task Dispatch

This repo runs agent daemons on multiple machines, coordinated through the MCP memory server at `mcp.howling.one`.

## Machines

| Alias | Agent ID | Platform | Notes |
|-------|----------|----------|-------|
| @thinkpad | thinkpad | Arch Linux | X1 Carbon, primary dev laptop |
| @power | power | Windows 11 | Legion desktop, RTX GPU, PowerShell |
| @wsl | wsl | WSL2 (Ubuntu) | Legion desktop, Linux environment |
| @mac | mac | macOS | MacBook |
| @vps | vps | Ubuntu 24.04 | Hetzner VPS, runs MCP server |

## @ Dispatch Convention

When the user mentions `@agent_id` in a message (e.g. "@thinkpad check the logs", "@legion what's GPU usage"), dispatch a task to that machine:

1. **Create the task file** with `write_memory`:
   - Filename: `task-YYYYMMDD-HHMM-{short-desc}.md`
   - Use the template below
   - Always include explicit allowed commands

2. **Notify the agent** with `notify_agent("{agent_id}", "{task_filename}")`

3. **Tell the user** the task was dispatched and how to check results

### Task types

There are two task types: **query** (default) and **code-edit**.

#### Query task (check something, report output)

```markdown
# Task: {short-desc}

## Meta
- status: pending
- type: query
- created: {ISO 8601 UTC timestamp}
- created_by: {this machine's agent ID, or "user" if interactive}
- target: {target agent_id}
- timeout: 120

## Request
{What to do — be specific and concise}

## Allowed Commands
- {explicit command 1}
- {explicit command 2}

## Result
_(pending)_

## Log
- {timestamp} [{creator}] Created task
```

#### Code-edit task (modify files in the synced repo)

The daemon automatically handles: lock acquisition → edit → git commit → lock release.

```markdown
# Task: {short-desc}

## Meta
- status: pending
- type: code-edit
- created: {ISO 8601 UTC timestamp}
- created_by: {this machine's agent ID, or "user" if interactive}
- target: {target agent_id}
- files: {comma-separated list of files to edit, e.g. agent_daemon.py, mcp_client.py}
- timeout: 300

## Request
{What to change — be specific about the desired behavior}

## Allowed Commands
- {any shell commands needed, e.g. running tests}

## Result
_(pending)_

## Log
- {timestamp} [{creator}] Created task
```

The `files` field is required for code-edit tasks. The daemon will:
1. Acquire MCP file locks for each listed file
2. Wait for Syncthing to settle (ensure latest files)
3. Run `claude --print` with Edit, Read, and Bash tools
4. Git add + commit the changed files
5. Release all locks
6. Syncthing propagates the commit to all machines

### Common allowed commands by platform

**Linux (thinkpad, wsl):**
- `uptime`, `free -h`, `df -h`, `ps aux`, `top -bn1`
- `ip addr`, `ss -tlnp`, `ping -c 3 {host}`
- `systemctl status {service}`, `journalctl -u {service} -n 50`
- `cat {path}`, `ls -la {path}`, `find {path} -name {pattern}`

**Windows (power):**
- `nvidia-smi`
- `tasklist /FI "IMAGENAME eq {name}"`
- `Get-Process`, `Get-Service`
- `systeminfo`, `wmic cpu get loadpercentage`

**macOS (mac):**
- `system_profiler SPHardwareDataType`, `top -l 1`
- `diskutil list`, `df -h`
- `pmset -g batt` (battery status)

## Checking Results

When the user asks about a dispatched task ("what did thinkpad say?", "is it done?"):
1. `read_memory("{task_filename}")` to check the task file
2. If status is `completed` → show the Result section
3. If status is `running` or `pending` → let the user know it's still in progress
4. If status is `failed` → show the error

## File Locking

File locking is handled automatically by the daemon for `code-edit` tasks. Lock files are stored in the MCP server as `lock-{safe-filename}.md`. Stale locks (holder offline >10 min) are broken automatically.

If you need to manually check or break a lock:
- Check: `read_memory("lock-{safe-filename}.md")` (e.g. `lock-agent-daemon-py.md`)
- Break: `delete_memory("lock-{safe-filename}.md")`
