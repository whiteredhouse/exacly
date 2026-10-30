"""CLI authorization, strict inputs and retained-operation routing for local handoffs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from exacly.cli import exacly_main

Doc = dict[str, Any]
HANDOFF = "sha256:" + "a" * 64
TOKEN = "synthetic-handoff-token"


def answer(status: str, *, dry_run: bool = False) -> Doc:
    return {"schema_version": "0.1.0", "operation": "handoff", "status": status, "dry_run": dry_run}


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> Doc:
    import exacly

    calls: Doc = {
        "request": [], "preview": [], "apply": [], "show": [], "recover": [], "recover_journal": [],
        "initialize": [], "initialize_repository": [], "inspect_repository": [], "error": None,
        "report": answer("published"), "override_preview": None,
        "repository": {"schema_version": "0.1.0", "path": "/protected/repository.git", "instance_id": "b" * 32},
    }  # fmt: skip
    module = ModuleType("exacly.handoff")
    storage = ModuleType("exacly.handoff_store")
    git = ModuleType("exacly.handoff_git")

    def request(document: Any) -> SimpleNamespace:
        calls["request"].append(document)
        return SimpleNamespace(document=document)

    def operation(name: str) -> Any:
        def run(*args: Any, **kwargs: Any) -> Doc:
            calls[name].append((args, kwargs))
            if calls["error"] is not None:
                raise ValueError(calls["error"])
            if name == "preview":
                return calls["override_preview"] or answer("ready", dry_run=True)
            if name == "recover_journal":
                return answer("recovered")
            return dict(calls["report"])

        return run

    def initialize(*args: Any, **kwargs: Any) -> Doc:
        calls["initialize"].append((args, kwargs))
        return {"schema_version": "0.1.0", "status": "initialized", "journal_id": "c" * 32}

    def repository(name: str) -> Any:
        def run(*args: Any, **kwargs: Any) -> Doc:
            calls[name].append((args, kwargs))
            if calls["error"] is not None:
                raise ValueError(calls["error"])
            return dict(calls["repository"])

        return run

    monkeypatch.setattr(module, "request_from_document", request, raising=False)
    for name in ("preview", "apply", "show", "recover", "recover_journal"):
        monkeypatch.setattr(module, name, operation(name), raising=False)
    monkeypatch.setattr(module, "as_text", lambda report: f"Handoff: {report['status']}", raising=False)
    monkeypatch.setattr(storage, "initialize", initialize, raising=False)
    for name in ("initialize_repository", "inspect_repository"):
        monkeypatch.setattr(git, name, repository(name), raising=False)
    for name, replacement in (("handoff", module), ("handoff_store", storage), ("handoff_git", git)):
        monkeypatch.setitem(sys.modules, "exacly." + name, replacement)
        monkeypatch.setattr(exacly, name, replacement, raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return calls


def invocation(tmp_path: Path, *, apply: bool = False) -> tuple[list[str], dict[str, Path]]:
    documents = {
        "request": {"schema_version": "0.1.0", "reviewed": "exact handoff 日本語"},
        "authorization": {"schema_version": "0.1.0", "handoff_id": HANDOFF, "approved": "publication"},
    }
    paths = {key: tmp_path / f"{key}.json" for key in documents}
    for key, document in documents.items():
        paths[key].write_text(json.dumps(document))
    args = ["handoff", "local-git", "--request", str(paths["request"])]
    if apply:
        args.extend(["--apply", "--authorization", str(paths["authorization"])])
    return args, paths


@pytest.mark.parametrize("explicit", [False, True])
def test_default_preview_is_read_only_and_decodes_only_the_explicit_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, explicit: bool
) -> None:
    args, paths = invocation(tmp_path)
    original = {path: path.read_bytes() for path in paths.values()}
    if explicit:
        args.append("--dry-run")
    assert exacly_main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == answer("ready", dry_run=True)
    (request,), options = backend["preview"][0]
    assert request.document == json.loads(paths["request"].read_text()) and options == {}
    assert backend["request"] == [request.document]
    assert not backend["apply"] and not backend["initialize"] and not backend["initialize_repository"]
    assert {path: path.read_bytes() for path in paths.values()} == original


def test_apply_routes_exact_reviewed_authorization_and_one_lock_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    args, paths = invocation(tmp_path, apply=True)
    assert exacly_main([*args, "--lock-timeout", "0", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == answer("published")
    (request,), options = backend["apply"][0]
    assert request.document == json.loads(paths["request"].read_text())
    assert options == {"authorization": json.loads(paths["authorization"].read_text()), "lock_timeout": 0}
    assert not backend["preview"] and not backend["initialize"]


def test_apply_requires_separate_authorization_before_reading_the_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    with pytest.raises(SystemExit) as error:
        exacly_main(["handoff", "local-git", "--request", str(tmp_path / "absent"), "--apply"])
    assert error.value.code == 2 and "requires --authorization" in capsys.readouterr().err
    assert not backend["request"] and not backend["apply"]


@pytest.mark.parametrize("explicit", [False, True])
def test_authorization_cannot_turn_a_preview_into_publication(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, explicit: bool
) -> None:
    args, paths = invocation(tmp_path)
    if explicit:
        args.append("--dry-run")
    with pytest.raises(SystemExit) as error:
        exacly_main([*args, "--authorization", str(paths["authorization"])])
    assert error.value.code == 2 and "requires --apply" in capsys.readouterr().err
    assert not backend["request"] and not backend["preview"] and not backend["apply"]


@pytest.mark.parametrize("document", ["request", "authorization"])
@pytest.mark.parametrize("content", ['{"same":1,"same":2}', '{"number":NaN}', '{"number":Infinity}', "\udcff"])
def test_ambiguous_or_invalid_json_cannot_reach_handoff_effects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, document: str, content: str
) -> None:
    args, paths = invocation(tmp_path, apply=True)
    paths[document].write_bytes(content.encode("utf-8", errors="surrogateescape"))
    assert exacly_main(args) == 2
    assert capsys.readouterr().err
    assert not backend["apply"] and not backend["preview"]


@pytest.mark.parametrize("document", ["request", "authorization"])
def test_oversized_documents_are_rejected_before_handoff_effects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, document: str
) -> None:
    args, paths = invocation(tmp_path, apply=True)
    paths[document].write_bytes(b" " * (8 * 1024 * 1024 + 1))
    assert exacly_main(args) == 2
    assert "permitted JSON document size" in capsys.readouterr().err
    assert not backend["apply"]


@pytest.mark.parametrize("status", ["prepared", "conflict", "blocked"])
def test_unfinished_publication_is_exit_one_without_rendering_untrusted_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, status: str
) -> None:
    args, _ = invocation(tmp_path, apply=True)
    backend["report"] = {**answer(status), "artifact": "untrusted candidate output"}
    assert exacly_main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == f"Handoff: {status}\n" and not captured.err
    assert "untrusted" not in captured.out


@pytest.mark.parametrize(
    "operation,status,code",
    [
        ("show", "prepared", 0),
        ("show", "published", 0),
        ("show", "missing", 1),
        ("recover", "prepared", 1),
        ("recover", "published", 0),
        ("recover", "conflict", 1),
        ("recover", "missing", 1),
    ],
)
def test_retained_operations_need_only_journal_and_handoff_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, operation: str, status: str, code: int
) -> None:
    backend["report"] = answer(status)
    journal = tmp_path / "handoff.sqlite3"
    assert (
        exacly_main(
            ["handoffs", operation, "--journal", str(journal), "--handoff", HANDOFF, "--lock-timeout", "0", "--json"]
        )
        == code
    )
    assert backend[operation] == [((journal, HANDOFF), {"lock_timeout": 0})]
    assert json.loads(capsys.readouterr().out) == answer(status)
    assert not backend["request"] and not backend["apply"] and not backend["preview"]


def test_explicit_repository_initialization_wraps_the_canonical_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    repository = tmp_path / "target.git"
    assert exacly_main(["handoffs", "init-repository", "--repository", str(repository), "--json"]) == 0
    assert backend["initialize_repository"] == [((repository,), {})]
    report = json.loads(capsys.readouterr().out)
    assert report == {"schema_version": "0.1.0", "status": "initialized", "repository": backend["repository"]}
    assert not backend["initialize"] and not backend["request"]


def test_explicit_journal_initialization_pins_the_checked_repository_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    repository, journal = tmp_path / "target.git", tmp_path / "handoff.sqlite3"
    assert (
        exacly_main(
            [
                "handoffs",
                "init",
                "--journal",
                str(journal),
                "--repository",
                str(repository),
                "--lock-timeout",
                "0",
                "--json",
            ]
        )
        == 0
    )
    assert backend["inspect_repository"] == [((repository,), {})]
    assert backend["initialize"] == [((journal,), {"repository": backend["repository"], "lock_timeout": 0})]
    assert json.loads(capsys.readouterr().out)["journal_id"] == "c" * 32
    assert not backend["initialize_repository"]


def test_journal_repair_requires_explicit_authorization_before_storage_access(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc
) -> None:
    args = ["handoffs", "recover-journal", "--journal", str(tmp_path / "handoff.sqlite3")]
    with pytest.raises(SystemExit) as error:
        exacly_main(args)
    assert error.value.code == 2 and "--authorize-recovery" in capsys.readouterr().err
    assert not backend["recover_journal"]
    assert exacly_main([*args, "--authorize-recovery", "--lock-timeout", "0", "--json"]) == 0
    assert backend["recover_journal"] == [((tmp_path / "handoff.sqlite3",), {"authorize": True, "lock_timeout": 0})]
    assert json.loads(capsys.readouterr().out) == answer("recovered")


@pytest.mark.parametrize(
    "field,value", [("schema_version", "unsupported"), ("status", "invented"), ("status", "ready"), ("dry_run", True)]
)
def test_invalid_backend_reports_cannot_claim_publication(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, field: str, value: Any
) -> None:
    args, _ = invocation(tmp_path, apply=True)
    backend["report"][field] = value
    assert exacly_main(args) == 2
    assert "invalid report" in capsys.readouterr().err


@pytest.mark.parametrize("seconds", ["-1", "NaN", "Infinity", "-Infinity", "invalid"])
def test_invalid_lock_budget_prevents_any_handoff_effect(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: Doc, seconds: str
) -> None:
    args, _ = invocation(tmp_path, apply=True)
    with pytest.raises(SystemExit) as error:
        exacly_main([*args, f"--lock-timeout={seconds}"])
    assert error.value.code == 2 and "nonnegative" in capsys.readouterr().err
    assert not backend["request"] and not backend["apply"]


def test_failures_redact_available_credentials_without_using_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, backend: Doc
) -> None:
    args, _ = invocation(tmp_path, apply=True)
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    backend["error"] = f"publication failed with {TOKEN}"
    assert exacly_main(args) == 2
    captured = capsys.readouterr()
    assert TOKEN not in captured.err and "[redacted]" in captured.err
    assert os.environ["GH_TOKEN"] == TOKEN


def test_parser_errors_also_redact_available_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, backend: Doc
) -> None:
    args, _ = invocation(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    with pytest.raises(SystemExit) as error:
        exacly_main([*args, f"--unknown={TOKEN}"])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert TOKEN not in captured.err and "[redacted]" in captured.err
    assert not backend["request"]
