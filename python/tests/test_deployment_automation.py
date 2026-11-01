"""Controlled real byte serving, independently keyed observation and owner crash/CAS checks."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from exacly import delivery_automation as delivery
from exacly import delivery_profile
from exacly import deployment_automation as deploy
from exacly import execution_store as store
from exacly import local_static_deployment as owner
from exacly import operational_observation as observer

Doc = dict[str, Any]


def identity(value: str) -> str:
    return delivery.digest({"fixture": value})


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


class Environment:
    def __init__(self, path: Path) -> None:
        self.root = _private(path / "owner")
        keys = _private(path / "keys")
        self.owner_key = {"path": str(keys / "owner.key"), "id": owner.initialize_key(keys / "owner.key")}
        self.observer_key = {"path": str(keys / "observer.key"), "id": observer.initialize_key(keys / "observer.key")}
        self.workflow = {
            "repository": {"id": 42, "full_name": "example/static"},
            "id": "deploy-static",
            "revision": "a" * 40,
            "contract_digest": identity("owner-workflow"),
        }
        self.source = {"repository": {"id": 42, "full_name": "example/static"}, "revision": "b" * 40}
        self.publication_verification_contract = {
            field: identity("publication-acceptance-" + field)
            for field in ("model", "criteria", "bindings", "observer")
        }
        self.publication_verification_producer = {
            **self.workflow,
            "id": "independent-artifact-verifier",
            "revision": "c" * 40,
        }
        self.owner_id = uuid.uuid4().hex
        owner.initialize(
            self.root, owner_id=self.owner_id, application="docs", environment="test", workflow=self.workflow
        )
        self.owner = owner.LocalStaticOwner(self.root, self.owner_id, self.owner_key)
        self.server = owner.StaticDeploymentServer(self.owner)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config: Doc = {
            "schema_version": deploy.VERSION,
            "profile": deploy.PROFILE,
            "owner": {"path": str(self.root), "id": self.owner_id},
            "owner_key": self.owner_key,
            "workflow": self.workflow,
            "publisher_workflow": {**self.workflow, "id": "publish-static"},
            "registry": {"path": str(_private(path / "registry")), "id": uuid.uuid4().hex},
            "publisher_key": {"path": str(keys / "publisher.key"), "id": identity("independent-publisher")},
            "application": "docs",
            "environment": "test",
            "observer": {
                "schema_version": observer.VERSION,
                "profile": observer.PROFILE,
                "application": "docs",
                "environment": "test",
                "url": f"http://127.0.0.1:{self.server.server_port}/artifact",
                "key": self.observer_key,
                "samples": 2,
                "interval_ms": 1,
                "max_latency_ms": 2000,
                "max_age_ms": 60000,
                "max_artifact_bytes": 1024 * 1024,
            },
            "call_limits": {
                method: {"wall_time_ms": 60000, "read_calls": 12, "write_calls": int(method == "invoke")}
                for method in ("inspect", "invoke", "reconcile")
            },
        }
        self.contents: dict[str, bytes] = {}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def reader(self, configuration: Doc, policy: Doc, request: Doc, *, deadline: store.Deadline) -> bytes:
        deadline.check()
        # Controlled test bootstrap. The production default uses authenticated owning_publisher.
        return self.contents[request["subject"]["artifact"]["digest"]]

    def operation(
        self,
        content: bytes,
        *,
        stage: str = "deploy",
        rollback: Doc | None = None,
        failure: Doc | None = None,
        expected_generation: str | None = None,
        source: Doc | None = None,
        artifact_name: str = "site.bin",
        package: str = "site",
        version: str | None = None,
        default_reader: bool = False,
    ) -> tuple[deploy.LocalStaticDeploymentAdapter, Doc, Doc, Doc, Doc]:
        artifact: Doc = {
            "name": artifact_name,
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
        self.contents[artifact["digest"]] = content
        policy: Doc = {
            "schema_version": deploy.VERSION,
            "profile": deploy.PROFILE,
            "stage": stage,
            "contract": {field: identity(field) for field in ("model", "criteria", "bindings", "observer")},
            "source": source or self.source,
            "package": package,
            "version": version or artifact["digest"][7:19],
            "observer_configuration_digest": observer.configuration_digest(self.config["observer"]),
            "publication_verification_contract": copy.deepcopy(self.publication_verification_contract),
            "publication_verification_producer": copy.deepcopy(self.publication_verification_producer),
            "rollback": rollback,
        }
        reader = None if default_reader else self.reader
        adapter = deploy.LocalStaticDeploymentAdapter(self.config, policy, artifact_reader=reader)
        payload: Doc = {
            "schema_version": deploy.VERSION,
            "profile": deploy.PROFILE,
            "operation_profile_digest": adapter.operation_profile_digest,
            "source": policy["source"],
            "package": policy["package"],
            "version": policy["version"],
            "effect_nonce": uuid.uuid4().hex,
            "rollback": rollback,
            "failure_observation": failure,
        }
        snapshot = self.owner.snapshot(deadline=store.Deadline(10))
        subject = {
            "workflow": self.workflow,
            "application": "docs",
            "environment": "test",
            "artifact": artifact,
            "expected_generation": expected_generation or snapshot["generation"],
            "strategy": "replace-static-v1",
        }
        if stage == "rollback":
            subject.update(
                observed_artifact=snapshot["rollout"]["subject"]["artifact"],
                conditions_digest=delivery.digest(rollback),
            )
        request = {
            "schema_version": delivery.VERSION,
            "profile": delivery.PROFILE,
            "kind": "delivery-request",
            "objective": {"id": identity("objective"), "original_goal": identity("goal")},
            "operation_key": identity(payload["effect_nonce"]),
            "stage": stage,
            "subject": subject,
            "native_payload_digest": delivery.digest(payload),
        }
        now = dt.datetime.now(dt.UTC)
        approval: Doc = {
            "schema_version": delivery.VERSION,
            "profile": delivery.PROFILE,
            "kind": "delivery-approval",
            "issuer": "independent-reviewer",
            "approval_id": uuid.uuid4().hex,
            "objective": request["objective"],
            "stage": stage,
            "operation_key": request["operation_key"],
            "request_digest": delivery.digest(request),
            "subject_digest": delivery.digest(subject),
            "resource_key": delivery.resource_key(request),
            "policy_digest": delivery.digest(policy),
            "adapter": {"id": adapter.adapter_id, "configuration_digest": adapter.configuration_digest},
            "contract": policy["contract"],
            "journal": {"path": str(self.root.parent / "independent-engine.json"), "id": uuid.uuid4().hex},
            "limits": {"operations": 1, "wall_time_ms": 60000, "reads": 12, "rollbacks": int(stage == "rollback")},
            "not_before": (now - dt.timedelta(seconds=10)).isoformat(),
            "expires_at": (now + dt.timedelta(minutes=10)).isoformat(),
        }
        pin = delivery.ApprovalPin(approval["issuer"], approval["approval_id"], delivery.digest(approval))
        adapter = deploy.LocalStaticDeploymentAdapter(self.config, policy, approval_pin=pin, artifact_reader=reader)
        intent = {
            "schema_version": delivery.VERSION,
            "profile": delivery.PROFILE,
            "kind": "delivery-intent",
            "request": request,
            "request_digest": delivery.digest(request),
            "subject_digest": delivery.digest(subject),
            "resource_key": delivery.resource_key(request),
            "authority_digest": delivery.digest(approval),
            "approval_id": approval["approval_id"],
            "policy_digest": delivery.digest(policy),
            "adapter": approval["adapter"],
            "journal": approval["journal"],
            "cost": approval["limits"],
        }
        intent["id"] = delivery.digest(intent)
        delivery.validate_intent(intent, approval=approval)
        return adapter, request, payload, intent, approval


@pytest.fixture
def environment(tmp_path: Path) -> Iterator[Environment]:
    value = Environment(tmp_path)
    try:
        yield value
    finally:
        value.close()


def invoke(operation: tuple[deploy.LocalStaticDeploymentAdapter, Doc, Doc, Doc, Doc]) -> Doc:
    adapter, _request, payload, intent, approval = operation
    return adapter.invoke(intent, payload, approval, deadline=store.Deadline(30))


def test_environment_and_artifact_need_separate_deployment_authority(environment: Environment) -> None:
    operation = environment.operation(b"exact static bytes\x00\xff")
    adapter, request, payload, intent, approval = operation
    assert adapter.inspect(request, payload, deadline=store.Deadline(30))["ready"]
    wrong = copy.deepcopy(approval)
    wrong["stage"] = "publish"
    assert adapter.invoke(intent, payload, wrong, deadline=store.Deadline(30))["outcome"] == "rejected"
    assert not list(environment.root.glob("*.intent.json"))
    changed = copy.deepcopy(request)
    changed["subject"]["environment"] = "production"
    with pytest.raises(ValueError, match="scope"):
        adapter.validate_payload(changed, payload)
    result = invoke(operation)
    assert result["outcome"] == "applied"
    assert result["provenance"]["facts"]["evaluation"]["status"] == "healthy"
    assert adapter.validate_result("invoke", request, payload, result) == result
    record, actual = environment.owner.served(deadline=store.Deadline(10))
    assert actual == b"exact static bytes\x00\xff"
    assert record["authority_digest"] == delivery.digest(approval)


def test_current_rollout_needs_fresh_environment_specific_observations(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = environment.operation(b"site")
    result = invoke(operation)
    report = result["provenance"]["facts"]["observation"]
    evaluation = result["provenance"]["facts"]["evaluation"]
    assert evaluation["status"] == "healthy" and evaluation["generic_application_health"] is False
    expected = deploy.expected_observation(result["provenance"]["facts"]["rollout"])
    wrong = {**expected, "environment": "production"}
    with pytest.raises((ValueError, store.StoreError)):
        observer.verify(report, wrong, environment.config["observer"], now=dt.datetime.now(dt.UTC))
    with pytest.raises(store.StoreError, match="stale"):
        observer.verify(
            report, expected, environment.config["observer"], now=delivery._timestamp(report["window"]["expires_at"])
        )
    with pytest.raises(store.StoreError, match="consumed"):
        observer.verify(
            report,
            expected,
            environment.config["observer"],
            now=dt.datetime.now(dt.UTC),
            consumed_report_ids=(report["id"],),
        )
    adapter, request, payload, intent, _approval = operation
    monkeypatch.setattr(observer, "observe", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("missing")))
    discovered = adapter.reconcile(intent, payload, deadline=store.Deadline(30))
    assert discovered["outcome"] == "applied"
    assert discovered["effect"] == result["effect"]
    assert discovered["provenance"]["facts"]["evaluation"] is None
    adapter.validate_result("reconcile", request, payload, discovered)


def test_uncertain_or_partial_rollout_is_reconciled_before_retry(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = environment.operation(b"new artifact")
    adapter, request, payload, intent, _approval = operation
    original = owner._replace

    def lose_reply(path: Path, value: Doc) -> None:
        original(path, value)
        raise OSError("reply lost after active generation changed")

    monkeypatch.setattr(owner, "_replace", lose_reply)
    result = invoke(operation)
    assert result["outcome"] == "uncertain"
    adapter.validate_result("invoke", request, payload, result)
    before = {path.name: path.read_bytes() for path in environment.root.iterdir()}
    monkeypatch.setattr(owner, "_replace", lambda *args: pytest.fail("reconcile must not replace"))
    recovered = adapter.reconcile(intent, payload, deadline=store.Deadline(30))
    assert recovered["outcome"] == "applied"
    assert recovered["provenance"]["write_started"] is False
    assert before == {path.name: path.read_bytes() for path in environment.root.iterdir()}
    assert invoke(operation)["outcome"] == "rejected"
    assert len(list(environment.root.glob("*.intent.json"))) == 1


def test_rollback_is_bounded_and_requires_its_own_authority(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = environment.operation(b"prior")
    prior = invoke(first)["provenance"]["facts"]["rollout"]
    current = invoke(environment.operation(b"current"))["provenance"]["facts"]["rollout"]
    # Real server returns bytes from a different artifact: trusted measurement records the failure.
    original = environment.owner.served

    def unhealthy(*, deadline: store.Deadline) -> tuple[Doc, bytes]:
        record, _content = original(deadline=deadline)
        return record, b"broken"

    monkeypatch.setattr(environment.owner, "served", unhealthy)
    failure = observer.observe(
        deploy.expected_observation(current), environment.config["observer"], deadline=store.Deadline(10)
    )
    assert (
        observer.verify(
            failure, deploy.expected_observation(current), environment.config["observer"], now=dt.datetime.now(dt.UTC)
        )["status"]
        == "unhealthy"
    )
    conditions = {
        "current_rollout_id": current["rollout_id"],
        "prior_generation": prior["generation"],
        "failure_observation_digest": delivery.digest(failure),
        "maximum_rollbacks": 1,
        "expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)).isoformat(),
    }
    rollback = environment.operation(
        b"prior", stage="rollback", rollback=conditions, failure=failure, source=prior["source"]
    )
    adapter, request, payload, intent, approval = rollback
    assert request["subject"]["conditions_digest"] == delivery.digest(conditions)
    assert adapter.inspect(request, payload, deadline=store.Deadline(30))["ready"]
    inherited = copy.deepcopy(approval)
    inherited["stage"] = "deploy"
    assert adapter.invoke(intent, payload, inherited, deadline=store.Deadline(30))["outcome"] == "rejected"
    monkeypatch.setattr(environment.owner, "served", original)
    result = invoke(rollback)
    assert result["outcome"] == "applied" and result["provenance"]["facts"]["evaluation"]["status"] == "healthy"
    assert result["effect"]["artifact"] == prior["subject"]["artifact"]
    assert result["effect"]["generation"] != prior["generation"]
    assert invoke(rollback)["outcome"] == "rejected"
    assert len([path for path in environment.root.glob("*.intent.json")]) == 3


def test_failed_operation_without_active_chain_fences_environment(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = environment.operation(b"staged")
    monkeypatch.setattr(owner, "_replace", lambda *args: (_ for _ in ()).throw(OSError("before CAS")))
    assert invoke(operation)["outcome"] == "uncertain"
    adapter, _request, payload, intent, _approval = operation
    assert adapter.reconcile(intent, payload, deadline=store.Deadline(30))["outcome"] == "uncertain"
    assert adapter.inspect(operation[1], payload, deadline=store.Deadline(30))["ready"] is False
    conflicting = environment.operation(b"conflict")
    assert invoke(conflicting)["outcome"] == "uncertain"
    assert len(list(environment.root.glob("*.intent.json"))) == 1
    assert environment.owner.snapshot(deadline=store.Deadline(10))["rollout"] is None


def test_concurrent_exact_generation_cas_allows_one_effect(environment: Environment) -> None:
    first, second = environment.operation(b"one"), environment.operation(b"two")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, (first, second)))
    assert sum(result["outcome"] == "applied" for result in results) == 1
    assert len(list(environment.root.glob("*.intent.json"))) == 1


def test_definite_unhealthy_effect_retains_failed_observation(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = environment.owner.served

    def unhealthy(*, deadline: store.Deadline) -> tuple[Doc, bytes]:
        record, _content = original(deadline=deadline)
        return record, b"wrong real HTTP bytes"

    monkeypatch.setattr(environment.owner, "served", unhealthy)
    operation = environment.operation(b"approved")
    result = invoke(operation)
    assert result["outcome"] == "applied"
    facts = result["provenance"]["facts"]
    assert facts["evaluation"]["status"] == "unhealthy"
    assert facts["observation"]["samples"][0]["digest"] != operation[1]["subject"]["artifact"]["digest"]
    operation[0].validate_result("invoke", operation[1], operation[2], result)
    assert delivery.CLAIMS["operational_health_established"] is False


@pytest.mark.parametrize("field", ["artifact", "environment", "workflow", "source", "generation"])
def test_actual_result_substitution_is_rejected(environment: Environment, field: str) -> None:
    operation = environment.operation(b"exact")
    result = invoke(operation)
    bad = copy.deepcopy(result)
    if field == "source":
        bad["provenance"]["facts"]["rollout"]["source"]["revision"] = "c" * 40
    elif field == "generation":
        bad["effect"]["generation"] = identity("different")
    else:
        bad["effect"][field] = {"changed": True} if field != "environment" else "production"
    with pytest.raises((ValueError, store.StoreError)):
        operation[0].validate_result("invoke", operation[1], operation[2], bad)


@pytest.mark.parametrize("point", ["intent", "slot", "rollout"])
def test_partial_create_failure_never_activates_or_retries(
    environment: Environment, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    operation = environment.operation(b"never active")
    original = owner._create
    suffix = {"intent": ".intent.json", "slot": ".artifact", "rollout": ".rollout.json"}[point]

    def fail(path: Path, content: bytes) -> None:
        if path.name.endswith(suffix):
            original(path, content[:10])
            raise OSError("controlled partial write")
        original(path, content)

    monkeypatch.setattr(owner, "_create", fail)
    result = invoke(operation)
    assert result["outcome"] == "uncertain"
    assert environment.owner.snapshot(deadline=store.Deadline(10))["rollout"] is None
    adapter, _request, payload, intent, _approval = operation
    before = {path.name: path.read_bytes() for path in environment.root.iterdir()}
    assert adapter.reconcile(intent, payload, deadline=store.Deadline(30))["outcome"] == "uncertain"
    assert before == {path.name: path.read_bytes() for path in environment.root.iterdir()}


def test_owner_key_and_observer_domains_are_separate(environment: Environment) -> None:
    result = invoke(environment.operation(b"exact"))
    record = result["provenance"]["facts"]["rollout"]
    with pytest.raises(ValueError, match=r"identity|purpose"):
        owner.verify_record(record, environment.observer_key)
    substituted = copy.deepcopy(environment.config)
    substituted["owner_key"] = substituted["observer"]["key"]
    with pytest.raises(ValueError, match=r"separate|distinct"):
        deploy.validate_configuration(substituted)


def test_owned_storage_is_private_and_never_resets(environment: Environment) -> None:
    result = invoke(environment.operation(b"private"))
    assert result["outcome"] == "applied"
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in environment.root.iterdir())
    with pytest.raises(ValueError, match="empty"):
        owner.initialize(
            environment.root,
            owner_id=environment.owner_id,
            application="docs",
            environment="test",
            workflow=environment.workflow,
        )


def test_no_pin_or_wrong_artifact_reader_cannot_start(environment: Environment) -> None:
    operation = environment.operation(b"approved")
    adapter, _request, payload, intent, approval = operation
    no_pin = deploy.LocalStaticDeploymentAdapter(environment.config, adapter.policy, artifact_reader=environment.reader)
    assert no_pin.invoke(intent, payload, approval, deadline=store.Deadline(30))["outcome"] == "rejected"
    adapter._reader = lambda *args, **kwargs: b"substitution"
    assert invoke(operation)["outcome"] == "rejected"
    assert not list(environment.root.glob("*.intent.json"))


def test_stale_generation_and_mutated_contract_are_blocked(environment: Environment) -> None:
    stale = environment.operation(b"stale")
    assert invoke(environment.operation(b"current"))["outcome"] == "applied"
    assert stale[0].inspect(stale[1], stale[2], deadline=store.Deadline(30))["ready"] is False
    assert invoke(stale)["outcome"] == "rejected"
    changed = copy.deepcopy(stale[0].policy)
    changed["contract"]["criteria"] = identity("weakened")
    with pytest.raises(ValueError, match="profile"):
        deploy.validate_payload(stale[1], stale[2], environment.config, changed)


def test_static_service_rejects_nonloopback_binding(environment: Environment) -> None:
    with pytest.raises(ValueError, match="loopback"):
        owner.StaticDeploymentServer(environment.owner, ("0.0.0.0", 0))


@pytest.mark.parametrize("after_cas", [False, True])
def test_real_process_kill_leaves_discoverable_or_fenced_owner(environment: Environment, after_cas: bool) -> None:
    operation = environment.operation(b"actual process-kill fixture")
    adapter, request, payload, intent, approval = operation
    script = str(Path(__file__).parent / "fixtures" / "static_deployment_kill.py")
    packet: Doc = {
        "configuration": environment.config,
        "policy": copy.deepcopy(adapter.policy),
        "intent": copy.deepcopy(intent),
        "payload": copy.deepcopy(payload),
        "approval": copy.deepcopy(approval),
        "after_cas": after_cas,
        "content": b"actual process-kill fixture".hex(),
    }
    # The actual installed controller execs fresh. Earlier tests may have loaded unrelated ELF
    # objects into this pytest process; those are intentionally part of this parent's own role.
    # Fingerprint and independently approve the exact fresh role rather than echoing parent pins.
    bootstrap = subprocess.run(
        [sys.executable, script, "fingerprints"],
        input=json.dumps(packet),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert bootstrap.returncode == 0, bootstrap.stderr
    actual = json.loads(bootstrap.stdout)
    policy = packet["policy"]
    policy["observer_configuration_digest"] = actual["observer_configuration_digest"]
    payload = packet["payload"]
    payload["operation_profile_digest"] = actual["operation_profile_digest"]
    request = copy.deepcopy(request)
    request["native_payload_digest"] = delivery.digest(payload)
    approval = packet["approval"]
    approval.update(
        policy_digest=delivery.digest(policy),
        request_digest=delivery.digest(request),
        subject_digest=delivery.digest(request["subject"]),
        resource_key=delivery.resource_key(request),
        adapter={"id": deploy.PROFILE, "configuration_digest": actual["configuration_digest"]},
    )
    intent = packet["intent"]
    intent.update(
        request=request,
        request_digest=delivery.digest(request),
        subject_digest=delivery.digest(request["subject"]),
        resource_key=delivery.resource_key(request),
        authority_digest=delivery.digest(approval),
        policy_digest=approval["policy_digest"],
        adapter=copy.deepcopy(approval["adapter"]),
    )
    intent["id"] = delivery.digest({name: value for name, value in intent.items() if name != "id"})
    delivery.validate_intent(intent, approval=approval)
    assert approval["limits"] == operation[4]["limits"] and approval["expires_at"] == operation[4]["expires_at"]
    child = subprocess.run(
        [sys.executable, script, "kill"],
        input=json.dumps(packet),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr
    before = {path.name: path.read_bytes() for path in environment.root.iterdir()}
    reconciled = subprocess.run(
        [sys.executable, script, "reconcile"],
        input=json.dumps(packet),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert reconciled.returncode == 0, reconciled.stderr
    result = json.loads(reconciled.stdout)
    assert result["outcome"] == ("applied" if after_cas else "uncertain")
    if after_cas:
        activation = result["provenance"]["facts"]["activation"]
        assert activation["kind"] == "active-ancestry"
        assert activation["generation"] == result["effect"]["generation"]
        facts = result["provenance"]["facts"]
        assert facts["observation"]["configuration_digest"] == policy["observer_configuration_digest"]
        assert facts["evaluation"]["status"] == facts["operational_status"]["status"] == "healthy"
    assert result["provenance"]["request_digest"] == delivery.digest(request)
    assert before == {path.name: path.read_bytes() for path in environment.root.iterdir()}
    assert len(list(environment.root.glob("*.intent.json"))) == 1


def test_healthy_observation_does_not_authorize_rollback(environment: Environment) -> None:
    prior = invoke(environment.operation(b"prior"))["provenance"]["facts"]["rollout"]
    current_result = invoke(environment.operation(b"current"))
    current = current_result["provenance"]["facts"]["rollout"]
    report = current_result["provenance"]["facts"]["observation"]
    conditions = {
        "current_rollout_id": current["rollout_id"],
        "prior_generation": prior["generation"],
        "failure_observation_digest": delivery.digest(report),
        "maximum_rollbacks": 1,
        "expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)).isoformat(),
    }
    operation = environment.operation(b"prior", stage="rollback", rollback=conditions, failure=report)
    adapter, request, payload, _intent, _approval = operation
    assert adapter.inspect(request, payload, deadline=store.Deadline(30))["ready"] is False
    assert invoke(operation)["outcome"] == "rejected"
    assert len(list(environment.root.glob("*.intent.json"))) == 2


def test_prepared_rollout_without_activation_cannot_validate_as_applied(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = environment.operation(b"only staged")
    monkeypatch.setattr(owner, "_replace", lambda *args: (_ for _ in ()).throw(OSError("before activation")))
    assert invoke(operation)["outcome"] == "uncertain"
    adapter, request, payload, _intent, _approval = operation
    record = owner._document(next(environment.root.glob("*.rollout.json")))
    owner.verify_record(record, environment.owner_key)
    forged = {
        "outcome": "applied",
        "effect": deploy.effect(record),
        "reason": "staged does not prove applied",
        "provenance": adapter._proof("invoke", request, payload, rollout=record, started=True),
    }
    with pytest.raises(ValueError, match="prepared"):
        adapter.validate_result("invoke", request, payload, forged)


def test_activation_attestation_uses_distinct_domain_and_exact_record(environment: Environment) -> None:
    operation = environment.operation(b"activated")
    result = invoke(operation)
    record = result["provenance"]["facts"]["rollout"]
    activation = result["provenance"]["facts"]["activation"]
    assert owner.verify_activation(activation, record, environment.owner_key) == activation
    body = {name: value for name, value in activation.items() if name not in ("key_id", "signature")}
    wrong_domain = owner._sign(body, environment.owner_key)
    with pytest.raises(ValueError, match="authentication"):
        owner.verify_activation(wrong_domain, record, environment.owner_key)
    changed = copy.deepcopy(result)
    changed["provenance"]["facts"]["activation"]["record_digest"] = identity("different staged record")
    with pytest.raises(ValueError, match="exact"):
        operation[0].validate_result("invoke", operation[1], operation[2], changed)


def test_installed_source_runtime_and_schema_are_pinned(environment: Environment) -> None:
    adapter = environment.operation(b"profile")[0]
    profile = deploy.installed_profile()
    assert {
        "deployment_automation.py",
        "local_static_deployment.py",
        "operational_observation.py",
        "delivery_automation.py",
    } <= profile["implementation"].keys()
    assert profile["validation_runtime"]["inputs"]
    assert profile["delivery_runtime"]["stdlib"] and profile["delivery_runtime"]["native"]
    assert profile["delivery_runtime"]["transport"]["tls_trust_roots"]
    changed = copy.deepcopy(environment.config)
    changed["observer"]["max_latency_ms"] += 1
    assert deploy.configuration_digest(changed, adapter.policy) != adapter.configuration_digest


def test_known_unobservable_artifact_is_blocked_before_any_effect(environment: Environment) -> None:
    environment.config["observer"]["max_artifact_bytes"] = 2
    operation = environment.operation(b"larger than the approved observation ceiling")
    with pytest.raises(ValueError, match="observer byte ceiling"):
        operation[0].inspect(operation[1], operation[2], deadline=store.Deadline(30))
    assert not list(environment.root.glob("*.intent.json"))
    assert environment.owner.snapshot(deadline=store.Deadline(10))["rollout"] is None


@pytest.mark.parametrize("method", ["invoke", "reconcile"])
def test_real_health_expiry_never_invalidates_an_authenticated_applied_effect(
    environment: Environment, method: str
) -> None:
    environment.config["observer"]["max_age_ms"] = 50
    operation = environment.operation(b"activated effect outlives health window")
    adapter, request, payload, intent, _approval = operation
    result = invoke(operation)
    if method == "reconcile":
        result = adapter.reconcile(intent, payload, deadline=store.Deadline(30))
    assert result["outcome"] == "applied"
    facts = result["provenance"]["facts"]
    report = copy.deepcopy(facts["observation"])
    assert facts["evaluation"]["status"] == "healthy"
    assert facts["evaluation"]["scope"] == "historical-static-measurements"
    assert facts["evaluation"]["current_readiness_established"] is False
    expiry = delivery._timestamp(report["window"]["expires_at"])
    remaining = (expiry - dt.datetime.now(dt.UTC)).total_seconds()
    if remaining > 0:
        time.sleep(remaining + 0.002)
    before = {path.name: path.read_bytes() for path in environment.root.iterdir()}
    assert adapter.validate_result(method, request, payload, result) == result
    assert facts["observation"] == report
    current = adapter.retained_operational_status(request, payload, result["provenance"])
    assert current["status"] == "unknown" and current["freshness"] == "stale"
    assert current["historical_status"] == "healthy" and current["current_readiness_established"] is False
    with pytest.raises(store.StoreError, match="stale"):
        observer.verify(
            report,
            deploy.expected_observation(facts["rollout"]),
            environment.config["observer"],
            now=dt.datetime.now(dt.UTC),
        )
    assert environment.owner.snapshot(deadline=store.Deadline(10))["rollout"]["intent_id"] == intent["id"]
    assert before == {path.name: path.read_bytes() for path in environment.root.iterdir()}


def test_expired_authenticated_unhealthy_measurements_cannot_authorize_rollback(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment.config["observer"]["max_age_ms"] = 50
    prior = invoke(environment.operation(b"prior"))["provenance"]["facts"]["rollout"]
    current = invoke(environment.operation(b"current"))["provenance"]["facts"]["rollout"]
    original = environment.owner.served

    def unhealthy(*, deadline: store.Deadline) -> tuple[Doc, bytes]:
        record, _content = original(deadline=deadline)
        return record, b"unhealthy real response bytes"

    monkeypatch.setattr(environment.owner, "served", unhealthy)
    report = observer.observe(
        deploy.expected_observation(current), environment.config["observer"], deadline=store.Deadline(10)
    )
    expiry = delivery._timestamp(report["window"]["expires_at"])
    remaining = (expiry - dt.datetime.now(dt.UTC)).total_seconds()
    if remaining > 0:
        time.sleep(remaining + 0.002)
    assert (
        observer.authenticate(
            report, deploy.expected_observation(current), environment.config["observer"], now=dt.datetime.now(dt.UTC)
        )["status"]
        == "unhealthy"
    )
    conditions = {
        "current_rollout_id": current["rollout_id"],
        "prior_generation": prior["generation"],
        "failure_observation_digest": delivery.digest(report),
        "maximum_rollbacks": 1,
        "expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)).isoformat(),
    }
    operation = environment.operation(b"prior", stage="rollback", rollback=conditions, failure=report)
    adapter, request, payload, _intent, _approval = operation
    assert adapter.inspect(request, payload, deadline=store.Deadline(30))["ready"] is False
    assert invoke(operation)["outcome"] == "rejected"
    assert len(list(environment.root.glob("*.intent.json"))) == 2
    assert environment.owner.snapshot(deadline=store.Deadline(10))["generation"] == current["generation"]


def _published_artifact(root: Path, content: bytes, *, version: str, previous: Doc | None = None) -> Doc:
    """Run the real fixed owning publisher with a separately selected exact publication grant."""
    from exacly import release_publication as publication
    from release_publication_fixtures import make_case, retained

    case: Doc = make_case(root, data=content)
    if previous is not None:
        case["owner"]["registry"] = copy.deepcopy(previous["owner"]["registry"])
        case["owner"]["key"] = copy.deepcopy(previous["owner"]["key"])
    subject = case["request"]["subject"]
    subject["version"] = version
    subject["destination"]["registry"] = "local-release-registry:" + case["owner"]["registry"]["id"]
    key = delivery.digest({"publish": subject})
    case["request"]["operation_key"] = key
    case["owner"]["scope"] = {"objective": case["request"]["objective"], "operation_key": key, "subject": subject}
    case["configuration"]["owner_configuration"]["digest"] = retained(
        Path(case["configuration"]["owner_configuration"]["path"]), case["owner"]
    )
    adapter = publication.ReleasePublicationAdapter(case["configuration"], case["policy"])
    case["adapter"] = adapter
    case["payload"]["operation_profile_digest"] = adapter.operation_profile_digest
    request, approval, intent = case["request"], case["approval"], case["intent"]
    request["native_payload_digest"] = delivery.digest(case["payload"])
    approval.update(
        operation_key=key,
        request_digest=delivery.digest(request),
        subject_digest=delivery.digest(subject),
        resource_key=delivery.resource_key(request),
        adapter={"id": adapter.adapter_id, "configuration_digest": adapter.configuration_digest},
    )
    intent.update(
        request=request,
        request_digest=delivery.digest(request),
        subject_digest=delivery.digest(subject),
        resource_key=delivery.resource_key(request),
        authority_digest=delivery.digest(approval),
        adapter=copy.deepcopy(approval["adapter"]),
    )
    intent["id"] = delivery.digest({name: value for name, value in intent.items() if name != "id"})
    delivery.validate_intent(intent, approval=approval)
    result = adapter.invoke(intent, case["payload"], approval, deadline=store.Deadline(120))
    assert result["outcome"] == "applied", result
    case["publication_result"] = result
    return case


def _select_publication(environment: Environment, case: Doc) -> None:
    environment.config["registry"] = copy.deepcopy(case["owner"]["registry"])
    environment.config["publisher_key"] = copy.deepcopy(case["owner"]["key"])
    environment.config["publisher_workflow"] = copy.deepcopy(case["request"]["subject"]["workflow"])
    environment.publication_verification_contract = copy.deepcopy(case["owner"]["contract"])
    environment.publication_verification_producer = copy.deepcopy(case["owner"]["verification_producer"])


def _published_operation(
    environment: Environment, case: Doc, **kwargs: Any
) -> tuple[deploy.LocalStaticDeploymentAdapter, Doc, Doc, Doc, Doc]:
    subject = case["request"]["subject"]
    return environment.operation(
        case["artifact_bytes"],
        artifact_name=subject["artifacts"][0]["name"],
        source=subject["source"],
        package=subject["destination"]["package"],
        version=subject["version"],
        default_reader=True,
        **kwargs,
    )


def _tree_bytes(root: Path) -> dict[str, tuple[bytes | None, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None, path.stat().st_mode & 0o777)
        for path in root.rglob("*")
    }


def test_real_owning_publication_default_deployment_reader_and_bounded_rollback(
    environment: Environment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior_publication = _published_artifact(
        tmp_path / "prior-publication", b"real prior published opaque bytes\x00\xff", version="1.2.3"
    )
    _select_publication(environment, prior_publication)
    prior_operation = _published_operation(environment, prior_publication)
    assert prior_operation[0]._reader is deploy._published_bytes
    assert prior_operation[0].policy["contract"] != prior_operation[0].policy["publication_verification_contract"]
    assert prior_operation[4]["stage"] == "deploy" and prior_publication["approval"]["stage"] == "publish"
    assert prior_operation[4]["journal"] != prior_publication["approval"]["journal"]
    prior_result = invoke(prior_operation)
    assert prior_result["outcome"] == "applied"
    assert (
        prior_operation[0].retained_operational_status(
            prior_operation[1], prior_operation[2], prior_result["provenance"]
        )["status"]
        == "healthy"
    )
    prior = prior_result["provenance"]["facts"]["rollout"]
    current_publication = _published_artifact(
        tmp_path / "current-publication", b"real current published bytes", version="1.2.4", previous=prior_publication
    )
    current_operation = _published_operation(environment, current_publication)
    current_result = invoke(current_operation)
    assert current_result["outcome"] == "applied"
    current = current_result["provenance"]["facts"]["rollout"]
    original = environment.owner.served

    def unhealthy(*, deadline: store.Deadline) -> tuple[Doc, bytes]:
        record, _content = original(deadline=deadline)
        return record, b"actual broken static response"

    monkeypatch.setattr(environment.owner, "served", unhealthy)
    failure = observer.observe(
        deploy.expected_observation(current), environment.config["observer"], deadline=store.Deadline(120)
    )
    assert (
        observer.verify(
            failure, deploy.expected_observation(current), environment.config["observer"], now=dt.datetime.now(dt.UTC)
        )["status"]
        == "unhealthy"
    )
    conditions = {
        "current_rollout_id": current["rollout_id"],
        "prior_generation": prior["generation"],
        "failure_observation_digest": delivery.digest(failure),
        "maximum_rollbacks": 1,
        "expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)).isoformat(),
    }
    rollback = _published_operation(
        environment, prior_publication, stage="rollback", rollback=conditions, failure=failure
    )
    assert rollback[4]["stage"] == "rollback" and rollback[4]["limits"]["rollbacks"] == 1
    monkeypatch.setattr(environment.owner, "served", original)
    result = invoke(rollback)
    assert result["outcome"] == "applied"
    assert result["effect"]["artifact"] == prior_publication["request"]["subject"]["artifacts"][0]
    assert result["effect"]["generation"] != prior["generation"]
    assert (
        rollback[0].retained_operational_status(rollback[1], rollback[2], result["provenance"])["status"] == "healthy"
    )
    assert environment.owner.served(deadline=store.Deadline(10))[1] == prior_publication["artifact_bytes"]


@pytest.mark.parametrize("wrong", ["contract", "producer"])
def test_default_consumer_rejects_unapproved_publication_acceptance_before_effect(
    environment: Environment, tmp_path: Path, wrong: str
) -> None:
    published = _published_artifact(
        tmp_path / "publication", b"real independently verified publication", version="1.2.3"
    )
    _select_publication(environment, published)
    if wrong == "contract":
        environment.publication_verification_contract["criteria"] = identity("different-artifact-acceptance-meaning")
    else:
        environment.publication_verification_producer["revision"] = "d" * 40
    operation = _published_operation(environment, published)
    owner_before = _tree_bytes(environment.root)
    registry = Path(published["owner"]["registry"]["path"])
    registry_before = _tree_bytes(registry)
    assert operation[0].inspect(operation[1], operation[2], deadline=store.Deadline(120))["ready"] is False
    assert invoke(operation)["outcome"] == "rejected"
    assert _tree_bytes(environment.root) == owner_before
    assert _tree_bytes(registry) == registry_before
    assert not list(environment.root.glob("*.intent.json"))


@pytest.mark.parametrize("method", ["invoke", "reconcile"])
def test_genuine_foreign_intent_activation_never_completes_the_requested_intent(
    environment: Environment, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    operation = environment.operation(b"one exact request under distinct independent grants")
    adapter, request, payload, intent, approval = operation
    initial = environment.owner.snapshot(deadline=store.Deadline(10))
    other_approval = copy.deepcopy(approval)
    other_approval["expires_at"] = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=7)).isoformat()
    other_approval["journal"]["id"] = uuid.uuid4().hex
    other_intent = copy.deepcopy(intent)
    other_intent["authority_digest"] = delivery.digest(other_approval)
    other_intent["journal"] = copy.deepcopy(other_approval["journal"])
    other_intent["id"] = delivery.digest({name: value for name, value in other_intent.items() if name != "id"})
    delivery.validate_intent(other_intent, approval=other_approval)
    assert other_intent["id"] != intent["id"] and other_intent["request"] == intent["request"]
    other_pin = delivery.ApprovalPin(
        other_approval["issuer"], other_approval["approval_id"], delivery.digest(other_approval)
    )
    other_adapter = deploy.LocalStaticDeploymentAdapter(
        environment.config, adapter.policy, approval_pin=other_pin, artifact_reader=environment.reader
    )
    foreign = other_adapter.invoke(other_intent, payload, other_approval, deadline=store.Deadline(120))
    assert foreign["outcome"] == "applied"
    facts = foreign["provenance"]["facts"]
    record, activation = facts["rollout"], facts["activation"]
    assert record["intent_id"] == activation["intent_id"] == other_intent["id"]
    owner.verify_record(record, environment.owner_key)
    owner.verify_activation(activation, record, environment.owner_key)
    before = _tree_bytes(environment.root)

    if method == "invoke":
        # Faulty transport/owner returns B's genuine response after A observed stale preconditions.
        monkeypatch.setattr(owner.LocalStaticOwner, "snapshot", lambda *args, **kwargs: copy.deepcopy(initial))
        monkeypatch.setattr(owner.LocalStaticOwner, "apply", lambda *args, **kwargs: (record, activation))
        result = adapter.invoke(intent, payload, approval, deadline=store.Deadline(120))
    else:
        monkeypatch.setattr(owner.LocalStaticOwner, "discover", lambda *args, **kwargs: (record, activation))
        result = adapter.reconcile(intent, payload, deadline=store.Deadline(120))
    assert result["outcome"] == "uncertain" and result["effect"] is None
    assert result["provenance"]["facts"]["rollout"] == record
    assert result["provenance"]["facts"]["activation"] == activation
    assert result["provenance"]["facts"]["observation"] is None
    assert adapter.validate_result(method, request, payload, result) == result
    assert _tree_bytes(environment.root) == before


@pytest.mark.parametrize("placement", ["owner", "nested", "alias"])
def test_observer_key_cannot_share_the_owning_workflow_lock_namespace(
    environment: Environment, tmp_path: Path, placement: str
) -> None:
    original = Path(environment.config["observer"]["key"]["path"])
    destination = environment.root / "observer.key"
    if placement == "nested":
        destination = _private(environment.root / "observer-storage") / "observer.key"
    original.rename(destination)
    if placement == "alias":
        alias = tmp_path / "owner-alias"
        alias.symlink_to(environment.root, target_is_directory=True)
        destination = alias / "observer.key"
    environment.config["observer"]["key"]["path"] = str(destination)
    before = _tree_bytes(environment.root)
    with pytest.raises(ValueError, match=r"outside the owning workflow root|aliased lock domains"):
        deploy.validate_configuration(environment.config)
    with pytest.raises(ValueError, match=r"outside the owning workflow root|aliased lock domains"):
        environment.operation(b"must never dispatch with an observer lock alias")
    assert not list(environment.root.glob("*.intent.json"))
    assert environment.owner.snapshot(deadline=store.Deadline(10))["rollout"] is None
    assert _tree_bytes(environment.root) == before


def test_missing_live_observer_key_blocks_before_any_owner_intent(environment: Environment) -> None:
    operation = environment.operation(b"key existed at independent approval")
    Path(environment.config["observer"]["key"]["path"]).unlink()
    before = _tree_bytes(environment.root)
    assert operation[0].inspect(operation[1], operation[2], deadline=store.Deadline(30))["ready"] is False
    assert invoke(operation)["outcome"] == "rejected"
    assert not list(environment.root.glob("*.intent.json"))
    assert _tree_bytes(environment.root) == before


@pytest.mark.parametrize("phase", ["inspect", "invoke", "reconcile"])
def test_native_read_ceiling_includes_the_final_generation_snapshot(environment: Environment, phase: str) -> None:
    environment.config["call_limits"][phase]["read_calls"] = environment.config["observer"]["samples"] + 4
    before = _tree_bytes(environment.root)
    with pytest.raises(ValueError, match="read ceiling"):
        environment.operation(b"insufficient ceiling must block before effects")
    assert not list(environment.root.glob("*.intent.json"))
    assert _tree_bytes(environment.root) == before


def test_live_generation_read_is_accounted_and_cannot_reuse_a_superseded_report(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = environment.operation(b"rollout A")
    first_result = invoke(first)
    adapter, request, payload, intent, _approval = first
    original = copy.deepcopy(first_result)
    native_point = first_result["provenance"]["facts"]["operational_status"]
    assert native_point["status"] == "healthy" and native_point["freshness"] == "current"
    second_result = invoke(environment.operation(b"rollout B"))
    assert second_result["outcome"] == "applied"
    before = _tree_bytes(environment.root)
    charges: list[int] = []
    reads: list[str] = []
    discover = owner.LocalStaticOwner.discover

    def reserve() -> None:
        if charges:
            raise ValueError("caller-owned live read budget exhausted")
        charges.append(1)

    def counted(
        selected: owner.LocalStaticOwner, intent_id: str, *, deadline: store.Deadline
    ) -> tuple[Doc, Doc] | None:
        assert charges == [1]
        reads.append(intent_id)
        return discover(selected, intent_id, deadline=deadline)

    monkeypatch.setattr(owner.LocalStaticOwner, "discover", counted)
    scope = deploy.OperationalReadScope(environment.owner_id, "docs", "test", reserve)
    status = adapter.operational_status(
        request, payload, first_result["provenance"], deadline=store.Deadline(120), read_scope=scope
    )
    assert charges == [1] and reads == [intent["id"]]
    assert status["status"] == "unknown" and status["freshness"] == "superseded"
    assert status["current_readiness_established"] is False
    assert status["generation"]["expected"] == first_result["effect"]["generation"]
    assert status["generation"]["observed"] == second_result["effect"]["generation"]
    with pytest.raises(ValueError, match="budget exhausted"):
        adapter.operational_status(
            request, payload, first_result["provenance"], deadline=store.Deadline(120), read_scope=scope
        )
    assert reads == [intent["id"]]

    # Presentation and outcome binding cannot silently perform another live read or key/profile IO.
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("pure retained presentation/result validation performed IO")

    monkeypatch.setattr(owner.LocalStaticOwner, "discover", forbidden)
    monkeypatch.setattr(owner, "_key", forbidden)
    monkeypatch.setattr(observer, "authenticate", forbidden)
    monkeypatch.setattr(delivery_profile, "implementation", forbidden)
    retained = adapter.retained_operational_status(request, payload, first_result["provenance"])
    assert retained["evaluated_at"] == native_point["evaluated_at"]
    assert retained["generation"] == native_point["generation"]
    assert retained["scope"] == "as-of-recorded-environment-observation"
    assert adapter.validate_result("invoke", request, payload, first_result) == first_result
    assert first_result == original
    assert _tree_bytes(environment.root) == before


def test_native_final_snapshot_blocks_health_when_another_workflow_supersedes_the_rollout(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = environment.operation(b"sampled rollout A")
    adapter, request, payload, _intent, _approval = operation
    refresh = adapter._refresh_activation
    competing: list[Doc] = []

    def after_competing_workflow(record: Doc, intent_id: str, *, deadline: store.Deadline) -> Doc | None:
        # A separate approved writer completes after A's sampling/authentication, before its final read.
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(invoke, environment.operation(b"separately authorized rollout B")).result(
                timeout=60
            )
        assert result["outcome"] == "applied"
        competing.append(result)
        return refresh(record, intent_id, deadline=deadline)

    monkeypatch.setattr(adapter, "_refresh_activation", after_competing_workflow)
    result = invoke(operation)
    assert result["outcome"] == "applied"
    facts = result["provenance"]["facts"]
    assert facts["evaluation"]["status"] == "healthy"
    assert facts["operational_status"]["status"] == "unknown"
    assert facts["operational_status"]["freshness"] == "superseded"
    assert facts["current_activation"]["active_generation"] == competing[0]["effect"]["generation"]
    assert facts["current_activation"]["generation"] == result["effect"]["generation"]
    adapter.validate_provenance("invoke", request, payload, result["provenance"])
    assert adapter.validate_result("invoke", request, payload, result) == result
    assert adapter.retained_operational_status(request, payload, result["provenance"])["freshness"] == "superseded"


@pytest.mark.parametrize("forged_point", ["evaluation", "activation"])
def test_authenticating_native_boundary_rejects_future_operational_points(
    environment: Environment, forged_point: str
) -> None:
    operation = environment.operation(b"genuine activated effect and measurements")
    adapter, request, payload, _intent, _approval = operation
    result = invoke(operation)
    assert result["outcome"] == "applied"
    forged = copy.deepcopy(result)
    facts = forged["provenance"]["facts"]
    future = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=10)
    if forged_point == "activation":
        changed = {
            name: value for name, value in facts["current_activation"].items() if name not in ("key_id", "signature")
        }
        changed["observed_at"] = future.isoformat()
        facts["current_activation"] = owner._sign(changed, environment.owner_key, domain=owner.ACTIVATION_DOMAIN)
    facts["operational_status"] = deploy._point_status(
        facts["rollout"], facts["current_activation"], facts["observation"], facts["evaluation"], evaluated_at=future
    )
    with pytest.raises(ValueError, match="future"):
        adapter.validate_provenance("invoke", request, payload, forged["provenance"])
    assert adapter.validate_provenance("invoke", request, payload, result["provenance"]) == result["provenance"]
    assert adapter.validate_result("invoke", request, payload, result) == result
