"""Private trusted worker: gated startup, parent-death containment and bounded IPC."""

from __future__ import annotations

import base64
import ctypes
import math
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(2)
    descriptor, parent = (int(value) for value in sys.argv[1:3])
    expires = float(sys.argv[3])
    if not math.isfinite(expires):
        raise SystemExit(2)
    # Installing the signal and then rechecking closes the parent-death initialization race.
    if os.getppid() != parent:
        raise SystemExit(2)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        raise SystemExit(2)
    with socket.socket(fileno=descriptor) as channel:
        remaining = expires - time.monotonic()
        if remaining <= 0:
            os._exit(124)
        # Kernel termination also works while the parent is suspended or Python cleanup is
        # already unwinding. Bubblewrap dies with this worker; the parent still must verify
        # exact namespace termination before freeing its durable concurrency reservation.
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
        signal.setitimer(signal.ITIMER_REAL, remaining)
        # Execute this file directly under -I so even the package initializer runs only after
        # parent-death containment and the full wall-clock timer are installed.
        from exacly import executor_runtime as control
        from exacly import isolation

        try:
            request = control._receive(channel)
            control._require(
                isinstance(request, dict) and set(request) == {"runtime", "argv", "inputs", "out", "timeout"},
                "invalid trusted worker request",
            )
            runtime = isolation.parse_policy(request["runtime"])
            encoded = request["inputs"]
            control._require(isinstance(encoded, dict), "invalid worker snapshots")
            inputs = {name: base64.b64decode(value, validate=True) for name, value in encoded.items()}

            def before_release(pid: int, facts: dict[str, Any]) -> None:
                saved = control.identity(pid)
                control._require(
                    saved["pid_namespace"] == facts["namespaces"]["pid"], "namespace identity differs from attestation"
                )
                control._send(channel, {"type": "namespace", "identity": saved})
                control._require(
                    control._receive(channel, limit=1024) == {"type": "release"}, "candidate release refused"
                )

            report = isolation.run(
                runtime,
                request["argv"],
                inputs,
                Path(request["out"]),
                timeout=request["timeout"],
                on_namespace=before_release,
            )
            control._send(channel, {"type": "result", "report": report})
        except (OSError, ValueError, TypeError, KeyError) as error:
            signal.setitimer(signal.ITIMER_REAL, 0)
            control._send(channel, {"type": "error", "reason": str(error)[:4096]})
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    main()
