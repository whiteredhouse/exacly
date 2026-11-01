"""Bounded input snapshots and immutable local implementation-proposal bundles.

The final ``proposal.json`` file is the bundle's create-only commit marker. A directory
without that marker is incomplete and grants no authority. Retained paths are evidence
references; loading a bundle never follows its original source paths.
"""

from __future__ import annotations

import copy
import datetime as dt
import glob
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from exacly import composition_io, evidence_retention, planning
from exacly.applicability import Target

Doc = dict[str, Any]
MAX_DOCUMENT_BYTES = composition_io.MAX_DOCUMENT_BYTES
MAX_INPUT_BYTES = composition_io.MAX_INPUT_BYTES
MAX_DOCUMENTS = composition_io.MAX_DOCUMENTS
SUFFIXES = {".json", ".yaml", ".yml"}
RETAINED_PATH = re.compile(r"inputs/(?:model-[0-9]{4}|intent|policy|target|previous)\.(?:json|yaml|yml)")


def _require_storage() -> None:
    evidence_retention.require_private_filesystem()
    if not hasattr(os, "link"):
        raise ValueError("immutable implementation bundles require create-only local file links")


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _json_bytes(document: Any, label: str) -> bytes:
    """Bound serialization as well as reads; repeated task metadata can exceed input size."""
    chunks: list[bytes] = []
    size = 1
    try:
        for text in json.JSONEncoder(indent=2, sort_keys=True, allow_nan=False).iterencode(document):
            chunk = text.encode("utf-8")
            size += len(chunk)
            if size > MAX_DOCUMENT_BYTES:
                raise ValueError(f"implementation {label} exceeds the document size limit")
            chunks.append(chunk)
    except (TypeError, RecursionError, UnicodeError) as error:
        raise ValueError(f"implementation {label} is not bounded JSON-compatible content") from error
    return b"".join(chunks) + b"\n"


def _parse(content: bytes, suffix: str) -> Any:
    try:
        text = content.decode("utf-8")
        if suffix == ".json":

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
                if index > composition_io.MAX_NODES:
                    raise ValueError("implementation input exceeds the YAML token limit")
                if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
                    raise ValueError("implementation inputs do not support YAML aliases or anchors")
            document = yaml.load(text, composition_io.UniqueLoader)
        document = planning.plain(document)
        composition_io._plain(document)
        return document
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as error:
        raise ValueError(f"invalid or excessively nested implementation input: {error}") from error


def _raw(path: Path) -> bytes:
    evidence_retention.require_private_filesystem()
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("implementation inputs must be regular files")
        content = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("implementation input exceeds the document size limit")
    return content


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except RuntimeError as error:
        raise ValueError("implementation input path cannot be resolved") from error


@dataclass(frozen=True)
class Snapshot:
    role: str
    source: Path
    path: str
    content: bytes
    document_digest: str

    def entry(self) -> Doc:
        return {
            "role": self.role,
            "source": str(self.source),
            "path": self.path,
            "bytes": len(self.content),
            "raw_digest": _digest(self.content),
            "document_digest": self.document_digest,
        }


@dataclass
class LoadedInputs:
    documents: Doc
    intent: Any
    policy: Any
    target: Any
    previous: Any | None
    snapshots: tuple[Snapshot, ...]
    model_folders: tuple[Path, ...]


def load_inputs(
    models: list[Path], *, intent: Path, policy: Path, target: Path, previous: Path | None = None
) -> LoadedInputs:
    """Snapshot every explicit input once; retain those exact bytes rather than rereading it later."""
    _require_storage()
    files: list[Path] = []
    folders: set[Path] = set()
    for supplied in models:
        if glob.has_magic(str(supplied)):
            prefix = []
            for part in supplied.parts:
                if glob.has_magic(part):
                    break
                prefix.append(part)
            folders.add(Path(*prefix).resolve() if prefix else Path.cwd())
            found = sorted(Path(name) for name in glob.glob(str(supplied)) if Path(name).is_file())
            if not found:
                raise ValueError(f"model input matches no files: {supplied}")
            files.extend(found)
        elif supplied.is_dir():
            folders.add(_resolved(supplied))
            files.extend(
                sorted(
                    item
                    for item in supplied.rglob("*")
                    if item.is_file()
                    and item.suffix in SUFFIXES
                    and not any(part.startswith(".") for part in item.relative_to(supplied).parts)
                )
            )
        else:
            files.append(supplied)
    if not files:
        raise ValueError("at least one model document is required")
    if len(files) > MAX_DOCUMENTS:
        raise ValueError("implementation input document count exceeds the limit")
    snapshots: list[Snapshot] = []
    total = 0

    def capture(role: str, path: Path, name: str) -> Any:
        nonlocal total
        resolved = _resolved(path)
        content = _raw(resolved)
        total += len(content)
        if total > MAX_INPUT_BYTES:
            raise ValueError("implementation inputs exceed the total size limit")
        suffix = path.suffix.lower() if path.suffix.lower() in SUFFIXES else ".yaml"
        document = _parse(content, suffix)
        snapshots.append(Snapshot(role, resolved, f"inputs/{name}{suffix}", content, planning.digest(document)))
        return document

    documents: Doc = {}
    seen: set[Path] = set()
    for index, path in enumerate(files):
        resolved = _resolved(path)
        if resolved in seen:
            raise ValueError("duplicate model input or alias")
        seen.add(resolved)
        folders.add(resolved.parent)
        documents[str(path)] = capture("model", path, f"model-{index:04d}")
    intent_doc = capture("intent", intent, "intent")
    policy_doc = capture("policy", policy, "policy")
    target_doc = capture("target", target, "target")
    previous_doc = capture("previous", previous, "previous") if previous is not None else None
    return LoadedInputs(
        documents, intent_doc, policy_doc, target_doc, previous_doc, tuple(snapshots), tuple(sorted(folders))
    )


def protect_output(out: Path, loaded: LoadedInputs) -> Path:
    return composition_io.protect_output(out, [snapshot.source for snapshot in loaded.snapshots], loaded.model_folders)


def retain(out: Path, loaded: LoadedInputs, proposal: Doc) -> Path:
    """Reserve a new private destination and publish proposal.json after the complete input manifest.

    Reserving the directory with mkdir and publishing its marker with link are create-only
    operations: another invocation cannot replace a previous bundle, even an empty directory.
    Errors before the marker leave private incomplete diagnostics. No fallible normal-path work
    follows the final marker publication.
    """
    _require_storage()
    destination = protect_output(out, loaded)
    proposal_bytes = _json_bytes(proposal, "proposal")
    manifest: Doc = {
        "schema_version": "0.1.0",
        "kind": "implementation-proposal-bundle",
        "proposal": {"path": "proposal.json", "raw_digest": _digest(proposal_bytes), "digest": proposal["digest"]},
        "inputs": [snapshot.entry() for snapshot in loaded.snapshots],
    }
    manifest["digest"] = planning.digest(manifest)
    manifest_bytes = _json_bytes(manifest, "manifest")
    evidence_retention.private_parents(destination.parent)
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    inputs = destination / "inputs"
    inputs.mkdir(mode=0o700)
    inputs.chmod(0o700)
    for snapshot in loaded.snapshots:
        evidence_retention.write_private(destination / snapshot.path, snapshot.content)
    evidence_retention.sync_directory(inputs)
    evidence_retention.write_private(destination / "manifest.json", manifest_bytes)
    marker = destination / ".proposal-prepared"
    evidence_retention.write_private(marker, proposal_bytes)
    evidence_retention.sync_directory(destination)
    evidence_retention.sync_directory(destination.parent)
    # Keep the prepared marker as a second hardlink: cleanup after publication could turn
    # successful publication into a reported error, or damage a concurrently inspected bundle.
    try:
        os.link(marker, destination / "proposal.json", follow_symlinks=False)
    except NotImplementedError as error:
        raise ValueError("immutable implementation bundles require create-only local file links") from error
    return destination


def load_bundle(bundle: Path) -> Doc:
    """Verify a complete bundle and every retained byte without reading original source paths."""
    root = _resolved(bundle)
    if not root.is_dir():
        raise ValueError("--bundle must be an implementation proposal directory")
    manifest = _parse(_raw(root / "manifest.json"), ".json")
    proposal_bytes = _raw(root / "proposal.json")
    proposal = _parse(proposal_bytes, ".json")
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "kind", "proposal", "inputs", "digest"}
        or manifest["schema_version"] != "0.1.0"
        or manifest["kind"] != "implementation-proposal-bundle"
        or planning.digest({key: value for key, value in manifest.items() if key != "digest"}) != manifest["digest"]
        or not isinstance(proposal, dict)
        or manifest["proposal"]
        != {"path": "proposal.json", "raw_digest": _digest(proposal_bytes), "digest": proposal.get("digest")}
    ):
        raise ValueError("implementation bundle identity is invalid")
    entries = manifest["inputs"]
    if not isinstance(entries, list) or not 4 <= len(entries) <= MAX_DOCUMENTS + 4:
        raise ValueError("implementation bundle input inventory is invalid")
    roles: list[str] = []
    paths: set[str] = set()
    documents: Doc = {}
    named: Doc = {}
    total = 0
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"role", "source", "path", "bytes", "raw_digest", "document_digest"}
            or not isinstance(entry["role"], str)
            or entry["role"] not in {"model", "intent", "policy", "target", "previous"}
            or not isinstance(entry["source"], str)
            or not isinstance(entry["path"], str)
            or RETAINED_PATH.fullmatch(entry["path"]) is None
            or entry["path"] in paths
            or type(entry["bytes"]) is not int
            or entry["bytes"] < 0
            or (entry["role"] == "model") != Path(entry["path"]).stem.startswith("model-")
            or (entry["role"] != "model" and Path(entry["path"]).stem != entry["role"])
        ):
            raise ValueError("implementation bundle input inventory is invalid")
        retained = root / entry["path"]
        if retained.parent.is_symlink() or not _resolved(retained).is_relative_to(root):
            raise ValueError("implementation bundle input escapes its private directory")
        content = _raw(retained)
        total += len(content)
        if total > MAX_INPUT_BYTES:
            raise ValueError("implementation bundle exceeds the total input size limit")
        document = _parse(content, retained.suffix)
        if (
            len(content) != entry["bytes"]
            or _digest(content) != entry["raw_digest"]
            or planning.digest(document) != entry["document_digest"]
        ):
            raise ValueError("retained implementation input bytes disagree with the manifest")
        if entry["role"] == "model":
            documents[entry["path"]] = document
        else:
            named[entry["role"]] = document
        paths.add(entry["path"])
        roles.append(entry["role"])
    if any(roles.count(role) != 1 for role in ("intent", "policy", "target")) or roles.count("previous") > 1:
        raise ValueError("implementation bundle required input roles disagree")
    if "model" not in roles:
        raise ValueError("implementation bundle has no model input")
    if roles.count("model") > MAX_DOCUMENTS:
        raise ValueError("implementation bundle model input inventory exceeds the limit")
    from exacly import implementation_planning

    try:
        approval = proposal["approval"]
        reproduced = implementation_planning.create(
            documents,
            named["intent"],
            named["policy"],
            Target(named["target"]),
            dt.date.fromisoformat(proposal["as_of"]),
            approved_intent_digest=approval["intent_digest"],
            approved_policy_digest=approval["policy_digest"],
            issuer=approval["issuer"],
            approval_id=approval["id"],
            previous=named.get("previous"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("implementation bundle cannot reproduce its proposal from retained inputs") from error
    if reproduced != proposal:
        raise ValueError("implementation proposal disagrees with its retained input snapshots")
    return copy.deepcopy(proposal)
