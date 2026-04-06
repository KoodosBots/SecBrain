"""SSH deploy to VPS via paramiko."""

from __future__ import annotations

import io
import os
import sys
import tarfile
from pathlib import Path
from rich.console import Console

console = Console(legacy_windows=False)

SERVICE_TEMPLATE = """[Unit]
Description=SecBrain Memory MCP Server
After=network.target

[Service]
ExecStart={python} -m secbrain.server
WorkingDirectory=/opt/secbrain
Environment=VAULT_PATH=/opt/secbrain/vault
Environment=MCP_API_KEY={api_key}
Environment=MCP_PORT=8765
Restart=always
User=root

[Install]
WantedBy=multi-user.target
"""


def to_vps(ssh_str: str, api_key: str) -> str:
    """
    Deploy SecBrain server to a VPS via SSH.
    Returns the vault URL (http://host:8765).
    """
    client = _connect(ssh_str)
    host = ssh_str.split("@", 1)[1]

    with console.status("[bold]Uploading SecBrain package...[/bold]"):
        _upload_package(client)

    console.print("[green][OK][/green] Package uploaded")

    with console.status("[bold]Installing dependencies...[/bold]"):
        python = _find_python(client)
        _run(client, f"{python} -m pip install fastmcp starlette uvicorn --quiet")

    console.print("[green][OK][/green] Dependencies installed")

    with console.status("[bold]Creating vault directories...[/bold]"):
        _run(client, "mkdir -p /opt/secbrain/vault/_system /opt/secbrain/vault/projects "
                     "/opt/secbrain/vault/skills/global /opt/secbrain/vault/raw /opt/secbrain/vault/wiki")

    with console.status("[bold]Configuring systemd service...[/bold]"):
        service = SERVICE_TEMPLATE.format(api_key=api_key, python=python)
        sftp = client.open_sftp()
        with sftp.open("/etc/systemd/system/secbrain.service", "w") as f:
            f.write(service)
        sftp.close()
        _run(client, "systemctl daemon-reload && systemctl enable --now secbrain")

    console.print("[green][OK][/green] systemd service started")
    client.close()

    vault_url = f"http://{host}:8765"
    console.print(f"[green][OK][/green] Server running at {vault_url}")
    return vault_url


def redeploy(ssh_str: str, api_key: str) -> str:
    """Update and restart SecBrain on VPS."""
    client = _connect(ssh_str)
    host = ssh_str.split("@", 1)[1]

    with console.status("Uploading updated package..."):
        _upload_package(client)

    with console.status("Restarting service..."):
        _run(client, "systemctl restart secbrain")

    client.close()
    vault_url = f"http://{host}:8765"
    console.print(f"[green][OK][/green] Server redeployed at {vault_url}")
    return vault_url


def _connect(ssh_str: str):
    """Connect via SSH and return the client."""
    try:
        import paramiko
    except ImportError:
        raise RuntimeError("paramiko is required: pip install paramiko")

    if "@" not in ssh_str:
        raise ValueError(f"Invalid SSH address '{ssh_str}'. Expected: user@host")

    user, host = ssh_str.split("@", 1)

    with console.status(f"[bold]Connecting to {host}...[/bold]"):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(host, username=user)

    console.print(f"[green][OK][/green] Connected to {host}")
    return client


def _upload_package(client):
    """
    Bundle the local secbrain package into a tarball and upload it to /opt/secbrain/
    on the remote host. This avoids the need for PyPI.
    """
    # Find the package directory (same dir as this file)
    pkg_dir = Path(__file__).parent

    # Create in-memory tarball of the secbrain package
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Add secbrain/*.py files
        for py_file in sorted(pkg_dir.glob("*.py")):
            tar.add(str(py_file), arcname=f"secbrain/{py_file.name}")
        # Add pyproject.toml from parent
        pyproject = pkg_dir.parent / "pyproject.toml"
        if pyproject.exists():
            tar.add(str(pyproject), arcname="pyproject.toml")

    buf.seek(0)
    tarball_bytes = buf.read()

    sftp = client.open_sftp()
    _run(client, "mkdir -p /tmp/secbrain-deploy")
    with sftp.open("/tmp/secbrain-deploy/secbrain.tar.gz", "wb") as f:
        f.write(tarball_bytes)
    sftp.close()

    _run(client, "cd /tmp/secbrain-deploy && tar -xzf secbrain.tar.gz")
    _run(client, "mkdir -p /opt/secbrain && cp -r /tmp/secbrain-deploy/secbrain /opt/secbrain/")
    _run(client, "cp /tmp/secbrain-deploy/pyproject.toml /opt/secbrain/ 2>/dev/null || true")
    _run(client, "rm -rf /tmp/secbrain-deploy")


def _find_python(client) -> str:
    """Return the path to python3 on the remote host."""
    for candidate in ["python3", "python"]:
        _, stdout, _ = client.exec_command(f"which {candidate}")
        path = stdout.read().decode().strip()
        if path:
            return path
    return "python3"


def _run(client, cmd: str) -> str:
    """Execute a command on the remote host; raise on non-zero exit."""
    _, stdout, stderr = client.exec_command(cmd)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode()
    err = stderr.read().decode()
    if exit_code != 0:
        raise RuntimeError(f"Remote command failed (exit {exit_code}):\n{cmd}\n{err}")
    return out
