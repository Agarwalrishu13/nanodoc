"""The command line behind `start.py`.

Nobody using this app needs to read this file. It exists because the same
entry point has to work three ways: double-clicked, run as
`python -m nanodoc`, and called by a test.
"""

from __future__ import annotations

import argparse
import sys

from . import engine, store
from .server import PORT, VERSION, build_app


def render_doctor() -> list:
    """An honest account of what this computer has, in plain sentences."""
    settings = store.load_settings()
    found = engine.detect({
        "base_url": settings.get("custom_base_url", ""),
        "api_key": settings.get("custom_api_key", ""),
    })
    lines = []
    for item in found:
        if item.get("ok") and item.get("models"):
            lines.append("%-12s running — %d model(s): %s"
                         % (item["name"], len(item["models"]), ", ".join(item["models"][:4])))
        elif item.get("ok"):
            lines.append("%-12s running, but no models downloaded yet" % item["name"])
        else:
            lines.append("%-12s %s" % (item["name"], item.get("error") or "not found"))

    if not any(item.get("ok") and item.get("models") for item in found):
        lines.append("")
        lines.append("No engine with a model was found. nanoDoc still reads, searches and quotes")
        lines.append("your documents — it just cannot write the answer out as sentences.")
        command = engine.install_command()
        if command:
            lines.append("")
            lines.append("To get one:  " + " ".join(command))
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="nanodoc", description="Drag in a document. Ask it anything about it.")
    parser.add_argument("command", nargs="?", default="start", choices=["start", "doctor", "version"])
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "version":
        print("nanoDoc %s" % VERSION)
        return 0

    if args.command == "doctor":
        print()
        print("  nanoDoc %s  ·  Python %s" % (VERSION, sys.version.split()[0]))
        print()
        print("  Memory       %s GB" % (engine.total_ram_gb() or "could not be measured"))
        print("  Your folder  %s" % store.home())
        print()
        print("  AI engines")
        for line in render_doctor():
            print("    " + line)
        print()
        return 0

    app = build_app()
    print()
    print("  nanoDoc — drag in a document, ask it anything about it.")
    print("  Everything stays on this computer.")
    app.serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
