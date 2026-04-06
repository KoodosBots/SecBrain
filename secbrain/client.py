"""Client for calling vault tools from CLI commands."""

from __future__ import annotations

import asyncio

from fastmcp import Client
from fastmcp.client.transports.http import StreamableHttpTransport


class VaultClient:
    """Thin wrapper around FastMCP's Client for CLI-side vault calls."""

    def __init__(self, cfg: dict):
        vault_url = cfg.get("vault_url", "http://127.0.0.1:8765").rstrip("/")
        self._mcp_url = f"{vault_url}/mcp"
        self._api_key = cfg.get("api_key", "")

    def _transport(self) -> StreamableHttpTransport:
        return StreamableHttpTransport(
            url=self._mcp_url,
            auth=self._api_key if self._api_key else None,
        )

    def health(self) -> bool:
        """Check if the server is reachable. Raises on failure."""
        import httpx
        resp = httpx.get(
            self._mcp_url.replace("/mcp", "/"),
            timeout=5,
            headers={"Authorization": f"Bearer {self._api_key}"} if self._api_key else {},
        )
        # Any response (even 405) means the server is up
        return True

    def call(self, tool_name: str, params: dict | None = None) -> str:
        """Call an MCP tool and return the text result."""
        params = params or {}
        return asyncio.run(self._call_async(tool_name, params))

    async def _call_async(self, tool_name: str, params: dict) -> str:
        async with Client(self._transport()) as c:
            result = await c.call_tool(tool_name, params)
            if result.is_error:
                raise RuntimeError(str(result.content))
            if result.content:
                return "\n".join(
                    item.text for item in result.content
                    if hasattr(item, "text")
                )
            return str(result.data) if result.data is not None else ""
