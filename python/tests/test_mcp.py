"""Checks the MCP server: the protocol messages, and each tool's answer over the invoice reminders example.

Run with `mise run test`, or from the repository root: uv run pytest python/tests
"""

import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from exacly import mcp
from exacly.cli import ask

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "spec" / "examples" / "0.2" / "invoice-reminders"
SERVER = mcp.Server([EXAMPLE], ask, "1.2.3")


def request(method: str, params: dict[str, Any] | None = None, rid: int = 1) -> dict[str, Any]:
    response = SERVER.handle({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params else {})})
    assert response is not None and response["id"] == rid
    return response


def call(name: str, **arguments: Any) -> dict[str, Any]:
    result: dict[str, Any] = request("tools/call", {"name": name, "arguments": arguments})["result"]
    return result


def test_initialize_agrees_on_a_protocol_version() -> None:
    result = request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"] == {"name": "exacly", "version": "1.2.3"}
    newer = request("initialize", {"protocolVersion": "2099-01-01"})["result"]
    assert newer["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]


def test_notifications_get_no_response() -> None:
    assert SERVER.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_one_tool_per_question() -> None:
    tools = request("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["exacly_show", "exacly_trace", "exacly_impact", "exacly_gaps"]
    assert all(t["inputSchema"]["type"] == "object" and t["description"] for t in tools)
    assert request("ping")["result"] == {}


def test_answers_as_text_and_data() -> None:
    result = call("exacly_show", record="REQ-001")
    assert result["content"][0]["text"].startswith("REQ-001 Requirement: Remind before due date")
    assert result["structuredContent"]["record"]["id"] == "REQ-001"
    assert "isError" not in result
    assert call("exacly_trace", record="APP-001")["structuredContent"]["why"]["id"] == "APP-001"
    assert call("exacly_impact", records=["INF-003"])["structuredContent"]["outcomes"] == ["OUT-001"]
    assert call("exacly_gaps")["structuredContent"]["result"] == "conforms"


def test_a_question_the_model_cant_answer_is_a_tool_error() -> None:
    assert call("exacly_show", record="REQ-404")["isError"] is True
    assert call("exacly_show")["content"][0]["text"] == "Invalid arguments: 'record' is a required property"
    assert "record IDs or a base revision" in call("exacly_impact")["content"][0]["text"]


def test_protocol_errors() -> None:
    assert request("tools/call", {"name": "exacly_delete"})["error"]["code"] == -32602
    assert request("resources/list")["error"]["code"] == -32601


def test_serve_reads_and_writes_one_message_per_line() -> None:
    incoming = io.StringIO(
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                "",
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                "{not json",
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            ]
        )
    )
    outgoing = io.StringIO()
    mcp.serve(SERVER, incoming, outgoing)
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert [r["id"] for r in responses] == [1, None, 2]
    assert responses[1]["error"]["code"] == -32700


def test_the_exacly_mcp_command() -> None:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "exacly_gaps", "arguments": {}}},
    ]
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from exacly.cli import exacly_main; sys.exit(exacly_main())",
            "mcp",
            "-m",
            str(EXAMPLE),
        ],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        check=True,
    )
    responses = [json.loads(line) for line in done.stdout.splitlines()]
    assert [r["id"] for r in responses] == [1, 2]
    assert responses[1]["result"]["structuredContent"]["result"] == "conforms"


def test_tools_say_they_only_read() -> None:
    for tool in request("tools/list")["result"]["tools"]:
        assert tool["title"]
        assert tool["annotations"] == {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
        assert tool["inputSchema"]["additionalProperties"] is False


def test_arguments_are_checked_against_the_input_schema() -> None:
    wrong = call("exacly_show", record=123)
    assert wrong["isError"] is True
    assert wrong["content"][0]["text"].startswith("Invalid arguments: 123 is not of type 'string'")
    assert call("exacly_impact", records="INF-003")["isError"] is True
    assert call("exacly_gaps", extra=True)["isError"] is True


def test_an_unexpected_failure_is_an_internal_error_and_the_server_keeps_serving() -> None:
    def broken(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], str]:
        raise RuntimeError("disk on fire")

    server = mcp.Server([EXAMPLE], broken)
    incoming = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "exacly_gaps"}})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        + "\n"
    )
    outgoing = io.StringIO()
    mcp.serve(server, incoming, outgoing)
    first, second = (json.loads(line) for line in outgoing.getvalue().splitlines())
    assert first["error"] == {"code": -32603, "message": "internal error: disk on fire"}
    assert second == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_a_model_that_does_not_parse_is_a_tool_error() -> None:
    with tempfile.TemporaryDirectory() as folder:
        broken = Path(folder)
        for name in ("model.yaml", "billing.yaml"):
            (broken / name).write_text((EXAMPLE / name).read_text())
        (broken / "billing.yaml").write_text("records: [unclosed\n")
        result = mcp.Server([broken], ask).call("exacly_gaps", {})
        assert result["isError"] is True
        problem = "expected ',' or ']', but got '<stream end>'"
        assert (
            result["content"][0]["text"]
            == f"{broken / 'billing.yaml'} is not valid YAML at line 2, column 1: {problem}"
        )


def test_a_value_yaml_cant_construct_is_a_tool_error() -> None:
    with tempfile.TemporaryDirectory() as folder:
        broken = Path(folder)
        for name in ("model.yaml", "billing.yaml"):
            (broken / name).write_text((EXAMPLE / name).read_text())
        billing = (broken / "billing.yaml").read_text()
        billing = billing.replace("last_reviewed_at: null", "last_reviewed_at: 2026-13-24", 1)
        (broken / "billing.yaml").write_text(billing)
        result = mcp.Server([broken], ask).call("exacly_gaps", {})
        assert result["isError"] is True
        problem = "line 15, column 23: '2026-13-24' is not a valid timestamp: month must be in 1..12"
        assert result["content"][0]["text"] == f"{broken / 'billing.yaml'} is not valid YAML at {problem}"


def test_arguments_that_are_not_an_object_are_rejected() -> None:
    wrongs: list[Any] = [False, 0, [], ""]
    for wrong in wrongs:
        result = request("tools/call", {"name": "exacly_gaps", "arguments": wrong})["result"]
        assert result["isError"] is True, wrong
        assert result["content"][0]["text"].startswith("Invalid arguments: ")
    assert "isError" not in request("tools/call", {"name": "exacly_gaps"})["result"]
