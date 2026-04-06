"""Interactive setup wizard for SecBrain."""

from __future__ import annotations

import secrets
import subprocess
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from . import config, deploy, hooks, inject

console = Console(legacy_windows=False)

BANNER = r"""[bold cyan]
   ____  ___  ____ ____  ____      _    ___ _   _
  / ___|| __|/ ___| __ )|  _ \    / \  |_ _| \ | |
  \___ \|  _|| |   |  _ \| |_) |  / _ \  | ||  \| |
   ___) | |__| |___| |_) |  _ <  / ___ \ | || |\  |
  |____/|____|\____|____/|_| \_\/_/   \_\___|_| \_|
  Persistent memory for Claude Code[/bold cyan]
"""


def run():
    """Full setup wizard: vault location, server, project registration."""
    console.print(BANNER)

    # Check for existing config
    existing = config.load()
    if existing:
        if not Confirm.ask(
            "[yellow]SecBrain is already initialized. Reinitialize?[/yellow]",
            default=False,
        ):
            connect_project()
            return

    # 1. Vault location
    mode = _choice("Where should your vault live?", [
        ("local", "Locally  (~/.secbrain/vault)"),
        ("vps",   "VPS      (remote server via SSH)"),
    ])

    api_key = secrets.token_hex(24)
    vault_url = None
    ssh_str = None

    if mode == "vps":
        vps_action = _choice("VPS setup", [
            ("deploy",  "Deploy SecBrain to a new VPS (via SSH)"),
            ("connect", "Connect to a VPS where SecBrain is already running"),
        ])
        if vps_action == "deploy":
            ssh_str = Prompt.ask("VPS address [bold](user@host)[/bold]")
            if Confirm.ask("Deploy server now?", default=True):
                try:
                    vault_url = deploy.to_vps(ssh_str, api_key)
                except Exception as e:
                    console.print(f"[red]Deploy failed:[/red] {e}")
                    console.print("[yellow]You can retry later with:[/yellow] secbrain deploy")
                    vault_url = f"http://{ssh_str.split('@')[-1]}:8765"
        else:
            vault_url = Prompt.ask("Vault URL", default="http://your-vps-ip:8765")
            api_key = Prompt.ask("API key")
            ssh_str = Prompt.ask("VPS address for future deploys [bold](user@host, optional)[/bold]", default="")
    else:
        vault_url = "http://127.0.0.1:8765"
        _start_local(api_key)

    # 2. Register project
    default_name = _detect_project_name()
    project_name = Prompt.ask("Project name for this directory", default=default_name)

    # 3. Save config
    cfg_data: dict = {
        "mode": mode,
        "vault_url": vault_url,
        "api_key": api_key,
        "projects": {str(Path.cwd()): project_name},
    }
    if ssh_str:
        cfg_data["ssh"] = ssh_str

    config.save(cfg_data)

    # 4. Inject into current project
    inject.inject(project_name, vault_url, api_key)
    hooks.install(vault_url, api_key)

    console.print(Panel(
        f"[green][OK][/green] Project '[bold]{project_name}[/bold]' registered in vault\n"
        f"[green][OK][/green] CLAUDE.md updated with memory instructions\n"
        f"[green][OK][/green] .mcp.json created\n"
        f"[green][OK][/green] Claude Code hooks configured\n"
        f"[green][OK][/green] API key: [dim]{api_key[:12]}...[/dim]\n\n"
        f"Start a Claude Code session to activate memory.",
        title="[bold green]SecBrain Ready[/bold green]",
        border_style="green",
    ))


def connect_project(name: str | None = None):
    """Connect current directory to an existing or new vault project."""
    cfg = config.load()
    if not cfg:
        console.print("[red]Run 'secbrain init' first.[/red]")
        return

    from .client import VaultClient
    try:
        existing_raw = VaultClient(cfg).call("vault_list_projects")
        existing = [p for p in existing_raw.splitlines() if p.strip()]
    except Exception:
        existing = []

    if name is None:
        default = _detect_project_name()
        choices = [("__new__", f"Create new: {default}")] + [(p, p) for p in existing]
        choice = _choice("Connect to existing project or create new?", choices)
        name = default if choice == "__new__" else choice

    cfg["projects"][str(Path.cwd())] = name
    config.save(cfg)
    inject.inject(name, cfg["vault_url"], cfg["api_key"])

    console.print(
        f"[green][OK][/green] Connected. Memory active for '[bold]{name}[/bold]'."
    )


def _detect_project_name() -> str:
    """Try git remote name; fall back to directory name."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, cwd=Path.cwd(), timeout=5,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            return url.rstrip("/").split("/")[-1].removesuffix(".git")
    except Exception:
        pass
    return Path.cwd().name


def _start_local(api_key: str):
    """Start the local vault server as a background subprocess."""
    import os
    import sys
    from pathlib import Path as _Path

    vault_path = str(_Path.home() / ".secbrain" / "vault")
    env = {**os.environ, "MCP_API_KEY": api_key, "VAULT_PATH": vault_path}

    with console.status("[bold]Starting local vault server...[/bold]"):
        proc = subprocess.Popen(
            [sys.executable, "-m", "secbrain.server"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Brief wait to check it started
        time.sleep(1.5)
        if proc.poll() is not None:
            console.print("[yellow]Warning: local server may not have started. "
                          "Run 'secbrain start' manually.[/yellow]")
            return

    console.print("[green][OK][/green] Local vault server started (PID: "
                  f"[dim]{proc.pid}[/dim])")

    # Save PID for stop command
    pid_file = _Path.home() / ".secbrain" / "server.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(proc.pid))


def _choice(prompt: str, options: list[tuple[str, str]]) -> str:
    """Display a numbered selection prompt; return the selected value."""
    console.print(f"\n[bold]{prompt}[/bold]")
    for i, (_, label) in enumerate(options):
        marker = ">" if i == 0 else " "
        console.print(f"  {marker} [dim]{i + 1}.[/dim] {label}")
    idx = Prompt.ask(
        "Select",
        choices=[str(i + 1) for i in range(len(options))],
        default="1",
    )
    return options[int(idx) - 1][0]
