#!/usr/bin/env python3
"""Double-click entry point for nanoDoc.

Keeps the "just run it" path dead simple: this file works no matter what your
current directory is, no matter which Python is on PATH, and it explains itself
in plain language if anything is missing.
"""

import os
import sys

MIN_PYTHON = (3, 9)


def _fail(message: str, code: int = 1) -> "None":
    print()
    print("  " + message)
    print()
    if os.name == "nt":
        try:
            input("  Press Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(code)


def main() -> None:
    if sys.version_info < MIN_PYTHON:
        _fail(
            "This app needs Python %d.%d or newer, but you are running %d.%d."
            % (*MIN_PYTHON, sys.version_info.major, sys.version_info.minor)
        )

    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    try:
        from nanodoc.__main__ import main as cli
    except ImportError as exc:  # pragma: no cover - only on a broken checkout
        _fail("Could not load the app (%s).\n  Make sure you downloaded the whole folder." % exc)

    cli()


if __name__ == "__main__":
    main()
