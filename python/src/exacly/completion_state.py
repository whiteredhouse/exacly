"""Pure observer-owned state merging shared by completion and retained-proof validation."""

from __future__ import annotations

import copy
import datetime as dt
import functools
from collections.abc import Mapping
from typing import Any

from exacly.core import bindings, identity
from exacly.refresh import Run, assemble, carry, observed_again, observes, replace

Doc = dict[str, Any]


def merge(
    documents: Mapping[str, Any],
    previous: Doc,
    admitted_state: Doc,
    *,
    workflow: str,
    job: str,
    target: Doc,
    written_at: str,
) -> Doc:
    """Replace only tests bindings owned by the admitted producer, preserving other state.

    Callers first validate the model and both state documents. Retained-journal validation uses
    this same transformation after independently deriving the admitted observations from proof.
    Pending completion is separate: this function carries every current pending entry forward.
    """
    run = Run(
        revision=target["revision"],
        model_revision=target["model_revision"],
        context=target["context"],
        observed_at=dt.datetime.fromisoformat(admitted_state["written_at"].replace("Z", "+00:00")),
        workflow=workflow,
        job=job,
    )
    carried = carry(documents, target["context"], previous)
    for rid, record in carried.records.items():
        selected = bindings(record, "tests")
        current = [identity("tests", binding) for binding in selected]
        seen = [identity("tests", binding) for binding in selected if observes(binding, run)]
        fresh = copy.deepcopy(admitted_state["records"].get(rid, {}).get("assessments", []))
        replace(carried, rid, fresh, functools.partial(observed_again, set(), seen, current, len(selected) == 1))
    assembled = assemble(carried, target["context"], target["model_revision"], written_at)
    result = copy.deepcopy(previous)
    for key in ("relationships", "pending"):
        result.pop(key, None)
    result.update(assembled)
    return result
