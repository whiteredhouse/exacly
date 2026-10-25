"""Checks the query core and the exacly command: show, trace, impact and gaps over the invoice reminders example.

Run with `mise run test`, or from the repository root: uv run pytest python/tests
"""

import contextlib
import copy
import io
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from exacly import query
from exacly.cli import exacly_main, load

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "spec" / "examples" / "0.2" / "invoice-reminders"
STATE: dict[str, Any] = {
    "exacly": "0.2.0",
    "namespace": "invoicing",
    "context": "production",
    "config_revision": "0b6ad3b",
    "serial": 3,
    "lineage": "9b0c6d1e-6f1a-4c39-9a57-1c4a3f0e2b11",
    "written_at": "2026-10-24T09:00:00Z",
    "records": {
        "REQ-001": {
            "assessments": [
                {
                    "dimension": "verification",
                    "state": "failed",
                    "context": "0b6ad3b",
                    "at": "2026-10-24T09:00:00Z",
                    "evidence": [
                        {
                            "locator": "https://ci.example/runs/7",
                            "kind": "test",
                            "claim": "1 of 4 selected test cases failed",
                            "observed_at": "2026-10-24",
                            "applies_to": "0b6ad3b",
                        }
                    ],
                    "reason": "Failed: tests/reminders::test_duplicate.",
                }
            ]
        }
    },
    "pending": [{"record": "REQ-001", "task": "TASK-12", "opened_at": "2026-10-24T09:00:00Z"}],
}


def documents(state: bool = True) -> dict[str, Any]:
    docs = load([EXAMPLE])
    if state:
        docs["state/production.json"] = copy.deepcopy(STATE)
    return docs


def model(state: bool = True) -> query.Model:
    return query.load(documents(state))


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = exacly_main(list(argv))
    return code, out.getvalue(), err.getvalue()


def ids(nodes: list[dict[str, Any]]) -> list[str]:
    return [n["id"] for n in nodes]


def walk(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [node, *(n for child in node.get("children", []) for n in walk(child))]


class TestShow:
    def test_a_record_with_its_relationships_both_ways(self) -> None:
        answer = query.show(model(), "REQ-001")
        assert answer["record"]["name"] == "Remind before due date"
        assert answer["document"].endswith("billing.yaml")
        rels = {(r["direction"], r["type"], r["record"]["id"]) for r in answer["relationships"]}
        assert rels == {("out", "supports", "PROC-001"), ("in", "implements", "APP-001")}

    def test_its_state_and_pending_work_by_context(self) -> None:
        state = query.show(model(), "REQ-001")["state"]
        assert list(state) == ["production"]
        assert state["production"]["config_revision"] == "0b6ad3b"
        assert state["production"]["assessments"][0]["state"] == "failed"
        assert state["production"]["pending"] == STATE["pending"]

    def test_the_findings_about_it(self) -> None:
        # The example is proposed and has no released implementation, so the failed verification is no claim;
        # its proposed Outcome's Measure is proposed too. Every finding shown names this record.
        answer = query.show(model(), "OUT-001")
        assert all(f["record"] == "OUT-001" for f in answer["findings"])

    def test_a_reference_qualified_with_the_namespace(self) -> None:
        assert query.show(model(), "invoicing:APP-001")["record"]["id"] == "APP-001"

    def test_an_unknown_record_suggests_close_ids(self) -> None:
        with pytest.raises(query.QueryError, match=r"REQ-01 is not a record.*REQ-001"):
            query.show(model(), "REQ-01")


class TestTrace:
    def test_why_leads_up_to_the_outcome(self) -> None:
        why = query.trace(model(), "APP-001")["why"]
        assert [n["id"] for n in walk(why)] == ["APP-001", "REQ-001", "PROC-001", "CAP-001", "OUT-001", "PROC-001"]
        # The Process is reached twice: through the Requirement, and because it uses the Application.
        assert walk(why)[-1] == {**walk(why)[-1], "repeated": True, "via": {"type": "uses", "direction": "in"}}

    def test_how_leads_down_to_what_runs_it(self) -> None:
        how = query.trace(model(), "REQ-001")["how"]
        nodes = {n["id"]: n for n in walk(how) if not n.get("repeated")}
        assert nodes["APP-001"]["via"] == {"type": "implements", "direction": "in"}
        assert nodes["INF-003"]["via"] == {"type": "depends_on", "direction": "out"}
        assert {"INFO-001", "INF-001", "INF-002"} <= set(nodes)

    def test_information_is_held_by_its_storage(self) -> None:
        trace = query.trace(model(), "INFO-001")
        assert trace["how"]["children"] == [
            {
                **query.Model.summary(model(), "INF-003"),
                "via": {"type": "stores", "direction": "in", "role": "system_of_record"},
            }
        ]
        assert ids(trace["why"]["children"]) == ["APP-001", "APP-001"]  # It reads and writes.

    def test_nodes_carry_their_status(self) -> None:
        assert query.trace(model(), "REQ-001")["why"]["status"] == {"production": {"verification": "failed"}}


class TestImpact:
    def test_what_relies_on_a_changed_record_with_the_shortest_path(self) -> None:
        answer = query.impact(model(), ["INF-003"])
        paths = {a["id"]: a["path"] for a in answer["affected"]}
        assert paths["APP-001"] == ["INF-003", "APP-001"]
        # The Process that uses the Application is closer than the Requirement it implements.
        assert paths["OUT-001"] == ["INF-003", "APP-001", "PROC-001", "CAP-001", "OUT-001"]
        assert paths["REQ-001"] == ["INF-003", "APP-001", "REQ-001"]
        assert answer["outcomes"] == ["OUT-001"]
        assert "INF-003" not in paths

    def test_requirements_to_check_again_with_their_bindings(self) -> None:
        recheck = query.impact(model(), ["APP-001"])["recheck"]
        assert recheck == [
            {
                **query.Model.summary(model(), "REQ-001"),
                "observe": {"tests": {"workflow": "ci.yml", "path": "tests/reminders"}},
            }
        ]

    def test_a_changed_requirement_is_itself_checked_again(self) -> None:
        assert ids(query.impact(model(), ["REQ-001"])["recheck"]) == ["REQ-001"]

    def test_an_outcome_affects_nothing_above_it(self) -> None:
        answer = query.impact(model(), ["OUT-001"])
        assert answer["affected"] == [] and answer["recheck"] == []


class TestGaps:
    def test_coverage_beyond_conformance(self) -> None:
        answer = query.gaps(model())
        assert ids(answer["coverage"]["unverified"]) == ["REQ-001"]
        assert answer["coverage"]["unverified"][0]["verification"] == {"production": "failed"}
        assert ids(answer["coverage"]["unreleased"]) == ["REQ-001"]
        assert answer["coverage"]["unobserved"] == []
        assert answer["coverage"]["proposed_measures"][0]["evaluates"] == ["OUT-001"]
        assert answer["pending"] == [{"context": "production", **STATE["pending"][0]}]
        assert answer["contexts"] == {"production": {"config_revision": "0b6ad3b", "serial": 3}}

    def test_the_validators_findings(self) -> None:
        answer = query.gaps(model(state=False))
        assert answer["result"] == "conforms"
        assert answer["errors"] == []
        assert all(set(f) >= {"severity", "rule", "message"} for f in answer["warnings"])

    def test_a_requirement_without_a_binding_is_unobserved(self) -> None:
        docs = documents()
        billing = next(d for name, d in docs.items() if name.endswith("billing.yaml"))
        del next(r for r in billing["records"] if r["id"] == "REQ-001")["observe"]
        assert ids(query.gaps(query.load(docs))["coverage"]["unobserved"]) == ["REQ-001"]


class TestChanges:
    def edited(self) -> query.Model:
        docs = documents()
        billing = next(d for name, d in docs.items() if name.endswith("billing.yaml"))
        billing["records"] = [r for r in billing["records"] if r["id"] != "INF-002"]
        billing["relationships"] = [r for r in billing["relationships"] if "INF-002" not in (r["source"], r["target"])]
        next(r for r in billing["records"] if r["id"] == "INF-001")["description"] = "Changed."
        return query.load(docs)

    def test_added_changed_and_removed(self) -> None:
        diff = query.changes(model(), self.edited())
        assert diff["records"]["added"] == []
        assert diff["records"]["changed"] == [{**query.Model.summary(model(), "INF-001"), "fields": ["description"]}]
        assert ids(diff["records"]["removed"]) == ["INF-002"]
        assert diff["relationships"] == {"added": [], "changed": [], "removed": ["APP-001 --depends_on--> INF-002"]}

    def test_the_impact_of_a_removal_is_traced_in_the_base(self) -> None:
        answer = query.change_impact(model(), self.edited())["impact"]
        assert ids(answer["changed"]) == ["INF-001"]
        # APP-001 relied on the removed INF-002: losing that relationship affects it, though its fields didn't change.
        assert answer["affected"][0] == {**query.Model.summary(model(), "APP-001"), "path": ["APP-001"]}
        assert "OUT-001" in answer["outcomes"]
        assert ids(answer["recheck"]) == ["REQ-001"]

    def test_relationship_fields_and_external_references(self) -> None:
        docs = documents()
        billing = next(d for name, d in docs.items() if name.endswith("billing.yaml"))
        rels = billing["relationships"]
        next(r for r in rels if r["target"] == "INF-001")["conditions"] = "Also for retries."
        next(r for r in rels if r["type"] == "stores")["role"] = "replica"
        rels.append({"source": "APP-001", "type": "depends_on", "target": "mail:SMTP-1"})
        diff = query.changes(model(), query.load(docs))
        assert diff["records"] == {"added": [], "changed": [], "removed": []}
        assert diff["relationships"] == {
            "added": ["APP-001 --depends_on--> mail:SMTP-1"],
            "changed": [
                {"relationship": "APP-001 --depends_on--> INF-001", "fields": ["conditions"]},
                {"relationship": "INF-003 --stores--> INFO-001", "fields": ["role"]},
            ],
            "removed": [],
        }

    def test_a_changed_relationship_affects_the_record_that_relies_on_it(self) -> None:
        docs = documents()
        billing = next(d for name, d in docs.items() if name.endswith("billing.yaml"))
        next(r for r in billing["relationships"] if r["type"] == "stores")["role"] = "replica"
        answer = query.change_impact(model(), query.load(docs))["impact"]
        # Information is why its storage exists, so the Information relies on the stores relationship.
        assert answer["affected"][0] == {**query.Model.summary(model(), "INFO-001"), "path": ["INFO-001"]}
        assert "APP-001" in ids(answer["affected"])

    def test_a_retargeted_relationship_affects_both_versions(self) -> None:
        def with_requirements(target: str) -> query.Model:
            docs = documents()
            billing = next(d for name, d in docs.items() if name.endswith("billing.yaml"))
            second = copy.deepcopy(next(r for r in billing["records"] if r["id"] == "REQ-001"))
            billing["records"].append({**second, "id": "REQ-002"})
            billing["relationships"].append({"source": "REQ-002", "type": "supports", "target": "PROC-001"})
            implements = next(r for r in billing["relationships"] if r["type"] == "implements")
            implements.update(id="REL-1", target=target)
            return query.load(docs)

        diff = query.change_impact(with_requirements("REQ-001"), with_requirements("REQ-002"))
        assert diff["relationships"]["changed"] == [
            {"relationship": "APP-001 --implements--> REQ-002", "fields": ["target"]}
        ]
        # REQ-001 lost its implementer and REQ-002 gained one, so both are checked again.
        assert ids(diff["impact"]["recheck"]) == ["REQ-001", "REQ-002"]

    def test_a_qualified_reference_to_the_model_itself_is_the_same_relationship(self) -> None:
        docs = documents()
        billing = next(d for name, d in docs.items() if name.endswith("billing.yaml"))
        next(r for r in billing["relationships"] if r["type"] == "evaluates")["target"] = "invoicing:OUT-001"
        assert query.changes(model(), query.load(docs))["relationships"] == {"added": [], "changed": [], "removed": []}

    def test_from_an_empty_base(self) -> None:
        diff = query.change_impact(query.empty("invoicing"), model())
        assert len(diff["records"]["added"]) == len(model().records)


def test_a_model_that_does_not_load() -> None:
    docs = documents()
    docs["state/production.json"]["serial"] = "three"
    with pytest.raises(query.QueryError, match="can't be read"):
        query.load(docs)


class TestCommand:
    def test_show_as_text_and_json(self) -> None:
        code, out, _ = run("show", "REQ-001", "-m", str(EXAMPLE))
        assert code == 0
        assert out.startswith("REQ-001 Requirement: Remind before due date (proposed)\n")
        assert "\n  supports -> PROC-001 Process: " in out
        assert "\n  implements <- APP-001 Application: " in out
        code, out, _ = run("show", "REQ-001", "-m", str(EXAMPLE), "--json")
        assert json.loads(out)["record"]["id"] == "REQ-001"

    def test_trace_impact_and_gaps(self) -> None:
        for argv in (["trace", "APP-001"], ["impact", "INF-003"], ["gaps"]):
            code, out, _ = run(*argv, "-m", str(EXAMPLE))
            assert code == 0 and out.strip()
        assert "via INF-003 -> APP-001\n" in run("impact", "INF-003", "-m", str(EXAMPLE))[1]

    def test_an_unknown_record_fails(self) -> None:
        code, _, err = run("show", "REQ-404", "-m", str(EXAMPLE))
        assert code == 1 and "REQ-404 is not a record" in err

    def test_impact_needs_records_or_a_base(self) -> None:
        code, _, err = run("impact", "-m", str(EXAMPLE))
        assert code == 1 and "impact takes record IDs or a base revision" in err

    def test_validate_runs_the_validator(self) -> None:
        code, out, _ = run("validate", str(EXAMPLE))
        assert code == 0 and "conforms" in out

    def test_a_date_that_does_not_exist_fails_with_where_it_is(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            broken = Path(folder) / "model.yaml"
            broken.write_text("exacly: 0.2.0\nrecords:\n  - id: REQ-001\n    last_reviewed_at: 2026-13-24\n")
            for argv in (["validate", str(broken)], ["gaps", "-m", str(broken)]):
                code, out, err = run(*argv)
                assert code == 1 and not out
                assert err.strip().endswith(
                    f"{broken} is not valid YAML at line 4, column 23: '2026-13-24' is not a valid timestamp: "
                    "month must be in 1..12"
                )

    def test_a_pattern_is_matched_against_each_revision(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as folder_outside:
            repo = Path(folder)
            model_dir = repo / "spec" / "exacly"
            (model_dir / "state").mkdir(parents=True)
            for name in ("model.yaml", "billing.yaml"):
                (model_dir / name).write_text((EXAMPLE / name).read_text())
            (model_dir / "state" / "notes.yaml").write_text("not: a model document\n")
            (model_dir / ".notes.yaml").write_text("hidden: [not parsed\n")  # glob leaves hidden files out
            for args in (
                ["init", "-q"],
                ["add", "."],
                ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"],
            ):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
            (model_dir / "billing.yaml").unlink()
            monkeypatch.chdir(repo)
            code, out, err = run("impact", "--base", "HEAD", "-m", "spec/exacly/*.yaml", "--json")
            assert code == 0, err
            removed = json.loads(out)["records"]["removed"]
            assert "REQ-001" in ids(removed) and len(removed) == 10
            monkeypatch.chdir(folder_outside)
            code, out, _ = run("impact", "--base", "HEAD", "-m", str(model_dir / "*.yaml"), "--json")
            assert code == 0 and len(json.loads(out)["records"]["removed"]) == 10
            # Through a symbolic link to the repository, as macOS's /var is to /private/var.
            linked = Path(folder_outside) / "linked"
            linked.symlink_to(repo)
            code, out, err = run("impact", "--base", "HEAD", "-m", str(linked / "spec" / "exacly" / "*.yaml"), "--json")
            assert code == 0, err
            assert len(json.loads(out)["records"]["removed"]) == 10
            code, _, err = run("gaps", "-m", str(model_dir / "*.json"))
            assert code == 1 and "matches no files" in err

    def test_impact_against_a_git_revision(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as folder_outside:
            repo = Path(folder)
            model_dir = repo / "spec" / "exacly"
            model_dir.mkdir(parents=True)

            def git(*args: str) -> None:
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

            git("init", "-q")
            for name in ("model.yaml", "billing.yaml"):
                (model_dir / name).write_text((EXAMPLE / name).read_text())
            git("add", ".")
            git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base")
            billing = yaml.safe_load((model_dir / "billing.yaml").read_text())
            next(r for r in billing["records"] if r["id"] == "INF-003")["description"] = "Moved."
            (model_dir / "billing.yaml").write_text(yaml.safe_dump(billing))
            monkeypatch.chdir(repo)
            code, out, _ = run("impact", "--base", "HEAD")
            assert code == 0
            assert out.startswith("~ INF-003 Infrastructure: ")
            assert "(description)" in out and "Outcomes: OUT-001" in out
            code, out, _ = run("impact", "--base", "HEAD", "--json")
            assert json.loads(out)["impact"]["recheck"][0]["id"] == "REQ-001"
            # A file name Git would quote in its output.
            (model_dir / "billing.yaml").rename(model_dir / "facturación.yaml")
            git("add", "-A")
            git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "rename")
            code, out, _ = run("impact", "--base", "HEAD")
            assert code == 0 and out == "No changes to the model.\n"
            (model_dir / "facturación.yaml").rename(model_dir / "billing.yaml")
            git("reset", "-q", "--hard", "HEAD~1")
            (model_dir / "billing.yaml").write_text(yaml.safe_dump(billing))
            code, _, err = run("impact", "--base", "no-such-revision")
            assert code == 1 and "git failed" in err
            # An absolute model path, from inside the repository and from outside it.
            assert run("impact", "--base", "HEAD", "-m", str(model_dir))[1].startswith("~ INF-003 Infrastructure: ")
            monkeypatch.chdir(folder_outside)
            assert run("impact", "--base", "HEAD", "-m", str(model_dir))[1].startswith("~ INF-003 Infrastructure: ")
            assert run(
                "impact", "--base", "HEAD", "-m", str(model_dir / "billing.yaml"), "-m", str(model_dir / "model.yaml")
            )[1].startswith("~ INF-003 Infrastructure: ")
