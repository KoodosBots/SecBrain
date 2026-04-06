"""SecBrain CLI -- persistent memory for Claude Code."""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import config, hooks, inject, wizard

app = typer.Typer(
    help="SecBrain -- persistent memory for Claude Code",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
# legacy_windows=False forces ANSI output instead of the Windows Console API,
# which avoids cp1252 encoding errors with Unicode symbols like checkmarks.
console = Console(legacy_windows=False)

# ---------------------------------------------------------------------------
# Core commands
# ---------------------------------------------------------------------------


@app.command()
def init():
    """Full setup wizard. Run once globally, then once per project."""
    wizard.run()


@app.command()
def connect(name: str = typer.Argument(None, help="Project name to connect")):
    """Connect current directory to a vault project."""
    cfg = config.load()
    if not cfg:
        console.print("[red]Run 'secbrain init' first.[/red]")
        raise typer.Exit(1)
    wizard.connect_project(name)


@app.command()
def projects():
    """List all projects registered in the vault."""
    cfg = config.load()
    if not cfg:
        console.print("[yellow]Not initialized. Run 'secbrain init'[/yellow]")
        raise typer.Exit(1)
    from .client import VaultClient
    try:
        result = VaultClient(cfg).call("vault_list_projects")
        if result.strip():
            for p in result.splitlines():
                if p.strip():
                    console.print(f"  [cyan]*[/cyan] {p.strip()}")
        else:
            console.print("[dim]No projects yet.[/dim]")
    except Exception as e:
        console.print(f"[red]Failed to list projects:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def status():
    """Show vault connection, server health, and current project."""
    cfg = config.load()
    if not cfg:
        console.print("[yellow]Not initialized. Run 'secbrain init'[/yellow]")
        return

    from .client import VaultClient
    console.print(f"\n[bold]SecBrain Status[/bold]")
    console.print(f"  Mode:      [cyan]{cfg.get('mode', 'unknown')}[/cyan]")
    console.print(f"  Vault URL: [cyan]{cfg.get('vault_url', 'N/A')}[/cyan]")

    try:
        VaultClient(cfg).health()
        console.print(f"  Server:    [green][OK] online[/green]")
    except Exception as e:
        console.print(f"  Server:    [red][!!] unreachable ({e})[/red]")

    cwd = str(Path.cwd())
    project = cfg.get("projects", {}).get(cwd)
    if project:
        console.print(f"  Project:   [green]{project}[/green]  [dim]({cwd})[/dim]")
    else:
        console.print(f"  Project:   [yellow]not connected[/yellow]  [dim](run 'secbrain connect')[/dim]")

    all_projects = cfg.get("projects", {})
    if len(all_projects) > 1:
        console.print(f"\n  [dim]Registered projects:[/dim]")
        for path, pname in all_projects.items():
            marker = "[green]->[/green]" if path == cwd else " "
            console.print(f"    {marker} [bold]{pname}[/bold]  [dim]{path}[/dim]")
    console.print()


@app.command()
def logs(project: str = typer.Argument(None, help="Project name (defaults to current)")):
    """Show session log for a project."""
    cfg = config.load()
    if not cfg:
        console.print("[red]Not initialized.[/red]")
        raise typer.Exit(1)
    p = project or config.current_project(cfg)
    if not p:
        console.print("[yellow]No project connected. Specify a project name or run 'secbrain connect'[/yellow]")
        raise typer.Exit(1)
    from .client import VaultClient
    try:
        result = VaultClient(cfg).call("vault_read", {"path": "_system/log.md"})
        lines = result.splitlines()
        # Filter for lines mentioning this project
        filtered = [l for l in lines if p in l or l.startswith("##") or l.startswith("# ")]
        output = "\n".join(filtered[-50:])
        console.print(output or "[dim]No log entries yet.[/dim]")
    except Exception as e:
        console.print(f"[red]Error reading logs:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def bugs(project: str = typer.Argument(None, help="Project name (defaults to current)")):
    """Show known bugs for a project."""
    cfg = config.load()
    if not cfg:
        console.print("[red]Not initialized.[/red]")
        raise typer.Exit(1)
    p = project or config.current_project(cfg)
    if not p:
        console.print("[yellow]No project connected. Specify a project name.[/yellow]")
        raise typer.Exit(1)
    from .client import VaultClient
    try:
        result = VaultClient(cfg).call("vault_read", {"path": f"projects/{p}/bugs.md"})
        console.print(result or "[dim]No bugs logged yet.[/dim]")
    except Exception as e:
        console.print(f"[red]Error reading bugs:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def tasks(
    project: str = typer.Argument(None, help="Project name (defaults to current)"),
    status: str = typer.Option(None, "--status", "-s", help="Filter: todo|doing|review|done"),
):
    """Show tasks for a project."""
    cfg = config.load()
    if not cfg:
        console.print("[red]Not initialized.[/red]")
        raise typer.Exit(1)
    p = project or config.current_project(cfg)
    if not p:
        console.print("[yellow]No project connected. Specify a project name.[/yellow]")
        raise typer.Exit(1)
    from .client import VaultClient
    try:
        result = VaultClient(cfg).call("vault_read", {"path": f"projects/{p}/tasks.md"})
        lines = result.splitlines()
        if status:
            tag = f"[{status.upper()}]"
            lines = [l for l in lines if tag in l or l.startswith("#")]
        console.print("\n".join(lines) or "[dim]No tasks yet.[/dim]")
    except Exception as e:
        console.print(f"[red]Error reading tasks:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def ingest(
    url_or_path: str = typer.Argument(..., help="URL (https://...) or local file path"),
    project: str = typer.Option(None, "--project", "-p", help="Project name"),
    title: str = typer.Option("", "--title", "-t", help="Override title"),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags"),
):
    """Ingest a URL or file into the vault for future recall."""
    cfg = config.load()
    if not cfg:
        console.print("[red]Not initialized.[/red]")
        raise typer.Exit(1)
    p = project or config.current_project(cfg)
    if not p:
        console.print("[yellow]No project connected. Use --project to specify one.[/yellow]")
        raise typer.Exit(1)
    from .client import VaultClient
    client = VaultClient(cfg)
    try:
        if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
            result = client.call("vault_ingest_url", {
                "url": url_or_path, "project": p, "title": title, "tags": tags
            })
        else:
            result = client.call("vault_ingest_file", {
                "file_path": url_or_path, "project": p, "title": title, "tags": tags
            })
        console.print(f"[green][OK][/green] {result}")
    except Exception as e:
        console.print(f"[red]Ingest failed:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def eject():
    """Remove SecBrain from the current project."""
    if not typer.confirm("Remove SecBrain from this project?", default=False):
        raise typer.Abort()
    inject.eject()
    hooks.remove()
    console.print("[yellow]SecBrain removed from this project.[/yellow]")
    console.print("[dim]Config at ~/.secbrain/config.json is preserved.[/dim]")


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------


@app.command()
def start():
    """Start the local vault server (local mode only)."""
    cfg = config.load()
    if not cfg:
        console.print("[red]Not initialized. Run 'secbrain init'[/red]")
        raise typer.Exit(1)
    if cfg.get("mode") != "local":
        console.print("[yellow]VPS mode -- server runs remotely. Use 'secbrain deploy' to manage it.[/yellow]")
        raise typer.Exit(1)
    wizard._start_local(cfg.get("api_key", ""))


@app.command()
def stop():
    """Stop the local vault server."""
    from pathlib import Path as _Path
    pid_file = _Path.home() / ".secbrain" / "server.pid"
    if not pid_file.exists():
        console.print("[yellow]No PID file found. Server may not be running.[/yellow]")
        return
    pid = int(pid_file.read_text().strip())
    try:
        import signal, os
        os.kill(pid, signal.SIGTERM)
        pid_file.unlink()
        console.print(f"[green][OK][/green] Server stopped (PID {pid})")
    except ProcessLookupError:
        pid_file.unlink()
        console.print(f"[yellow]Server (PID {pid}) was not running.[/yellow]")
    except Exception as e:
        console.print(f"[red]Error stopping server:[/red] {e}")


@app.command()
def update():
    """Upgrade SecBrain to the latest version and redeploy VPS if needed."""
    import subprocess as _sp
    import sys as _sys

    cfg = config.load()

    # 1. Upgrade local pip package
    with console.status("[bold]Upgrading secbrain...[/bold]"):
        result = _sp.run(
            [_sys.executable, "-m", "pip", "install", "--upgrade", "secbrain", "-q"],
            capture_output=True, text=True,
        )
    if result.returncode != 0:
        console.print(f"[red]pip upgrade failed:[/red] {result.stderr}")
        raise typer.Exit(1)
    console.print("[green][OK][/green] Local package upgraded")

    # Reinstall hook scripts so latest versions are on disk
    if cfg:
        from .hooks import install as _install_hooks
        _install_hooks(cfg["vault_url"], cfg["api_key"])
        console.print("[green][OK][/green] Hook scripts updated")

    # 2. Redeploy VPS if configured
    if cfg and cfg.get("mode") == "vps" and cfg.get("ssh"):
        if typer.confirm(f"Redeploy server to {cfg['ssh']}?", default=True):
            try:
                from . import deploy as _deploy
                _deploy.redeploy(cfg["ssh"], cfg.get("api_key", ""))
            except Exception as e:
                console.print(f"[red]VPS redeploy failed:[/red] {e}")
                raise typer.Exit(1)

    console.print("\n[bold]Restart Claude Code to activate the new version.[/bold]")


@app.command(name="deploy")
def deploy_cmd(
    ssh: str = typer.Option(None, "--ssh", help="user@host (overrides config)"),
):
    """(Re)deploy SecBrain server to VPS."""
    cfg = config.load()
    if not cfg:
        console.print("[red]Not initialized. Run 'secbrain init'[/red]")
        raise typer.Exit(1)
    ssh_str = ssh or cfg.get("ssh")
    if not ssh_str:
        ssh_str = typer.prompt("VPS address (user@host)")
    try:
        from . import deploy
        vault_url = deploy.redeploy(ssh_str, cfg.get("api_key", ""))
        cfg["vault_url"] = vault_url
        cfg["ssh"] = ssh_str
        config.save(cfg)
        console.print(f"[green][OK][/green] Deployed to {vault_url}")
    except Exception as e:
        console.print(f"[red]Deploy failed:[/red] {e}")
        raise typer.Exit(1)

# ---------------------------------------------------------------------------
# Skills commands
# ---------------------------------------------------------------------------

skills_app = typer.Typer(help="Manage learned skills in the vault", no_args_is_help=True)
app.add_typer(skills_app, name="skills")


@skills_app.callback(invoke_without_command=True)
def skills_list(
    ctx: typer.Context,
    project: str = typer.Argument(None, help="Project name (defaults to current)"),
):
    """List all learned skills."""
    if ctx.invoked_subcommand is not None:
        return
    cfg = config.load()
    if not cfg:
        console.print("[red]Not initialized.[/red]")
        raise typer.Exit(1)
    p = project or config.current_project(cfg)
    from .client import VaultClient
    client = VaultClient(cfg)

    table = Table(title="Learned Skills", show_header=True, header_style="bold cyan")
    table.add_column("Scope", style="dim", width=10)
    table.add_column("Skill")
    table.add_column("Tags", style="dim")

    for scope in ["global"] + ([p] if p else []):
        try:
            result = client.call("vault_read", {"path": f"skills/{scope}/index.md"})
            for line in result.splitlines():
                if line.startswith("- ["):
                    # Parse: - [title](slug.md) -- tags [timestamp]
                    import re
                    m = re.match(r"- \[(.+?)\]\(.+?\) -- (.+?) \[", line)
                    if m:
                        table.add_row(scope, m.group(1), m.group(2))
        except Exception:
            pass

    if table.row_count:
        console.print(table)
    else:
        console.print("[dim]No skills saved yet.[/dim]")


@skills_app.command(name="search")
def skills_search(
    query: str = typer.Argument(..., help="Search query"),
    project: str = typer.Option(None, "--project", "-p", help="Project name"),
):
    """Search for skills matching a query."""
    cfg = config.load()
    if not cfg:
        console.print("[red]Not initialized.[/red]")
        raise typer.Exit(1)
    p = project or config.current_project(cfg) or ""
    from .client import VaultClient
    try:
        result = VaultClient(cfg).call("vault_recall_skill", {"query": query, "project": p})
        console.print(result or "[dim]No matching skills found.[/dim]")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@skills_app.command(name="show")
def skills_show(
    name: str = typer.Argument(..., help="Skill name (slug or title)"),
    scope: str = typer.Option("project", "--scope", "-s", help="global or project"),
    project: str = typer.Option(None, "--project", "-p", help="Project name"),
):
    """Show full content of a skill."""
    cfg = config.load()
    if not cfg:
        console.print("[red]Not initialized.[/red]")
        raise typer.Exit(1)
    p = project or config.current_project(cfg) or ""
    folder = "global" if scope == "global" else p
    slug = name.lower().replace(" ", "-")[:50]
    from .client import VaultClient
    try:
        result = VaultClient(cfg).call("vault_read", {"path": f"skills/{folder}/{slug}.md"})
        console.print(result)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    app()


if __name__ == "__main__":
    main()
