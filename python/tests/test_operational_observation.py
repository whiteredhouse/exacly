"""Controlled independent HTTP measurements never substitute for selected staging acceptance."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import os
import socket
import socketserver
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from exacly import execution_store as store
from exacly import operational_observation as observation
from exacly import planning

Doc = dict[str, Any]
BODY = b"opaque static artifact\x00\xff\n"


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


@pytest.fixture
def expected() -> Doc:
    return {
        "application": "invoice-web",
        "environment": "nonproduction-test",
        "artifact": {"name": "opaque.bin", "digest": _digest(BODY), "size": len(BODY)},
        "source": {"repository": {"id": 123, "full_name": "owner/project"}, "revision": "a" * 40},
        "rollout_id": "test-rollout-123",
        "effect_nonce": "b" * 32,
        "generation": "2",
    }


@pytest.fixture
def configuration(tmp_path: Path) -> Doc:
    parent = tmp_path / "independent-observer"
    parent.mkdir(mode=0o700)
    key = parent / "observer.key"
    identity = observation.initialize_key(key)
    return {
        "schema_version": observation.VERSION,
        "profile": observation.PROFILE,
        "application": "invoice-web",
        "environment": "nonproduction-test",
        "url": "http://127.0.0.1:1234/artifact",
        "key": {"path": str(key), "id": identity},
        "samples": 2,
        "interval_ms": 2,
        "max_latency_ms": 500,
        "max_age_ms": 30000,
        "max_artifact_bytes": 1024,
    }


@contextmanager
def _service(expected: Doc, configuration: Doc, **changes: Any) -> Iterator[Doc]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if changes.get("delay"):
                time.sleep(changes["delay"])
            body = changes.get("body", BODY)
            self.send_response(changes.get("status", 200))
            self.send_header("Content-Length", str(len(body)))
            for key in observation.HEADERS:
                value = expected["source"]["revision"] if key == "source_revision" else expected[key]
                self.send_header("X-Exacly-" + key.replace("_", "-"), changes.get(key, value))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    selected = {**configuration, "url": f"http://127.0.0.1:{server.server_port}/artifact"}
    try:
        yield selected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _observe(expected: Doc, configuration: Doc) -> Doc:
    return observation.observe(expected, configuration, deadline=store.Deadline(3))


def _verify(report: Doc, expected: Doc, configuration: Doc, **kwargs: Any) -> Doc:
    return observation.verify(report, expected, configuration, now=dt.datetime.now(dt.UTC), **kwargs)


def _resign(report: Doc, configuration: Doc) -> Doc:
    """Trusted test signer probes semantic validation, never an input surface of the observer."""
    report = copy.deepcopy(report)
    unsigned = {key: value for key, value in report.items() if key not in ("id", "signature")}
    secret = Path(configuration["key"]["path"]).read_bytes()[len(observation.MAGIC) :]
    report["id"] = planning.digest(unsigned)
    report["signature"] = hmac.new(
        secret, observation.DOMAIN + store.canonical(unsigned).encode(), hashlib.sha256
    ).hexdigest()
    return report


def test_real_opaque_response_bytes_and_exact_rollout_window_are_independently_measured(
    expected: Doc, configuration: Doc
) -> None:
    with _service(expected, configuration) as configuration:
        report = _observe(expected, configuration)
        result = _verify(report, expected, configuration)
    assert result["status"] == "healthy"
    assert all(sample["digest"] == _digest(BODY) and sample["size"] == len(BODY) for sample in report["samples"])
    assert len(report["samples"]) == report["window"]["required_samples"] == 2
    assert report["expected"] == expected
    assert result["generic_application_health"] is result["deployment_authority"] is False
    assert BODY.hex() not in store.canonical(report)


def test_retained_authentication_is_separate_from_current_freshness(expected: Doc, configuration: Doc) -> None:
    with _service(expected, configuration) as configuration:
        report = _observe(expected, configuration)
        expiry = dt.datetime.fromisoformat(report["window"]["expires_at"])
        historical = observation.authenticate(report, expected, configuration, now=expiry)
        assert historical["status"] == "healthy"
        assert historical["scope"] == "historical-static-measurements"
        assert historical["current_readiness_established"] is False
        current = observation.current_status(report, expected, configuration, now=expiry)
        assert current["status"] == "unknown" and current["freshness"] == "stale"
        with pytest.raises(ValueError, match="stale"):
            observation.verify(report, expected, configuration, now=expiry)
        before_measurement = dt.datetime.fromisoformat(report["window"]["started_at"]) - dt.timedelta(microseconds=1)
        with pytest.raises(ValueError, match="future"):
            observation.authenticate(report, expected, configuration, now=before_measurement)


def test_actual_clock_freshness_is_decided_after_authentication(
    expected: Doc, configuration: Doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _service(expected, configuration) as configuration:
        report = _observe(expected, configuration)
        entry = dt.datetime.fromisoformat(report["window"]["ended_at"])
        expiry = dt.datetime.fromisoformat(report["window"]["expires_at"])
        clock = [entry]
        after_authentication = [expiry]
        authenticate = observation.authenticate

        def authenticated_after_expiry(*args: Any, **kwargs: Any) -> Doc:
            measured = authenticate(*args, **kwargs)
            clock[0] = after_authentication[0]
            return measured

        monkeypatch.setattr(observation, "_now", lambda: clock[0])
        monkeypatch.setattr(observation, "authenticate", authenticated_after_expiry)
        current = observation.current_status(report, expected, configuration)
        assert current["evaluated_at"] == expiry.isoformat()
        assert current["historical_status"] == "healthy"
        assert current["status"] == "unknown" and current["freshness"] == "stale"
        assert current["current_readiness_established"] is False
        # An explicit clock remains a deterministic historical/as-of query, clearly timestamped.
        as_of_entry = observation.current_status(report, expected, configuration, now=entry)
        assert as_of_entry["evaluated_at"] == entry.isoformat()
        assert as_of_entry["status"] == "healthy" and as_of_entry["freshness"] == "current"
        clock[0] = entry
        after_authentication[0] = entry - dt.timedelta(microseconds=1)
        with pytest.raises(ValueError, match="future"):
            observation.current_status(report, expected, configuration)


@pytest.mark.parametrize(
    "changes",
    [
        {"body": b"silently substituted bytes"},
        {"status": 503},
        {"generation": "earlier-generation"},
        {"rollout_id": "another-rollout"},
        {"effect_nonce": "c" * 32},
        {"source_revision": "c" * 40},
    ],
)
def test_workflow_identity_or_http_success_cannot_replace_actual_applicable_bytes(
    expected: Doc, configuration: Doc, changes: Doc
) -> None:
    with _service(expected, configuration, **changes) as configuration:
        report = _observe(expected, configuration)
        assert _verify(report, expected, configuration)["status"] == "unhealthy"


def test_absent_and_oversized_responses_remain_unknown(expected: Doc, configuration: Doc) -> None:
    with _service(expected, configuration, body=b"x" * 2048) as configuration:
        report = _observe(expected, configuration)
        assert _verify(report, expected, configuration)["status"] == "unknown"
        assert all(sample["failure"] == "oversized" and sample["digest"] is None for sample in report["samples"])
    with socket.socket() as selected:
        selected.bind(("127.0.0.1", 0))
        port = selected.getsockname()[1]
    configuration = {**configuration, "url": f"http://127.0.0.1:{port}/artifact"}
    report = _observe(expected, configuration)
    assert _verify(report, expected, configuration)["status"] == "unknown"
    assert all(sample["failure"] == "transport" for sample in report["samples"])


@pytest.mark.parametrize(
    "field", ["application", "environment", "artifact", "source", "rollout_id", "effect_nonce", "generation"]
)
def test_report_cannot_be_rebound_to_another_artifact_environment_or_rollout(
    expected: Doc, configuration: Doc, field: str
) -> None:
    with _service(expected, configuration) as configuration:
        report = _observe(expected, configuration)
        changed = copy.deepcopy(expected)
        if field == "artifact":
            changed[field]["digest"] = "sha256:" + "0" * 64
        elif field == "source":
            changed[field]["revision"] = "0" * 40
        elif field == "effect_nonce":
            changed[field] = "0" * 32
        else:
            changed[field] = "other"
        with pytest.raises(ValueError):
            _verify(report, changed, configuration)


def test_stale_future_and_consumed_reports_fail_closed(expected: Doc, configuration: Doc) -> None:
    with _service(expected, configuration) as configuration:
        report = _observe(expected, configuration)
        end = dt.datetime.fromisoformat(report["window"]["ended_at"])
        expiry = dt.datetime.fromisoformat(report["window"]["expires_at"])
        for now in (end - dt.timedelta(microseconds=1), expiry, expiry + dt.timedelta(seconds=1)):
            with pytest.raises(ValueError, match="stale, future or incomplete"):
                observation.verify(report, expected, configuration, now=now)
        with pytest.raises(ValueError, match="already been consumed"):
            _verify(report, expected, configuration, consumed_report_ids={report["id"]})


@pytest.mark.parametrize("damage", ["sample", "expected", "window", "signature", "identity", "field"])
def test_changed_or_unsigned_candidate_observations_are_rejected(
    expected: Doc, configuration: Doc, damage: str
) -> None:
    with _service(expected, configuration) as configuration:
        report = _observe(expected, configuration)
        if damage == "sample":
            report["samples"][0]["digest"] = "sha256:" + "0" * 64
        elif damage == "expected":
            report["expected"]["generation"] = "fake"
        elif damage == "window":
            report["window"]["required_samples"] = 3
        elif damage == "signature":
            report["signature"] = "0" * 64
        elif damage == "identity":
            report["id"] = "sha256:" + "0" * 64
        else:
            report["health"] = True
        with pytest.raises(ValueError):
            _verify(report, expected, configuration)


@pytest.mark.parametrize("damage", ["missing_sample", "required_samples", "interval", "completeness", "expiry"])
def test_even_trusted_signed_incomplete_or_inconsistent_sampling_windows_are_rejected(
    expected: Doc, configuration: Doc, damage: str
) -> None:
    with _service(expected, configuration) as configuration:
        report = _observe(expected, configuration)
        if damage == "missing_sample":
            report["samples"].pop()
        elif damage == "required_samples":
            report["window"]["required_samples"] = 3
        elif damage == "interval":
            report["samples"][1]["started_at"] = report["samples"][0]["ended_at"]
        elif damage == "completeness":
            report["samples"][0]["complete"] = False
        else:
            report["window"]["expires_at"] = report["window"]["ended_at"]
        report = _resign(report, configuration)
        with pytest.raises(ValueError):
            _verify(report, expected, configuration)


def test_observer_key_is_private_exclusive_and_purpose_and_domain_separated(expected: Doc, configuration: Doc) -> None:
    key = Path(configuration["key"]["path"])
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert observation.key_identity(key) == configuration["key"]["id"]
    assert key.read_bytes().startswith(observation.MAGIC)
    with pytest.raises(FileExistsError):
        observation.initialize_key(key)
    with _service(expected, configuration) as configuration:
        report = _observe(expected, configuration)
        unsigned = {name: value for name, value in report.items() if name not in ("id", "signature")}
        secret = key.read_bytes()[len(observation.MAGIC) :]
        other_domain = hmac.new(
            secret, b"exacly.delivery.merge.marker.v1\0" + store.canonical(unsigned).encode(), hashlib.sha256
        )
        report["signature"] = other_domain.hexdigest()
        with pytest.raises(ValueError, match="signature"):
            _verify(report, expected, configuration)
    key.write_bytes(b"EXACLY DELIVERY MERGE KEY v1\n" + os.urandom(32))
    with pytest.raises(ValueError, match="purpose"):
        observation.key_identity(key)


@pytest.mark.parametrize("damage", ["mode", "symlink", "hardlink", "replacement", "public_parent"])
def test_untrusted_observer_key_storage_never_authenticates_reports(
    expected: Doc, configuration: Doc, damage: str, tmp_path: Path
) -> None:
    with _service(expected, configuration) as configuration:
        report = _observe(expected, configuration)
        key = Path(configuration["key"]["path"])
        if damage == "mode":
            key.chmod(0o640)
        elif damage == "public_parent":
            key.parent.chmod(0o755)
        elif damage == "replacement":
            key.write_bytes(observation.MAGIC + os.urandom(32))
        else:
            other = tmp_path / "outside.key"
            key.rename(other)
            if damage == "symlink":
                key.symlink_to(other)
            else:
                key.hardlink_to(other)
        with pytest.raises((ValueError, OSError)):
            _verify(report, expected, configuration)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:1234/artifact",
        "http://localhost:1234/artifact",
        "http://example.org:1234/artifact",
        "http://127.0.0.1/artifact",
        "http://user:secret@127.0.0.1:1234/artifact",
        "http://127.0.0.1:1234/artifact?override=1",
        "http://127.0.0.1:1234/artifact#other",
        "http://127.0.0.1:1234/a b",
    ],
)
def test_observer_cannot_select_external_or_ambiguous_destinations(configuration: Doc, url: str) -> None:
    with pytest.raises(ValueError):
        observation.validate_configuration({**configuration, "url": url})


def test_fresh_configuration_digest_is_rederived_and_limits_are_not_candidate_authority(
    expected: Doc, configuration: Doc
) -> None:
    first = observation.configuration_digest(configuration)
    assert first != observation.configuration_digest({**configuration, "max_latency_ms": 501})
    with _service(expected, configuration) as configuration:
        report = _observe(expected, configuration)
        changed = {**configuration, "samples": 3}
        with pytest.raises(ValueError, match="observer"):
            _verify(report, expected, changed)
    with pytest.raises(ValueError):
        observation.validate_configuration({**configuration, "runtime_digest": "sha256:" + "0" * 64})


def test_deadline_and_sample_latency_bound_real_http_work(expected: Doc, configuration: Doc) -> None:
    configuration = {**configuration, "max_latency_ms": 20}
    with _service(expected, configuration, delay=0.1) as configuration:
        started = time.monotonic()
        report = _observe(expected, configuration)
        elapsed = time.monotonic() - started
        assert elapsed < 0.5
        assert _verify(report, expected, configuration)["status"] == "unknown"
        with pytest.raises(ValueError):
            observation.observe(expected, configuration, deadline=store.Deadline(0))


def test_configuration_and_reports_are_bounded_before_encoding(configuration: Doc) -> None:
    with pytest.raises(ValueError):
        observation.validate_configuration({**configuration, "url": "x" * (observation.MAX_REPORT_BYTES + 1)})
    with pytest.raises(ValueError):
        observation.validate_configuration({**configuration, "unknown": [None] * 10001})


@pytest.mark.parametrize("field", ["artifact_digest", "source_revision", "effect_nonce"])
def test_identity_fields_reject_trailing_newline_aliases(expected: Doc, configuration: Doc, field: str) -> None:
    expected = copy.deepcopy(expected)
    if field == "artifact_digest":
        expected["artifact"]["digest"] += "\n"
    elif field == "source_revision":
        expected["source"]["revision"] += "\n"
    else:
        expected["effect_nonce"] += "\n"
    with pytest.raises(ValueError):
        observation.observe(expected, configuration, deadline=store.Deadline(3))


@pytest.mark.parametrize(
    "response",
    [
        b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort",
        b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Encoding: gzip\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 00\r\n\r\n",
    ],
)
def test_malformed_or_incomplete_real_http_never_establishes_readiness(
    expected: Doc, configuration: Doc, response: bytes
) -> None:
    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.recv(4096)
            self.request.sendall(response)

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        configuration = {**configuration, "url": f"http://127.0.0.1:{server.server_address[1]}/artifact"}
        try:
            report = _observe(expected, configuration)
            assert _verify(report, expected, configuration)["status"] == "unknown"
            assert all(sample["failure"] == "protocol" for sample in report["samples"])
        finally:
            server.shutdown()
            thread.join(timeout=2)
