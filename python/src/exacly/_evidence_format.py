"""Shared wire contract for packaged GitHub verification evidence.

These definitions describe the bundle, not admission receipts or proof of its origin.
Producer and consumer validation remain independent.
"""

from typing import Any

MANIFEST_VERSION = "0.1.0"
REPORT_LIMIT = 8 * 1024 * 1024
MANIFEST_LIMIT = 64 * 1024

MANIFEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"const": MANIFEST_VERSION},
        "repository_id": {"type": "integer", "minimum": 1},
        "repository": {"type": "string"},
        "run_id": {"type": "integer", "minimum": 1},
        "run_attempt": {"type": "integer", "minimum": 1},
        "workflow_path": {"type": "string", "pattern": r"\S"},
        "job": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_-]*$"},
        "implementation_revision": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "report_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "check_outcome": {"enum": ["success", "failure"]},
    },
    "required": [
        "schema_version",
        "repository_id",
        "repository",
        "run_id",
        "run_attempt",
        "workflow_path",
        "job",
        "implementation_revision",
        "report_digest",
        "check_outcome",
    ],
    "additionalProperties": False,
}
