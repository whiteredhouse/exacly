"""Sequential composition of independently approved source admissions and readiness gates.

The approved definition identifies every required role and its exact policy/model inputs. Each
invocation supplies a complete immutable run selection pinned to that definition's content identity.
Validation and evaluation preserve member namespaces; this module performs no filesystem writes.
"""

from __future__ import annotations

import copy
import datetime as dt
import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, cast
from xml.etree import ElementTree as ET

import jsonschema

from exacly import admission, planning, policy, query
from exacly.applicability import TARGET_SCHEMA, Target

Doc = dict[str, Any]
VERSION = "0.1.0"
MAX_MEMBERS = 64
MAX_CONSTRAINTS = 128
MEMBER_ID = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"}
REFERENCE = {"type": "string", "minLength": 1, "maxLength": 4096}
LABEL = {"type": "string", "pattern": r"\S", "maxLength": 256}
DEFINITION_SCHEMA: Doc = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "name": LABEL,
        "version": LABEL,
        "members": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_MEMBERS,
            "items": planning.fields(
                {
                    "id": MEMBER_ID,
                    "trust_policy": REFERENCE,
                    "readiness_policy": REFERENCE,
                    "model_root": REFERENCE,
                }
            ),
        },
        "constraints": {
            "type": "array",
            "maxItems": MAX_CONSTRAINTS,
            "items": planning.fields(
                {
                    "kind": {"enum": ["same_revision", "same_model"]},
                    "members": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": MAX_MEMBERS,
                        "uniqueItems": True,
                        "items": MEMBER_ID,
                    },
                }
            ),
        },
    }
)
SELECTION_SCHEMA: Doc = planning.fields(
    {
        "schema_version": {"const": VERSION},
        "definition_digest": planning.DIGEST,
        "members": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_MEMBERS,
            "items": planning.fields(
                {
                    "id": MEMBER_ID,
                    "run": {"type": "integer", "minimum": 1},
                    "attempt": {"type": "integer", "minimum": 1},
                    "target": TARGET_SCHEMA,
                }
            ),
        },
    }
)


@dataclass(frozen=True)
class MemberInputs:
    """Resolved policy and named config documents for one required member."""

    trust_policy: Any
    readiness_policy: Any
    documents: dict[str, Any]


@dataclass(frozen=True)
class PreparedMember:
    id: str
    inputs: MemberInputs
    model_identity: Doc
    trust_policy_digest: str
    readiness_policy_digest: str


@dataclass(frozen=True)
class PreparedDefinition:
    """A copied definition and its resolved inputs; evaluation rechecks their content identity."""

    definition: Doc
    inputs: dict[str, MemberInputs]
    members: tuple[PreparedMember, ...]
    digest: str
    date: dt.date

    def as_dict(self) -> Doc:
        """Describe validation and the required inventory without implying approval or readiness."""
        return {
            "schema_version": VERSION,
            "operation": "validate-composition-definition",
            "definition": {
                "name": self.definition["name"],
                "version": self.definition["version"],
                "digest": self.digest,
            },
            "definition_digest": self.digest,
            "as_of": self.date.isoformat(),
            "members": [_identity(member) for member in self.members],
            "constraints": copy.deepcopy(self.definition["constraints"]),
            "claims": {"policy_approval_verified": False, "readiness_evaluated": False, "self_authenticating": False},
        }


@dataclass(frozen=True)
class PreparedComposition:
    definition: PreparedDefinition
    selection: Doc
    members: tuple[Doc, ...]
    date: dt.date


@dataclass
class CompositionResult:
    report: Doc
    admissions: dict[str, admission.AdmissionResult]


def _schema(document: Any, schema: Doc, name: str) -> Doc:
    try:
        jsonschema.validate(document, schema)
    except (jsonschema.ValidationError, RecursionError) as error:
        detail = error.message if isinstance(error, jsonschema.ValidationError) else "document is excessively nested"
        raise ValueError(f"invalid composition {name}: {detail}") from error
    return copy.deepcopy(document)


def validate_definition_document(definition: Any) -> Doc:
    """Validate the closed definition structure before following any file references.

    References are portable relative paths resolved by a caller relative to the definition file.
    Parent-directory references are permitted; no path is discovered implicitly.
    """
    chosen = _schema(definition, DEFINITION_SCHEMA, "definition")
    identifiers: set[str] = set()
    for member in chosen["members"]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", member["id"]):
            raise ValueError("composition member IDs must be safe ASCII identifiers")
        if member["id"] in identifiers:
            raise ValueError(f"duplicate composition member: {member['id']}")
        identifiers.add(member["id"])
        for key in ("trust_policy", "readiness_policy", "model_root"):
            path = member[key]
            if (
                not path.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in path)
                or "\\" in path
                or PurePosixPath(path).is_absolute()
                or re.match(r"^[A-Za-z]:", path)
            ):
                raise ValueError(f"composition {key} must be a relative portable path")
    constraints: set[tuple[str, tuple[str, ...]]] = set()
    for constraint in chosen["constraints"]:
        if set(constraint["members"]) - identifiers:
            raise ValueError("composition constraint refers to an unknown member")
        constraint_key = constraint["kind"], tuple(sorted(constraint["members"]))
        if constraint_key in constraints:
            raise ValueError("duplicate composition consistency constraint")
        constraints.add(constraint_key)
    return chosen


def _identity(member: PreparedMember) -> Doc:
    return {
        "id": member.id,
        "trust_policy": {
            "name": member.inputs.trust_policy["name"],
            "version": member.inputs.trust_policy["version"],
            "digest": member.trust_policy_digest,
        },
        "readiness_policy": {
            "name": member.inputs.readiness_policy["name"],
            "version": member.inputs.readiness_policy["version"],
            "digest": member.readiness_policy_digest,
        },
        "model": copy.deepcopy(member.model_identity),
    }


def validate_definition(definition: Any, inputs: Mapping[str, MemberInputs], date: dt.date) -> PreparedDefinition:
    """Pure preflight of every required member and referenced policy/model.

    The fingerprint binds member roles to complete trust/readiness policy contents and approved
    model contents, in addition to the declared references and consistency rules. It is a content
    identity, not independent approval. All snapshots are copied before validation.
    """
    if type(date) is not dt.date:
        raise ValueError("an explicit evaluation date is required")
    chosen = validate_definition_document(definition)
    expected = {member["id"] for member in chosen["members"]}
    if not isinstance(inputs, Mapping) or set(inputs) != expected:
        raise ValueError("resolved composition inputs must exactly cover the required members")
    snapshots: dict[str, MemberInputs] = {}
    members = []
    for entry in chosen["members"]:
        identifier = entry["id"]
        supplied = inputs[identifier]
        if not isinstance(supplied, MemberInputs):
            raise ValueError(f"invalid resolved composition inputs for {identifier}")
        try:
            trusted = admission.validate_policy(supplied.trust_policy)
            documents = copy.deepcopy(supplied.documents)
            model = admission.validate_model(trusted, documents, date)
            readiness = policy.validate_policy(model, supplied.readiness_policy)
            copied = MemberInputs(trusted, readiness, documents)
            identity = {
                "repository": copy.deepcopy(trusted["repository"]),
                "namespace": model.graph.namespace,
                "revision": trusted["model"]["revision"],
                "files": sorted(trusted["model"]["files"]),
                "digest": trusted["model"]["digest"],
            }
            member = PreparedMember(identifier, copied, identity, planning.digest(trusted), planning.digest(readiness))
        except (ValueError, query.QueryError, RecursionError) as error:
            raise ValueError(f"invalid composition member {identifier}: {error}") from error
        snapshots[identifier] = copied
        members.append(member)
    by_id = {member.id: member for member in members}
    for constraint in chosen["constraints"]:
        if constraint["kind"] == "same_model":
            identities = [by_id[identifier].model_identity for identifier in constraint["members"]]
            if any(identity != identities[0] for identity in identities[1:]):
                raise ValueError("same_model constraint has inconsistent approved model identities")
        elif constraint["kind"] == "same_revision":
            repositories = [snapshots[identifier].trust_policy["repository"] for identifier in constraint["members"]]
            if any(repository != repositories[0] for repository in repositories[1:]):
                raise ValueError("same_revision constraint requires members of the same approved repository")
    identity_document = {
        "definition": chosen,
        "members": [
            {
                "id": member.id,
                "trust_policy": member.inputs.trust_policy,
                "readiness_policy": member.inputs.readiness_policy,
                "documents": member.inputs.documents,
            }
            for member in members
        ],
    }
    return PreparedDefinition(chosen, snapshots, tuple(members), planning.digest(identity_document), date)


def validate_selection(prepared: PreparedDefinition, selection: Any, date: dt.date) -> PreparedComposition:
    """Validate the complete immutable selection before any evidence source call.

    Revalidation detects mutation of exposed prepared dictionaries and freezes a fresh snapshot for
    this invocation. Equality constraints apply only to the explicitly listed member groups.
    """
    if not isinstance(prepared, PreparedDefinition):
        raise ValueError("a validated composition definition is required")
    fresh = validate_definition(prepared.definition, prepared.inputs, date)
    if fresh.digest != prepared.digest:
        raise ValueError("prepared composition definition content changed; validate and review its new identity")
    chosen = _schema(selection, SELECTION_SCHEMA, "selection")
    if chosen["definition_digest"] != fresh.digest:
        raise ValueError("selection does not match the approved composition definition digest")
    identifiers = [member["id"] for member in chosen["members"]]
    expected = {member.id for member in fresh.members}
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("duplicate composition member selection")
    if set(identifiers) != expected:
        raise ValueError("composition selection must exactly cover every required member")
    selected: dict[str, Doc] = {}
    for member in chosen["members"]:
        identifier = member["id"]
        try:
            _, target = admission.validate_request(
                fresh.inputs[identifier].trust_policy, Target(member["target"]), member["run"], member["attempt"], date
            )
        except ValueError as error:
            raise ValueError(f"invalid composition selection {identifier}: {error}") from error
        selected[identifier] = {**member, "target": target}
    for constraint in fresh.definition["constraints"]:
        if constraint["kind"] == "same_revision":
            identities = [
                (
                    fresh.inputs[identifier].trust_policy["repository"],
                    selected[identifier]["target"]["revision"],
                )
                for identifier in constraint["members"]
            ]
            if any(identity != identities[0] for identity in identities[1:]):
                raise ValueError("same_revision constraint has inconsistent repository/revision identities")
    return PreparedComposition(fresh, chosen, tuple(selected[member.id] for member in fresh.members), date)


def evaluate(
    source: admission.Source,
    prepared: PreparedDefinition,
    selection: Any,
    date: dt.date,
    *,
    on_member: Callable[[Doc, admission.AdmissionResult], None] | None = None,
    retain_admissions: bool = True,
) -> CompositionResult:
    """Freshly admit and gate every required member, sequentially, without publishing state.

    Normal admission rejection or unsatisfied readiness does not skip the remaining members.
    ``on_member`` is called with each completed member and its native admission result so a caller
    can durably retain partial evidence before the next source call. Callback or unexpected errors
    propagate; a caller must never publish a completed success for an interrupted invocation.
    ``complete`` means every required evaluator returned. Evidence paths describe the supported
    retention layout; this pure API does not claim that a caller has written those files.

    By default the result retains every native admission and its evidence bytes. With
    ``retain_admissions=False``, a synchronous ``on_member`` callback is required: after it
    returns ``None``, this evaluator releases that member's native result before the next admission.
    Streaming callbacks must finish synchronously; lazy callbacks and returned awaitables or
    generators are rejected rather than treated as completed retention.
    The returned admissions mapping is empty. Approved model snapshots and aggregate gate
    metadata still scale with the composition; this does not bound the caller or source's memory.
    """
    if type(retain_admissions) is not bool:
        raise ValueError("retain_admissions must be a boolean")
    if on_member is not None and not callable(on_member):
        raise ValueError("on_member must be a callable retention callback")
    if not retain_admissions and on_member is None:
        raise ValueError("streaming admission retention requires an on_member callback")
    if not retain_admissions and any(
        inspect.iscoroutinefunction(callback)
        or inspect.isasyncgenfunction(callback)
        or inspect.isgeneratorfunction(callback)
        for callback in (on_member, type(on_member).__call__)
    ):
        raise ValueError("streaming admission retention requires a synchronous on_member callback")
    invocation = validate_selection(prepared, selection, date)
    definition = invocation.definition
    identities = {member.id: member for member in definition.members}
    admissions: dict[str, admission.AdmissionResult] = {}
    results: list[Doc] = []
    for selected in invocation.members:
        identifier = selected["id"]
        member = identities[identifier]
        target = Target(selected["target"])
        admitted = admission.admit(
            source, member.inputs.trust_policy, target, selected["run"], selected["attempt"], date
        )
        if retain_admissions:
            admissions[identifier] = admitted
        gate = None
        if admitted.receipt["status"] == "admitted" and admitted.state is not None:
            model = query.load(
                {**member.inputs.documents, "__admitted_state__": admitted.state},
                date,
                revision=target.data["model_revision"],
            )
            gate = policy.evaluate(model, member.inputs.readiness_policy, target)
            del model
        satisfied = gate is not None and gate["result"] == "satisfied"
        evidence = {"receipt": f"members/{identifier}/receipt.json"}
        if admitted.receipt["status"] == "admitted":
            evidence.update(
                {
                    "archive": f"members/{identifier}/archive.zip",
                    "report": f"members/{identifier}/junit.xml",
                    "state": f"members/{identifier}/state.json",
                }
            )
        if gate is not None:
            evidence["gate"] = f"members/{identifier}/gate.json"
        report = {
            **_identity(member),
            "selection": copy.deepcopy(selected),
            "status": "satisfied" if satisfied else "blocked",
            "admission": copy.deepcopy(admitted.receipt),
            "gate": gate,
            "evidence": evidence,
            "reason": (
                "fresh admission accepted and readiness gate satisfied"
                if satisfied
                else f"readiness gate is {gate['result']}"
                if gate is not None
                else "evidence admission rejected"
            ),
        }
        results.append(report)
        if on_member is not None:
            callback_result = cast(Callable[[Doc, admission.AdmissionResult], object], on_member)(
                copy.deepcopy(report), copy.deepcopy(admitted)
            )
            if not retain_admissions and callback_result is not None:
                if inspect.iscoroutine(callback_result) or inspect.isgenerator(callback_result):
                    callback_result.close()
                raise ValueError("streaming on_member callback must finish synchronously and return None")
            del callback_result
        # Drop the original before evaluating the next admission's RHS. Omitting it from
        # `admissions` alone would keep the previous evidence alive during that source call.
        del admitted
    report = {
        "schema_version": VERSION,
        "operation": "compose",
        "complete": True,
        "result": "satisfied" if all(member["status"] == "satisfied" for member in results) else "blocked",
        "definition": {
            "name": definition.definition["name"],
            "version": definition.definition["version"],
            "digest": definition.digest,
        },
        "definition_digest": definition.digest,
        "selection_digest": planning.digest(invocation.selection),
        "as_of": date.isoformat(),
        "required_members": [member.id for member in definition.members],
        "members": results,
        "claims": {
            "scope": "required_member_admission_and_readiness",
            "test_honesty_verified": False,
            "coverage_verified": False,
            "self_authenticating": False,
        },
    }
    return CompositionResult(report, admissions)


def as_text(report: Doc) -> str:
    lines = [f"Composition: {report['definition']['name']} version {report['definition']['version']}"]
    lines += [f"Result: {report['result']}", f"Complete: {report['complete']}", f"As of: {report['as_of']}"]
    lines += [f"  {member['id']}: {member['status']} — {member['reason']}" for member in report["members"]]
    return "\n".join(lines)


JUNIT_PREFIX = "exacly.composition/v1"
JUNIT_MAX_BYTES = policy.JUNIT_MAX_BYTES
JUNIT_MAX_CHECKS = policy.JUNIT_MAX_CHECKS
JUNIT_MAX_CASES = JUNIT_MAX_CHECKS + MAX_MEMBERS
_ADMISSION_STAGES = ("repository", "run", "observer", "producer", "artifact", "observation")


def _junit_object(value: Any, label: str) -> Doc:
    if not isinstance(value, dict):
        raise ValueError(f"composition JUnit requires {label}")
    return value


def _junit_string(value: Any) -> str:
    policy._junit_identity(value)
    return cast(str, value)


def _junit_digest_field(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError("composition JUnit requires valid content digest metadata")
    return value


def _junit_policy_identity(value: Any) -> Doc:
    identity = _junit_object(value, "native policy identity")
    for key in ("name", "version"):
        _junit_string(identity.get(key))
    _junit_digest_field(identity.get("digest"))
    return identity


def _junit_members(report: Doc) -> list[Doc]:
    """Check internal report identities, without authenticating digests or evaluating evidence."""
    if (
        report.get("schema_version") != VERSION
        or report.get("operation") != "compose"
        or report.get("complete") is not True
    ):
        raise ValueError("composition JUnit requires a completed native composition report")
    definition = _junit_policy_identity(report.get("definition"))
    if _junit_digest_field(report.get("definition_digest")) != definition["digest"]:
        raise ValueError("composition JUnit definition identities disagree")
    _junit_digest_field(report.get("selection_digest"))
    date = _junit_string(report.get("as_of"))
    try:
        if dt.date.fromisoformat(date).isoformat() != date:
            raise ValueError("noncanonical date")
    except ValueError as error:
        raise ValueError("composition JUnit requires a native evaluation date") from error
    required, members = report.get("required_members"), report.get("members")
    if (
        not isinstance(required, list)
        or not 1 <= len(required) <= MAX_MEMBERS
        or any(
            not isinstance(identifier, str) or re.fullmatch(MEMBER_ID["pattern"], identifier) is None
            for identifier in required
        )
        or len(set(required)) != len(required)
        or not isinstance(members, list)
        or len(members) != len(required)
    ):
        raise ValueError("composition JUnit requires every required member exactly once")
    checked: dict[str, Doc] = {}
    for value in members:
        member = _junit_object(value, "native member reports")
        identifier = member.get("id")
        if not isinstance(identifier, str) or identifier not in required or identifier in checked:
            raise ValueError("composition JUnit requires every required member exactly once")
        checked[identifier] = member
        trust = _junit_policy_identity(member.get("trust_policy"))
        readiness = _junit_policy_identity(member.get("readiness_policy"))
        model = _junit_object(member.get("model"), "native model identity")
        _junit_string(model.get("namespace"))
        _junit_digest_field(model.get("digest"))
        repository = _junit_object(model.get("repository"), "native repository identity")
        _junit_string(repository.get("full_name"))
        if (
            not admission.positive_id(repository.get("id"))
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository["full_name"]) is None
        ):
            raise ValueError("composition JUnit requires native repository identity")
        selected = _junit_object(member.get("selection"), "member selection")
        target = _junit_object(selected.get("target"), "member target")
        if (
            selected.get("id") != identifier
            or not admission.positive_id(selected.get("run"))
            or not admission.positive_id(selected.get("attempt"))
            or set(target) != {"context", "model_revision", "revision"}
            or any(
                not isinstance(target[key], str) or re.fullmatch(r"[0-9a-f]{40}", target[key]) is None
                for key in ("model_revision", "revision")
            )
            or model.get("revision") != target["model_revision"]
        ):
            raise ValueError("composition JUnit member selection and model identities disagree")
        _junit_string(target["context"])
        receipt = _junit_object(member.get("admission"), "native admission receipt")
        status = receipt.get("status")
        checks = receipt.get("checks")
        if (
            receipt.get("schema_version") != admission.RECEIPT_VERSION
            or status not in ("admitted", "rejected")
            or receipt.get("target") != target
            or receipt.get("policy") != trust
            or receipt.get("as_of") != date
            or not isinstance(checks, list)
            or not 1 <= len(checks) <= len(_ADMISSION_STAGES)
        ):
            raise ValueError("composition JUnit admission and member identities disagree")
        _junit_digest_field(receipt.get("digest"))
        for index, check in enumerate(checks):
            if (
                not isinstance(check, dict)
                or check.get("code") != _ADMISSION_STAGES[index]
                or type(check.get("passed")) is not bool
                or not isinstance(check.get("reason"), str)
                or check["passed"] is not (status == "admitted" or index < len(checks) - 1)
            ):
                raise ValueError("composition JUnit requires consistent native admission checks")
        if "gate" not in member:
            raise ValueError("composition JUnit requires an explicit native gate result or null")
        gate = member["gate"]
        evidence = {"receipt": f"members/{identifier}/receipt.json"}
        if status == "admitted":
            origin = _junit_object(receipt.get("origin"), "admitted origin identity")
            if (
                len(checks) != len(_ADMISSION_STAGES)
                or any(not admission.positive_id(origin.get(key)) for key in ("repository_id", "run_id", "run_attempt"))
                or origin.get("repository") != repository["full_name"]
                or origin.get("repository_id") != repository["id"]
                or origin.get("run_id") != selected["run"]
                or origin.get("run_attempt") != selected["attempt"]
                or origin.get("workflow_revision") != target["revision"]
            ):
                raise ValueError("composition JUnit admitted origin and selection identities disagree")
            for key in ("archive_digest", "report_digest"):
                _junit_digest_field(origin.get(key))
            _junit_digest_field(receipt.get("state_digest"))
            gate = _junit_object(gate, "the actual admitted member gate")
            if (
                gate.get("target") != target
                or gate.get("policy") != {key: readiness[key] for key in ("name", "version")}
                or gate.get("as_of") != date
                or gate.get("result") not in ("satisfied", "not_satisfied", "unknown")
            ):
                raise ValueError("composition JUnit native gate and member identities disagree")
            evidence.update(
                {
                    key: f"members/{identifier}/{name}"
                    for key, name in (
                        ("archive", "archive.zip"),
                        ("report", "junit.xml"),
                        ("state", "state.json"),
                        ("gate", "gate.json"),
                    )
                }
            )
        elif gate is not None:
            raise ValueError("composition JUnit cannot invent a gate for rejected admission")
        expected = "satisfied" if gate is not None and gate["result"] == "satisfied" else "blocked"
        if (
            member.get("status") != expected
            or not isinstance(member.get("reason"), str)
            or member.get("evidence") != evidence
        ):
            raise ValueError("composition JUnit member outcome or retained evidence layout disagrees")
    result = "satisfied" if all(member["status"] == "satisfied" for member in checked.values()) else "blocked"
    if report.get("result") != result:
        raise ValueError("composition JUnit aggregate and member outcomes disagree")
    return [checked[identifier] for identifier in required]


def as_junit(report: Doc) -> str:
    """Serialize completed native composition readiness without fetching or evaluating evidence.

    Every required member has a verdict case, including rejected admissions with no gate. Actual
    gate prerequisites retain the native serializer and use ``<composition name>/<member ID>`` as
    their logical report label. Selectors exclude changing versions, revisions and content digests.
    At most 4,096 native prerequisites plus 64 verdicts and 8 MiB of XML are supported. Metadata
    content digests identify supplied reports; they neither authenticate them nor establish fresh tests.
    """
    try:
        members = _junit_members(_junit_object(report, "a native composition report"))
        definition = report["definition"]
        composition_name = definition["name"]
        root = ET.Element("testsuites")
        verdicts = ET.SubElement(
            root, "testsuite", {"name": f"{JUNIT_PREFIX}/{policy._junit_identity(composition_name)}"}
        )
        properties = ET.SubElement(verdicts, "properties")
        metadata = {
            "exacly.report.kind": "composition-readiness-evaluation",
            "exacly.composition.name": composition_name,
            "exacly.composition.version": definition["version"],
            "exacly.composition.definition.digest": report["definition_digest"],
            "exacly.composition.selection.digest": report["selection_digest"],
            "exacly.composition.result": report["result"],
            "exacly.as_of": report["as_of"],
            "exacly.native_report.digest": policy._junit_digest(report),
            "exacly.evidence.aggregate": "report.json",
        }
        for key, value in metadata.items():
            policy._junit_property(properties, key, value)
        native_checks = 0
        for member in members:
            identifier = member["id"]
            classname = (
                f"{JUNIT_PREFIX}/{policy._junit_identity(composition_name)}/{policy._junit_identity(identifier)}"
            )
            case = ET.SubElement(verdicts, "testcase", {"classname": classname, "name": "verdict"})
            case_properties = ET.SubElement(case, "properties")
            ET.SubElement(case_properties, "property", {"name": "exacly-case", "value": f"{classname}/verdict"})
            metadata = {
                "exacly.composition.member": identifier,
                "exacly.composition.status": member["status"],
                "exacly.admission.status": member["admission"]["status"],
                "exacly.admission.digest": member["admission"]["digest"],
                "exacly.namespace": member["model"]["namespace"],
                "exacly.model.digest": member["model"]["digest"],
                "exacly.model.revision": member["model"]["revision"],
                "exacly.repository": member["model"]["repository"]["full_name"],
                "exacly.target.digest": policy._junit_digest(member["selection"]["target"]),
                "exacly.selection.run": str(member["selection"]["run"]),
                "exacly.selection.attempt": str(member["selection"]["attempt"]),
            }
            for category in ("trust_policy", "readiness_policy"):
                metadata.update(
                    {f"exacly.{category}.{key}": member[category][key] for key in ("name", "version", "digest")}
                )
            metadata.update(
                {f"exacly.target.{key}": value for key, value in sorted(member["selection"]["target"].items())}
            )
            metadata.update({f"exacly.evidence.{key}": value for key, value in sorted(member["evidence"].items())})
            for key, value in metadata.items():
                policy._junit_property(case_properties, key, value)
            if member["status"] != "satisfied":
                gate = member["gate"]
                outcome = "admission_rejected" if gate is None else gate["result"]
                failure = ET.SubElement(
                    case,
                    "failure",
                    {
                        "type": f"exacly.composition.{outcome}",
                        "message": policy._junit_text(member["reason"], 512),
                    },
                )
                reason = member["reason"]
                if gate is None:
                    reason += "\n" + member["admission"]["checks"][-1]["reason"]
                failure.text = policy._junit_text(reason)
            if member["gate"] is not None:
                checks = member["gate"].get("checks")
                if not isinstance(checks, list):
                    raise ValueError("composition JUnit requires native gate prerequisites")
                native_checks += len(checks)
                if native_checks > JUNIT_MAX_CHECKS:
                    raise ValueError("composition JUnit exceeds the aggregate prerequisite limit")
                native = ET.fromstring(
                    policy.as_junit(
                        member["gate"],
                        namespace=member["model"]["namespace"],
                        report_id=f"{composition_name}/{identifier}",
                    )
                )
                root.extend(native)
            if len(ET.tostring(root, encoding="utf-8", xml_declaration=True)) > JUNIT_MAX_BYTES:
                raise ValueError("composition JUnit exceeds the aggregate XML size limit")
        for suite in root:
            cases = list(suite.iter("testcase"))
            suite.attrib.update(
                tests=str(len(cases)),
                failures=str(sum(case.find("failure") is not None for case in cases)),
                errors="0",
                skipped="0",
            )
        counts = {
            key: str(sum(int(suite.attrib[key]) for suite in root))
            for key in ("tests", "failures", "errors", "skipped")
        }
        root.attrib.update(counts)
        content: bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        if int(counts["tests"]) > JUNIT_MAX_CASES or len(content) > JUNIT_MAX_BYTES:
            raise ValueError("composition JUnit exceeds aggregate report limits")
        return content.decode("utf-8")
    except RecursionError as error:
        raise ValueError("composition JUnit report is excessively nested") from error
