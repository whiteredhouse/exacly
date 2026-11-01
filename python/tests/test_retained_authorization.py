"""Historical data parsing is independent of expired host storage and never authorizes effects."""

from __future__ import annotations

import copy
import inspect
import os
from pathlib import Path
from typing import Any, NoReturn

import pytest
from exacly import automation_authorization as standing
from exacly import execution_store, planning
from test_standing_authorization_scope import Scenario
from test_standing_authorization_scope import scenario as scenario

Doc = dict[str, Any]


def test_retained_delegation_and_facts_survive_the_original_private_store_and_key_paths(scenario: Scenario) -> None:
    scenario.authorizer()
    key = scenario.config_path.parent / "publication-key"
    key.write_bytes(b"controlled fixture key")
    key.chmod(0o600)
    original_delegation = copy.deepcopy(scenario.config)
    original_facts = copy.deepcopy(scenario.effect.facts)
    original_facts["authorization"]["retained_key_locator"] = str(key)
    old_directory = scenario.config_path.parent
    old_directory.rename(old_directory.with_name("retired-private-host"))

    parsed_delegation = standing.parse_retained_delegation(original_delegation)
    parsed_facts = standing.parse_retained_facts(original_facts)
    assert parsed_delegation == original_delegation
    assert parsed_facts == original_facts
    assert standing.digest(parsed_delegation) == standing.digest(original_delegation)
    assert standing.digest(parsed_facts) == standing.digest(original_facts)
    assert not old_directory.exists() and not key.exists()
    with pytest.raises((execution_store.StoreError, OSError)):
        standing.validate_delegation(original_delegation)
    with pytest.raises((execution_store.StoreError, OSError)):
        standing.validate_facts(original_facts)
    with pytest.raises((execution_store.StoreError, OSError)):
        standing.Authorizer(
            scenario.config_path,
            approved_digest=standing.digest(original_delegation),
            issuer="independent-maintainer",
            approval_id="reviewed-packet-001",
            effects={"execute": scenario.effect},
        )
    assert scenario.effect.invocations == 0


def test_retained_parsers_make_no_filesystem_calls(scenario: Scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("retained parsing tried to inspect the current filesystem")

    with monkeypatch.context() as guard:
        guard.setattr(Path, "resolve", forbidden)
        guard.setattr(Path, "lstat", forbidden)
        guard.setattr(os, "open", forbidden)
        guard.setattr(execution_store, "location", forbidden)
        assert standing.parse_retained_delegation(scenario.config) == scenario.config
        assert standing.parse_retained_facts(scenario.effect.facts) == scenario.effect.facts


def test_lexically_valid_retained_paths_make_no_current_symlink_or_ownership_claim(scenario: Scenario) -> None:
    scenario.authorizer()
    alias = scenario.config_path.parent.with_name("old-host-alias")
    alias.symlink_to(scenario.config_path.parent, target_is_directory=True)
    delegation = copy.deepcopy(scenario.config)
    delegation["config_path"] = str(alias / "delegation.json")
    facts = copy.deepcopy(scenario.effect.facts)
    facts["native"]["store"]["path"] = str(alias / "native-intent.json")
    assert standing.parse_retained_delegation(delegation) == delegation
    assert standing.parse_retained_facts(facts) == facts
    with pytest.raises(execution_store.StoreError, match="symbolic links"):
        standing.validate_delegation(delegation)
    with pytest.raises(execution_store.StoreError, match="symbolic links"):
        standing.validate_facts(facts)


def test_runtime_authorizer_still_rejects_current_unsafe_storage_after_successful_retained_parse(
    scenario: Scenario,
) -> None:
    scenario.authorizer()
    scenario.config_path.parent.chmod(0o755)
    assert standing.parse_retained_delegation(scenario.config) == scenario.config
    assert standing.parse_retained_facts(scenario.effect.facts) == scenario.effect.facts
    with pytest.raises(execution_store.StoreError, match="0700"):
        standing.validate_delegation(scenario.config)
    with pytest.raises(execution_store.StoreError, match="0700"):
        standing.validate_facts(scenario.effect.facts)
    with pytest.raises(execution_store.StoreError, match="0700"):
        scenario.authorizer()
    assert scenario.effect.invocations == 0


def test_runtime_release_never_uses_retained_parsers(scenario: Scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(value: Any) -> NoReturn:
        raise AssertionError("runtime release called a historical data parser")

    monkeypatch.setattr(standing, "parse_retained_delegation", forbidden)
    monkeypatch.setattr(standing, "parse_retained_facts", forbidden)
    authorizer = scenario.authorizer()
    assert scenario.execute(authorizer)["status"] == "completed"
    assert scenario.effect.invocations == 1


def test_public_runtime_signatures_have_no_storage_bypass_parameter() -> None:
    assert set(inspect.signature(standing.validate_delegation).parameters) == {"value"}
    assert set(inspect.signature(standing.validate_facts).parameters) == {"value"}
    assert set(inspect.signature(standing.parse_retained_delegation).parameters) == {"value"}
    assert set(inspect.signature(standing.parse_retained_facts).parameters) == {"value"}
    assert "check_storage" not in inspect.signature(standing.Authorizer).parameters
    assert "path_validator" not in inspect.signature(standing.Authorizer).parameters


@pytest.mark.parametrize(
    "path",
    [
        "relative/store",
        "",
        "/",
        "//ambiguous/root",
        "/one//two",
        "/one/./two",
        "/one/../two",
        "/one/two/",
        "/nul\0path",
    ],
)
def test_retained_paths_reject_noncanonical_or_ambiguous_absolute_locators(scenario: Scenario, path: str) -> None:
    delegation = copy.deepcopy(scenario.config)
    delegation["config_path"] = path
    with pytest.raises(execution_store.StoreError):
        standing.parse_retained_delegation(delegation)
    facts = copy.deepcopy(scenario.effect.facts)
    facts["native"]["store"]["path"] = path
    with pytest.raises(execution_store.StoreError):
        standing.parse_retained_facts(facts)


@pytest.mark.parametrize(
    "damage",
    [
        "field",
        "version",
        "store_uuid",
        "revocation_uuid",
        "native_identity",
        "native_pin",
        "budget",
        "capability",
        "source",
        "lifetime",
        "reader",
    ],
)
def test_retained_delegation_keeps_all_nonfilesystem_contract_checks(scenario: Scenario, damage: str) -> None:
    value = copy.deepcopy(scenario.config)
    if damage == "field":
        value["approval_bypass"] = True
    elif damage == "version":
        value["schema_version"] = "999"
    elif damage == "store_uuid":
        value["accounting"]["store_id"] = "0" * 32
    elif damage == "revocation_uuid":
        value["revocations"]["store_id"] = planning.digest("not a store UUID")
    elif damage == "native_identity":
        value["effects"]["execute"]["native_store"]["id"] = "uninitialized"
    elif damage == "native_pin":
        value["native_configuration_digest"] = "self-approved"
    elif damage == "budget":
        value["limits"]["wall_time_ms"] = True
    elif damage == "capability":
        value["effects"]["execute"]["capabilities"] = ["network-anywhere"]
    elif damage == "source":
        value["allowed_paths"] = ["python/src/exacly/"]
    elif damage == "lifetime":
        value["expires_at"] = value["not_before"]
    else:
        value["reads"] = {
            "authority-preflight": {
                "adapter_id": "candidate-provider",
                "contract_digest": planning.digest("contract"),
                "max_wall_time_ms": 100,
            }
        }
    with pytest.raises(execution_store.StoreError):
        standing.parse_retained_delegation(value)


@pytest.mark.parametrize(
    "damage",
    [
        "field",
        "version",
        "objective",
        "store",
        "native_pin",
        "capability",
        "budget",
        "model_service",
        "authorization",
        "source",
        "destination",
    ],
)
def test_retained_facts_keep_all_nonfilesystem_contract_checks(scenario: Scenario, damage: str) -> None:
    value = copy.deepcopy(scenario.effect.facts)
    if damage == "field":
        value["trusted"] = True
    elif damage == "version":
        value["schema_version"] = "999"
    elif damage == "objective":
        value["objective_id"] = "not a digest"
    elif damage == "store":
        value["native"]["store"]["id"] = "not a store identity"
    elif damage == "native_pin":
        value["preconditions"].pop("native_configuration_digest")
    elif damage == "capability":
        value["capabilities"] = ["model-service-anywhere"]
    elif damage == "budget":
        value["cost"]["wall_time_ms"] = False
    elif damage == "model_service":
        value["cost"]["model_requests"] = 1
    elif damage == "authorization":
        value["authorization"] = {}
    elif damage == "source":
        value["source_paths"] = ["../private-key"]
    else:
        value["destination"] = {"repository_id": value["repository"]["id"], "ref": "refs/heads/main"}
    with pytest.raises(execution_store.StoreError):
        standing.parse_retained_facts(value)


def test_retained_schema_does_not_make_differently_scoped_facts_authorized(scenario: Scenario) -> None:
    authorizer = scenario.authorizer()
    altered = copy.deepcopy(scenario.effect.facts)
    altered["contract"]["criteria"] = planning.digest("different acceptance criteria")
    assert standing.parse_retained_facts(altered) == altered  # A well-shaped historical claim only.
    scenario.effect.facts = altered
    assert scenario.execute(authorizer)["status"] == "denied"
    assert scenario.effect.invocations == 0


def test_retained_delegation_preserves_all_four_added_closed_reader_identities(scenario: Scenario) -> None:
    readers = {
        "authority-preflight": "req039-authority-preflight-v1",
        "negative-admission": "native-negative-admission-v1",
        "budget-exhaustion": "req039-budget-exhaustion-v1",
        "remote-precondition": "native-remote-precondition-v1",
    }
    assert {key: standing.READERS[key] for key in readers} == readers
    value = copy.deepcopy(scenario.config)
    value["reads"] = {
        key: {"adapter_id": adapter, "contract_digest": planning.digest(key), "max_wall_time_ms": 100}
        for key, adapter in readers.items()
    }
    assert standing.parse_retained_delegation(value) == value
