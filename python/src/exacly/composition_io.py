"""Bounded composition input snapshots and private, fail-closed result retention."""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import os
import stat
import tempfile
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from exacly import admission, composition, evidence_retention, planning

if TYPE_CHECKING:
    from collections.abc import Iterable

Doc = dict[str, Any]
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_DOCUMENTS = 1024
MAX_DEPTH = 64
MAX_NODES = 200_000


class UniqueLoader(yaml.SafeLoader):
    """Composition documents reject ambiguous duplicate mappings and unsupported scalar values."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        result: dict[Hashable, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ValueError("composition document mapping keys must be strings")
            if key in result:
                raise ValueError(f"duplicate document key: {key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _plain(document: Any) -> None:
    count = 0
    pending = [(document, 0)]
    while pending:
        value, depth = pending.pop()
        count += 1
        if depth > MAX_DEPTH or count > MAX_NODES:
            raise ValueError("composition document exceeds nesting or node limits")
        if isinstance(value, dict):
            if not all(isinstance(key, str) for key in value):
                raise ValueError("composition document mapping keys must be strings")
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif not isinstance(value, (str, int, float, bool, type(None))) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            raise ValueError("composition documents must contain finite JSON-compatible values")


def _read_document(path: Path) -> tuple[Any, int]:
    """Read bounded JSON or YAML without aliases, duplicate keys, nonfinite or excessively deep values."""
    evidence_retention.require_private_filesystem()
    try:
        selected = _resolve(path)
        descriptor = os.open(selected, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("composition inputs must be regular files")
            encoded = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(encoded) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"{path} exceeds the composition document size limit")
        text = encoded.decode("utf-8")
        if path.suffix.lower() == ".json":

            def unique(pairs: list[tuple[str, Any]]) -> Doc:
                result: Doc = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError(f"duplicate document key: {key}")
                    result[key] = value
                return result

            def nonfinite(value: str) -> Any:
                raise ValueError(f"nonfinite JSON value: {value}")

            document = json.loads(text, object_pairs_hook=unique, parse_constant=nonfinite)
        else:
            for index, token in enumerate(yaml.scan(text)):
                if index > MAX_NODES:
                    raise ValueError("composition document exceeds the token limit")
                if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
                    raise ValueError("composition documents do not support YAML aliases or anchors")
            document = yaml.load(text, UniqueLoader)
        _plain(document)
        return document, len(encoded)
    except (RecursionError, RuntimeError) as error:
        raise ValueError(f"{path} exceeds composition nesting limits or cannot be resolved") from error
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValueError(f"{path} is not a valid composition input: {error}") from error


def read_document(path: Path) -> Any:
    """Load one bounded regular JSON/YAML input without following a changed final symlink."""
    return _read_document(path)[0]


def _resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except RuntimeError as error:
        raise ValueError(f"composition path cannot be resolved: {path}") from error


@dataclass
class LoadedDefinition:
    prepared: composition.PreparedDefinition
    paths: tuple[Path, ...]
    model_folders: tuple[Path, ...]


def load_definition(path: Path, date: dt.date) -> LoadedDefinition:
    """Resolve only explicitly approved model files, snapshot all inputs, then validate them offline."""
    definition_path = _resolve(path)
    base = definition_path.parent
    cache: dict[Path, tuple[Any, int]] = {}
    total = 0

    def document(file: Path) -> Any:
        nonlocal total
        resolved = _resolve(file)
        if resolved not in cache:
            if len(cache) >= MAX_DOCUMENTS:
                raise ValueError("composition exceeds the input document count limit")
            cache[resolved] = _read_document(resolved)
        value, size = cache[resolved]
        total += size
        if total > MAX_INPUT_BYTES:
            raise ValueError("composition exceeds the total input size limit")
        return copy.deepcopy(value)

    definition = composition.validate_definition_document(document(definition_path))
    inputs: dict[str, composition.MemberInputs] = {}
    model_folders: set[Path] = set()
    for member in definition["members"]:
        trust = admission.validate_policy(document(base / member["trust_policy"]))
        readiness = document(base / member["readiness_policy"])
        root = _resolve(base / member["model_root"])
        if not root.is_dir():
            raise ValueError("member model_root must name an explicit directory")
        documents: Doc = {}
        for name in trust["model"]["files"]:
            selected = _resolve(root / name)
            if not selected.is_relative_to(root):
                raise ValueError(f"selected model file escapes member model_root: {name}")
            documents[name] = document(selected)
            model_folders.add(selected.parent)
        inputs[member["id"]] = composition.MemberInputs(trust, readiness, documents)
    prepared = composition.validate_definition(definition, inputs, date)
    return LoadedDefinition(prepared, tuple(cache), tuple(sorted(model_folders)))


def protect_output(out: Path, inputs: Iterable[Path], model_folders: Iterable[Path]) -> Path:
    """Refuse output collisions and model-folder aliases before credentials or live source access."""
    evidence_retention.require_private_filesystem()
    if os.path.lexists(out):
        raise ValueError("--out must be a new directory, not an existing path or symlink")
    try:
        destination = out.parent.resolve() / out.name
        protected = {path.resolve() for path in inputs}
        folders = [path.resolve() for path in model_folders]
        if destination in protected or any(destination.is_relative_to(folder) for folder in folders):
            raise ValueError("--out must be outside composition inputs and declared model folders")
        parent = destination.parent
        missing = 0
        while not parent.exists():
            missing += 1
            if missing > evidence_retention.MAX_MISSING_PARENTS:
                raise ValueError("output path exceeds the permitted missing-parent depth")
            parent = parent.parent
        if not parent.is_dir():
            raise ValueError("--out parent must be a directory")
    except RuntimeError as error:
        raise ValueError("--out or its protected input paths cannot be resolved") from error
    return destination


def _replace_private(out: Path, name: str, data: bytes) -> None:
    """Replace one private result atomically; fallible durability work precedes publication."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{Path(name).stem}-", dir=out, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        evidence_retention.sync_directory(out)
        os.replace(temporary, out / name)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _replace_report(out: Path, report: Doc) -> None:
    """Replace the aggregate atomically; all fallible durability work precedes publication."""
    _replace_private(out, "report.json", evidence_retention.json_bytes(report))


class ResultWriter:
    """Retain each member before proceeding; incomplete invocations never publish aggregate success."""

    def __init__(self, out: Path, loaded: LoadedDefinition, selection: Doc, date: dt.date) -> None:
        self.out = out
        self.loaded = loaded
        protect_output(out, loaded.paths, loaded.model_folders)
        evidence_retention.private_parents(out.parent)
        out.mkdir(mode=0o700)
        out.chmod(0o700)
        members = out / "members"
        members.mkdir(mode=0o700)
        members.chmod(0o700)
        self.report: Doc = {
            "schema_version": "0.1.0",
            "operation": "compose",
            "complete": False,
            "result": "blocked",
            "definition": {
                "name": loaded.prepared.definition["name"],
                "version": loaded.prepared.definition["version"],
                "digest": loaded.prepared.digest,
            },
            "as_of": date.isoformat(),
            "definition_digest": loaded.prepared.digest,
            "selection_digest": planning.digest(selection),
            "required_members": [member.id for member in loaded.prepared.members],
            "selection": selection,
            "members": [],
        }
        _replace_report(out, self.report)
        evidence_retention.sync_directory(out.parent)

    def member(self, report: Doc, admitted: admission.AdmissionResult) -> None:
        member_id = report["id"]
        destination = self.out / "members" / member_id
        evidence_retention.admission_bundle(destination, admitted)
        if report.get("gate") is not None:
            evidence_retention.write_private(destination / "gate.json", evidence_retention.json_bytes(report["gate"]))
            evidence_retention.sync_directory(destination)
        self.report["members"].append(copy.deepcopy(report))
        _replace_report(self.out, self.report)

    def finish(self, report: Doc, *, readiness_junit: bool = False) -> None:
        if report.get("complete") is not True or report.get("result") not in {"satisfied", "blocked"}:
            raise ValueError("composition returned an invalid aggregate status")
        if len(report["members"]) != len(self.report["members"]):
            raise ValueError("composition did not retain every required member")
        evidence_retention.sync_directory(self.out / "members")
        if not readiness_junit:
            _replace_report(self.out, report)
            return
        readiness = self.out / "readiness.xml"
        try:
            # The XML describes all completed evaluations; the canonical aggregate becomes
            # complete only after the requested derived output has been retained durably.
            xml = composition.as_junit(report)
            _replace_private(self.out, readiness.name, xml.encode("utf-8"))
            evidence_retention.sync_directory(self.out)
            _replace_report(self.out, report)
        except BaseException:
            # Ordinary write failures and controlled interruptions must not leave a passing
            # derived artifact beside a confirmed incomplete aggregate. A signal can arrive
            # just after the final rename commits: never undo an already complete bundle.
            # If the marker cannot be read, preserve the XML and the original exception.
            try:
                retained = json.loads((self.out / "report.json").read_bytes())
                if isinstance(retained, dict) and retained.get("complete") is False:
                    readiness.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
            raise
