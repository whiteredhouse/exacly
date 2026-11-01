"""Opt-in live observation of the approved nonproduction pilot's retained publication.

This test does not publish. The supported live controller must first retain native intent/receipt on
its canonical host. Ordinary CI skips this case, leaving the full REQ-037 live binding unchecked.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from exacly import execution_store, git_remote_transport, remote_publication
from exacly.github_evidence import GitHubClient


def test_live_github_exact_publication_and_operation_marker() -> None:
    configured = os.environ.get("EXACLY_LIVE_PUBLICATION_PROOF")
    if configured is None:
        pytest.skip("Live nonproduction publication proof is unavailable; controlled cases establish no live claim.")
    proof = Path(configured)
    assert proof.is_absolute()
    execution_store.location(proof)
    descriptor = os.open(proof, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = execution_store.file_info(descriptor)
        assert info.st_size <= 1024 * 1024
        with os.fdopen(os.dup(descriptor), "rb") as source:
            content = source.read(1024 * 1024 + 1)
        assert len(content) <= 1024 * 1024
    finally:
        os.close(descriptor)
    value = json.loads(
        content, object_pairs_hook=execution_store.unique_object, parse_constant=execution_store.invalid_constant
    )
    assert isinstance(value, dict) and set(value) == {"schema_version", "request", "operation_id"}
    assert value["schema_version"] == "0.1.0"
    request = remote_publication.request_from_document(value["request"])
    assert request.repository_id == "1411700094" and request.ref == "refs/heads/pilot/req039-consumer"
    assert request.operation_marker_ref is not None
    assert request.operation_marker_ref.startswith("refs/tags/req039-operations/")
    # Configured live acceptance must fail explicitly on missing credentials rather than silently
    # skipping a required producer/transport test after the pilot has been selected.
    token = os.environ.get("EXACLY_REQ039_PUBLISH_TOKEN")
    assert token, "The approved live runner lacks its protected publisher/read credential."
    transport = git_remote_transport.GitHubGitTransport(
        "https://github.com/whiteredhouse/exacly.git", request.repository_id, GitHubClient(token)
    )
    retained = remote_publication.show(request.journal, request.journal_id, value["operation_id"])
    assert retained["status"] == "published"
    assert retained["intent"]["request"] == value["request"]
    observed = transport.inspect(retained["intent"], timeout=300)
    assert observed["revision"] == retained["intent"]["authorization"]["commit"]
    assert observed["commit_present"] is True
    assert observed["operation_id"] == retained["operation_id"]
