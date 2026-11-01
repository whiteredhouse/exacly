"""Closed, independently reviewed inputs for the first bounded local executor."""

from __future__ import annotations

import copy
import datetime as dt
import math
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import jsonschema

from exacly import execution_store, isolation, planning, resource_guard
from exacly.applicability import Target
from exacly.executor_interfaces import LOCAL_ADAPTER, IdentityValidator

Doc = dict[str, Any]
VERSION = "0.1.0"
ADAPTER = LOCAL_ADAPTER
TASK_INPUT = ".exacly-task.json"
TEXT: Doc = {"type": "string", "minLength": 1}
STORAGE = planning.fields(
    {
        **{name: TEXT for name in ("journal", "ledger", "state", "output_root")},
        "journal_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    }
)
POLICY_SCHEMA = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "name": TEXT,
        "version": TEXT,
        "adapter": {"const": ADAPTER},
        "storage": STORAGE,
        "runtime": {"type": "object"},
        "argv": {"type": "array", "minItems": 1, "maxItems": 1024, "items": {"type": "string"}},
        "expected_outputs": {"type": "array", "minItems": 1, "maxItems": 1024, "uniqueItems": True, "items": TEXT},
        "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600},
        "permitted_repository_effects": {"const": []},
        "resource_guard": {"type": "object"},
    },
    optional=("resource_guard",),
)
CLAIMS = {
    "outputs_trusted": False,
    "evidence_admitted": False,
    "verification_established": False,
    "task_completion_authorized": False,
    "executor_output_link_verified": False,
    "self_authenticating": False,
}


@dataclass(frozen=True)
class Request:
    plan: Any
    action_id: str
    readiness_policy: Any
    trust_policy: Any
    executor_policy: Any
    target: Target
    date: dt.date
    binding_id: str
    inputs: Mapping[str, bytes]


def require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def relative(value: Any) -> str:
    require(
        isinstance(value, str) and value and "\0" not in value and "\\" not in value,
        "invalid executor snapshot or artifact path",
    )
    path = PurePosixPath(value)
    require(
        bool(path.parts)
        and not path.is_absolute()
        and path.as_posix() == value
        and all(part not in (".", "..") for part in path.parts)
        and len(value.encode()) <= 4096,
        "executor paths must be normalized relative paths",
    )
    return str(value)


def canonical_path(value: Any) -> Path:
    require(isinstance(value, str) and "\0" not in value, "executor storage paths must be strings")
    path = Path(value)
    require(
        path.is_absolute() and str(path) == value and path == path.resolve(),
        "executor storage requires canonical absolute paths without symbolic links",
    )
    return path


def validate_policy(value: Any, *, check_storage: bool = True) -> Doc:
    try:
        chosen = planning.plain(value)
        jsonschema.validate(chosen, POLICY_SCHEMA)
        chosen = copy.deepcopy(chosen)
    except (jsonschema.ValidationError, RecursionError, TypeError) as error:
        raise ValueError(f"invalid executor policy: {error}") from error
    isolation.parse_policy(chosen["runtime"])
    if "resource_guard" in chosen:
        resource_guard.from_descriptor(chosen["resource_guard"])
    require(type(chosen["max_attempts"]) is int, "executor attempt limit must be an integer")
    require(
        type(chosen["timeout_seconds"]) in (int, float) and math.isfinite(chosen["timeout_seconds"]),
        "executor timeout must be finite",
    )
    require(
        chosen["argv"][0]
        and all("\0" not in item for item in chosen["argv"])
        and sum(len(item.encode()) for item in chosen["argv"]) <= 128 * 1024,
        "invalid executor command vector",
    )
    chosen["expected_outputs"] = [relative(name) for name in chosen["expected_outputs"]]
    require(chosen["expected_outputs"] == sorted(chosen["expected_outputs"]), "expected output paths must be sorted")
    paths: dict[str, Path] = {}
    for name in ("journal", "ledger", "state", "output_root"):
        selected = chosen["storage"][name]
        path = Path(selected)
        require(
            path.is_absolute() and str(path) == selected and ".." not in path.parts and "\0" not in selected,
            "executor storage requires normalized absolute paths",
        )
        paths[name] = canonical_path(selected) if check_storage else path
    protected = {
        str(paths[name]) + suffix for name in ("journal", "ledger") for suffix in ("", "-journal", "-wal", "-shm")
    }
    require(
        len(protected) == 8 and str(paths["state"]) not in protected,
        "executor journal, binding ledger and state paths overlap",
    )
    output = paths["output_root"]
    runtime_root = Path(chosen["runtime"]["rootfs"])
    require(
        not any(
            path == runtime_root
            or runtime_root in path.parents
            or (name == "output_root" and path in runtime_root.parents)
            for name, path in paths.items()
        ),
        "executor storage overlaps the candidate runtime root",
    )
    require(
        not any(
            path == output or output in path.parents or path in output.parents
            for name, path in paths.items()
            if name != "output_root"
        ),
        "executor output root overlaps authoritative storage",
    )
    if check_storage:
        execution_store.location(paths["journal"])
        execution_store.location(paths["ledger"])
        execution_store.location(output / ".executor-output")
        info = output.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
            "executor output root must be an owner-private 0700 directory",
        )
    return dict(chosen)


def snapshots(value: Mapping[str, bytes]) -> dict[str, bytes]:
    require(
        isinstance(value, Mapping) and len(value) < isolation.MAX_INPUT_FILES,
        "executor input snapshots exceed the file limit",
    )
    result = {relative(name): data for name, data in value.items()}
    require(
        TASK_INPUT not in result and all(isinstance(data, bytes) for data in result.values()),
        "executor snapshots require bytes and cannot replace the approved task input",
    )
    require(
        not any(PurePosixPath(TASK_INPUT) in PurePosixPath(name).parents for name in result),
        "executor snapshots overlap the approved task input",
    )
    require(
        sum(len(data) for data in result.values()) <= isolation.MAX_INPUT_BYTES,
        "executor input snapshot bytes exceed the limit",
    )
    for name in result:
        require(
            not any(parent.as_posix() in result for parent in PurePosixPath(name).parents),
            "executor input file conflicts with a directory",
        )
    return result


def roster(inputs: Mapping[str, bytes]) -> list[Doc]:
    import hashlib

    return [
        {"path": name, "size": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
        for name, data in sorted(inputs.items())
    ]


def output_path(chosen: Doc, execution_id: str, attempt: int) -> Path:
    return Path(chosen["storage"]["output_root"]) / execution_id.removeprefix("sha256:") / f"attempt-{attempt}"


def _digest(value: Any) -> bool:
    return isinstance(value, str) and execution_store.DIGEST.fullmatch(value) is not None


def artifact_roster(value: Any, *, snapshots_only: bool = False) -> list[Doc]:
    limit = isolation.MAX_INPUT_FILES if snapshots_only else isolation.MAX_ARTIFACT_FILES
    byte_limit = isolation.MAX_INPUT_BYTES if snapshots_only else isolation.MAX_ARTIFACT_BYTES
    require(isinstance(value, list) and len(value) <= limit, "invalid retained executor file roster")
    names: list[str] = []
    total = 0
    for item in value:
        require(
            isinstance(item, dict) and set(item) == {"path", "size", "digest"},
            "invalid retained executor file identity",
        )
        name = relative(item["path"])
        require(snapshots_only or name.startswith("artifacts/"), "retained build path is outside artifacts")
        require(
            type(item["size"]) is int and item["size"] >= 0 and _digest(item["digest"]),
            "invalid retained executor file size or digest",
        )
        names.append(name)
        total += item["size"]
    require(len(names) == len(set(names)) and total <= byte_limit, "retained executor file roster exceeds limits")
    return list(value)


def validate_history(
    work: Doc,
    *,
    journal_id: str | None = None,
    ledger: str | None = None,
    max_concurrency: int | None = None,
    owner_validator: IdentityValidator | None = None,
    worker_validator: IdentityValidator | None = None,
) -> Doc:
    """Independently check retained authorization, process facts and terminal receipt semantics.

    Hash chains establish exact retained bytes; these checks establish that those bytes still mean
    the approved action and cannot introduce another task, output identity or completion authority.
    Historical checks use no current model, state or output files so exact replay remains immutable.
    """
    from exacly import executions, query

    if owner_validator is None or worker_validator is None:
        from exacly.executor_runtime import validate_identity

        owner_validator = owner_validator or validate_identity
        worker_validator = worker_validator or validate_identity
    from exacly.core import bindings

    try:
        intent = work["intent"]
        contract = intent["contract"]
        require(
            set(contract)
            == {
                "policy",
                "binding",
                "plan",
                "action",
                "config_digest",
                "readiness_policy",
                "trust_policy",
                "target",
                "task",
                "inputs",
            },
            "invalid executor contract fields",
        )
        chosen = validate_policy(contract["policy"], check_storage=False)
        storage = chosen["storage"]
        require(
            (journal_id is None or journal_id == storage["journal_id"])
            and (ledger is None or ledger == storage["ledger"]),
            "retained executor contract belongs to another storage instance",
        )
        binding = executions.validate_record(contract["binding"])
        action = contract["action"]
        identity = {
            "schema_version": VERSION,
            "adapter": ADAPTER,
            "pending_key": binding["pending_key"],
            "action_id": binding["contract"]["plan"]["action_id"],
        }
        require(
            intent["identity"] == identity
            and intent["id"] == planning.digest(identity)
            and intent["max_attempts"] == chosen["max_attempts"],
            "retained executor work identity changed",
        )
        require(
            action["id"] == identity["action_id"]
            and contract["plan"] == binding["contract"]["plan"]["digest"]
            and {key: action[key] for key in ("record", "dimension", "criteria_digest", "bindings_digest")}
            == binding["contract"]["goal"]
            and all(contract[key] == binding["contract"][key] for key in ("target", "trust_policy"))
            and contract["readiness_policy"] == binding["contract"]["policy"]
            and _digest(contract["config_digest"]),
            "retained executor action or binding contract changed",
        )
        task = contract["task"]
        require(
            set(task)
            == {
                "schema_version",
                "task",
                "binding_id",
                "criteria_digest",
                "bindings_digest",
                "requirements",
                "input_implementation",
                "dependencies",
                "expected_observations",
            }
            and task["schema_version"] == VERSION
            and task["binding_id"] == binding["id"]
            and task["task"] == binding["contract"]["pending"]["task"]
            and task["criteria_digest"] == action["criteria_digest"]
            and task["bindings_digest"] == action["bindings_digest"]
            and task["input_implementation"] == contract["target"]
            and isinstance(task["requirements"], list)
            and len(task["requirements"]) == 1,
            "retained executor task metadata changed",
        )
        requirement = task["requirements"][0]
        require(
            requirement["id"] == action["record"]
            and requirement["criteria_digest"] == action["criteria_digest"]
            and requirement["bindings_digest"] == action["bindings_digest"]
            and planning.digest(
                {key: requirement[key] for key in ("statement", "acceptance_criteria", "verification_method")}
            )
            == action["criteria_digest"],
            "retained executor criteria changed",
        )
        observed = {"observe": task["expected_observations"]}
        require(
            planning.digest(
                {source: bindings(observed, source) for source in sorted(query.bound(observed, "verification"))}
            )
            == action["bindings_digest"],
            "retained executor observation bindings changed",
        )
        inputs = artifact_roster(contract["inputs"], snapshots_only=True)
        encoded_task = (planning.canonical(task) + "\n").encode()
        require(
            [item for item in inputs if item["path"] == TASK_INPUT] == roster({TASK_INPUT: encoded_task})
            and [item for item in inputs if item["path"] != TASK_INPUT] == task["dependencies"],
            "retained executor task snapshot or dependencies changed",
        )
        previous: str | None = None
        for attempt in work["attempts"]:
            reservation = attempt["events"][0]["data"]
            require(
                set(reservation) == {"kind", "authorization", "owner"} and reservation["kind"] == "reservation",
                "invalid executor reservation",
            )
            owner_validator(reservation["owner"])
            authorization = reservation["authorization"]
            require(
                isinstance(authorization, dict) and _digest(authorization.get("state_digest")),
                "invalid retained executor authorization",
            )
            concurrency = authorization.get("max_concurrency")
            require(
                type(concurrency) is int
                and 1 <= concurrency <= 16
                and (max_concurrency is None or concurrency == max_concurrency),
                "retained executor concurrency authorization changed",
            )
            max_concurrency = concurrency
            evaluation_date = dt.date.fromisoformat(authorization["evaluation_date"])
            require(
                evaluation_date.isoformat() == authorization["evaluation_date"], "invalid executor authorization date"
            )
            require(
                authorization
                == {
                    "schema_version": VERSION,
                    "execution_id": intent["id"],
                    "attempt": attempt["number"],
                    "effect": "execute_local_snapshot",
                    "storage": storage,
                    "max_concurrency": concurrency,
                    "contract_digest": planning.digest(contract),
                    "history_head": previous,
                    "state_digest": authorization["state_digest"],
                    "evaluation_date": evaluation_date.isoformat(),
                    "output": str(output_path(chosen, intent["id"], attempt["number"])),
                },
                "retained executor authorization differs from its exact attempt",
            )
            processes: list[Doc] = []
            kinds: set[str] = set()
            cancelled = False
            for event in attempt["events"][1:]:
                data = event["data"]
                kind = data.get("kind")
                if kind in ("worker", "namespace"):
                    require(
                        set(data) == {"kind", "identity"}
                        and kind not in kinds
                        and (kind != "namespace" or "worker" in kinds),
                        "executor process identity order changed",
                    )
                    processes.append(worker_validator(data["identity"]))
                    kinds.add(kind)
                elif kind == "cancellation_requested":
                    require(set(data) == {"kind"} and not cancelled, "executor cancellation history changed")
                    cancelled = True
                else:
                    require(
                        kind == "receipt"
                        and event is attempt["events"][-1]
                        and attempt["phase"]
                        in {
                            "succeeded",
                            "failed",
                            "timed_out",
                            "output_limit",
                            "cancelled",
                            "interrupted",
                            "output_invalid",
                        },
                        "invalid executor event semantics",
                    )
                    receipt = data["receipt"]
                    require(
                        set(data) == {"kind", "receipt"}
                        and set(receipt)
                        == {
                            "schema_version",
                            "execution_id",
                            "attempt",
                            "authorization",
                            "task",
                            "binding_id",
                            "outcome",
                            "output",
                            "build",
                            "isolation_report_digest",
                            "termination",
                            "reason",
                            "claims",
                            "digest",
                        },
                        "invalid executor receipt fields",
                    )
                    require(
                        receipt["schema_version"] == VERSION
                        and receipt["execution_id"] == intent["id"]
                        and receipt["attempt"] == attempt["number"]
                        and receipt["authorization"] == authorization
                        and receipt["task"] == task["task"]
                        and receipt["binding_id"] == binding["id"]
                        and receipt["outcome"] == attempt["phase"]
                        and receipt["output"] == authorization["output"]
                        and receipt["claims"] == CLAIMS
                        and isinstance(receipt["reason"], str)
                        and (not cancelled or receipt["outcome"] == "cancelled")
                        and receipt["digest"]
                        == planning.digest({key: value for key, value in receipt.items() if key != "digest"}),
                        "retained executor receipt scope or authority changed",
                    )
                    proofs = receipt["termination"]
                    if isinstance(proofs, dict):
                        require(set(proofs) == {"worker", "namespace"}, "invalid executor termination proof fields")
                        proofs = [proof for proof in proofs.values() if proof is not None]
                    require(isinstance(proofs, list) and len(proofs) <= 2, "invalid executor termination proofs")
                    verified: list[Doc] = []
                    for proof in proofs:
                        require(
                            set(proof) == {"identity", "terminated", "reason"}
                            and proof["terminated"] is True
                            and proof["reason"]
                            in {"terminated", "already_exited", "absent", "pid_reused", "previous_boot"},
                            "invalid exact-process termination proof",
                        )
                        verified.append(worker_validator(proof["identity"]))
                    require(
                        all(process in verified for process in processes),
                        "executor receipt omits retained process termination",
                    )
                    build = receipt["build"]
                    if receipt["outcome"] == "succeeded":
                        require(
                            kinds == {"worker", "namespace"}
                            and isinstance(build, dict)
                            and set(build) == {"kind", "artifacts", "id"}
                            and build["kind"] == "build"
                            and _digest(receipt["isolation_report_digest"]),
                            "successful executor receipt has no isolated build identity",
                        )
                        artifacts = artifact_roster(build["artifacts"])
                        require(
                            build["id"] == planning.digest({"schema_version": VERSION, "artifacts": artifacts})
                            and all(
                                "artifacts/" + name in {item["path"] for item in artifacts}
                                for name in chosen["expected_outputs"]
                            ),
                            "retained executor build identity changed",
                        )
                    else:
                        require(build is None, "unsuccessful executor receipt cannot introduce a produced build")
                    require(
                        receipt["isolation_report_digest"] is None or _digest(receipt["isolation_report_digest"]),
                        "invalid retained isolation report identity",
                    )
            previous = attempt["digest"]
    except (KeyError, TypeError, AttributeError, IndexError, RecursionError) as error:
        raise ValueError("invalid retained executor history") from error
    return work
