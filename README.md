# OpenCode Intern Skill

A Claude Code skill that delegates coding tasks to an OpenCode "intern" agent via the  [ACP (Agent Communication Protocol)](https://agentclientprotocol.com/overview/introduction). This enables Claude Code to offload tasks to a secondary AI agent running in [OpenCode](https://opencode.ai/docs/acp/).

Opencode can read `AGENTS.md` in the same manner Claude Code does, so you can seamlessly share project context between both systems. You could even point the [Opencode Rules](https://opencode.ai/docs/rules/) to the `CLAUDE.md` file 

An added benefit of this setup is Opencode automatically uses skills in the `.claude/skills` directory so there's no need for duplicating files, and both agents can share the same working space and methodologies.

To avoid nested callbacks that Claude looses sight of, make sure to deny permission to the `opencode-intern` skill.

For more portability and less token use, you could transpose a basic prompt in to the `opencode.json` file directly rather than point to a markdown file. 

## Prerequisites

- [Claude Code](https://claude.com/product/claude-code) installed
- [OpenCode](https://opencode.ai) installed and available in PATH
- Python 3.8+
- The `opencode.json` is currently configured to use the free [Opencode Zen](https://opencode.ai/zen) Minimax M2.1 model through Zen, but obviously you can configure it with any you like.

## Installation

### 1. Copy the OpenCode configuration

Copy the following to your project root:

```
opencode.json          -> your-project/opencode.json
.opencode/             -> your-project/.opencode/
```

### 2. Copy the Claude Code skill

Copy the skill folder to your Claude Code skills directory:

```
.claude/skills/opencode-intern/  -> your-project/.claude/skills/opencode-intern/
```

Or to your global skills directory:
- **macOS/Linux**: `~/.claude/skills/opencode-intern/`
- **Windows**: `%USERPROFILE%\.claude\skills\opencode-intern\`

The default configuration uses the free OpenCode Zen model, so no API key is required to get started.

## Usage

In Claude Code, use phrases like:

- "Use the intern to refactor this function"
- "Ask the intern to write tests for this module"
- "Delegate this task to the intern"

Claude Code will invoke the skill, which communicates with OpenCode via ACP to execute the task.

## File Structure

```
opencode-intern-skill/
├── README.md                           # This file
├── opencode.json                       # OpenCode configuration
├── .opencode/
│   └── prompts/
│       └── intern.md                   # Intern agent system prompt
└── .claude/
    └── skills/
        └── opencode-intern/
            ├── skill.md                # Skill definition
            └── scripts/
                └── acp_client.py       # ACP protocol client
```

## Configuration

### Intern Agent Permissions

The intern agent has a minimal permission configuration:

```json
"permission": {
    "skill": {
        "opencode-intern": "deny"
    }
}
```

The `opencode-intern: deny` entry prevents recursive self-calls. You can extend this to allow or deny other skills and bash commands as needed.

### Intern System Prompt

The intern agent's behaviour is defined in `.opencode/prompts/intern.md`. Key characteristics:

- **Precision over initiative**: Executes exactly what is specified
- **No autonomous decisions**: Reports blockers instead of making assumptions
- **Scope boundaries**: Works strictly within defined parameters
- **Silent efficiency**: Minimal commentary unless requested

## ACP Client

The `acp_client.py` script handles communication with OpenCode:

```bash
python acp_client.py <project_path> "<prompt>" --json
```

### Parameters

| Parameter | Description |
|-----------|-------------|
| `project_path` | Absolute path to the project directory |
| `prompt` | The task to delegate to the intern |
| `--agent` | Agent name (default: "intern") |
| `--timeout` | Timeout in seconds (default: 300) |
| `--json` | Output results as JSON |

### Response Format

```json
{
    "success": true,
    "response": "Agent's response text",
    "tools": [
        {"name": "bash", "status": "completed"},
        {"name": "read", "status": "completed"}
    ],
    "errors": []
}
```

### Modifying the Intern's Behaviour

Edit `.opencode/prompts/intern.md` to adjust:
- Execution philosophy
- Processing protocol
- Ambiguity handling

### Inline Prompts

For smaller token usage, you can embed the prompt directly in `opencode.json` instead of referencing a file:

```json
{
    "agent": {
        "intern": {
            "prompt": "You are the Intern. Execute tasks precisely as specified. Report blockers instead of making assumptions."
        }
    }
}
```

## Troubleshooting

| Error | Solution |
|-------|----------|
| "OpenCode not found" | Ensure `opencode` is installed and in PATH |
| "Timeout" | Increase `--timeout` or break task into smaller pieces |
 "Session error" | Check OpenCode logs |
| "Init failed" | Verify OpenCode version supports ACP protocol |