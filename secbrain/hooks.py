"""
Write Claude Code lifecycle hooks to ~/.claude/settings.json.

Installs three hooks:
- SessionStart  : calls vault_start_session + loads last-session file changes
- Stop          : blocks once to force vault_end_session with real context
- PreCompact    : saves state to vault before context is destroyed
- PostToolUse   : silently tracks every file Claude modifies
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
HOOKS_DIR = Path.home() / ".secbrain" / "hooks"


# ---------------------------------------------------------------------------
# Hook scripts written to ~/.secbrain/hooks/
# ---------------------------------------------------------------------------

def _write_stop_hook(python: str, mcp_url: str, api_key: str) -> Path:
    """
    Stop hook: reads the JSONL transcript, extracts git diff + errors,
    blocks the session close ONCE to force Claude to call vault_end_session.
    Anti-loop: skip block if already blocked this session or stop_hook_active.
    """
    script = f'''#!/usr/bin/env python3
"""SecBrain Stop hook — forces vault_end_session before session closes."""
import json, os, sys, subprocess, asyncio, hashlib
from pathlib import Path

hook_input = json.loads(sys.stdin.read())

# Never block if a stop hook is already running (prevents infinite loops)
if hook_input.get("stop_hook_active"):
    sys.exit(0)

cwd       = hook_input.get("cwd", os.getcwd())
session_id = hook_input.get("session_id", "unknown")
project   = os.path.basename(cwd)
mcp_url   = "{mcp_url}"
api_key   = "{api_key}"

# Anti-loop: only block ONCE per session
flag_file = Path(os.environ.get("TEMP", "/tmp")) / f"secbrain_stop_{{session_id[:16]}}"
if flag_file.exists():
    # Already blocked once — now just auto-save silently and exit
    try:
        async def _save():
            from fastmcp import Client
            from fastmcp.client.transports.http import StreamableHttpTransport
            t = StreamableHttpTransport(url=mcp_url, auth=api_key)
            async with Client(t) as c:
                await c.call_tool("vault_end_session", {{
                    "project": project,
                    "summary": "Auto-saved: session closed without explicit summary.",
                }})
        asyncio.run(_save())
    except Exception:
        pass
    flag_file.unlink(missing_ok=True)
    sys.exit(0)

# --- Gather context from git ---
git_lines = []
try:
    diff = subprocess.run(
        ["git", "diff", "--stat", "HEAD"],
        capture_output=True, text=True, cwd=cwd, timeout=5
    ).stdout.strip()
    if diff:
        git_lines.append("Git changes this session:")
        git_lines.extend(diff.splitlines()[-15:])

    status = subprocess.run(
        ["git", "status", "--short"],
        capture_output=True, text=True, cwd=cwd, timeout=5
    ).stdout.strip()
    if status:
        git_lines.append("Untracked/unstaged:")
        git_lines.extend(status.splitlines()[:8])
except Exception:
    pass

# --- Parse transcript for errors ---
errors = []
try:
    transcript_path = hook_input.get("transcript_path", "")
    # Workaround for stale-path bug: use most recently modified .jsonl
    if transcript_path:
        jsonl_dir = Path(transcript_path).parent
        if jsonl_dir.exists():
            candidates = sorted(jsonl_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
            if candidates:
                transcript_path = str(candidates[-1])

    if transcript_path and Path(transcript_path).exists():
        lines = Path(transcript_path).read_text(errors="replace").splitlines()
        # Track what tool each result belongs to
        last_tool_name = ""
        for line in lines[-300:]:
            try:
                entry = json.loads(line)
                content = entry.get("message", {{}}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        # Track tool_use names so we know which tool errored
                        if block.get("type") == "tool_use":
                            last_tool_name = block.get("name", "")
                        # Only capture errors from code/bash execution
                        # Skip Edit/Write/file tool errors (Claude's own mistakes)
                        if block.get("is_error") and last_tool_name in (
                            "Bash", "bash", "execute_command", "run_command",
                            "mcp__ide__executeCode", "computer",
                        ):
                            raw = block.get("content", "")
                            if isinstance(raw, list):
                                raw = " ".join(str(r) for r in raw)
                            text = str(raw).strip()[:200]
                            if text and text not in errors:
                                errors.append(text)
            except Exception:
                pass
except Exception:
    pass

# --- Build the block message ---
parts = [
    "[SecBrain] Call vault_end_session(\\"" + project + "\\", summary) RIGHT NOW before stopping.",
    "Include: what you built/fixed, any bugs still open, decisions made, next steps.",
]
if git_lines:
    parts.append("\\n".join(git_lines[:12]))
if errors:
    parts.append("Errors encountered: " + "; ".join(errors[:3]))

# Set flag so we don't block again
flag_file.touch()

print(json.dumps({{
    "decision": "block",
    "reason": "\\n".join(parts),
}}))
sys.exit(0)
'''
    path = HOOKS_DIR / "stop_hook.py"
    path.write_text(script, encoding="utf-8")
    return path


def _write_precompact_hook(python: str, mcp_url: str, api_key: str) -> Path:
    """
    PreCompact hook: parses transcript before context is destroyed.
    Extracts errors/decisions/files, calls vault API directly.
    Cannot block compaction but can save everything first.
    """
    script = f'''#!/usr/bin/env python3
"""SecBrain PreCompact hook — saves session state before context compaction."""
import json, os, sys, subprocess, asyncio, re
from pathlib import Path

hook_input = json.loads(sys.stdin.read())
cwd      = hook_input.get("cwd", os.getcwd())
project  = os.path.basename(cwd)
mcp_url  = "{mcp_url}"
api_key  = "{api_key}"

errors_found    = []
decisions_found = []
files_changed   = []

# --- Parse transcript ---
try:
    transcript_path = hook_input.get("transcript_path", "")
    if transcript_path:
        jsonl_dir = Path(transcript_path).parent
        if jsonl_dir.exists():
            candidates = sorted(jsonl_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
            if candidates:
                transcript_path = str(candidates[-1])

    if transcript_path and Path(transcript_path).exists():
        for line in Path(transcript_path).read_text(errors="replace").splitlines():
            try:
                entry = json.loads(line)
                content = entry.get("message", {{}}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        # Capture errors
                        if block.get("is_error"):
                            txt = str(block.get("content", ""))[:200]
                            if txt and txt not in errors_found:
                                errors_found.append(txt)
                        # Capture file edits
                        if block.get("type") == "tool_use" and block.get("name") in ("Edit","Write","MultiEdit"):
                            fp = block.get("input", {{}}).get("file_path", "")
                            if fp and fp not in files_changed:
                                files_changed.append(fp)
            except Exception:
                pass
except Exception:
    pass

# --- Get git context ---
git_summary = ""
try:
    diff = subprocess.run(
        ["git", "diff", "--stat", "HEAD"],
        capture_output=True, text=True, cwd=cwd, timeout=5
    ).stdout.strip()
    if diff:
        git_summary = diff
except Exception:
    pass

if not (errors_found or files_changed or git_summary):
    sys.exit(0)

# --- Call vault API ---
try:
    async def _save():
        from fastmcp import Client
        from fastmcp.client.transports.http import StreamableHttpTransport
        t = StreamableHttpTransport(url=mcp_url, auth=api_key)
        async with Client(t) as c:
            summary_parts = ["[Pre-compact snapshot]"]
            if files_changed:
                summary_parts.append(f"Files modified: {{', '.join(files_changed[:10])}}")
            if errors_found:
                summary_parts.append(f"Errors seen: {{'; '.join(errors_found[:3])}}")
            if git_summary:
                summary_parts.append(f"Git: {{git_summary[:300]}}")

            await c.call_tool("vault_end_session", {{
                "project": project,
                "summary": "\\n".join(summary_parts),
            }})

            # Log each error as a bug if not already there
            for err in errors_found[:3]:
                await c.call_tool("vault_log_bug", {{
                    "project": project,
                    "title": f"Error (pre-compact): {{err[:60]}}",
                    "description": err,
                    "severity": "medium",
                    "tags": "auto-captured",
                }})

    asyncio.run(_save())
except Exception:
    pass

sys.exit(0)
'''
    path = HOOKS_DIR / "precompact_hook.py"
    path.write_text(script, encoding="utf-8")
    return path


def _write_post_tool_use_hook(python: str) -> Path:
    """
    PostToolUse hook: silently appends modified file paths to a session
    dirty-file list. Used by vault_start_session to show what changed.
    Ultra lightweight — just a file append.
    """
    script = '''#!/usr/bin/env python3
"""SecBrain PostToolUse hook — tracks files modified by Claude."""
import json, os, sys
from pathlib import Path

try:
    hook_input = json.loads(sys.stdin.read())
    tool_name = hook_input.get("tool_name", "")
    if tool_name not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    file_path = tool_input.get("file_path", "") or tool_input.get("notebook_path", "")
    if not file_path:
        sys.exit(0)

    session_id = hook_input.get("session_id", "unknown")[:16]
    dirty_file = Path(os.environ.get("TEMP", "/tmp")) / f"secbrain_dirty_{session_id}"
    with open(dirty_file, "a", encoding="utf-8") as f:
        f.write(file_path + "\\n")
except Exception:
    pass

sys.exit(0)
'''
    path = HOOKS_DIR / "post_tool_use_hook.py"
    path.write_text(script, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install(vault_url: str, api_key: str):
    """Install all SecBrain hooks into ~/.claude/settings.json."""
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    settings = _load_settings()
    settings.setdefault("hooks", {})

    python = sys.executable.replace("\\", "/")
    mcp_url = vault_url.rstrip("/") + "/mcp"

    # Write hook scripts
    stop_path        = _write_stop_hook(python, mcp_url, api_key)
    precompact_path  = _write_precompact_hook(python, mcp_url, api_key)
    post_tool_path   = _write_post_tool_use_hook(python)

    def _cmd(path: Path) -> str:
        return f'"{python}" "{path.as_posix()}"'

    # SessionStart — call vault_start_session
    settings["hooks"]["SessionStart"] = [{
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": (
                f'"{python}" -c "'
                f'import sys,os,asyncio\n'
                f'try:\n'
                f' from fastmcp import Client\n'
                f' from fastmcp.client.transports.http import StreamableHttpTransport\n'
                f' async def r():\n'
                f'  t=StreamableHttpTransport(url=\\"{mcp_url}\\",auth=\\"{api_key}\\")\n'
                f'  async with Client(t) as c: await c.call_tool(\\"vault_start_session\\",'
                f'{{\\"project\\":os.path.basename(os.getcwd()),\\"cwd\\":os.getcwd()}})\n'
                f' asyncio.run(r())\n'
                f'except Exception: pass\n'
                f'" 2>nul || true'
            )
        }]
    }]

    # Stop — block once to force vault_end_session
    settings["hooks"]["Stop"] = [{
        "matcher": "",
        "hooks": [{"type": "command", "command": _cmd(stop_path)}]
    }]

    # PreCompact — save state before context compression
    settings["hooks"]["PreCompact"] = [{
        "matcher": "",
        "hooks": [{"type": "command", "command": _cmd(precompact_path)}]
    }]

    # PostToolUse — track file changes silently
    settings["hooks"]["PostToolUse"] = [{
        "matcher": "",
        "hooks": [{"type": "command", "command": _cmd(post_tool_path)}]
    }]

    _save_settings(settings)


def remove():
    """Remove all SecBrain hooks from ~/.claude/settings.json."""
    if not SETTINGS_PATH.exists():
        return
    settings = _load_settings()
    for hook in ["SessionStart", "Stop", "PreCompact", "PostToolUse"]:
        settings.get("hooks", {}).pop(hook, None)
    _save_settings(settings)


def _load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_settings(settings: dict):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
