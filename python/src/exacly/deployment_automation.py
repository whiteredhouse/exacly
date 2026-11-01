"""Separately authorized static deployment and bounded rollback, with independent observations.

An applied delivery effect and an operational evaluation are different facts. The common receipt
never claims health. This profile can establish only byte-exact readiness of its loopback static
service on one trusted nonproduction host; it cannot evaluate an arbitrary application's health.
"""

from __future__ import annotations

import copy
import datetime as dt
import importlib
import re
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from exacly import delivery_automation as delivery
from exacly import delivery_profile, planning
from exacly import execution_store as store
from exacly import local_static_deployment as owner
from exacly import operational_observation as observer

Doc = dict[str, Any]
PROFILE = "local-static-deployment-v1"
VERSION = "0.1.0"
FIELDS = planning.fields
TEXT = delivery.TEXT
DIGEST = delivery.DIGEST
KEY = FIELDS({"path": TEXT, "id": DIGEST})
LOCATION = FIELDS({"path": TEXT, "id": TEXT})
LIMIT = FIELDS(
    {
        "wall_time_ms": {"type": "integer", "minimum": 1, "maximum": 120000},
        "read_calls": {"type": "integer", "minimum": 1, "maximum": 128},
        "write_calls": {"type": "integer", "minimum": 0, "maximum": 1},
    }
)
CONFIGURATION_SCHEMA = FIELDS(
    {
        "schema_version": {"const": VERSION},
        "profile": {"const": PROFILE},
        "owner": LOCATION,
        "owner_key": KEY,
        "workflow": delivery.WORKFLOW,
        "publisher_workflow": delivery.WORKFLOW,
        "registry": LOCATION,
        "publisher_key": KEY,
        "application": TEXT,
        "environment": TEXT,
        "observer": {"type": "object"},
        "call_limits": FIELDS({method: LIMIT for method in ("inspect", "invoke", "reconcile")}),
    }
)
ROLLBACK = FIELDS(
    {
        "current_rollout_id": DIGEST,
        "prior_generation": DIGEST,
        "failure_observation_digest": DIGEST,
        "maximum_rollbacks": {"const": 1},
        "expires_at": TEXT,
    }
)
POLICY_SCHEMA = FIELDS(
    {
        "schema_version": {"const": VERSION},
        "profile": {"const": PROFILE},
        "stage": {"enum": ["deploy", "rollback"]},
        "contract": delivery.CONTRACT,
        "source": delivery.SOURCE,
        "package": TEXT,
        "version": TEXT,
        "observer_configuration_digest": DIGEST,
        "publication_verification_contract": delivery.CONTRACT,
        "publication_verification_producer": delivery.WORKFLOW,
        "rollback": {"anyOf": [{"type": "null"}, ROLLBACK]},
    }
)
PAYLOAD_SCHEMA = FIELDS(
    {
        "schema_version": {"const": VERSION},
        "profile": {"const": PROFILE},
        "operation_profile_digest": DIGEST,
        "source": delivery.SOURCE,
        "package": TEXT,
        "version": TEXT,
        "effect_nonce": observer.NONCE,
        "rollback": {"anyOf": [{"type": "null"}, ROLLBACK]},
        "failure_observation": {"type": ["object", "null"]},
    }
)
STATUS_SCHEMA = FIELDS(
    {
        "profile": {"const": observer.PROFILE},
        "scope": {"const": "as-of-recorded-environment-observation"},
        "report_id": {"anyOf": [DIGEST, {"type": "null"}]},
        "historical_status": {"enum": [None, "healthy", "unhealthy", "unknown"]},
        "status": {"enum": ["healthy", "unhealthy", "unknown"]},
        "freshness": {"enum": ["current", "stale", "missing", "superseded", "unavailable"]},
        "evaluated_at": TEXT,
        "generation": FIELDS(
            {
                "expected": DIGEST,
                "observed": {"anyOf": [DIGEST, {"type": "null"}]},
                "observed_at": {"type": ["string", "null"]},
            }
        ),
        "current_readiness_established": {"type": "boolean"},
        "reason": TEXT,
        "generic_application_health": {"const": False},
        "deployment_authority": {"const": False},
    }
)
PROOF_SCHEMA = FIELDS(
    {
        "schema_version": {"const": VERSION},
        "profile": {"const": PROFILE},
        "method": {"enum": ["inspect", "invoke", "reconcile"]},
        "request_digest": DIGEST,
        "payload_digest": DIGEST,
        "configuration_digest": DIGEST,
        "policy_digest": DIGEST,
        "write_started": {"type": "boolean"},
        "facts": FIELDS(
            {
                "snapshot": {"type": ["object", "null"]},
                "rollout": {"type": ["object", "null"]},
                "activation": {"type": ["object", "null"]},
                "current_activation": {"type": ["object", "null"]},
                "operational_status": {"anyOf": [STATUS_SCHEMA, {"type": "null"}]},
                "observation": {"type": ["object", "null"]},
                "evaluation": {"type": ["object", "null"]},
                "artifact_verified": {"type": "boolean"},
                "blocked": {"type": "boolean"},
            }
        ),
    }
)
ArtifactReader = Callable[..., bytes]


@dataclass(frozen=True)
class OperationalReadScope:
    """Trusted caller-owned scope/accounting; this object is no self-authenticating grant.

    ``reserve_read`` must charge one owner read to the caller's authoritative budget before release.
    It is trusted Python bootstrap, not a callback supplied by candidate JSON. No budget is created
    here; repeated calls must be bounded by that same external accounting authority.
    """

    owner_id: str
    application: str
    environment: str
    reserve_read: Callable[[], None]


def _path(value: str) -> Path:
    path = Path(value)
    owner.require(
        path.is_absolute() and str(path) == value and not value.startswith("//") and ".." not in path.parts,
        "deployment path must be canonical and absolute",
    )
    return path


def _observer_storage(configuration: Doc, *, live: bool = False) -> None:
    """Keep the observer's shared key lock outside the owner's exclusive lock namespace.

    Preview examines directory metadata only when it exists. Live preconditions additionally
    require supported real private directories and a private regular observer key before intent.
    Device/inode comparisons catch existing mount/path aliases without reading key bytes.
    """
    root = _path(configuration["owner"]["path"])
    key = _path(configuration["observer"]["key"]["path"])
    owner.require(root not in key.parents, "observer key must be outside the owning workflow root")
    if live:
        store.location(root / "owner.lock")
        store.location(key)
        store.check_file_info(key.lstat())
    try:
        root_info = root.stat()
    except FileNotFoundError:
        owner.require(not live, "owning workflow directory must exist before dispatch")
        return
    owner.require(stat.S_ISDIR(root_info.st_mode), "owning workflow root must be a directory")
    for directory in (key.parent, *key.parent.parents):
        try:
            info = directory.stat()
        except FileNotFoundError:
            owner.require(not live, "observer key directory must exist before dispatch")
            continue
        owner.require(stat.S_ISDIR(info.st_mode), "observer key parents must be directories")
        owner.require(
            (info.st_dev, info.st_ino) != (root_info.st_dev, root_info.st_ino),
            "observer key and owning workflow have aliased lock domains",
        )


def validate_configuration(value: Doc) -> Doc:
    selected = delivery._schema(value, CONFIGURATION_SCHEMA)
    selected["observer"] = observer.validate_configuration(selected["observer"])
    owner.require(
        all(selected["observer"][name] == selected[name] for name in ("application", "environment")),
        "operational observer belongs to another environment",
    )
    for field in ("owner", "owner_key", "publisher_key", "registry"):
        _path(selected[field]["path"])
    for field in ("owner", "registry"):
        owner.require(
            uuid.UUID(hex=selected[field]["id"]).hex == selected[field]["id"],
            "owner and registry identities must be canonical UUIDs",
        )
    for workflow in (selected["workflow"], selected["publisher_workflow"]):
        delivery._repository(workflow["repository"])
        owner.require(
            re.fullmatch(r"[0-9a-f]{40}", workflow["revision"]) is not None, "workflow revision must be exact"
        )
    for field in ("application", "environment"):
        owner.require(
            not any(ord(character) < 32 or ord(character) == 127 for character in selected[field]),
            "environment identity contains a control character",
        )
    owner.require(
        len(
            {selected[field]["path"] for field in ("owner", "owner_key", "publisher_key", "registry")}
            | {selected["observer"]["key"]["path"]}
        )
        == 5,
        "deployment, publication and observer storage must be distinct",
    )
    owner.require(
        len({selected["owner_key"]["id"], selected["publisher_key"]["id"], selected["observer"]["key"]["id"]}) == 3,
        "deployment owner, publisher and observer keys must be separate",
    )
    _observer_storage(selected)
    for method, limits in selected["call_limits"].items():
        owner.require(
            limits["write_calls"] == (1 if method == "invoke" else 0),
            "only invoke permits one owning-workflow transition",
        )
        owner.require(
            limits["read_calls"] >= selected["observer"]["samples"] + 5,
            "native read ceiling cannot cover owner, artifact and observation protocol",
        )
        owner.require(
            limits["wall_time_ms"] > selected["observer"]["interval_ms"] * (selected["observer"]["samples"] - 1),
            "native deadline cannot cover the observation window",
        )
    return selected


def validate_policy(value: Doc) -> Doc:
    selected = delivery._schema(value, POLICY_SCHEMA)
    delivery._repository(selected["source"]["repository"])
    delivery._repository(selected["publication_verification_producer"]["repository"])
    owner.require(
        re.fullmatch(r"[0-9a-f]{40}", selected["publication_verification_producer"]["revision"]) is not None,
        "publication acceptance producer revision must be exact",
    )
    owner.require(
        re.fullmatch(r"[0-9a-f]{40}", selected["source"]["revision"]) is not None,
        "artifact source revision must be exact",
    )
    owner.require(
        (selected["stage"] == "rollback") == (selected["rollback"] is not None),
        "rollback needs its own explicitly bounded policy",
    )
    if selected["rollback"] is not None:
        delivery._timestamp(selected["rollback"]["expires_at"])
    return selected


def installed_profile() -> Doc:
    """Use the shared actual installed enforcement/runtime profile plus closed stage schemas."""
    return {
        **delivery_profile.implementation(
            required=(
                "deployment_automation.py",
                "local_static_deployment.py",
                "operational_observation.py",
                "owning_publisher.py",
                "release_publication.py",
                "delivery_engine.py",
                "delivery_store.py",
                "delivery_runtime.py",
                "delivery_io.py",
                "cli.py",
            )
        ),
        "schemas": {
            "configuration": CONFIGURATION_SCHEMA,
            "policy": POLICY_SCHEMA,
            "payload": PAYLOAD_SCHEMA,
            "proof": PROOF_SCHEMA,
            "status": STATUS_SCHEMA,
        },
    }


def configuration_digest(configuration: Doc, policy: Doc) -> str:
    return delivery.digest(
        {
            "configuration": validate_configuration(configuration),
            "policy": validate_policy(policy),
            "installed": installed_profile(),
        }
    )


def operation_profile_digest(configuration: Doc, policy: Doc) -> str:
    # Credential rotation changes configuration authority, but never silently changes operation scope.
    return configuration_digest(configuration, policy)


def validate_payload(request: Doc, payload: Doc, configuration: Doc, policy: Doc) -> Doc:
    request = delivery.validate_request(request)
    native = delivery._schema(payload, PAYLOAD_SCHEMA)
    config, approved = validate_configuration(configuration), validate_policy(policy)
    owner.require(
        request["stage"] == approved["stage"] in ("deploy", "rollback"),
        "separate deployment or rollback authority is required",
    )
    subject = request["subject"]
    owner.require(
        subject["artifact"]["size"] <= config["observer"]["max_artifact_bytes"],
        "approved artifact exceeds the selected operational observer byte ceiling",
    )
    owner.require(
        all(subject[name] == config[name] for name in ("workflow", "application", "environment")),
        "deployment scope differs from selected owning workflow/environment",
    )
    owner.require(subject["strategy"] == "replace-static-v1", "unsupported deployment strategy")
    owner.require(
        native["source"] == approved["source"]
        and native["package"] == approved["package"]
        and native["version"] == approved["version"]
        and native["rollback"] == approved["rollback"],
        "native artifact source or rollback conditions differ from approved policy",
    )
    owner.require(
        native["operation_profile_digest"] == operation_profile_digest(config, approved),
        "deployment operation profile changed",
    )
    owner.require(request["native_payload_digest"] == delivery.digest(native), "deployment payload digest differs")
    owner.require(
        approved["observer_configuration_digest"] == observer.configuration_digest(config["observer"]),
        "selected operational observer contract changed",
    )
    if request["stage"] == "deploy":
        owner.require(native["failure_observation"] is None, "deployment cannot inherit rollback authority")
    else:
        owner.require(
            native["failure_observation"] is not None
            and delivery.digest(native["rollback"]) == subject["conditions_digest"]
            and delivery.digest(native["failure_observation"]) == native["rollback"]["failure_observation_digest"],
            "rollback does not bind its exact fresh failure and explicit conditions",
        )
    return native


def expected_observation(record: Doc) -> Doc:
    return {
        "application": record["subject"]["application"],
        "environment": record["subject"]["environment"],
        "artifact": copy.deepcopy(record["subject"]["artifact"]),
        "source": copy.deepcopy(record["source"]),
        "rollout_id": record["rollout_id"],
        "effect_nonce": record["effect_nonce"],
        "generation": record["generation"],
    }


def effect(record: Doc) -> Doc:
    subject = record["subject"]
    return {
        "workflow": copy.deepcopy(subject["workflow"]),
        "application": subject["application"],
        "environment": subject["environment"],
        "artifact": copy.deepcopy(subject["artifact"]),
        "generation": record["generation"],
        "rollout_id": record["rollout_id"],
        "remote_receipt_digest": delivery.digest(record),
    }


def _point_status(
    record: Doc,
    current_activation: Doc | None,
    observation: Doc | None,
    evaluation: Doc | None,
    *,
    evaluated_at: dt.datetime,
) -> Doc:
    """Pure applicability at one retained point; origin is authenticated by the native phase."""
    observed = observed_at = None
    if current_activation is not None:
        observed, observed_at = current_activation["active_generation"], current_activation["observed_at"]
        owner.require(
            observer._timestamp(observed_at) <= evaluated_at, "generation observation postdates its evaluation"
        )
    historical = None
    if observation is not None:
        owner.require(
            evaluation is not None
            and evaluation["scope"] == "historical-static-measurements"
            and evaluation["current_readiness_established"] is False
            and evaluation["report_id"] == observation["id"]
            and evaluation["expected"] == expected_observation(record)
            and evaluation["window"] == observation["window"],
            "retained measurements do not match their exact rollout/report",
        )
        assert evaluation is not None
        historical = evaluation["status"]
        owner.require(
            observer._timestamp(observation["window"]["ended_at"]) <= evaluated_at,
            "operational measurements postdate their evaluation",
        )
        if current_activation is not None:
            owner.require(
                observer._timestamp(current_activation["observed_at"])
                >= observer._timestamp(observation["window"]["ended_at"]),
                "current generation snapshot predates the measurement window",
            )
    else:
        owner.require(evaluation is None, "retained measurement evaluation lacks a report")
    if current_activation is None:
        freshness, status = "unavailable", "unknown"
        reason = "The active environment generation was not confirmed within the bounded native read."
    elif observed != record["generation"]:
        freshness, status = "superseded", "unknown"
        reason = "The selected rollout is not the observed active environment generation."
    elif observation is None:
        freshness, status = "missing", "unknown"
        reason = "Trusted static measurements are unavailable for the observed rollout."
    elif evaluated_at >= observer._timestamp(observation["window"]["expires_at"]):
        freshness, status = "stale", "unknown"
        reason = "The retained measurements expired before the native evaluation point."
    else:
        freshness = "current"
        assert evaluation is not None
        status = str(evaluation["status"])
        reason = evaluation["reason"]
    return {
        "profile": observer.PROFILE,
        "scope": "as-of-recorded-environment-observation",
        "report_id": None if observation is None else observation["id"],
        "historical_status": historical,
        "status": status,
        "freshness": freshness,
        "evaluated_at": evaluated_at.isoformat(),
        "generation": {"expected": record["generation"], "observed": observed, "observed_at": observed_at},
        "current_readiness_established": freshness == "current" and status == "healthy",
        "reason": reason,
        "generic_application_health": False,
        "deployment_authority": False,
    }


def _published_bytes(configuration: Doc, policy: Doc, request: Doc, *, deadline: store.Deadline) -> bytes:
    # Fixed installed workflow, never a candidate import/command. It authenticates the publisher's
    # retained receipt and checks the actual complete immutable registry inventory.
    publisher = importlib.import_module("exacly.owning_publisher")
    reader = cast(ArtifactReader, publisher.read_published_artifact)
    return reader(
        configuration["registry"],
        policy["package"],
        policy["version"],
        request["subject"]["artifact"],
        policy["source"],
        configuration["publisher_key"],
        configuration["publisher_workflow"],
        deadline=deadline,
        expected_verification_contract=policy["publication_verification_contract"],
        expected_verification_producer=policy["publication_verification_producer"],
    )


class LocalStaticDeploymentAdapter:
    """Trusted installed adapter. A candidate cannot select paths, keys, observer or workflow.

    ``approval_pin`` is selected independently by the trusted caller. It is required for effects;
    omitting it permits only local validation/inspection/discovery. ``artifact_reader`` is trusted
    Python bootstrap for controlled tests, not a document field or arbitrary command hook.
    """

    def __init__(
        self,
        configuration: Doc,
        policy: Doc,
        *,
        approval_pin: delivery.ApprovalPin | None = None,
        artifact_reader: ArtifactReader | None = None,
    ) -> None:
        self.configuration = validate_configuration(configuration)
        self.policy = validate_policy(policy)
        self._pin, self._reader = approval_pin, artifact_reader or _published_bytes

    @property
    def adapter_id(self) -> str:
        return PROFILE

    @property
    def configuration_digest(self) -> str:
        return configuration_digest(self.configuration, self.policy)

    @property
    def operation_profile_digest(self) -> str:
        return operation_profile_digest(self.configuration, self.policy)

    @property
    def call_limits(self) -> Doc:
        return copy.deepcopy(self.configuration["call_limits"])

    @property
    def approval_context(self) -> Doc:
        return {"policy_digest": delivery.digest(self.policy), "contract": copy.deepcopy(self.policy["contract"])}

    def validate_payload(self, request: Doc, payload: Doc) -> Doc:
        return validate_payload(request, payload, self.configuration, self.policy)

    def _owner(self) -> owner.LocalStaticOwner:
        return owner.LocalStaticOwner(
            Path(self.configuration["owner"]["path"]),
            self.configuration["owner"]["id"],
            self.configuration["owner_key"],
            approval_pin=self._pin,
            approval_context=self.approval_context,
            adapter_identity={"id": self.adapter_id, "configuration_digest": self.configuration_digest},
        )

    def _bytes(self, request: Doc, *, deadline: store.Deadline) -> bytes:
        deadline.check()
        content = self._reader(self.configuration, self.policy, request, deadline=deadline)
        artifact = request["subject"]["artifact"]
        owner.require(
            isinstance(content, bytes)
            and len(content) <= owner.MAX_BYTES
            and len(content) == artifact["size"]
            and owner._bytes_digest(content) == artifact["digest"],
            "publisher returned bytes differing from separately approved artifact",
        )
        deadline.check()
        return content

    def _preconditions(
        self,
        request: Doc,
        payload: Doc,
        snapshot: Doc,
        *,
        deadline: store.Deadline,
        prior_record: Doc | None = None,
    ) -> None:
        _observer_storage(self.configuration, live=True)
        subject = request["subject"]
        owner.require(
            snapshot["generation"] == subject["expected_generation"],
            "current deployment generation differs from approved precondition",
        )
        if request["stage"] != "rollback":
            return
        current, conditions = snapshot["rollout"], payload["rollback"]
        owner.require(
            current is not None
            and current["rollout_id"] == conditions["current_rollout_id"]
            and current["subject"]["artifact"] == subject["observed_artifact"],
            "rollback must first observe the exact partial/current rollout",
        )
        owner.require(
            dt.datetime.now(dt.UTC) < delivery._timestamp(conditions["expires_at"]), "rollback conditions expired"
        )
        failure = observer.verify(
            payload["failure_observation"],
            expected_observation(current),
            self.configuration["observer"],
            now=dt.datetime.now(dt.UTC),
            deadline=deadline,
        )
        owner.require(
            delivery._timestamp(payload["failure_observation"]["window"]["started_at"])
            >= delivery._timestamp(current["created_at"]),
            "rollback observation predates its actual rollout",
        )
        owner.require(
            failure["status"] == "unhealthy", "rollback needs a fresh trusted failure of its exact current rollout"
        )
        prior = (
            prior_record
            if prior_record is not None
            else self._owner().prior(conditions["prior_generation"], deadline=deadline)
        )
        owner.require(
            prior["generation"] != current["generation"]
            and prior["subject"]["artifact"] == subject["artifact"]
            and prior["source"] == payload["source"],
            "rollback artifact/source was not the explicitly allowed prior active build",
        )

    def _proof(
        self,
        method: str,
        request: Doc,
        payload: Doc,
        *,
        snapshot: Doc | None = None,
        rollout: Doc | None = None,
        activation: Doc | None = None,
        current_activation: Doc | None = None,
        operational_status: Doc | None = None,
        observation: Doc | None = None,
        evaluation: Doc | None = None,
        artifact_verified: bool = False,
        blocked: bool = False,
        started: bool = False,
    ) -> Doc:
        return {
            "schema_version": VERSION,
            "profile": PROFILE,
            "method": method,
            "request_digest": delivery.digest(request),
            "payload_digest": delivery.digest(payload),
            "configuration_digest": self.configuration_digest,
            "policy_digest": delivery.digest(self.policy),
            "write_started": started,
            "facts": {
                "snapshot": snapshot,
                "rollout": rollout,
                "activation": activation,
                "current_activation": current_activation,
                "operational_status": operational_status,
                "observation": observation,
                "evaluation": evaluation,
                "artifact_verified": artifact_verified,
                "blocked": blocked,
            },
        }

    def _authority(self, intent: Doc, authorization: Doc) -> None:
        owner.require(self._pin is not None, "deployment owner needs an independently selected approval pin")
        assert self._pin is not None
        approved = delivery.validate_approval(authorization, pin=self._pin, now=dt.datetime.now(dt.UTC))
        delivery.validate_intent(intent, approval=approved)
        owner.require(
            approved["stage"] == self.policy["stage"]
            and approved["adapter"] == {"id": self.adapter_id, "configuration_digest": self.configuration_digest}
            and approved["contract"] == self.policy["contract"]
            and approved["policy_digest"] == delivery.digest(self.policy),
            "deployment approval does not cover current owning workflow, policy and observer",
        )

    def inspect(self, request: Doc, payload: Doc, *, deadline: store.Deadline) -> Doc:
        payload = self.validate_payload(request, payload)
        snapshot = None
        try:
            self._bytes(request, deadline=deadline)
            snapshot = self._owner().snapshot(deadline=deadline, require_unfenced=True)
            self._preconditions(request, payload, snapshot, deadline=deadline)
            return {
                "ready": True,
                "reason": "Exact artifact and environment preconditions are ready; health remains unestablished.",
                "provenance": self._proof("inspect", request, payload, snapshot=snapshot, artifact_verified=True),
            }
        except (ValueError, OSError, store.StoreError):
            return {
                "ready": False,
                "reason": "Artifact, owner identity or exact current deployment/rollback preconditions block.",
                "provenance": self._proof("inspect", request, payload, snapshot=snapshot, blocked=True),
            }

    def _observed_result(
        self,
        method: str,
        intent: Doc,
        request: Doc,
        payload: Doc,
        record: Doc,
        activation: Doc,
        *,
        deadline: store.Deadline,
        started: bool,
    ) -> Doc:
        owner.verify_record(record, self.configuration["owner_key"])
        owner.verify_activation(activation, record, self.configuration["owner_key"])
        owner.require(
            record["request_digest"] == delivery.digest(request)
            and record["payload_digest"] == delivery.digest(payload),
            "owner discovered another deployment effect",
        )
        if (
            record["intent_id"] != intent["id"]
            or activation["intent_id"] != intent["id"]
            or record["authority_digest"] != intent["authority_digest"]
        ):
            return {
                "outcome": "uncertain",
                "effect": None,
                "reason": "The owner result belongs to another exact intent or authority; no retry is authorized.",
                "provenance": self._proof(
                    method, request, payload, rollout=record, activation=activation, blocked=True, started=started
                ),
            }
        observation = evaluation = None
        try:
            observation = observer.observe(
                expected_observation(record), self.configuration["observer"], deadline=deadline
            )
            evaluation = observer.authenticate(
                observation,
                expected_observation(record),
                self.configuration["observer"],
                now=dt.datetime.now(dt.UTC),
                deadline=deadline,
            )
            owner.require(
                delivery._timestamp(observation["window"]["started_at"]) >= delivery._timestamp(record["created_at"]),
                "operational observation predates its actual rollout",
            )
        except (ValueError, OSError, store.StoreError):
            # The definite active-chain effect remains applied. Missing health stays explicit.
            observation = evaluation = None
        # Finish fallible configuration/profile work before the last charged provider read. A
        # healthy report alone does not establish that its generation is still the active one.
        proof = self._proof(
            method,
            request,
            payload,
            rollout=record,
            activation=activation,
            observation=observation,
            evaluation=evaluation,
            started=started,
        )
        current_activation = self._refresh_activation(record, intent["id"], deadline=deadline)
        proof["facts"]["current_activation"] = current_activation
        proof["facts"]["operational_status"] = _point_status(
            record, current_activation, observation, evaluation, evaluated_at=dt.datetime.now(dt.UTC)
        )
        return {
            "outcome": "applied",
            "effect": effect(record),
            "reason": "The deployment effect is retained; inspect its separate operational evaluation for readiness.",
            "provenance": proof,
        }

    def _refresh_activation(self, record: Doc, intent_id: str, *, deadline: store.Deadline) -> Doc | None:
        """One bounded owner read; absence/unavailability cannot erase the already known effect."""
        try:
            selected = self._owner()  # Actual installed/configuration work precedes provider discovery.
            requested_at = dt.datetime.now(dt.UTC)
            discovered = selected.discover(intent_id, deadline=deadline)
            owner.require(discovered is not None, "current owning workflow generation is unavailable")
            assert discovered is not None
            current_record, activation = discovered
            owner.require(
                current_record == record and activation["intent_id"] == intent_id,
                "current owner discovery returned another exact intent",
            )
            owner.verify_activation(activation, record, self.configuration["owner_key"])
            deadline.check()
            owner.require(
                requested_at <= observer._timestamp(activation["observed_at"]) <= dt.datetime.now(dt.UTC),
                "current owner discovery replayed an old or future activation point",
            )
            return activation
        except (ValueError, OSError, store.StoreError):
            return None

    def invoke(self, intent: Doc, payload: Doc, authorization: Doc, *, deadline: store.Deadline) -> Doc:
        intent = delivery._intent(intent)
        request = delivery.validate_request(intent["request"])
        payload = self.validate_payload(request, payload)
        started = False
        try:
            self._authority(intent, authorization)
            content = self._bytes(request, deadline=deadline)
            snapshot = self._owner().snapshot(deadline=deadline)
            self._preconditions(request, payload, snapshot, deadline=deadline)
            self._authority(intent, authorization)
            deadline.check()
            started = True
            record, activation = self._owner().apply(
                intent,
                payload,
                authorization,
                content,
                deadline=deadline,
                precommit=lambda current, prior: self._preconditions(
                    request, payload, current, deadline=deadline, prior_record=prior
                ),
            )
            return self._observed_result(
                "invoke", intent, request, payload, record, activation, deadline=deadline, started=True
            )
        except (ValueError, OSError, store.StoreError):
            return {
                "outcome": "uncertain" if started else "rejected",
                "effect": None,
                "reason": "The workflow may have started; discover its generation before retry or conflicting work."
                if started
                else "Current independently pinned deployment authority or preconditions block.",
                "provenance": self._proof("invoke", request, payload, blocked=True, started=started),
            }

    def reconcile(self, intent: Doc, payload: Doc, *, deadline: store.Deadline) -> Doc:
        intent = delivery._intent(intent)
        request = delivery.validate_request(intent["request"])
        payload = self.validate_payload(request, payload)
        try:
            discovered = self._owner().discover(intent["id"], deadline=deadline)
            owner.require(discovered is not None, "no active-chain proof confirms this deployment")
            assert discovered is not None
            record, activation = discovered
            return self._observed_result(
                "reconcile", intent, request, payload, record, activation, deadline=deadline, started=False
            )
        except (ValueError, OSError, store.StoreError):
            return {
                "outcome": "uncertain",
                "effect": None,
                "reason": "Discovery cannot confirm this rollout; absence grants no retry or rollback.",
                "provenance": self._proof("reconcile", request, payload, blocked=True),
            }

    def validate_provenance(self, method: str, request: Doc, payload: Doc, provenance: Doc) -> Doc:
        self.validate_payload(request, payload)
        selected = delivery._schema(provenance, PROOF_SCHEMA)
        owner.require(
            selected["method"] == method
            and selected["request_digest"] == delivery.digest(request)
            and selected["payload_digest"] == delivery.digest(payload)
            and selected["configuration_digest"] == self.configuration_digest
            and selected["policy_digest"] == delivery.digest(self.policy),
            "deployment provenance scope changed",
        )
        owner.require(not selected["write_started"] or method == "invoke", "read-only phase claims a deployment write")
        facts = selected["facts"]
        owner.require(
            not facts["blocked"] or (facts["current_activation"] is None and facts["operational_status"] is None),
            "blocked operation cannot retain a new operational point",
        )
        snapshot = facts["snapshot"]
        if snapshot is not None:
            owner.require(
                method == "inspect" and set(snapshot) == {"generation", "rollout"},
                "invalid current-environment snapshot fields",
            )
            if snapshot["rollout"] is None:
                owner.require(
                    snapshot["generation"]
                    == delivery.digest({"owner": self.configuration["owner"]["id"], "empty": True}),
                    "empty environment generation differs from selected owner",
                )
            else:
                actual = owner.verify_record(snapshot["rollout"], self.configuration["owner_key"])
                owner.require(
                    actual["owner_id"] == self.configuration["owner"]["id"]
                    and actual["generation"] == snapshot["generation"],
                    "current-environment snapshot substituted another owner/generation",
                )
        owner.require(
            method == "inspect" or not facts["artifact_verified"], "effect proof substituted preflight artifact facts"
        )
        if facts["rollout"] is not None:
            record = owner.verify_record(facts["rollout"], self.configuration["owner_key"])
            owner.require(facts["activation"] is not None, "prepared rollout alone cannot establish an applied effect")
            owner.verify_activation(facts["activation"], record, self.configuration["owner_key"])
            if facts["current_activation"] is not None:
                owner.verify_activation(facts["current_activation"], record, self.configuration["owner_key"])
            owner.require(
                record["owner_id"] == self.configuration["owner"]["id"]
                and record["request_digest"] == delivery.digest(request)
                and record["payload_digest"] == delivery.digest(payload)
                and record["subject"] == request["subject"]
                and record["source"] == payload["source"],
                "actual owning workflow result differs from approved deployment",
            )
            if facts["observation"] is not None:
                evaluation = observer.authenticate(
                    facts["observation"],
                    expected_observation(record),
                    self.configuration["observer"],
                    now=dt.datetime.now(dt.UTC),
                )
                owner.require(
                    delivery._timestamp(facts["observation"]["window"]["started_at"])
                    >= delivery._timestamp(record["created_at"]),
                    "operational proof predates its actual rollout",
                )
                owner.require(
                    evaluation == facts["evaluation"], "operational evaluation differs from trusted measurements"
                )
            else:
                owner.require(facts["evaluation"] is None, "operational evaluation lacks trusted observation")
            if facts["operational_status"] is not None:
                owner.require(not facts["blocked"], "blocked operation cannot claim an operational evaluation point")
                self._recorded_status(request, payload, selected)
        else:
            owner.require(
                facts["activation"] is None
                and facts["current_activation"] is None
                and facts["operational_status"] is None
                and facts["observation"] is None
                and facts["evaluation"] is None,
                "operational observation lacks an actual rollout",
            )
        # These are authenticated native observation points, never caller-selected future clocks.
        # Parse after all other fallible work, then compare with actual UTC without another IO step.
        points = []
        if facts["operational_status"] is not None:
            points.append(observer._timestamp(facts["operational_status"]["evaluated_at"]))
        if facts["current_activation"] is not None:
            points.append(observer._timestamp(facts["current_activation"]["observed_at"]))
        checked_at = dt.datetime.now(dt.UTC)
        owner.require(all(point <= checked_at for point in points), "authenticated native operational point is future")
        return selected

    def _recorded_status(self, request: Doc, payload: Doc, provenance: Doc) -> Doc:
        """Pure consistency of an engine-authenticated retained point; no key/provider/profile IO."""
        request = delivery.validate_request(request)
        payload = delivery._schema(payload, PAYLOAD_SCHEMA)
        proof = delivery._schema(provenance, PROOF_SCHEMA)
        owner.require(
            proof["request_digest"] == delivery.digest(request)
            and proof["payload_digest"] == request["native_payload_digest"] == delivery.digest(payload)
            and proof["policy_digest"] == delivery.digest(self.policy),
            "retained operational point belongs to another request/policy",
        )
        facts = proof["facts"]
        record = facts["rollout"]
        owner.require(
            isinstance(record, dict) and not facts["blocked"], "retained operational point lacks a confirmed rollout"
        )
        owner.require(
            record["owner_id"] == self.configuration["owner"]["id"]
            and record["request_digest"] == proof["request_digest"]
            and record["payload_digest"] == proof["payload_digest"]
            and record["subject"] == request["subject"]
            and record["source"] == payload["source"],
            "retained operational point substituted its exact owner or rollout",
        )
        owner.activation_fields(facts["activation"], record, self.configuration["owner_key"]["id"])
        current = facts["current_activation"]
        if current is not None:
            owner.activation_fields(current, record, self.configuration["owner_key"]["id"])
        observation = facts["observation"]
        if observation is not None:
            observation = observer._schema(observation, observer.REPORT_SCHEMA)
        status = delivery._schema(facts["operational_status"], STATUS_SCHEMA)
        evaluated_at = observer._timestamp(status["evaluated_at"])
        owner.require(
            observer._timestamp(facts["activation"]["observed_at"]) <= evaluated_at,
            "retained operational evaluation predates the activated effect",
        )
        expected = _point_status(record, current, observation, facts["evaluation"], evaluated_at=evaluated_at)
        owner.require(status == expected, "retained operational status differs from its generation/report point")
        return status

    def retained_operational_status(self, request: Doc, payload: Doc, provenance: Doc) -> Doc:
        """Present already engine-authenticated evidence without another provider/key/profile read.

        This does not authenticate untrusted input. The native phase verified signatures and the
        engine retained this exact proof. The original evaluation/owner-observation timestamps are
        never advanced; only TTL aging can downgrade the retained point. No global current state
        beyond that signed observation is promised.
        """
        status = self._recorded_status(request, payload, provenance)
        observation = provenance["facts"]["observation"]
        expiry = None if observation is None else observer._timestamp(observation["window"]["expires_at"])
        point = observer._timestamp(status["evaluated_at"])
        now = dt.datetime.now(dt.UTC)  # After all pure validation/parsing, with no later IO.
        owner.require(point <= now, "retained operational evaluation point is future")
        if status["freshness"] == "current" and expiry is not None and now >= expiry:
            return {
                **status,
                "status": "unknown",
                "freshness": "stale",
                "current_readiness_established": False,
                "reason": "The measurements expired after the retained native observation point.",
            }
        return status

    def operational_status(
        self, request: Doc, payload: Doc, provenance: Doc, *, deadline: store.Deadline, read_scope: OperationalReadScope
    ) -> Doc:
        """One explicitly scoped, caller-accounted live owner read, inside the supplied deadline.

        The caller must reserve an authoritative read budget through ``read_scope``. This API
        performs no new HTTP sampling and grants no deployment/rollback authority. CLI rendering
        uses the pure retained presenter instead of calling this method after a native effect.
        """
        owner.require(
            isinstance(deadline, store.Deadline) and isinstance(read_scope, OperationalReadScope),
            "live operational status needs an explicit deadline and trusted read scope",
        )
        owner.require(
            (read_scope.owner_id, read_scope.application, read_scope.environment)
            == (
                self.configuration["owner"]["id"],
                self.configuration["application"],
                self.configuration["environment"],
            ),
            "live operational read scope selects another owner/environment",
        )
        deadline.check()
        proof = self.validate_provenance(provenance["method"], request, payload, provenance)
        self._recorded_status(request, payload, proof)
        facts = proof["facts"]
        deadline.check()
        read_scope.reserve_read()
        deadline.check()
        record = facts["rollout"]
        current = self._refresh_activation(record, record["intent_id"], deadline=deadline)
        status = _point_status(
            record, current, facts["observation"], facts["evaluation"], evaluated_at=dt.datetime.now(dt.UTC)
        )
        return {**status, "current_activation": current}

    def validate_result(self, method: str, request: Doc, payload: Doc, result: Doc) -> Doc:
        """Pure outcome binding after ``validate_provenance`` authenticates the exact proof.

        The engine calls those steps in order on copied bytes inside its native boundary. This
        method alone does not authenticate untrusted signatures, read keys or query the provider.
        """
        owner.require(
            set(result)
            == (
                {"ready", "reason", "provenance"}
                if method == "inspect"
                else {"outcome", "effect", "reason", "provenance"}
            ),
            "invalid native deployment result fields",
        )
        request = delivery.validate_request(request)
        payload = delivery._schema(payload, PAYLOAD_SCHEMA)
        proof = delivery._schema(result["provenance"], PROOF_SCHEMA)
        owner.require(
            proof["method"] == method
            and proof["request_digest"] == delivery.digest(request)
            and proof["payload_digest"] == request["native_payload_digest"] == delivery.digest(payload)
            and proof["policy_digest"] == delivery.digest(self.policy),
            "native result differs from its authenticated request/policy scope",
        )
        owner.require(not proof["write_started"] or method == "invoke", "read-only result claims an invocation")
        facts = proof["facts"]
        if facts["rollout"] is not None:
            record = facts["rollout"]
            owner.require(
                record["owner_id"] == self.configuration["owner"]["id"]
                and record["request_digest"] == proof["request_digest"]
                and record["payload_digest"] == proof["payload_digest"]
                and record["subject"] == request["subject"]
                and record["source"] == payload["source"],
                "native result substituted its authenticated exact effect",
            )
            owner.require(facts["activation"] is not None, "prepared rollout alone cannot establish an applied effect")
            owner.activation_fields(facts["activation"], record, self.configuration["owner_key"]["id"])
        if facts["operational_status"] is not None:
            self._recorded_status(request, payload, proof)
        if method == "inspect":
            owner.require(
                type(result["ready"]) is bool
                and result["ready"] == (facts["artifact_verified"] and not facts["blocked"]),
                "deployment readiness differs from actual preflight facts",
            )
            if result["ready"]:
                owner.require(
                    isinstance(facts["snapshot"], dict)
                    and facts["snapshot"]["generation"] == request["subject"]["expected_generation"],
                    "ready deployment lacks current generation evidence",
                )
        elif result["outcome"] == "applied":
            owner.require(
                facts["rollout"] is not None
                and facts["operational_status"] is not None
                and result["effect"] == effect(facts["rollout"])
                and not facts["blocked"],
                "applied deployment lacks its exact authenticated effect",
            )
            owner.require(
                proof["write_started"] == (method == "invoke"),
                "applied deployment has inconsistent invocation provenance",
            )
        else:
            owner.require(
                result["outcome"] in ("rejected", "uncertain")
                and result["effect"] is None
                and facts["blocked"]
                and (facts["rollout"] is None or result["outcome"] == "uncertain"),
                "unconfirmed deployment was relabeled",
            )
            owner.require(
                result["outcome"] != "rejected" or (method == "invoke" and not proof["write_started"]),
                "possibly started deployment cannot be rejected as effect-free",
            )
        return copy.deepcopy(result)
