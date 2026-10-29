"""Compatibility entry point for the installed GitHub evidence packager.

Use ``exacly package github-actions`` in producer workflows. The implementation lives in
``exacly.evidence_packaging`` so installed distributions do not need this repository script.
"""

from exacly.evidence_packaging import MAX_REPORT_BYTES as MAX_REPORT_BYTES
from exacly.evidence_packaging import EvidenceError as EvidenceError
from exacly.evidence_packaging import main as main
from exacly.evidence_packaging import package as package

if __name__ == "__main__":
    raise SystemExit(main())
