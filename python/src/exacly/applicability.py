"""Explicit observation subjects and conservative matching to a requested target."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import jsonschema

from exacly.core import revisions_match

Doc = dict[str, Any]
TEXT = {"type": "string", "pattern": r"\S"}
SUBJECT_SCHEMA: Doc = {
    "type": "object",
    "required": ["model_revision"],
    "additionalProperties": False,
    "properties": {
        "model_revision": TEXT,
        "revision": TEXT,
        "builds": {
            "type": "object",
            "minProperties": 1,
            "propertyNames": TEXT,
            "additionalProperties": {
                "type": "object",
                "minProperties": 1,
                "propertyNames": TEXT,
                "additionalProperties": TEXT,
            },
        },
        "release": TEXT,
        "environment": TEXT,
        "platform": TEXT,
    },
}
TARGET_SCHEMA: Doc = {
    **SUBJECT_SCHEMA,
    "required": ["context", "model_revision"],
    "properties": {**SUBJECT_SCHEMA["properties"], "context": TEXT},
    "anyOf": [{"required": ["revision"]}, {"required": ["builds"]}],
}


@dataclass(frozen=True)
class Target:
    """A validated query selection, independent of a state's container context."""

    data: Doc

    def __post_init__(self) -> None:
        try:
            jsonschema.validate(self.data, TARGET_SCHEMA)
        except jsonschema.ValidationError as error:
            raise ValueError(f"invalid target: {error.message}") from error
        object.__setattr__(self, "data", copy.deepcopy(self.data))


def subject_of(assessment: Doc) -> Doc:
    """Legacy refresh tests documented their config/source revision in assessment context.

    Other legacy observations do not establish implementation identity. In particular a state file's config
    revision may have advanced while retaining observations, so it cannot fill missing assessment metadata.
    """
    if "subject" in assessment:
        return dict(assessment["subject"])
    if assessment["dimension"] == "verification" and assessment.get("source", "tests") == "tests":
        return {"model_revision": assessment["context"], "revision": assessment["context"]}
    return {}


def mismatch(assessment: Doc, target: Target) -> str | None:
    """Explain why this observation cannot establish the requested identity/scope, or return None."""
    subject = subject_of(assessment)
    selection = dict(target.data)
    # Delivery is always to a target. Its environment defaults to the selected context; source verification
    # without an environment remains a source-only claim, not verification of that deployed environment.
    if assessment["dimension"] == "delivery":
        selection.setdefault("environment", target.data["context"])
    for key, wanted in selection.items():
        if key == "context":
            continue
        if key not in subject:
            return f"observation does not identify {key}"
        actual = subject[key]
        same = revisions_match(actual, wanted) if key in ("model_revision", "revision") else actual == wanted
        if not same:
            return f"{key} does not match target"
    # A check limited to one environment/platform/release does not prove the unrestricted claim.
    for key in ("environment", "platform", "release"):
        if key in subject and key not in selection:
            return f"observation is limited to {key}; select it explicitly"
    return None
