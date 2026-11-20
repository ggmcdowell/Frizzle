"""One bounded MCP stdio probe: start a server, list its tools, close it again.

A seat check calls this to prove a server really
answers; the acceptance rule is a non-empty tool list where every tool carries a name
and a schema. The MCP SDK is imported inside the probe, so this module imports under
any Python 3.11+ interpreter; every real caller runs the hub interpreter (the root .venv).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

BEARER = re.compile(r"(?i)(bearer)\s+\S+")
LABELLED_SECRET = re.compile(r"(?i)\b(token|credential|passwd|password|api[_-]?key)\b\s*(?:[=:]|\s)\s*\S+")
SECRET_RUN = re.compile(r"[A-Za-z0-9._-]{40,}")


class _Inactive(RuntimeError):
    """The server started and exposed no tool at all."""


def _scrub(text: Any) -> str:
    """Remove credential-shaped values before any diagnostic leaves this process."""
    value = str(text if text is not None else "")
    value = BEARER.sub(r"\1 [redacted]", value)
    value = LABELLED_SECRET.sub(lambda match: match.group(1) + " [redacted]", value)
    return SECRET_RUN.sub("[redacted]", value)


def _carries(error: BaseException, kind: type[BaseException]) -> bool:
    """A cancelled task group may deliver the real cause inside an exception group."""
    if isinstance(error, kind):
        return True
    return any(_carries(item, kind) for item in getattr(error, "exceptions", ()))


def probe_stdio(argv: Sequence[str], cwd, env: Mapping[str, str] | None,
                timeout: float = 70) -> tuple[bool, str]:
    """Initialize and discover a stdio server, closing it on success, failure, or timeout.

    A pass carries its own evidence: the tool count and the first names the server listed.
    """
    import anyio
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    import tempfile

    command = [str(item) for item in argv]
    evidence: list[str] = []

    async def probe(errlog) -> None:
        parameters = StdioServerParameters(command=command[0], args=command[1:],
                                           env=None if env is None else dict(env), cwd=str(cwd))
        with anyio.fail_after(timeout):
            async with stdio_client(parameters, errlog=errlog) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    tools = []
                    cursor = None
                    while True:
                        listed = await session.list_tools(cursor=cursor)
                        tools.extend(listed.tools)
                        cursor = listed.nextCursor
                        if not cursor:
                            break
                    if not tools:
                        raise _Inactive("inactive")
                    if any(not isinstance(tool.name, str) or not tool.name
                           or not isinstance(tool.inputSchema, dict) for tool in tools):
                        raise RuntimeError("MCP server exposed no valid tools")
                    # What the server really exposed: the count, then the first names.
                    evidence.append(f"{len(tools)} tools: "
                                    + ", ".join(tool.name for tool in tools[:6]))

    # The SDK hands errlog to the child as its stderr, so it must be a real file with a
    # descriptor; an in-memory buffer fails every probe with UnsupportedOperation('fileno').
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as errlog:
        try:
            anyio.run(probe, errlog)
        except Exception as exc:
            if _carries(exc, _Inactive):
                return False, "inactive"
            if _carries(exc, TimeoutError):
                return False, "timeout"
            errlog.seek(0)
            tail = [line for line in errlog.read().splitlines() if line.strip()][-40:]
            return False, "\n".join([_scrub(repr(exc)), *(_scrub(line) for line in tail)])
    return True, evidence[-1] if evidence else ""
