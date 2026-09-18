"""MCP(Model Context Protocol)client — 讓 Atlas 工作流呼叫任何 MCP server 的 tool。

一個 MCP 節點 = 連到一個 MCP server、呼叫指定 tool、把結果帶回 pipeline。
這樣就繼承整個 MCP 生態(GitHub / Slack / Notion / DB / filesystem … 上千個現成 server)。

支援兩種 transport:
  - stdio(預設):本機起 server subprocess,stateless、呼叫完收掉。安全面同本機執行。
  - remote(sse / streamable-http):連到一個 URL。⚠️ 會把 tool 參數送到外部端點、
    回傳內容進入工作流(信任 + 外送風險),所以只在使用者對該 server 明確同意後才啟用。
"""
from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from typing import Any, Optional


def _resolve_command(command: str) -> str:
    """Windows 上 npx/uvx 實際是 npx.cmd/uvx.exe;subprocess 不吃裸名 → 用 which 解析。"""
    return shutil.which(command) or command


@asynccontextmanager
async def _mcp_session(
    *, command: Optional[str] = None, args: Optional[list[str]] = None,
    env: Optional[dict] = None, url: Optional[str] = None,
    transport: str = "stdio", headers: Optional[dict] = None,
):
    """開一個已 initialize 的 ClientSession,依 transport 分派。

    stdio → 起本機子行程;sse / streamable-http → 連遠端 URL(headers 放 Authorization 等)。
    """
    from mcp import ClientSession
    t = (transport or "stdio").lower().replace("_", "-")
    if t == "sse":
        from mcp.client.sse import sse_client
        async with sse_client(url, headers=headers or {}) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif t in ("http", "streamable-http", "streamablehttp"):
        from mcp.client.streamable_http import streamablehttp_client
        # streamablehttp_client 回 3 元組(read, write, get_session_id)
        async with streamablehttp_client(url, headers=headers or {}) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:  # stdio
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(command=_resolve_command(command or ""), args=list(args or []), env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def _content_texts(res: Any) -> str:
    texts = []
    for c in (getattr(res, "content", None) or []):
        t = getattr(c, "text", None)
        if t is not None:
            texts.append(t)
    return "\n".join(texts)


async def call_mcp_tool(
    *, command: str = "", args: Optional[list[str]] = None, tool_name: str,
    tool_args: Optional[dict] = None, timeout: float = 60.0,
    env: Optional[dict] = None, url: Optional[str] = None,
    transport: str = "stdio", headers: Optional[dict] = None,
) -> dict:
    """連 MCP server(stdio 或 remote)→ initialize → call_tool。

    回傳 {ok, result_text, structured, is_error} 或 {ok:False, error}。
    """
    async def _run():
        async with _mcp_session(command=command, args=args, env=env,
                                url=url, transport=transport, headers=headers) as session:
            res = await session.call_tool(tool_name, tool_args or {})
            return {
                "ok": not bool(getattr(res, "isError", False)),
                "result_text": _content_texts(res),
                "structured": getattr(res, "structuredContent", None),
                "is_error": bool(getattr(res, "isError", False)),
            }

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"MCP 呼叫逾時({timeout}s)"}
    except Exception as e:
        return {"ok": False, "error": f"MCP 呼叫失敗:{type(e).__name__}: {e}"}


async def list_mcp_tools(
    *, command: str = "", args: Optional[list[str]] = None, timeout: float = 30.0,
    env: Optional[dict] = None, url: Optional[str] = None,
    transport: str = "stdio", headers: Optional[dict] = None,
) -> dict:
    """連 server 列出可用 tools(給前端 / AI 助手探索用)。"""
    async def _run():
        async with _mcp_session(command=command, args=args, env=env,
                                url=url, transport=transport, headers=headers) as session:
            tl = await session.list_tools()
            return {"ok": True, "tools": [
                {"name": t.name, "description": t.description or "",
                 "input_schema": getattr(t, "inputSchema", None)}
                for t in tl.tools]}

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
