"""Install an exact published version with a bounded wait for PyPI index propagation."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


def install(
    version: str, python: Path, timeout: float = 120, interval: float = 10, *, deadline: float | None = None
) -> None:
    """Retry installation within a wall-clock budget; retain the final resolver diagnostic."""
    if timeout <= 0 or interval <= 0:
        raise ValueError("timeout and interval must be positive")
    if deadline is None:
        deadline = time.monotonic() + timeout
    diagnostic = "no install attempt completed"
    attempt = 0
    while (remaining := deadline - time.monotonic()) > 0:
        attempt += 1
        print(f"Installing exacly=={version}, attempt {attempt}", flush=True)
        try:
            result = subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--refresh-package",
                    "exacly",
                    "--default-index",
                    "https://pypi.org/simple",
                    f"exacly=={version}",
                ],
                capture_output=True,
                text=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as error:
            detail = error.stderr or error.stdout
            diagnostic = detail.decode(errors="replace") if isinstance(detail, bytes) else detail or diagnostic
            raise RuntimeError(f"Installation exceeded {timeout:g}s: {diagnostic}") from error
        if result.returncode == 0:
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)
            return
        diagnostic = result.stderr or result.stdout or f"uv exited {result.returncode}"
        print(diagnostic, file=sys.stderr, flush=True)
        if (remaining := deadline - time.monotonic()) > 0:
            time.sleep(min(interval, remaining))
    raise RuntimeError(f"Could not install exacly=={version} within {timeout:g}s: {diagnostic}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--environment", required=True, type=Path)
    args = parser.parse_args()
    timeout = 120
    deadline = time.monotonic() + timeout
    try:
        subprocess.run(["uv", "venv", "--python", sys.executable, str(args.environment)], check=True, timeout=timeout)
        install(args.version, args.environment / "bin" / "python", timeout=timeout, deadline=deadline)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as error:
        print(f"Publication install failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
