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

## Node Identity

Each node should know who it is. To identify yourself:
- Read `devices.json` via `read_memory("devices.json")` — it maps OAuth client_ids to device names, specs, and aliases.
- Your hostname or platform can also be matched against the Machines table above.
- **Always** refer to other nodes by their `@alias` (e.g. `@power`, `@wsl`, `@arch`) — never use bare names like "Power" or "the WSL machine". This keeps it unambiguous and consistent with the dispatch convention.
- Use your own alias when setting `created_by` in task files.

## Inter-Node Communication

When you need information that another node is uniquely positioned to provide — **dispatch a task to that node** instead of telling the user you can't access it or asking them to check manually.

Examples of when to dispatch:
- You need GPU stats → dispatch to `@power` (it has the RTX 5070 Ti)
- You need to check a Windows service → dispatch to `@power`
- You need a macOS-specific check → dispatch to `@mac`
- You need to verify something on the VPS → dispatch to `@vps`
- You need Linux system info from the laptop → dispatch to `@arch`

Examples of when NOT to dispatch:
- The info is available locally on your own node
- The user already provided the info
- The question is about code/files synced across all nodes (just read locally)

When two or more nodes are running interactive sessions simultaneously, they can coordinate through the MCP server: read each other's recent task files, check agent registration status, or dispatch queries directly.

## @ Dispatch Convention

When the user mentions `@agent_id` in a message (e.g. "@thinkpad check the logs", "@power what's GPU usage"), or when you determine another node can answer better, dispatch a task to that machine:

1. **Create the task file** with `write_memory`:
   - Filename: `task-YYYYMMDD-HHMM-{short-desc}.json`
   - Use the JSON template below
   - Always include explicit allowed commands

2. **Notify the agent** with `notify_agent("{agent_id}", "{task_filename}")`

3. **Tell the user** the task was dispatched and how to check results

### Task types

There are two task types: **query** (default) and **code-edit**.

#### Query task (check something, report output)

```json
{
  "title": "{short description}",
  "status": "pending",
  "type": "query",
  "created": "{ISO 8601 UTC timestamp}",
  "created_by": "{this machine's agent ID, or 'user' if interactive}",
  "target": "{target agent_id}",
  "timeout": 120,
  "request": "{what to do — be specific and concise}",
  "allowed_commands": ["{command 1}", "{command 2}"],
  "files": [],
  "result": null,
  "log": [
    {"ts": "{timestamp}", "agent": "{creator}", "msg": "Created task"}
  ]
}
```

#### Code-edit task (modify files in the synced repo)

The daemon automatically handles: lock acquisition → edit → git commit → lock release.

```json
{
  "title": "{short description}",
  "status": "pending",
  "type": "code-edit",
  "created": "{ISO 8601 UTC timestamp}",
  "created_by": "{this machine's agent ID, or 'user' if interactive}",
  "target": "{target agent_id}",
  "timeout": 300,
  "request": "{what to change — be specific about the desired behavior}",
  "allowed_commands": ["{any shell commands needed}"],
  "files": ["{file1.py}", "{file2.py}"],
  "result": null,
  "log": [
    {"ts": "{timestamp}", "agent": "{creator}", "msg": "Created task"}
  ]
}
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
