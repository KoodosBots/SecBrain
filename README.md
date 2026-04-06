# SecBrain

**Persistent memory for Claude Code — local or VPS.**

SecBrain gives Claude Code a long-term memory vault that persists across sessions. Install it once globally, run `secbrain init` in any project, and Claude will automatically remember bugs, features, decisions, and learned patterns.

---

## Install

```bash
pip install secbrain
```

## Quick Start

```bash
# In any project directory
cd my-project
secbrain init
```

The wizard will:
1. Ask where your vault lives (local `~/.secbrain/vault` or remote VPS)
2. Deploy the MCP server (locally or via SSH)
3. Register the current project
4. Inject `CLAUDE.md` instructions and `.mcp.json` MCP config
5. Install Claude Code session hooks

Then start a Claude Code session — memory is active automatically.

---

## Commands

| Command | Description |
|---------|-------------|
| `secbrain init` | Full setup wizard |
| `secbrain connect [name]` | Connect current directory to a vault project |
| `secbrain projects` | List all vault projects |
| `secbrain status` | Show server health and current project |
| `secbrain start` | Start local vault server |
| `secbrain stop` | Stop local vault server |
| `secbrain logs [project]` | Show session log |
| `secbrain bugs [project]` | Show known bugs |
| `secbrain deploy` | (Re)deploy server to VPS |
| `secbrain eject` | Remove SecBrain from current project |
| `secbrain skills` | List learned skills |
| `secbrain skills search <query>` | Search skills |
| `secbrain skills show <name>` | Show a skill |

---

## What Gets Stored

```
~/.secbrain/vault/
├── _system/
│   ├── log.md          ← all session start/end events
│   └── entities.db     ← SQLite knowledge graph
├── projects/{name}/
│   ├── overview.md     ← project summary
│   ├── bugs.md         ← logged bugs
│   ├── features.md     ← logged features
│   ├── decisions.md    ← architectural decisions
│   ├── sessions.md     ← session summaries
│   └── user_model.md   ← learned user preferences
├── skills/
│   ├── global/         ← cross-project reusable patterns
│   └── {project}/      ← project-specific patterns
├── raw/                ← knowledge ingestion staging
└── wiki/               ← processed topic wikis
```

---

## MCP Tools Available to Claude

| Tool | When to call |
|------|-------------|
| `vault_start_session(project, cwd)` | Automatically via hook at session start |
| `vault_end_session(project, summary)` | At end of session |
| `vault_log_bug(project, title, description, severity)` | When discovering a bug |
| `vault_log_feature(project, title, description, status)` | When implementing a feature |
| `vault_log_decision(project, title, rationale)` | When making an arch decision |
| `vault_recall(project, query)` | Search everything |
| `vault_update_overview(project, overview)` | Update project description |
| `vault_save_skill(...)` | After solving something non-obvious |
| `vault_recall_skill(query, project)` | Before starting complex tasks |
| `vault_improve_skill(project, skill_name, refinement)` | When a skill gets better |
| `vault_update_user_model(project, observation)` | When noticing user patterns |
| `vault_read(path)` | Read any vault file |
| `vault_write(path, content)` | Write any vault file |
| `vault_list(path)` | List vault directory |
| `vault_list_projects()` | List all projects |

---

## VPS Deploy

```bash
secbrain init
# Select "VPS" → enter user@host → confirm deploy
```

SecBrain installs itself on the VPS via pip and runs as a systemd service on port 8765.

---

## Multiple Projects

```bash
cd ~/other-project
secbrain connect
# Select existing project or create new
```

---

## Development

```bash
git clone ...
cd secbrain
pip install -e ".[dev]"
```

---

## Requirements

- Python 3.10+
- Claude Code with MCP support
- For VPS: SSH access + pip on remote

---

## License

MIT
