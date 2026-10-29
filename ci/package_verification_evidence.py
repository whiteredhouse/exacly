"""Compatibility entry point for the installed GitHub evidence packager.

Use ``exacly evidence package`` in producer workflows. The implementation lives in
``exacly.evidence`` so installed distributions do not need this repository script.
"""

from exacly.evidence import MAX_REPORT_BYTES as MAX_REPORT_BYTES
from exacly.evidence import EvidenceError as EvidenceError
from exacly.evidence import main as main
from exacly.evidence import package as package

if __name__ == "__main__":
    raise SystemExit(main())
