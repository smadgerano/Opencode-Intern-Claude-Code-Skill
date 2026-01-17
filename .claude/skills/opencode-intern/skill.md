---
name: intern
description: Delegate tasks to the "intern" agent via ACP. Use when user says "use the intern", "ask the intern", "delegate to the intern", or similar. The intern can read files, write code, run commands, and complete development tasks.
---

# Intern

Run the ACP client script to delegate tasks:

`<skill-path>` is this skills location on disk.

## Background Execution (Default)

Tasks run in background by default, returning a task ID immediately:

```bash
# Launch task (background is default)
python <skill-path>/scripts/acp_client.py "<project_path>" "<prompt>" --json
# Returns: {"task_id": "abc123", "status": "launched"}

# Check status
python <skill-path>/scripts/acp_client.py --status abc123 --json

# Get result when complete
python <skill-path>/scripts/acp_client.py --result abc123 --json

# Wait for completion (blocking)
python <skill-path>/scripts/acp_client.py --wait abc123 --json --timeout 600

# List all tasks
python <skill-path>/scripts/acp_client.py --list-tasks --json
python <skill-path>/scripts/acp_client.py --list-tasks --filter running --json

# Cancel a running task
python <skill-path>/scripts/acp_client.py --cancel abc123

# Clean up old completed tasks
python <skill-path>/scripts/acp_client.py --cleanup
```

## Synchronous Execution

For simple tasks where you want to wait for the result:

```bash
python <skill-path>/scripts/acp_client.py "<project_path>" "<prompt>" --sync --json
```

Parse the JSON result directly. The project_path is usually the current working directory.

## Response Format

```json
{
  "success": true,
  "response": "Agent's response text",
  "tools": [{"name": "bash", "status": "completed"}],
  "errors": [],
  "agent_status": "end_turn"
}
```

### Agent Status Values

| Status | Meaning |
|--------|---------|
| `end_turn` | Normal successful completion |
| `idle` | Agent finished and is idle |
| `refusal` | Agent refused to complete the task |
| `cancelled` | Session was cancelled |
| `max_tokens` | Response truncated due to token limit |
| `max_turn_requests` | Hit maximum turn limit |
| `error` | Agent encountered an error |
| `unknown` | Status not determined |

### Task Status Values (Background Mode)

| Status | Meaning |
|--------|---------|
| `pending` | Task created, not yet started |
| `running` | Task is executing |
| `completed` | Task finished successfully |
| `failed` | Task failed with errors |
| `cancelled` | Task was cancelled by user |

## Parameters

### Execution Options
- `project_path` (required for sync): Absolute path to project
- `prompt` (required for sync): Task description (max 100KB)
- `--agent`: Agent name (default: intern)
- `--timeout`: Overall timeout in seconds (default: 300)
- `--activity-timeout`: Max seconds without activity (default: 120)
- `--json`: JSON output (always use this)
- `--debug`: Enable debug logging to stderr
- `--background`: Run task in background (default)
- `--sync`: Run task synchronously, wait for completion

### Task Management Options
- `--status TASK_ID`: Check status of a background task
- `--list-tasks`: List all background tasks
- `--filter STATUS`: Filter tasks by status (pending/running/completed/failed/cancelled)
- `--result TASK_ID`: Get the result of a completed task
- `--cancel TASK_ID`: Cancel a running task
- `--wait TASK_ID`: Wait for a task to complete
- `--cleanup`: Remove old completed tasks (>24 hours)

## Errors

| Error | Fix |
|-------|-----|
| "OpenCode not found in PATH" | Ensure `opencode` is installed and in PATH |
| "Overall timeout" | Increase `--timeout` or simplify task |
| "Activity timeout" | Agent may be hung; try simplifying task |
| "Agent process died" | Check opencode installation and logs |
| "Agent error" | Service issue; check opencode auth and API status |
| "Prompt exceeds maximum length" | Prompt is >100KB; break into smaller tasks |
| "Agent refused to complete" | Task may violate agent policies; rephrase |

## Features

- **Thread-safe request handling**: Safe for concurrent operations
- **Background task execution**: Launch tasks asynchronously, check status later
- **Activity timeout detection**: Detects hung agents that stop responding
- **Process health monitoring**: Detects if agent process crashes
- **Session cancellation**: Properly cancels stuck sessions per ACP protocol
- **Error status detection**: Detects and reports agent errors
- **Debug mode**: Enable `--debug` for troubleshooting
- **Partial response capture**: Returns partial responses even on failure
- **Windows path normalization**: Automatically converts `C:\path` to `C:/path` in prompts

## Best Practices

1. **Always use `--json`** for reliable parsing of results
2. **Use background mode for tasks >2 minutes** to avoid blocking
3. **Check task status periodically** rather than using long timeouts
4. **Use `--debug` when troubleshooting** to see protocol messages
5. **Keep prompts focused** - delegate one clear task at a time
6. **Clean up old tasks** periodically with `--cleanup`
