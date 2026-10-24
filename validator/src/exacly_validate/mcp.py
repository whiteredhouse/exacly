"""An MCP server over stdio, so coding agents can ask a model the same questions as the exacly command.

It speaks the Model Context Protocol's JSON-RPC messages, one per line, and offers one tool per question. Each
call reads the model again, so an agent sees its own edits to config. A result carries the text answer as
content and the same answer as data in `structuredContent`.

Run it with `exacly mcp`, from the repository that holds the model.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from exacly_validate import query

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")  # Newest first.
RECORD = {"type": "string", "description": "A record ID, such as REQ-001"}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "exacly_show",
        "description": "Everything the product model says about one record: its fields, its relationships both "
        "ways, its assessments and pending work by context, and validation findings about it. Use it before "
        "changing code or config that a record describes.",
        "inputSchema": {"type": "object", "properties": {"record": RECORD}, "required": ["record"]},
    },
    {
        "name": "exacly_trace",
        "description": "Why a record exists, up through Requirements, Processes and Capabilities to Outcomes, and "
        "how it is realized, down through Applications, Information and Infrastructure.",
        "inputSchema": {"type": "object", "properties": {"record": RECORD}, "required": ["record"]},
    },
    {
        "name": "exacly_impact",
        "description": "What a change affects: every record that relies on the changed ones, with the path, the "
        "Outcomes reached, and the Requirements whose tests to run again. Give record IDs, or a Git revision "
        "as base (such as main) to compare the working tree's model with.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "records": {"type": "array", "items": RECORD, "description": "The changed records"},
                "base": {"type": "string", "description": "A Git revision to compare with, instead of records"},
            },
        },
    },
    {
        "name": "exacly_gaps",
        "description": "What the model is missing: validation errors and warnings, Requirements not verified or "
        "released in any context, Requirements with no observation binding, proposed Measures, pending work.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class Server:
    """Answers MCP requests about the model at `paths`."""

    def __init__(
        self, paths: list[Path], ask: Callable[..., tuple[dict[str, Any], str]], version: str = "0.1.0"
    ) -> None:
        self.paths, self.ask, self.version = paths, ask, version

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """The response to one message, or None for a notification."""
        if "id" not in message:
            return None
        try:
            result = self.result(message.get("method", ""), message.get("params") or {})
        except LookupError as error:
            return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": str(error)}}
        except (TypeError, ValueError) as error:
            return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32602, "message": str(error)}}
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}

    def result(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "exacly", "version": self.version},
                "instructions": "Ask the product model (Exacly config and state) about records before and after "
                "changing what they describe.",
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self.call(params.get("name", ""), params.get("arguments") or {})
        raise LookupError(f"unknown method {method}")

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in {tool["name"] for tool in TOOLS}:
            raise ValueError(f"unknown tool {name}")
        try:
            answer, text = self.ask(
                name.removeprefix("exacly_"),
                self.paths,
                record=arguments.get("record"),
                records=arguments.get("records") or (),
                base=arguments.get("base"),
            )
        except query.QueryError as error:
            # A question the model can't answer is the tool's result, so the agent can correct the call.
            return {"content": [{"type": "text", "text": str(error)}], "isError": True}
        return {"content": [{"type": "text", "text": text}], "structuredContent": answer}


def serve(server: Server, incoming: IO[str] = sys.stdin, outgoing: IO[str] = sys.stdout) -> None:
    """Read messages from `incoming`, one per line, and write responses to `outgoing` until it closes."""
    for line in incoming:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            response: dict[str, Any] | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"not JSON: {error}"},
            }
        else:
            response = server.handle(message) if isinstance(message, dict) else None
        if response is not None:
            outgoing.write(json.dumps(response, default=str) + "\n")
            outgoing.flush()
