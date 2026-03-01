# Multi-Agent Task Dispatch

This repo runs agent daemons on multiple machines, coordinated through the MCP memory server at `mcp.howling.one`.

## Machines

| Alias | Agent ID | Platform | Notes |
|-------|----------|----------|-------|
| @thinkpad | thinkpad | Arch Linux | X1 Carbon, primary dev laptop |
| @legion | legion | Windows 11 | Desktop, RTX GPU, streaming/encoding |
| @wsl | wsl | WSL2 (Ubuntu) | Linux environment on the Windows desktop |
| @mac | mac | macOS | MacBook |

## @ Dispatch Convention

When the user mentions `@agent_id` in a message (e.g. "@thinkpad check the logs", "@legion what's GPU usage"), dispatch a task to that machine:

1. **Create the task file** with `write_memory`:
   - Filename: `task-YYYYMMDD-HHMM-{short-desc}.md`
   - Use the template below
   - Always include explicit allowed commands

2. **Notify the agent** with `notify_agent("{agent_id}", "{task_filename}")`

3. **Tell the user** the task was dispatched and how to check results

### Task template

```markdown
# Task: {short-desc}

## Meta
- status: pending
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

### Common allowed commands by platform

**Linux (thinkpad, wsl):**
- `uptime`, `free -h`, `df -h`, `ps aux`, `top -bn1`
- `ip addr`, `ss -tlnp`, `ping -c 3 {host}`
- `systemctl status {service}`, `journalctl -u {service} -n 50`
- `cat {path}`, `ls -la {path}`, `find {path} -name {pattern}`

**Windows (legion):**
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

## File Locking (for code edits via Syncthing)

When an agent needs to edit files in the synced repo:

1. **Check** for existing lock: `read_memory("lock-{safe-filename}.md")`
   - If it exists, another agent is editing — wait or work on something else
2. **Lock**: `write_memory("lock-{safe-filename}.md", "- holder: {agent_id}\n- acquired: {timestamp}\n- file: {filename}\n")`
3. **Edit** the file, commit locally with git
4. **Unlock**: `delete_memory("lock-{safe-filename}.md")`

Stale lock recovery: if the holder's `agent-reg-{id}.md` shows `status: offline` and `last_seen` is >10 minutes ago, the lock can be broken.
