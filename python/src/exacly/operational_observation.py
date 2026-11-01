"""Independent, purpose-keyed observation of one configured local static HTTP service.

This contract establishes byte-exact static readiness for one rollout and one fresh sampling
window. It is not a generic application-health probe, a native readiness gate, deployment
authority or proof that the independently trusted observer host/operator is honest. The owner
must retain consumed report identities durably; verification alone does not consume a report.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import socket
import time
import uuid
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import jsonschema

from exacly import delivery_profile, planning
from exacly import execution_store as store

Doc = dict[str, Any]
PROFILE = "local-static-http-observer-v1"
VERSION = "0.1.0"
MAGIC = b"EXACLY OPERATIONAL OBSERVER KEY v1\n"
DOMAIN = b"exacly.operational.static-observation.v1\0"
KEY_BYTES = 32
MAX_REPORT_BYTES = 128 * 1024
TEXT = {"type": "string", "minLength": 1, "maxLength": 1024, "not": {"pattern": "[\\x00-\\x1f\\x7f]"}}
DIGEST = {"type": "string", "minLength": 71, "maxLength": 71, "pattern": "^sha256:[0-9a-f]{64}$"}
NONCE = {"type": "string", "minLength": 32, "maxLength": 32, "pattern": "^[0-9a-f]{32}$"}
TIMESTAMP = {"type": "string", "minLength": 20, "maxLength": 40}
INTEGER = {"type": "integer", "minimum": 0, "maximum": 2**53 - 1}
EXPECTED_SCHEMA = planning.fields(
    {
        "application": TEXT,
        "environment": TEXT,
        "artifact": planning.fields({"name": TEXT, "digest": DIGEST, "size": INTEGER}),
        "source": planning.fields(
            {
                "repository": planning.fields(
                    {"id": {"type": "integer", "minimum": 1, "maximum": 2**53 - 1}, "full_name": TEXT}
                ),
                "revision": {"type": "string", "minLength": 40, "maxLength": 40, "pattern": "^[0-9a-f]{40}$"},
            }
        ),
        "rollout_id": TEXT,
        "effect_nonce": NONCE,
        "generation": TEXT,
    }
)
CONFIGURATION_SCHEMA = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "profile": {"const": PROFILE},
        "application": TEXT,
        "environment": TEXT,
        "url": TEXT,
        "key": planning.fields({"path": TEXT, "id": DIGEST}),
        "samples": {"type": "integer", "minimum": 2, "maximum": 32},
        "interval_ms": {"type": "integer", "minimum": 1, "maximum": 60000},
        "max_latency_ms": {"type": "integer", "minimum": 1, "maximum": 60000},
        "max_age_ms": {"type": "integer", "minimum": 1, "maximum": 3600000},
        "max_artifact_bytes": {"type": "integer", "minimum": 1, "maximum": 16 * 1024 * 1024},
    }
)
HEADERS = ("generation", "rollout_id", "effect_nonce", "source_revision")
SAMPLE_SCHEMA = planning.fields(
    {
        "started_at": TIMESTAMP,
        "ended_at": TIMESTAMP,
        "latency_ns": INTEGER,
        "status": {"type": "integer", "minimum": 0, "maximum": 599},
        "complete": {"type": "boolean"},
        "size": INTEGER,
        "digest": {"anyOf": [DIGEST, {"type": "null"}]},
        "headers": planning.fields({key: {"anyOf": [TEXT, {"type": "null"}]} for key in HEADERS}),
        "failure": {"enum": [None, "transport", "protocol", "oversized"]},
    }
)
REPORT_SCHEMA = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "profile": {"const": PROFILE},
        "kind": {"const": "operational-observation"},
        "id": DIGEST,
        "nonce": NONCE,
        "key_id": DIGEST,
        "configuration_digest": DIGEST,
        "expected": EXPECTED_SCHEMA,
        "window": planning.fields(
            {"started_at": TIMESTAMP, "ended_at": TIMESTAMP, "expires_at": TIMESTAMP, "required_samples": INTEGER}
        ),
        "samples": {"type": "array", "minItems": 2, "maxItems": 32, "items": SAMPLE_SCHEMA},
        "signature": {"type": "string", "minLength": 64, "maxLength": 64, "pattern": "^[0-9a-f]{64}$"},
    }
)


def _schema(value: Any, schema: Doc) -> Doc:
    try:
        pending = [(value, 0)]
        nodes = text_bytes = 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            store.require(nodes <= 10000 and depth <= 24, "operational observation document is too complex")
            if isinstance(item, dict):
                store.require(2 * len(item) + nodes <= 10000, "operational observation object exceeds its node bound")
                pending.extend((child, depth + 1) for child in item.values())
                pending.extend((key, depth + 1) for key in item)
            elif isinstance(item, list):
                store.require(len(item) + nodes <= 10000, "operational observation array exceeds its node bound")
                pending.extend((child, depth + 1) for child in item)
            elif isinstance(item, str):
                store.require(len(item) <= MAX_REPORT_BYTES, "operational observation text exceeds its bound")
                text_bytes += len(item.encode("utf-8"))
                store.require(text_bytes <= MAX_REPORT_BYTES, "operational observation text exceeds its bound")
        encoded = store.canonical(value).encode("utf-8")
        store.require(len(encoded) <= MAX_REPORT_BYTES, "operational observation document exceeds its bound")
        result = json.loads(encoded)
        jsonschema.Draft202012Validator(schema).validate(result)
        return cast(Doc, result)
    except (jsonschema.ValidationError, RecursionError, TypeError, UnicodeError):
        raise ValueError("invalid operational observation document") from None


def _timestamp(value: str) -> dt.datetime:
    try:
        result = dt.datetime.fromisoformat(value)
        store.require(result.tzinfo is not None and result.utcoffset() == dt.timedelta(0), "observer time must be UTC")
        store.require(result.isoformat() == value, "observer time must use canonical UTC timestamps")
        return result
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid operational observation timestamp") from None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _path(path: Path) -> Path:
    store.require(
        isinstance(path, Path) and path.is_absolute() and path == path.absolute() and ".." not in path.parts,
        "observer key requires a pinned canonical absolute path",
    )
    return store.location(path)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
    )


def initialize_key(path: Path, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> str:
    """Create a purpose-specific private observer key, never replacing an existing instance."""
    absolute = _path(path)
    with store.locked(absolute, write=True, lock_timeout=lock_timeout) as (selected, parent_fd):
        descriptor = os.open(selected, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            encoded = MAGIC + os.urandom(KEY_BYTES)
            offset = 0
            while offset < len(encoded):
                count = os.write(descriptor, encoded[offset:])
                store.require(count > 0, "observer key initialization was incomplete")
                offset += count
            info = store.file_info(descriptor)
            store.require(_identity(selected.lstat()) == _identity(info), "observer key changed during initialization")
            os.fsync(descriptor)
            os.fsync(parent_fd)
            return "sha256:" + hashlib.sha256(encoded).hexdigest()
        finally:
            os.close(descriptor)


def _key(path: Path, key_id: str | None, deadline: store.Deadline) -> tuple[str, bytes]:
    deadline.check()
    absolute = _path(path)
    with store.locked(absolute, write=False, deadline=deadline) as (selected, parent_fd):
        store.check_file_info(selected.lstat())
        descriptor = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = store.file_info(descriptor)
            store.require(before.st_size == len(MAGIC) + KEY_BYTES, "observer key has an unsupported format or purpose")
            encoded = os.read(descriptor, len(MAGIC) + KEY_BYTES + 1)
            store.require(
                len(encoded) == len(MAGIC) + KEY_BYTES
                and encoded.startswith(MAGIC)
                and _identity(os.fstat(descriptor)) == _identity(before)
                and _identity(selected.lstat()) == _identity(before),
                "observer key changed or has an unsupported format or purpose",
            )
            actual = "sha256:" + hashlib.sha256(encoded).hexdigest()
            store.require(
                key_id is None or hmac.compare_digest(actual, key_id), "observer key differs from pinned identity"
            )
            os.fsync(descriptor)
            os.fsync(parent_fd)
            deadline.check()
            return actual, encoded[len(MAGIC) :]
        finally:
            os.close(descriptor)


def key_identity(path: Path, *, lock_timeout: float = store.DEFAULT_LOCK_TIMEOUT) -> str:
    """Read only the identity of an already initialized, owner-private observer-purpose key."""
    return _key(path, None, store.Deadline(lock_timeout))[0]


def validate_configuration(value: Any) -> Doc:
    """Validate fixed trusted bootstrap configuration, without accessing the service or key."""
    result = _schema(value, CONFIGURATION_SCHEMA)
    url = urlsplit(result["url"])
    store.require(
        url.scheme == "http"
        and url.hostname in ("127.0.0.1", "::1")
        and url.username is None
        and url.password is None
        and url.port is not None
        and 1 <= url.port <= 65535
        and url.path.startswith("/")
        and result["url"].isascii()
        and not re.search(r"[\s\\]", url.path)
        and not url.query
        and not url.fragment,
        "static observer requires an exact loopback HTTP endpoint without credentials, query or fragment",
    )
    key_path = Path(result["key"]["path"])
    store.require(
        key_path.is_absolute() and key_path == key_path.absolute() and ".." not in key_path.parts,
        "observer key path must be canonical and absolute",
    )
    return result


def installed_profile() -> Doc:
    """Recompute the shared actual installed enforcement/runtime profile and closed schemas."""
    return {
        **delivery_profile.implementation(required=("operational_observation.py",)),
        "schemas": [EXPECTED_SCHEMA, CONFIGURATION_SCHEMA, REPORT_SCHEMA],
    }


def configuration_digest(configuration: Doc) -> str:
    """Pin actual installed observer code, closed schemas, runtime and selected configuration."""
    return planning.digest({"configuration": validate_configuration(configuration), "installed": installed_profile()})


def _expected(value: Any, configuration: Doc) -> Doc:
    result = _schema(value, EXPECTED_SCHEMA)
    store.require(
        result["application"] == configuration["application"] and result["environment"] == configuration["environment"],
        "observation scope differs from independently selected application/environment",
    )
    store.require(
        result["artifact"]["size"] <= configuration["max_artifact_bytes"], "artifact exceeds observer read bound"
    )
    # The common delivery schema has permissive historical text; reject control characters here.
    for value in (result["artifact"]["name"], result["source"]["repository"]["full_name"]):
        store.require(isinstance(value, str) and not re.search(r"[\x00-\x1f\x7f]", value), "invalid observed identity")
    return result


def _sample(configuration: Doc, deadline: store.Deadline) -> Doc:
    deadline.check()
    started, monotonic = _now(), time.monotonic_ns()
    result: Doc = {
        "started_at": started.isoformat(),
        "ended_at": started.isoformat(),
        "latency_ns": 0,
        "status": 0,
        "complete": False,
        "size": 0,
        "digest": None,
        "headers": dict.fromkeys(HEADERS),
        "failure": "transport",
    }
    try:
        url = urlsplit(configuration["url"])
        host, port = cast(str, url.hostname), cast(int, url.port)

        def remaining() -> float:
            deadline.check()
            sample_left = float(configuration["max_latency_ms"]) / 1000 - (time.monotonic_ns() - monotonic) / 1000000000
            store.require(sample_left > 0, "static response exceeded its sampling deadline")
            return min(deadline.remaining(), sample_left)

        with socket.create_connection((host, port), timeout=remaining()) as connection:
            connection.settimeout(remaining())
            host_header = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            request = (
                f"GET {url.path} HTTP/1.1\r\nHost: {host_header}\r\nConnection: close\r\n"
                "Accept-Encoding: identity\r\nCache-Control: no-cache\r\n\r\n"
            ).encode("ascii")
            connection.sendall(request)
            received = bytearray()
            while b"\r\n\r\n" not in received:
                connection.settimeout(remaining())
                block = connection.recv(4096)
                store.require(block, "static response ended before headers")
                received.extend(block)
                store.require(len(received) <= 65536, "static response headers exceed their bound")
            raw_headers, content = received.split(b"\r\n\r\n", 1)
            lines = raw_headers.decode("ascii").split("\r\n")
            store.require(
                re.fullmatch(r"HTTP/1\.[01] [1-5][0-9]{2}(?: [\x20-\x7e]*)?", lines[0]), "invalid static status"
            )
            result["status"] = int(lines[0].split(" ")[1])
            store.require(len(lines) <= 101, "too many static response headers")
            headers: dict[str, str] = {}
            for line in lines[1:]:
                name, separator, value = line.partition(":")
                store.require(
                    separator and re.fullmatch(r"[A-Za-z0-9-]+", name) and not re.search(r"[\x00-\x1f\x7f]", value),
                    "invalid static response header",
                )
                name = name.lower()
                store.require(name not in headers, "duplicate static response header")
                headers[name] = value.strip()
            result["headers"] = {key: headers.get("x-exacly-" + key.replace("_", "-")) for key in HEADERS}
            store.require(
                "transfer-encoding" not in headers
                and headers.get("content-encoding", "identity") == "identity"
                and re.fullmatch(r"0|[1-9][0-9]{0,8}", headers.get("content-length", "")),
                "static response requires one bounded identity Content-Length",
            )
            size = int(headers["content-length"])
            result["size"] = min(size, configuration["max_artifact_bytes"] + 1)
            if size > configuration["max_artifact_bytes"]:
                result["failure"] = "oversized"
            else:
                store.require(len(content) <= size, "static response body exceeds declared length")
                while len(content) < size:
                    connection.settimeout(remaining())
                    block = connection.recv(min(65536, size - len(content)))
                    store.require(block, "static response ended before declared body")
                    content.extend(block)
                remaining()
                result.update(complete=True, digest="sha256:" + hashlib.sha256(content).hexdigest(), failure=None)
    except (OSError, ValueError, store.StoreError, UnicodeError):
        # Retain a bounded diagnostic classification, never a remote body, exception or credentials.
        if result["status"]:
            result["failure"] = "protocol"
    finally:
        result["ended_at"] = _now().isoformat()
        result["latency_ns"] = time.monotonic_ns() - monotonic
    deadline.check()
    return result


def _unsigned(report: Doc) -> Doc:
    return {key: value for key, value in report.items() if key not in ("id", "signature")}


def observe(expected: Doc, configuration: Doc, *, deadline: store.Deadline) -> Doc:
    """Measure actual responses; signing failure never manufactures a trusted observation."""
    configuration = validate_configuration(configuration)
    expected = _expected(expected, configuration)
    selected_digest = configuration_digest(configuration)
    key_id, secret = _key(Path(configuration["key"]["path"]), configuration["key"]["id"], deadline)
    samples: list[Doc] = []
    for index in range(configuration["samples"]):
        if index:
            interval = configuration["interval_ms"] / 1000
            store.require(deadline.remaining() > interval, "observation window exceeds remaining deadline")
            time.sleep(interval)
        samples.append(_sample(configuration, deadline))
    end = _timestamp(samples[-1]["ended_at"])
    report: Doc = {
        "schema_version": VERSION,
        "profile": PROFILE,
        "kind": "operational-observation",
        "nonce": uuid.uuid4().hex,
        "key_id": key_id,
        "configuration_digest": selected_digest,
        "expected": expected,
        "window": {
            "started_at": samples[0]["started_at"],
            "ended_at": end.isoformat(),
            "expires_at": (end + dt.timedelta(milliseconds=configuration["max_age_ms"])).isoformat(),
            "required_samples": configuration["samples"],
        },
        "samples": samples,
    }
    content = store.canonical(report).encode()
    store.require(len(content) <= MAX_REPORT_BYTES, "operational observation report exceeds its bound")
    report["id"] = planning.digest(report)
    report["signature"] = hmac.new(secret, DOMAIN + content, hashlib.sha256).hexdigest()
    deadline.check()
    return _schema(report, REPORT_SCHEMA)


def _evaluate(
    report: Doc,
    expected: Doc,
    configuration: Doc,
    *,
    now: dt.datetime,
    consumed_report_ids: tuple[str, ...] | list[str] | set[str] = (),
    deadline: store.Deadline | None = None,
    require_current: bool,
) -> Doc:
    deadline = deadline or store.Deadline()
    deadline.check()
    configuration = validate_configuration(configuration)
    expected = _expected(expected, configuration)
    report = _schema(report, REPORT_SCHEMA)
    store.require(
        isinstance(now, dt.datetime) and now.tzinfo is not None and now.utcoffset() == dt.timedelta(0),
        "evaluation clock must be actual aware UTC",
    )
    store.require(
        report["expected"] == expected
        and report["configuration_digest"] == configuration_digest(configuration)
        and report["key_id"] == configuration["key"]["id"],
        "operational observation does not match selected rollout, environment, artifact or observer",
    )
    unsigned = _unsigned(report)
    store.require(report["id"] == planning.digest(unsigned), "operational observation content identity changed")
    store.require(report["id"] not in consumed_report_ids, "operational observation has already been consumed")
    _, secret = _key(Path(configuration["key"]["path"]), configuration["key"]["id"], deadline)
    signature = hmac.new(secret, DOMAIN + store.canonical(unsigned).encode(), hashlib.sha256).hexdigest()
    store.require(hmac.compare_digest(signature, report["signature"]), "operational observation signature is invalid")
    window = report["window"]
    start, end, expiry = (_timestamp(window[key]) for key in ("started_at", "ended_at", "expires_at"))
    samples = report["samples"]
    store.require(
        start <= end <= now
        and (not require_current or now < expiry)
        and expiry == end + dt.timedelta(milliseconds=configuration["max_age_ms"])
        and window["required_samples"] == configuration["samples"] == len(samples)
        and window["started_at"] == samples[0]["started_at"]
        and window["ended_at"] == samples[-1]["ended_at"],
        "operational observation window is stale, future or incomplete",
    )
    previous: dt.datetime | None = None
    unhealthy = False
    unknown = False
    for sample in samples:
        sample_start, sample_end = (_timestamp(sample[key]) for key in ("started_at", "ended_at"))
        store.require(start <= sample_start <= sample_end <= end, "sample lies outside declared observation window")
        if previous is not None:
            store.require(
                sample_start - previous >= dt.timedelta(milliseconds=configuration["interval_ms"]),
                "operational observation sampling window is incomplete",
            )
        previous = sample_end
        store.require(
            (sample["complete"] and sample["failure"] is None and sample["digest"] is not None)
            or (not sample["complete"] and sample["failure"] is not None and sample["digest"] is None),
            "operational sample completeness is inconsistent",
        )
        headers = sample["headers"]
        applicable = headers == {
            "generation": expected["generation"],
            "rollout_id": expected["rollout_id"],
            "effect_nonce": expected["effect_nonce"],
            "source_revision": expected["source"]["revision"],
        }
        if not sample["complete"]:
            unknown = True
        elif (
            sample["status"] != 200
            or sample["digest"] != expected["artifact"]["digest"]
            or sample["size"] != expected["artifact"]["size"]
            or sample["latency_ns"] > configuration["max_latency_ms"] * 1000000
            or not applicable
        ):
            unhealthy = True
    status = "unhealthy" if unhealthy else "unknown" if unknown else "healthy"
    deadline.check()
    return {
        "profile": PROFILE,
        "report_id": report["id"],
        "configuration_digest": report["configuration_digest"],
        "expected": copy.deepcopy(expected),
        "window": copy.deepcopy(window),
        "status": status,
        "scope": "current-static-readiness" if require_current else "historical-static-measurements",
        "current_readiness_established": require_current and status == "healthy",
        "reason": {
            "healthy": "Every measured sample served the approved static bytes and rollout within latency limits.",
            "unhealthy": "Observed static responses fail the approved byte, status, latency or rollout contract.",
            "unknown": "The required complete static response measurements are unavailable.",
        }[status],
        "generic_application_health": False,
        "deployment_authority": False,
    }


def authenticate(
    report: Doc,
    expected: Doc,
    configuration: Doc,
    *,
    now: dt.datetime,
    deadline: store.Deadline | None = None,
) -> Doc:
    """Authenticate retained measurements without granting current readiness or rollback rights.

    Expiry does not invalidate historical measurements. Future/incomplete windows, changed scope,
    signatures and malformed reports still fail closed. Call ``verify`` for current applicability.
    """
    return _evaluate(report, expected, configuration, now=now, deadline=deadline, require_current=False)


def verify(
    report: Doc,
    expected: Doc,
    configuration: Doc,
    *,
    now: dt.datetime,
    consumed_report_ids: tuple[str, ...] | list[str] | set[str] = (),
    deadline: store.Deadline | None = None,
) -> Doc:
    """Authenticate CURRENT applicable measurements; consumption remains the owner's CAS job."""
    return _evaluate(
        report,
        expected,
        configuration,
        now=now,
        consumed_report_ids=consumed_report_ids,
        deadline=deadline,
        require_current=True,
    )


def current_status(
    report: Doc,
    expected: Doc,
    configuration: Doc,
    *,
    now: dt.datetime | None = None,
    deadline: store.Deadline | None = None,
) -> Doc:
    """Derive readiness after authentication; explicit ``now`` selects an as-of evaluation.

    The default captures actual UTC after all fallible authentication/profile work and timestamp
    parsing. Authentication time cannot extend a live report's freshness window.
    """
    measured = authenticate(report, expected, configuration, now=_now() if now is None else now, deadline=deadline)
    ended_at = _timestamp(measured["window"]["ended_at"])
    expiry = _timestamp(measured["window"]["expires_at"])
    evaluated_at = _now() if now is None else now
    store.require(ended_at <= evaluated_at, "operational observation window is future at the freshness decision")
    current = evaluated_at < expiry
    return {
        "profile": PROFILE,
        "report_id": measured["report_id"],
        "expected": measured["expected"],
        "window": measured["window"],
        "historical_status": measured["status"],
        "status": measured["status"] if current else "unknown",
        "freshness": "current" if current else "stale",
        "evaluated_at": evaluated_at.isoformat(),
        "current_readiness_established": current and measured["status"] == "healthy",
        "reason": measured["reason"]
        if current
        else "The retained measurements have expired; current readiness is unestablished.",
        "generic_application_health": False,
        "deployment_authority": False,
    }
