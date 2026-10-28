"""Explicit operator registration of task/action/output associations.

The ledger is a controlled local store, not an authentication format for imported JSON. Its caller and
directory must be protected from candidate code; ownership checks do not isolate processes with one UID.
"""

from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from exacly import admission, execution_store, planning, policy
from exacly.applicability import TARGET_SCHEMA, TEXT, Target
from exacly.core import minor_version, schema_for

Doc = dict[str, Any]
VERSION = "0.1.0"
IDENTITY_KEYS = ("namespace", "context", "lineage", "record", "task", "opened_at")
POLICY_ID = planning.fields({"name": TEXT, "version": TEXT, "digest": planning.DIGEST})
PENDING_SCHEMA = planning.fields(
    {
        **{key: TEXT for key in IDENTITY_KEYS},
        "declared_dimension": {"enum": [None, "verification"]},
        "entry_digest": planning.DIGEST,
    }
)
CLAIMS = {
    "operator_registered": True,
    "task_completion_authorized": False,
    "executor_output_link_verified": False,
    "self_authenticating": False,
}
SCHEMA: Doc = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Exacly operator-registered execution association 0.1.0",
    **planning.fields(
        {
            "schema_version": {"const": VERSION},
            "id": planning.DIGEST,
            "pending_key": planning.DIGEST,
            "contract": planning.fields(
                {
                    "pending": PENDING_SCHEMA,
                    "plan": planning.fields({"digest": planning.DIGEST, "action_id": planning.DIGEST}),
                    "goal": planning.fields(
                        {
                            "record": TEXT,
                            "dimension": {"const": "verification"},
                            "criteria_digest": planning.DIGEST,
                            "bindings_digest": planning.DIGEST,
                        }
                    ),
                    "original_target": TARGET_SCHEMA,
                    "target": TARGET_SCHEMA,
                    "policy": POLICY_ID,
                    "trust_policy": POLICY_ID,
                    "adopted_verification": {"type": "boolean"},
                }
            ),
            "state_snapshot": planning.fields(
                {"serial": {"type": "integer", "minimum": 1}, "digest": planning.DIGEST, "config_revision": TEXT}
            ),
            "registered_at": {"type": "string", "format": "date-time"},
            "as_of": {"type": "string", "format": "date"},
            "supersedes": {"anyOf": [planning.DIGEST, {"type": "null"}]},
            "claims": planning.fields({key: {"const": value} for key, value in CLAIMS.items()}),
        }
    ),
}


@dataclass(frozen=True)
class Registration:
    plan: Any
    action_id: str
    state: Any
    task: str
    opened_at: str
    readiness_policy: Any
    trust_policy: Any
    target: Target
    date: dt.date
    adopt_verification: bool = False
    supersedes: str | None = None


def timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError("pending identity requires an ISO timestamp with a timezone")
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("pending identity requires an ISO timestamp with a timezone") from None
    if result.utcoffset() is None:
        raise ValueError("pending identity requires a timezone")
    return result.astimezone(dt.UTC)


def state_document(value: Any, date: dt.date) -> Doc:
    try:
        if not isinstance(value, dict) or minor_version(value.get("exacly")) != "0.2":
            raise ValueError("execution registration requires a 0.2 state document")
        jsonschema.validate(value, schema_for(value["exacly"], "state"), format_checker=jsonschema.FormatChecker())
        state = planning.plain(value)
    except jsonschema.ValidationError as error:
        raise ValueError(f"invalid execution state: {error.message}") from error
    except RecursionError:
        raise ValueError("invalid execution state: recursive or excessively nested content") from None
    if timestamp(state["written_at"]).date() > date:
        raise ValueError("state was written after the evaluation date")
    return dict(state)


def descriptor(value: Doc) -> Doc:
    return {"name": value["name"], "version": value["version"], "digest": planning.digest(value)}


def contract_inputs(request: Registration, *, check_state_identity: bool = True) -> tuple[Doc, Doc, Doc, Doc, Doc]:
    from exacly.reconciliation import saved_action, source_target

    if type(request.date) is not dt.date or type(request.adopt_verification) is not bool:
        raise ValueError("registration requires an explicit date and boolean legacy adoption")
    if not isinstance(request.target, Target):
        raise ValueError("registration requires an explicit output target")
    plan, action = saved_action(request.plan, request.action_id, request.date)
    target = source_target(request.target)
    chosen = admission.validate_policy(request.trust_policy)
    try:
        jsonschema.validate(request.readiness_policy, policy.SCHEMA)
    except jsonschema.ValidationError as error:
        raise ValueError(f"invalid readiness policy: {error.message}") from error
    if any(entry["dimensions"] != ["verification"] for entry in request.readiness_policy["requirements"]):
        raise ValueError("execution registration supports verification-only readiness policies")
    if descriptor(request.readiness_policy) != plan["policy"]:
        raise ValueError("readiness policy differs from the saved plan; replan before registration")
    if any(target[key] != plan["target"][key] for key in ("model_revision", "context")):
        raise ValueError("registered output must retain the planned model revision and context")
    if chosen["model"]["revision"] != target["model_revision"]:
        raise ValueError("trust policy model revision differs from the registered target")
    if (
        sum(
            entry["record"].removeprefix(plan["namespace"] + ":") == action["record"]
            for entry in request.readiness_policy["requirements"]
        )
        != 1
    ):
        raise ValueError("action must identify one unique readiness prerequisite")
    state = state_document(request.state, request.date)
    if check_state_identity and any(
        state[key] != expected
        for key, expected in (
            ("namespace", plan["namespace"]),
            ("context", target["context"]),
            ("config_revision", target["model_revision"]),
        )
    ):
        raise ValueError("state namespace, context or model revision differs from the planned contract")
    return plan, action, target, chosen, state


def selected_pending(state: Doc, record: str, task: str, opened_at: str, adopt: bool) -> tuple[Doc, Doc]:
    if not isinstance(task, str) or not task.strip():
        raise ValueError("an explicit pending task identifier is required")
    opened = timestamp(opened_at)
    matches = [
        entry
        for entry in state.get("pending", [])
        if entry["record"].removeprefix(state["namespace"] + ":") == record
        and entry["task"] == task
        and timestamp(entry["opened_at"]) == opened
    ]
    if len(matches) != 1:
        raise ValueError("select exactly one unambiguous pending item by record, task and opened_at")
    entry = matches[0]
    dimension = entry.get("dimension")
    if dimension not in (None, "verification"):
        raise ValueError("pending item is not verification work")
    if dimension is None and not adopt:
        raise ValueError("legacy pending work requires explicit verification adoption")
    if opened > timestamp(state["written_at"]):
        raise ValueError("pending item was opened after the state snapshot")
    pending = {
        **{key: state[key] for key in ("namespace", "context", "lineage")},
        "record": record,
        "task": task,
        "opened_at": opened.isoformat(),
        "declared_dimension": dimension,
        "entry_digest": planning.digest(entry),
    }
    return pending, entry


def location(ledger: Path, chosen: Doc) -> None:
    try:
        destinations = (ledger.resolve(), ledger.parent.resolve() / ledger.name)
        folders = [Path(name).parent.resolve() for name in chosen["model"]["files"]]
    except RuntimeError:
        raise ValueError("ledger or declared model paths cannot be resolved") from None
    if any(path.is_relative_to(folder) for path in destinations for folder in folders):
        raise ValueError("execution ledger must be outside declared model folders")


def record_identity(record: Doc) -> str:
    return planning.digest({key: record[key] for key in ("schema_version", "contract", "supersedes")})


def validate_record(record: Any) -> Doc:
    try:
        jsonschema.validate(record, SCHEMA, format_checker=jsonschema.FormatChecker())
        normalized = planning.plain(record)
    except jsonschema.ValidationError as error:
        raise ValueError(f"invalid stored execution binding: {error.message}") from error
    except RecursionError:
        raise ValueError("invalid stored execution binding nesting") from None
    pending = normalized["contract"]["pending"]
    if (
        record_identity(normalized) != normalized["id"]
        or planning.digest({key: pending[key] for key in IDENTITY_KEYS}) != normalized["pending_key"]
    ):
        raise ValueError("stored execution binding identity does not match its content")
    if normalized["contract"]["adopted_verification"] != (pending["declared_dimension"] is None):
        raise ValueError("stored execution binding has inconsistent verification adoption")
    return dict(normalized)


def register(ledger: Path, request: Registration, *, lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Record an explicit operator association, without observing evidence or changing pending work."""
    plan, action, target, chosen, state = contract_inputs(request)
    location(ledger, chosen)
    pending, _ = selected_pending(state, action["record"], request.task, request.opened_at, request.adopt_verification)
    record: Doc = {
        "schema_version": VERSION,
        "pending_key": planning.digest({key: pending[key] for key in IDENTITY_KEYS}),
        "contract": {
            "pending": pending,
            "plan": {"digest": plan["digest"], "action_id": action["id"]},
            "goal": {key: action[key] for key in ("record", "dimension", "criteria_digest", "bindings_digest")},
            "original_target": plan["target"],
            "target": target,
            "policy": descriptor(request.readiness_policy),
            "trust_policy": descriptor(chosen),
            "adopted_verification": pending["declared_dimension"] is None,
        },
        "state_snapshot": {
            "serial": state["serial"],
            "digest": planning.digest(state),
            "config_revision": state["config_revision"],
        },
        "registered_at": dt.datetime.now(dt.UTC).isoformat(),
        "as_of": request.date.isoformat(),
        "supersedes": request.supersedes,
        "claims": copy.deepcopy(CLAIMS),
    }
    record["id"] = record_identity(record)
    validate_record(record)
    return validate_record(execution_store.put(ledger, record, lock_timeout=lock_timeout))


def get(ledger: Path, binding_id: str, *, lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT) -> Doc:
    """Inspect an existing protected ledger; exported records cannot substitute for this read."""
    envelope = execution_store.get(ledger, binding_id, lock_timeout=lock_timeout)
    envelope["binding"] = validate_record(envelope["binding"])
    return envelope


def match(
    ledger: Path,
    binding_id: str,
    *,
    plan_doc: Any,
    action_id: str,
    state_doc: Any,
    readiness_policy: Any,
    trust_policy: Any,
    output_target: Target,
    date: dt.date,
    lock_timeout: float = execution_store.DEFAULT_LOCK_TIMEOUT,
) -> tuple[bool, str, Doc]:
    """Check a registered association against a current pending item without modifying the ledger."""
    envelope = get(ledger, binding_id, lock_timeout=lock_timeout)
    binding = envelope["binding"]
    contract = binding["contract"]
    pending = contract["pending"]
    request = Registration(
        plan_doc,
        action_id,
        state_doc,
        pending["task"],
        pending["opened_at"],
        readiness_policy,
        trust_policy,
        output_target,
        date,
        contract["adopted_verification"],
    )
    if not envelope["active"]:
        return False, "The execution binding was superseded; select its active replacement.", envelope
    plan, action, target, chosen, state = contract_inputs(request, check_state_identity=False)
    location(ledger, chosen)
    expected = {
        "plan": {"digest": plan["digest"], "action_id": action["id"]},
        "goal": {key: action[key] for key in ("record", "dimension", "criteria_digest", "bindings_digest")},
        "original_target": plan["target"],
        "target": target,
        "policy": descriptor(request.readiness_policy),
        "trust_policy": descriptor(chosen),
    }
    if any(contract[key] != value for key, value in expected.items()):
        return False, "Registered plan, action, policies or output association differs from this preview.", envelope
    if (
        any(state[key] != pending[key] for key in ("namespace", "context", "lineage"))
        or state["config_revision"] != target["model_revision"]
    ):
        return False, "Current state context or lineage differs from the registered item.", envelope
    try:
        selected, _ = selected_pending(
            state, pending["record"], pending["task"], pending["opened_at"], contract["adopted_verification"]
        )
    except ValueError as error:
        return False, str(error), envelope
    if selected != pending:
        return False, "The registered pending item's content changed; register an explicit replacement.", envelope
    watermark = envelope["state_watermark"]
    if state["serial"] < watermark["serial"] or (
        state["serial"] == watermark["serial"] and planning.digest(state) != watermark["digest"]
    ):
        return False, "Current state rolled back or changed at an already registered serial.", envelope
    return (
        True,
        "Active operator registration and the current pending item match; evidence still requires live admission.",
        envelope,
    )


def as_text(value: Doc) -> str:
    record = value.get("binding", value)
    pending = record["contract"]["pending"]
    lines = [
        f"Execution binding: {record['id']}",
        f"Task: {pending['task']} ({pending['record']})",
        f"Action: {record['contract']['plan']['action_id']}",
        f"Output target: {planning.canonical(record['contract']['target'])}",
    ]
    if "active" in value:
        lines.append(f"Active: {value['active']}")
        if value["superseded_by"]:
            lines.append(f"Superseded by: {value['superseded_by']}")
    lines.append("Operator association only; no task completion authority or machine-verified executor provenance.")
    return "\n".join(lines)
