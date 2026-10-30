"""Read-only completion previews backed by fresh GitHub evidence admission.

Saved plans and preview reports are advisory artifacts, not execution authority. The caller selects
the resulting implementation; this establishes applicability, not an executor-to-output association.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from importlib import metadata
from pathlib import Path
from typing import Any

import jsonschema

from exacly import admission, planning, policy, query
from exacly.applicability import TARGET_SCHEMA, TEXT, Target
from exacly.core import is_state
from exacly.github_evidence import GitHubError

Doc = dict[str, Any]
VERSION = "0.2.0"
POLICY_ID = planning.fields({"name": TEXT, "version": TEXT, "digest": planning.DIGEST})
CLAIMS = {
    "registered_binding_verified": False,
    "task_completion_authorized": False,
    "executor_output_link_verified": False,
    "self_authenticating": False,
    "test_honesty_verified": False,
    "coverage_verified": False,
}
SCHEMA: Doc = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Exacly advisory completion preview 0.2.0",
    **planning.fields(
        {
            "schema_version": {"const": VERSION},
            "advisory": {"const": True},
            "dry_run": {"const": True},
            "tool": planning.fields({"name": {"const": "exacly"}, "version": {"type": "string"}}),
            "status": {"enum": ["ready", "pending", "stale"]},
            "as_of": {"type": "string", "format": "date"},
            "plan": planning.fields({"digest": planning.DIGEST, "action_id": planning.DIGEST}),
            "original_target": TARGET_SCHEMA,
            "target": TARGET_SCHEMA,
            "goal": planning.fields(
                {
                    "record": {"type": "string"},
                    "dimension": {"const": "verification"},
                    "criteria_digest": planning.DIGEST,
                    "bindings_digest": planning.DIGEST,
                }
            ),
            "policy": POLICY_ID,
            "trust_policy": POLICY_ID,
            "claims": planning.fields(
                {
                    key: ({"type": "boolean"} if key == "registered_binding_verified" else {"const": value})
                    for key, value in CLAIMS.items()
                }
            ),
            "checks": {
                "type": "array",
                "items": planning.fields(
                    {"code": {"type": "string"}, "passed": {"type": "boolean"}, "reason": {"type": "string"}}
                ),
            },
            "admission": {"type": ["object", "null"]},
            "binding": {"type": ["object", "null"]},
            "readiness": {"type": ["object", "null"]},
            "digest": planning.DIGEST,
        }
    ),
}


def source_target(target: Target) -> Doc:
    selected = copy.deepcopy(target.data)
    if set(selected) != {"context", "model_revision", "revision"} or not all(
        re.fullmatch(r"[0-9a-f]{40}", str(selected[key])) for key in ("model_revision", "revision")
    ):
        raise ValueError(
            "reconciliation supports source verification targets with full model and implementation revisions"
        )
    return selected


def saved_action(plan_doc: Any, action_id: str, date: dt.date) -> tuple[Doc, Doc]:
    """Check artifact integrity and the selected action/check/completion chain, without granting trust."""
    try:
        plan = planning.plain(plan_doc)
        jsonschema.validate(plan, planning.SCHEMA, format_checker=jsonschema.FormatChecker())
    except RecursionError:
        raise ValueError("invalid saved plan: recursive or excessively nested content") from None
    except jsonschema.ValidationError as error:
        raise ValueError(f"invalid saved plan: {error.message}") from error
    payload = {key: value for key, value in plan.items() if key != "digest"}
    if planning.digest(payload) != plan["digest"]:
        raise ValueError("saved plan digest does not match its content")
    if date < dt.date.fromisoformat(plan["as_of"]):
        raise ValueError("evaluation date cannot precede the saved plan")
    if len({action["id"] for action in plan["actions"]}) != len(plan["actions"]):
        raise ValueError("saved plan action identities are ambiguous")
    matches = [action for action in plan["actions"] if action["id"] == action_id]
    if len(matches) != 1:
        raise ValueError("select exactly one existing saved plan action")
    action = matches[0]
    original = source_target(Target(plan["target"]))
    if plan["status"] != "work_proposed" or plan["inputs"]["config"]["revision"] != original["model_revision"]:
        raise ValueError("saved plan status or model identity contradicts its action")
    checks = [check for check in plan["checks"] if check.get("action_id") == action_id]
    goals = [
        check
        for check in plan["checks"]
        if check["record"] == action["record"] and check["dimension"] == action["dimension"]
    ]
    if len(checks) != 1 or len(goals) != 1:
        raise ValueError("saved action must have exactly one linked verification check")
    check = checks[0]
    expected_result = "not_satisfied" if action["kind"] == "investigate_failure" else "unknown"
    if (
        check["record"] != action["record"]
        or check["dimension"] != action["dimension"]
        or check["classification"] != action["kind"]
        or check["result"] != expected_result
    ):
        raise ValueError("saved action contradicts its linked verification check")
    completion = {
        "record": action["record"],
        "dimension": "verification",
        "required_state": "passed",
        "target": original,
        "criteria_digest": action["criteria_digest"],
        "bindings_digest": action["bindings_digest"],
        "requires_trusted_observation": True,
    }
    if action["completion"] != completion:
        raise ValueError("saved action completion condition is inconsistent")
    identity = {
        "schema_version": plan["schema_version"],
        "namespace": plan["namespace"],
        "target": original,
        "policy": plan["policy"],
        **{key: action[key] for key in ("record", "dimension", "kind", "criteria_digest", "bindings_digest")},
    }
    if planning.digest(identity) != action_id:
        raise ValueError("saved action identity does not match its approved goal")
    return plan, action


def approved_model(documents: Doc, plan: Doc, action: Doc, target: Doc, date: dt.date) -> query.Model:
    """Recheck the saved goal against the approved config without granting execution authority."""
    model = query.load(documents, date, revision=target["model_revision"])
    if model.report.errors:
        raise query.QueryError(f"approved model does not conform: {model.report.errors[0].message}")
    if (
        model.graph.namespace != plan["namespace"]
        or admission.config_digest({name: doc for name, doc in documents.items() if not is_state(doc)})
        != plan["inputs"]["config"]["digest"]
    ):
        raise ValueError("approved model content or namespace differs from the saved plan")
    record = model.record(action["record"])
    if record["type"] != "Requirement" or record["lifecycle"] == "retired" or record["id"] != action["record"]:
        raise ValueError("the saved action does not identify an eligible Requirement")
    candidate = planning.proposal(
        {"record": action["record"], "classification": action["kind"], "reason": action["reason"]},
        record,
        model.graph.namespace,
        plan["target"],
        plan["policy"],
    )
    if any(candidate[key] != action[key] for key in ("id", "criteria_digest", "bindings_digest")):
        raise ValueError("approved criteria, bindings or action identity changed")
    return model


def preview(
    source: admission.Source,
    plan_doc: Any,
    action_id: str,
    readiness_policy: Any,
    trust_policy: Any,
    output_target: Target,
    run_id: int,
    attempt: int,
    date: dt.date,
    *,
    ledger: Path | None = None,
    binding_id: str | None = None,
    state_doc: Any = None,
    lock_timeout: float | None = None,
) -> Doc:
    """Re-evaluate one advisory action from live evidence, leaving plans, state and pending work unchanged.

    Model and context remain fixed; an explicit resulting implementation may differ from the original.
    Fresh admission is mandatory. No supplied receipt, report or observed-state artifact is accepted.
    """
    if type(date) is not dt.date or type(run_id) is not int or run_id < 1 or type(attempt) is not int or attempt < 1:
        raise ValueError("an explicit evaluation date and positive run and attempt are required")
    if not isinstance(output_target, Target):
        raise ValueError("an explicit output target is required")
    if lock_timeout is not None:
        from exacly import execution_store

        execution_store.Deadline(lock_timeout)
        if ledger is None:
            raise ValueError("lock timeout applies only to a registered ledger preview")
    if any(value is not None for value in (ledger, binding_id, state_doc)) and not all(
        value is not None for value in (ledger, binding_id, state_doc)
    ):
        raise ValueError("ledger, binding ID and current state must be supplied together")
    target = source_target(output_target)
    chosen = admission.validate_policy(trust_policy)
    plan, action = saved_action(plan_doc, action_id, date)
    try:
        jsonschema.validate(readiness_policy, policy.SCHEMA)
    except jsonschema.ValidationError as error:
        raise ValueError(f"invalid readiness policy: {error.message}") from error
    if any(entry["dimensions"] != ["verification"] for entry in readiness_policy["requirements"]):
        raise ValueError("reconciliation supports verification-only readiness policies")
    selected_policy = {
        "name": readiness_policy["name"],
        "version": readiness_policy["version"],
        "digest": planning.digest(readiness_policy),
    }
    report: Doc = {
        "schema_version": VERSION,
        "advisory": True,
        "dry_run": True,
        "tool": {"name": "exacly", "version": metadata.version("exacly")},
        "status": "pending",
        "as_of": date.isoformat(),
        "plan": {"digest": plan["digest"], "action_id": action_id},
        "original_target": plan["target"],
        "target": target,
        "goal": {key: action[key] for key in ("record", "dimension", "criteria_digest", "bindings_digest")},
        "policy": selected_policy,
        "trust_policy": {"name": chosen["name"], "version": chosen["version"], "digest": planning.digest(chosen)},
        "claims": copy.deepcopy(CLAIMS),
        "checks": [],
        "admission": None,
        "binding": None,
        "readiness": None,
    }

    def finish(status: str, code: str, reason: str) -> Doc:
        report["status"] = status
        report["checks"].append({"code": code, "passed": status == "ready", "reason": reason})
        report["digest"] = planning.digest(report)
        return report

    if any(target[key] != plan["target"][key] for key in ("model_revision", "context")):
        return finish("stale", "scope", "The approved model or context changed; generate and review a new plan.")
    if chosen["model"]["revision"] != target["model_revision"]:
        return finish("stale", "model", "The trust policy selects a different model revision; replan explicitly.")
    if selected_policy != plan["policy"]:
        return finish("stale", "policy", "The readiness policy changed; generate and review a new plan.")
    report["checks"].append(
        {"code": "plan", "passed": True, "reason": "Saved action integrity, policy and scope matched."}
    )

    def check_binding(stage: str) -> str | None:
        if ledger is None or binding_id is None:
            return None
        from exacly import executions

        matched, reason, envelope = executions.match(
            ledger,
            binding_id,
            plan_doc=plan,
            action_id=action_id,
            state_doc=state_doc,
            readiness_policy=readiness_policy,
            trust_policy=chosen,
            output_target=Target(target),
            date=date,
            **({"lock_timeout": lock_timeout} if lock_timeout is not None else {}),
        )
        binding = envelope["binding"]
        report["binding"] = {
            "id": binding["id"],
            "pending": binding["contract"]["pending"],
            "registered_at": binding["registered_at"],
            "state_snapshot": binding["state_snapshot"],
            "state_watermark": envelope["state_watermark"],
            "active": envelope["active"],
            "superseded_by": envelope["superseded_by"],
            "checked_at": dt.datetime.now(dt.UTC).isoformat(),
        }
        if "validation" in envelope:
            report["binding"]["history_validation"] = {
                "scope": "protected_view_at_stage_entry",
                "stage": stage,
                "facts": envelope["validation"],
            }
        report["claims"]["registered_binding_verified"] = matched
        if not matched:
            return reason
        report["checks"].append({"code": stage, "passed": True, "reason": reason})
        return None

    binding_failure = check_binding("binding")
    if binding_failure is not None:
        return finish("stale", "binding", binding_failure)
    admitted = admission.admit(source, chosen, Target(target), run_id, attempt, date)
    report["admission"] = admitted.receipt
    if admitted.receipt["status"] != "admitted" or admitted.state is None:
        return finish("pending", "admission", "Fresh evidence was not admitted; inspect the admission receipt.")
    report["checks"].append({"code": "admission", "passed": True, "reason": "Fresh live evidence was admitted."})
    try:
        documents = admission.model_files(source, f"/repos/{chosen['repository']['full_name']}", chosen["model"], date)
        model = query.load({**documents, "__admitted_state__": admitted.state}, date, revision=target["model_revision"])
    except (admission.Rejected, GitHubError, query.QueryError, ValueError) as error:
        return finish("pending", "model", f"The admitted model could not be evaluated: {error}")
    if (
        model.graph.namespace != plan["namespace"]
        or admission.config_digest(documents) != plan["inputs"]["config"]["digest"]
    ):
        return finish("stale", "model", "Approved model content or namespace differs from the saved plan; replan.")
    try:
        record = model.record(action["record"])
    except query.QueryError:
        return finish("stale", "goal", "The selected Requirement is absent from the approved model; replan.")
    if record["type"] != "Requirement" or record["lifecycle"] == "retired" or record["id"] != action["record"]:
        return finish("stale", "goal", "The saved action no longer identifies an eligible Requirement; replan.")
    candidate = planning.proposal(
        {"record": action["record"], "classification": action["kind"], "reason": action["reason"]},
        record,
        model.graph.namespace,
        plan["target"],
        selected_policy,
    )
    if any(candidate[key] != action[key] for key in ("criteria_digest", "bindings_digest", "id")):
        return finish("stale", "goal", "The approved criteria, bindings or action identity changed; replan.")
    report["checks"].append(
        {"code": "goal", "passed": True, "reason": "Approved model, criteria and bindings matched."}
    )
    readiness = policy.evaluate(model, readiness_policy, Target(target))
    report["readiness"] = readiness
    selected = [
        check
        for check in readiness["checks"]
        if check["record"] == action["record"] and check["dimension"] == "verification"
    ]
    if len(selected) != 1:
        raise ValueError("the selected action is not a unique prerequisite of the readiness policy")
    if selected[0]["result"] != "satisfied":
        return finish("pending", "completion", selected[0]["reason"])
    binding_failure = check_binding("binding_recheck")
    if binding_failure is not None:
        return finish("stale", "binding_recheck", binding_failure)
    return finish(
        "ready", "completion", "Fresh evidence satisfies this action's completion condition; no task was changed."
    )


def as_text(report: Doc) -> str:
    lines = [
        f"Exacly completion preview: {report['status']}",
        f"Plan: {report['plan']['digest']}",
        f"Action: {report['plan']['action_id']} ({report['goal']['record']})",
        f"Original target: {planning.canonical(report['original_target'])}",
        f"Resulting target: {planning.canonical(report['target'])}",
        f"As of: {report['as_of']}",
    ]
    lines += [f"  {check['code']}: {check['reason']}" for check in report["checks"]]
    if report["binding"] is not None:
        lines.append(f"Registered binding: {report['binding']['id']}")
    if report["readiness"] is not None:
        lines.append(f"Whole-policy readiness: {report['readiness']['result']}")
    lines.append("Advisory dry run: no task changes or completion authority; executor-to-output linkage is unverified.")
    lines.append("Unsigned report; test honesty and acceptance coverage remain unverified.")
    return "\n".join(lines)
