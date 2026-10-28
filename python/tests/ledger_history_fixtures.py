"""Development-only canonical ledger histories for scalability measurements.

Controlled evidence fixtures pass through a normal completion first. Synthetic subsequent events
recompute every identity and proof association, preserve one shared state's serial/lineage history,
and receive full production validation before measurement. They grant no production authority.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import sqlite3
import stat
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from exacly import admission, completion, completion_state, completion_store, executions, planning, state_writer
from exacly import execution_store as store
from exacly.refresh import Run
from test_admission import END, Source, bundle
from test_completion import prepare_scenario
from test_reconciliation import update_model
from test_refresh import config, requirement

Doc = dict[str, Any]
BASE_TIME = dt.datetime(2026, 10, 26, 9, 15, tzinfo=dt.UTC)


@dataclass(frozen=True)
class History:
    ledger: Path
    state: Path
    completion_ids: tuple[str, ...]
    bindings: int
    tasks: int
    testcases: int
    requirements: int
    backlog: bool


def report(cases: int, requirements: int, run_id: int) -> bytes:
    """Create actual passing testcases and distinct deterministic diagnostic output per run."""
    root = ET.Element("testsuite", name="controlled-contract-checks", tests=str(cases))
    for index in range(cases):
        rid = index % requirements + 1
        item = ET.SubElement(
            root,
            "testcase",
            file="checks/test_contract.py",
            name=f"test_requirement_{rid:04d}_case_{index:06d}",
            time="0.001",
        )
        ET.SubElement(item, "system-out").text = json.dumps(
            {
                "case": index,
                "requirement": rid,
                "run": run_id,
                "result": hashlib.sha256(f"{run_id}:{index}".encode()).hexdigest(),
            },
            sort_keys=True,
        )
    return bytes(ET.tostring(root, encoding="utf-8"))


def source(cases: int, requirements: int) -> Source:
    if cases < 1 or requirements < 1 or requirements > cases:
        raise ValueError("require 1 <= requirements <= testcases")
    result = Source(layout="custom", report=report(cases, requirements, 777))
    records = [
        requirement(
            f"REQ-{index}",
            {
                "workflow": result.workflow_path,
                "job": result.job_id,
                "cases": [f"checks/test_contract.py::test_requirement_{index:04d}_case_*"],
            },
        )
        for index in range(1, requirements + 1)
    ]
    update_model(result, config(*records))
    return result


def timestamp(index: int) -> str:
    return (BASE_TIME + dt.timedelta(seconds=index)).isoformat().replace("+00:00", "Z")


def pending(index: int) -> Doc:
    return {
        "record": "REQ-1",
        "task": f"issues/benchmark-{index + 1}",
        "opened_at": "2026-10-26T08:00:00Z",
        "dimension": "verification",
    }


def binding_chain(template: Doc, state: Doc, selected: Doc, depth: int) -> list[Doc]:
    """Create legitimate supersessions for one pending task; the final contract selects B."""
    previous: str | None = None
    result: list[Doc] = []
    for stage in range(depth):
        value = copy.deepcopy(template)
        identity, _ = executions.selected_pending(state, "REQ-1", selected["task"], selected["opened_at"], False)
        value["contract"]["pending"] = identity
        value["pending_key"] = planning.digest({key: identity[key] for key in executions.IDENTITY_KEYS})
        if stage < depth - 1:
            value["contract"]["target"]["revision"] = hashlib.sha1(
                f"controlled-prior-output:{selected['task']}:{stage}".encode()
            ).hexdigest()
        value["state_snapshot"] = {
            "serial": state["serial"],
            "digest": planning.digest(state),
            "config_revision": state["config_revision"],
        }
        value["supersedes"] = previous
        value["id"] = executions.record_identity(value)
        value["registered_at"] = state["written_at"]
        result.append(executions.validate_record(value))
        previous = value["id"]
    return result


def insert_bindings(connection: sqlite3.Connection, values: list[Doc], state: Doc) -> None:
    for value in values:
        connection.execute(
            "INSERT INTO bindings VALUES (?,?,?,?)",
            (value["id"], value["pending_key"], value["supersedes"], store.canonical(value)),
        )
    current = values[-1]
    connection.execute("INSERT INTO active VALUES (?,?)", (current["pending_key"], current["id"]))
    connection.execute(
        "INSERT INTO watermarks VALUES (?,?,?)", (current["pending_key"], state["serial"], planning.digest(state))
    )


def build(
    root: Path,
    tasks: int,
    *,
    depth: int = 1,
    cases: int = 1,
    requirements: int = 1,
    backlog: bool = False,
    bindings_only: bool = False,
) -> History:
    """Generate a valid v1 open-task ledger or v2 sequential completed-task history.

    Sequential mode publishes each newly queued task between completions, increasing the serial.
    Backlog mode queues every task in the first state and each subsequent completion consumes one;
    consequently each retained before-state is exactly the previous retained after-state.
    """
    if tasks < 1 or depth < 1:
        raise ValueError("tasks and supersession depth must be positive")
    producer = source(cases, requirements)
    scenario = prepare_scenario(root, source=producer, upgrade=not bindings_only)
    initial = scenario.state
    initial["lineage"] = uuid.uuid5(uuid.NAMESPACE_URL, "exacly:controlled-ledger-history").hex
    initial["pending"] = [pending(index) for index in range(tasks)] if backlog or bindings_only else [pending(0)]
    initial["written_at"] = timestamp(0)
    documents = {producer.model_file: producer.config}
    template = scenario.binding
    completion_ids: list[str] = []
    retained: Doc | None = None
    if not bindings_only:
        # Obtain one genuine normal completion payload from the controlled source before cloning.
        with patch.object(completion, "now", lambda: timestamp(1)):
            completed = scenario.complete()
        with store.locked(scenario.ledger, write=False) as (path, _), store.database(path, create=False) as (conn, _):
            retained = completion_store.get(conn, completed["completion_id"])
    with sqlite3.connect(scenario.ledger) as connection:
        if not bindings_only:
            connection.execute("DELETE FROM completion_receipts")
            connection.execute("DELETE FROM completion_intents")
        connection.execute("DELETE FROM active")
        connection.execute("DELETE FROM watermarks")
        connection.execute("DELETE FROM bindings")
        current = copy.deepcopy(initial)
        for index in range(tasks):
            selected = pending(index)
            if not backlog and not bindings_only and index:
                current["serial"] += 1
                current["written_at"] = timestamp(2 * index)
                current["pending"] = [selected]
            captured = initial if backlog or bindings_only else current
            values = binding_chain(template, captured, selected, depth)
            if bindings_only:
                insert_bindings(connection, values, captured)
                continue
            assert retained is not None
            active = values[-1]
            run_id = 100000 + index
            content = report(cases, requirements, run_id)
            evidence = copy.deepcopy(retained["admission_receipt"])
            evidence["origin"].update(
                run_id=run_id,
                artifact_id=200000 + index,
                artifact_name=f"{producer.artifact_prefix}-{run_id}-1",
                job_id=300000 + index,
                branch_protection_checked_at=timestamp(2 * index),
            )
            observed = admission.observe_report(
                documents,
                content,
                Run(
                    revision=active["contract"]["target"]["revision"],
                    model_revision=active["contract"]["target"]["model_revision"],
                    context=current["context"],
                    observed_at=admission.timestamp(END),
                    workflow=producer.workflow_path,
                    job=producer.job_id,
                    job_status="success",
                    url=f"https://github.com/{producer.policy['repository']['full_name']}/actions/runs/{run_id}/attempts/1",
                    source_url=f"https://github.com/{producer.policy['repository']['full_name']}/blob/{active['contract']['target']['revision']}",
                ),
            )
            observed["lineage"] = uuid.uuid5(uuid.NAMESPACE_URL, f"exacly:controlled-run:{run_id}").hex
            manifest = {**producer.manifest, "run_id": run_id, "report_digest": admission.sha256(content)}
            regular = stat.S_IFREG | 0o644
            archive = bundle(
                [("manifest.json", json.dumps(manifest).encode(), regular), ("junit.xml", content, regular)]
            )
            evidence["origin"]["archive_digest"] = admission.sha256(archive)
            evidence["origin"]["report_digest"] = admission.sha256(content)
            evidence["state_digest"] = planning.digest(observed)
            evidence["digest"] = planning.digest({key: value for key, value in evidence.items() if key != "digest"})
            after = completion_state.merge(
                documents,
                current,
                observed,
                workflow=producer.workflow_path,
                job=producer.job_id,
                target=active["contract"]["target"],
                written_at=timestamp(2 * index + 1),
            )
            after["pending"] = [item for item in after.get("pending", []) if item != selected]
            if not after["pending"]:
                after.pop("pending")
            payload = {
                "before_state": state_writer.serialize(current),
                "after_state": state_writer.serialize(after),
                "archive": archive,
                "report": content,
                "admission_receipt": evidence,
                "model_documents": documents,
                "admitted_state": observed,
            }
            request = copy.deepcopy(retained["intent"]["request"])
            request.update(binding_id=active["id"], run_id=run_id)
            identity = uuid.uuid5(uuid.NAMESPACE_URL, f"exacly:controlled-completion:{index}").hex
            metadata = completion_store._metadata(
                identity, request, timestamp(2 * index), active, payload, current, after
            )
            with patch.object(completion, "now", lambda index=index: timestamp(2 * index + 1)):
                receipt = completion._receipt(metadata, active)
            insert_bindings(connection, values, after)
            connection.execute(
                "INSERT INTO completion_intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    active["id"],
                    active["pending_key"],
                    metadata["request_digest"],
                    "committed",
                    store.canonical(metadata),
                    payload["before_state"],
                    payload["after_state"],
                    archive,
                    content,
                    store.canonical(evidence),
                    store.canonical(documents),
                    store.canonical(observed),
                ),
            )
            connection.execute(
                "INSERT INTO completion_receipts VALUES (?,?,?,?)",
                (active["id"], identity, metadata["request_digest"], store.canonical(receipt)),
            )
            completion_ids.append(identity)
            current = after
    scenario.state_path.write_bytes(state_writer.serialize(current))
    with store.locked(scenario.ledger, write=False) as (path, _), store.database(path, create=False) as (connection, _):
        store.validate(connection)
    return History(
        scenario.ledger, scenario.state_path, tuple(completion_ids), tasks * depth, tasks, cases, requirements, backlog
    )
