"""Write Claude Code lifecycle hooks to ~/.claude/settings.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Python-based hook — cross-platform, no bash/curl required.
# Uses the same Python that runs secbrain so fastmcp is available.
_SESSION_START_HOOK = """\
{python} -c "
import sys, os, asyncio
sys.path.insert(0, '')
try:
    from fastmcp import Client
    from fastmcp.client.transports.http import StreamableHttpTransport
    async def run():
        t = StreamableHttpTransport(url='{mcp_url}', auth='{api_key}')
        async with Client(t) as c:
            await c.call_tool('vault_start_session', {{
                'project': os.path.basename(os.getcwd()),
                'cwd': os.getcwd(),
            }})
    asyncio.run(run())
except Exception:
    pass
" 2>/dev/null || true\
"""


def install(vault_url: str, api_key: str):
    """Install SecBrain hooks into ~/.claude/settings.json."""
    settings = _load_settings()
    settings.setdefault("hooks", {})

    python = sys.executable.replace("\\", "/")
    mcp_url = vault_url.rstrip("/") + "/mcp"

    start_cmd = _SESSION_START_HOOK.format(
        python=python,
        mcp_url=mcp_url,
        api_key=api_key,
    )

    settings["hooks"]["SessionStart"] = [
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": start_cmd}]
        }
    ]

    settings["hooks"]["Stop"] = [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "echo '[SecBrain] Session ending. Call vault_end_session() with a summary.'",
                }
            ]
        }
    ]

    settings["hooks"]["PreCompact"] = [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "echo '[SecBrain] Context compacting. Save findings via vault_log_bug/feature/decision.'",
                }
            ]
        }
    ]

    _save_settings(settings)


def remove():
    """Remove SecBrain hooks from ~/.claude/settings.json."""
    if not SETTINGS_PATH.exists():
        return
    settings = _load_settings()
    for hook in ["SessionStart", "Stop", "PreCompact"]:
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
