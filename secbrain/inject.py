"""Inject and eject SecBrain configuration into a project directory."""

from __future__ import annotations

import json
from pathlib import Path

SECBRAIN_START = "<!-- SECBRAIN:START -->"
SECBRAIN_END = "<!-- SECBRAIN:END -->"

CLAUDE_MD_BLOCK = """<!-- SECBRAIN:START -->
## SecBrain Memory
VAULT_PROJECT={project}

At session START: `vault_start_session` fires automatically. You get a vault index
(titles + counts only). Use `vault_recall` to fetch details on demand.

### Index-first workflow
Before touching any feature or bug, call:
`vault_recall(project, query, tags='relevant,tags')`
Example: working on Dominoes UI? -> `vault_recall(project, 'UI', tags='ui,mobile')`
This pulls only what matters. Do NOT read the whole vault.

### First session only
`vault_scan_project(project, cwd)` — scan codebase if no overview exists.

### Log as you work (always add tags)
- Bug -> `vault_log_bug(project, title, description, severity, tags='ui,auth')`
- Feature -> `vault_log_feature(project, title, description, status?, tags='api,ui')`
- Task -> `vault_log_task(project, title, description, status)` | `vault_update_task(...)`
- Decision -> `vault_log_decision(project, title, rationale, tags='architecture')`
- Snippet -> `vault_log_code_example(project, description, code, language?, tags?)`

At session END: `vault_end_session(project, summary)`.

### Deeper lookups
- `vault_recall_skill(query, project)` — solved this before?
- `vault_search_code(project, query)` — saved snippets
- `vault_search_docs(project, query)` — ingested docs
- `vault_ingest_url(url, project)` / `vault_ingest_file(path, project)` — store reference

### Self-improvement
- `vault_save_skill(project, title, problem, solution, tags?, scope?)` after non-obvious fixes
  scope="global" if reusable across projects
- `vault_improve_skill(project, skill_name, refinement)` when a skill gets better
- `vault_update_user_model(project, observation)` when noticing user patterns

Only save skills that are: non-obvious, reusable, proven.
<!-- SECBRAIN:END -->
"""

MCP_JSON_ENTRY = {
    "type": "http",
    "url": "{vault_url}/mcp/",
    "headers": {
        "Authorization": "Bearer {api_key}"
    }
}


def inject(project: str, vault_url: str, api_key: str, project_dir: Path | None = None):
    """
    Inject SecBrain configuration into the project directory.
    - Appends/replaces the SECBRAIN block in CLAUDE.md
    - Creates/updates .mcp.json with the secbrain server entry
    """
    cwd = project_dir or Path.cwd()
    _inject_claude_md(cwd, project)
    _inject_mcp_json(cwd, vault_url, api_key)


def eject(project_dir: Path | None = None):
    """
    Remove SecBrain configuration from the project directory.
    - Removes the SECBRAIN block from CLAUDE.md
    - Removes the secbrain entry from .mcp.json
    """
    cwd = project_dir or Path.cwd()
    _eject_claude_md(cwd)
    _eject_mcp_json(cwd)


def _inject_claude_md(cwd: Path, project: str):
    """Append or replace the SECBRAIN block in CLAUDE.md."""
    claude_md = cwd / "CLAUDE.md"
    block = CLAUDE_MD_BLOCK.format(project=project)

    if claude_md.exists():
        content = claude_md.read_text()
        # Remove existing block if present
        content = _remove_secbrain_block(content)
        # Append new block
        claude_md.write_text(content.rstrip() + "\n\n" + block)
    else:
        claude_md.write_text(block)


def _eject_claude_md(cwd: Path):
    """Remove the SECBRAIN block from CLAUDE.md."""
    claude_md = cwd / "CLAUDE.md"
    if not claude_md.exists():
        return
    content = claude_md.read_text()
    new_content = _remove_secbrain_block(content).rstrip()
    if new_content:
        claude_md.write_text(new_content + "\n")
    else:
        claude_md.unlink()


def _remove_secbrain_block(content: str) -> str:
    """Remove <!-- SECBRAIN:START --> ... <!-- SECBRAIN:END --> block from content."""
    start = content.find(SECBRAIN_START)
    end = content.find(SECBRAIN_END)
    if start == -1 or end == -1:
        return content
    return content[:start] + content[end + len(SECBRAIN_END):]


def _inject_mcp_json(cwd: Path, vault_url: str, api_key: str):
    """Create or update .mcp.json with secbrain server entry."""
    mcp_json = cwd / ".mcp.json"
    if mcp_json.exists():
        try:
            data = json.loads(mcp_json.read_text())
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}

    data.setdefault("mcpServers", {})
    data["mcpServers"]["secbrain"] = {
        "type": "http",
        "url": f"{vault_url.rstrip('/')}/mcp/",
        "headers": {
            "Authorization": f"Bearer {api_key}"
        }
    }
    mcp_json.write_text(json.dumps(data, indent=2))


def _eject_mcp_json(cwd: Path):
    """Remove the secbrain entry from .mcp.json."""
    mcp_json = cwd / ".mcp.json"
    if not mcp_json.exists():
        return
    try:
        data = json.loads(mcp_json.read_text())
    except json.JSONDecodeError:
        return
    data.get("mcpServers", {}).pop("secbrain", None)
    if data.get("mcpServers"):
        mcp_json.write_text(json.dumps(data, indent=2))
    elif not data or data == {"mcpServers": {}}:
        mcp_json.unlink()
    else:
        mcp_json.write_text(json.dumps(data, indent=2))
