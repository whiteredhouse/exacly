"""Reviewed dispatch inputs and a deterministic in-memory GitHub issue adapter."""

from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from exacly import dispatch, dispatch_store, state_writer
from test_admission import READINESS, Source
from test_executions import run
from test_reconciliation import make_plan, selected

Doc = dict[str, Any]
DATE = dt.date(2026, 10, 30)


class FakeIssues:
    def __init__(self) -> None:
        self.issues: list[Doc] = []
        self.calls: list[str] = []

    def repository(self, repository: Doc) -> Doc:
        self.calls.append("repository")
        return copy.deepcopy(repository)

    def discover(self, repository: Doc, marker: str, effect: Doc) -> list[Doc]:
        self.calls.append("discover")
        return copy.deepcopy([issue for issue in self.issues if marker in issue["body"]])

    def create(self, repository: Doc, effect: Doc) -> Doc:
        self.calls.append("create")
        number = len(self.issues) + 1
        result = {
            "repository": copy.deepcopy(repository),
            "id": 1000 + number,
            "number": number,
            "url": f"https://github.com/{repository['full_name']}/issues/{number}",
            "created_at": "2026-10-30T09:00:00+00:00",
            **copy.deepcopy(effect),
        }
        self.issues.append(result)
        return copy.deepcopy(result)


@dataclass
class Scenario:
    source: Source
    plan: Doc
    documents: Doc
    state_path: Path
    journal: Path
    ledger: Path
    policy: Doc
    request: dispatch.Request
    client: FakeIssues

    @property
    def authorization(self) -> Doc:
        return dict(dispatch.preview(self.documents, self.state_path, self.request)["authorization_template"])

    @property
    def state(self) -> Doc:
        snapshot = state_writer.read(self.state_path)
        assert snapshot.document is not None
        return snapshot.document

    def apply(self, **changes: Any) -> Doc:
        options: Doc = {"authorization": self.authorization, "client": self.client}
        options.update(changes)
        return dispatch.apply(self.journal, self.ledger, self.documents, self.state_path, self.request, **options)


@pytest.fixture
def dispatch_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    from exacly.refresh import refresh

    monkeypatch.setattr(dispatch, "now", lambda: "2026-10-30T10:00:00+00:00")
    source = Source()
    plan = make_plan(source)
    documents = {source.model_file: source.config}
    state = refresh(documents, {}, run(source, DATE))
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    state_folder = tmp_path / "state"
    state_folder.mkdir(mode=0o700)
    state_path = state_folder / "current.json"
    state_path.write_bytes(state_writer.serialize(state))
    state_path.chmod(0o600)
    journal, ledger = private / "dispatch.sqlite3", private / "executions.sqlite3"
    initialized = dispatch_store.initialize(journal)
    chosen: Doc = {
        "schema_version": "0.1.0",
        "name": "reviewed-dispatch",
        "version": "1",
        "adapter": "github-issue-v1",
        "repository": {"full_name": "acme/demo", "id": 123},
        "issue": {"title": "Verify REQ-1", "body": "Collect independent verification evidence."},
        "max_attempts": 4,
        "storage": {
            "journal": str(journal),
            "ledger": str(ledger),
            "state": str(state_path),
            "journal_id": initialized["journal_id"],
        },
    }
    request = dispatch.Request(
        plan, plan["actions"][0]["id"], copy.deepcopy(READINESS), source.policy, chosen, selected(), DATE
    )
    return Scenario(source, plan, documents, state_path, journal, ledger, chosen, request, FakeIssues())
