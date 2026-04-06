"""Inject and eject SecBrain configuration into a project directory."""

from __future__ import annotations

import json
from pathlib import Path

SECBRAIN_START = "<!-- SECBRAIN:START -->"
SECBRAIN_END = "<!-- SECBRAIN:END -->"

CLAUDE_MD_BLOCK = """<!-- SECBRAIN:START -->
## SecBrain Memory
VAULT_PROJECT={project}

At session START: `vault_start_session` runs automatically via hook.

### Log as you work
- Bug/error -> `vault_log_bug(project, title, description, severity, file_path?)`
- Feature implemented -> `vault_log_feature(project, title, description, status?)`
- Work item -> `vault_log_task(project, title, description, status, assignee?)`
  - status: todo | doing | review | done
  - Update as it progresses: `vault_update_task(project, title, new_status, note?)`
- Decision -> `vault_log_decision(project, title, rationale)`
- Reusable snippet -> `vault_log_code_example(project, description, code, language?, tags?)`

At session END: call `vault_end_session(project, summary)`.

### First session only
- `vault_scan_project(project, cwd)` — index the codebase if no overview exists yet

### Before complex tasks
- `vault_recall_skill(query, project)` — check if this problem was solved before
- `vault_search_code(project, query)` — check saved code examples
- `vault_search_docs(project, query)` — search ingested documentation
- `vault_recall(project, query)` — broad search across everything

### Ingest reference material
- `vault_ingest_url(url, project, title?, tags?)` — fetch and store a doc page
- `vault_ingest_file(file_path, project, title?, tags?)` — store local file (.md/.txt/.pdf)

### Self-improvement
After solving something non-obvious:
- `vault_save_skill(project, title, problem, solution, code_example?, tags?, scope?)`
  - scope="global" if cross-project, "project" if specific

When a skill improves:
- `vault_improve_skill(project, skill_name, refinement)`

When noticing a user pattern:
- `vault_update_user_model(project, observation)`

### Skill quality bar
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
