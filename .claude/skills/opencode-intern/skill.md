---
name: opencode-intern
description: Delegate coding tasks to the OpenCode "intern" agent via ACP. Use when user says "use the intern", "ask the intern", "delegate to the intern", or similar. The intern can read files, write code, run commands, and complete development tasks.
---

# OpenCode Intern

Run the ACP client script to delegate tasks:

```bash
python scripts/acp_client.py "<project_path>" "<prompt>" --json
```

Parse the JSON result and report to user. The project_path is usually the current working directory.

## Response Format

```json
{"success": true, "response": "Agent's response text", "tools": [{"name": "bash", "status": "completed"}], "errors": []}
```

## Parameters

- `project_path` (required): Absolute path to project
- `prompt` (required): Task description
- `--agent`: Agent name (default: intern)
- `--timeout`: Seconds (default: 300)
- `--json`: JSON output (always use this)

## Errors

| Error | Fix |
|-------|-----|
| "OpenCode not found" | Add `opencode` to PATH |
| "Timeout" | Increase `--timeout` or simplify task |
