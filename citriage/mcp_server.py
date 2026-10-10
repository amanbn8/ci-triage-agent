"""Model Context Protocol (MCP) server over stdio.

Lets AI assistants (Claude, Copilot, Cursor and other MCP clients) call the
triage tools directly: "why did my CI fail?" becomes a tool call that returns
a grounded diagnosis instead of the model guessing from a pasted log.

Implemented from the spec with the standard library: JSON-RPC 2.0 messages,
one per line on stdin/stdout; logs go to stderr so they never corrupt the stream.
"""

import json
import sys

from . import __version__
from .fingerprint import fingerprint
from .flaky import TestResult, find_flaky
from .report import to_markdown
from .triage import triage_log

SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

TOOLS = [
    {
        "name": "triage_ci_log",
        "description": "Diagnose a failed CI job from its raw log: category, failing step, "
                       "evidence lines, whether a re-run helps, and a suggested fix.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "log": {"type": "string", "description": "Raw GitHub Actions job log text"},
                "format": {"type": "string", "enum": ["json", "markdown"], "default": "markdown"},
            },
            "required": ["log"],
        },
    },
    {
        "name": "find_flaky_tests",
        "description": "Find flaky tests: tests that both passed and failed on the same commit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "test_id": {"type": "string"},
                            "commit": {"type": "string"},
                            "passed": {"type": "boolean"},
                        },
                        "required": ["test_id", "commit", "passed"],
                    },
                }
            },
            "required": ["results"],
        },
    },
    {
        "name": "fingerprint_error",
        "description": "Stable hash of an error message with run-specific noise (paths, numbers, "
                       "hashes) removed, for grouping the same failure across runs.",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
]


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _text(payload) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def call_tool(name: str, args: dict) -> dict:
    if name == "triage_ci_log":
        diagnosis = triage_log(args["log"])
        if args.get("format") == "json":
            return _text(diagnosis.to_dict())
        return _text(to_markdown(diagnosis))
    if name == "find_flaky_tests":
        results = [TestResult(r["test_id"], r["commit"], bool(r["passed"])) for r in args["results"]]
        return _text(find_flaky(results))
    if name == "fingerprint_error":
        return _text({"fingerprint": fingerprint(args["message"])})
    raise RpcError(-32602, f"Unknown tool: {name}")


def handle(message: dict) -> dict | None:
    """Process one JSON-RPC message; return the response, or None for notifications."""
    msg_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if msg_id is None:
        return None  # notification (e.g. notifications/initialized): no reply allowed
    try:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ci-triage-agent", "version": __version__},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                result = call_tool(params.get("name", ""), params.get("arguments") or {})
            except (KeyError, TypeError, ValueError) as exc:
                # Bad tool input is reported to the model as a tool error it can correct.
                result = {"content": [{"type": "text", "text": f"Invalid arguments: {exc}"}],
                          "isError": True}
        else:
            raise RpcError(-32601, f"Method not found: {method}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    except RpcError as exc:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": exc.code, "message": str(exc)}}


def serve(stdin=sys.stdin, stdout=sys.stdout) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error"}}
        else:
            response = handle(message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    print("ci-triage-agent MCP server ready on stdio", file=sys.stderr)
    serve()
