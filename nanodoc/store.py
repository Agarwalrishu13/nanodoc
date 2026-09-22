"""Where your documents and your choices are kept.

Everything lives in one folder in your home directory:

```
~/.nanodoc/
├── settings.json     your choices (which engine, which model)
├── library.json      one line per document you have added
├── docs/<id>.json    the text that was read out of each document
└── uploads/          scratch space while a file is still arriving
```

Delete that folder and the app forgets everything — which is possible because
there is nowhere else anything is kept. No account, no server, no copies
anywhere else.

The text of a document is written to `docs/` once, when it is dropped in, so
reopening an old document is instant and does not need the original file. The
original is never modified, and never moved.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from . import documents

APP_DIR = ".nanodoc"

DEFAULT_SETTINGS = {
    "engine_id": "",
    "model": "",
    "custom_base_url": "",
    "custom_api_key": "",
    "last_opened": "",
}


def home() -> Path:
    override = os.environ.get("NANODOC_HOME")
    root = Path(override) if override else Path(os.path.expanduser("~")) / APP_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def docs_dir() -> Path:
    path = home() / "docs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def uploads_dir() -> Path:
    path = home() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return home() / "settings.json"


def library_path() -> Path:
    return home() / "library.json"


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _write_json(path: Path, data) -> None:
    """Write JSON so that a crash mid-write cannot corrupt the old file."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def load_settings() -> dict:
    stored = _read_json(settings_path(), {})
    if not isinstance(stored, dict):
        stored = {}
    return {**DEFAULT_SETTINGS, **stored}


def save_settings(patch: dict) -> dict:
    settings = load_settings()
    for key, value in (patch or {}).items():
        if key in DEFAULT_SETTINGS:
            settings[key] = value
    _write_json(settings_path(), settings)
    return settings


# --------------------------------------------------------------------------
# The library
# --------------------------------------------------------------------------
def _safe_name(name: str) -> str:
    """A file name that cannot point anywhere except at itself.

    A dropped file's name is only ever used as a label and as part of the
    ``source_name``, but it is still cleaned: a name containing a slash or
    ``..`` has no business being written into a JSON file that later gets
    joined to a path.
    """
    cleaned = re.sub(r"[\\/\x00-\x1f]", "-", str(name or ""))
    # A name is only ever a label here, but a name that still reads ".." has no
    # business being written into a JSON file that later gets joined to a path.
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = cleaned.strip().strip(".").strip()
    cleaned = cleaned or "document"
    return cleaned[:180]


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def load_library() -> list:
    entries = _read_json(library_path(), [])
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("id")]


def _save_library(entries: list) -> None:
    _write_json(library_path(), entries)


def save_document(doc: documents.Doc) -> dict:
    """Store a freshly read document, replacing an earlier copy of the same file.

    Dropping the same file in twice is nearly always a mistake or a newer
    version of it, so the old text is replaced rather than piling up a second
    entry with the same name. Everything else is additive.
    """
    name = _safe_name(doc.name)
    doc.name = name

    entries = load_library()
    existing = next((entry for entry in entries if entry.get("source_name") == name), None)
    doc_id = existing["id"] if existing else new_id()

    doc_json = doc.to_dict(with_pages=True)
    _write_json(docs_dir() / (doc_id + ".json"), doc_json)

    entry = documents.summary(doc)
    entry["id"] = doc_id
    entry["source_name"] = name
    if existing:
        entry["replaced"] = True
        entries = [item for item in entries if item.get("id") != doc_id]

    entries.insert(0, entry)
    _save_library(entries)
    return entry


def load_document(doc_id: str) -> documents.Doc | None:
    if not re.fullmatch(r"[0-9a-f]{6,32}", str(doc_id or "")):
        return None
    path = docs_dir() / (doc_id + ".json")
    if not path.is_file():
        return None
    data = _read_json(path, None)
    if not isinstance(data, dict):
        return None
    doc = documents.Doc.from_dict(data)
    doc.added = float(data.get("added", 0.0))
    return doc


def entry_for(doc_id: str) -> dict | None:
    for entry in load_library():
        if entry.get("id") == doc_id:
            return entry
    return None


def delete_document(doc_id: str) -> bool:
    entry = entry_for(doc_id)
    if not entry:
        return False
    _save_library([item for item in load_library() if item.get("id") != doc_id])
    try:
        (docs_dir() / (doc_id + ".json")).unlink()
    except OSError:
        pass
    return True


def forget_everything() -> None:
    """Empty the library. Used by tests, and by anyone who wants a clean slate."""
    shutil.rmtree(docs_dir(), ignore_errors=True)
    _save_library([])


def stats() -> dict:
    entries = load_library()
    return {
        "documents": len(entries),
        "words": sum(int(entry.get("words", 0)) for entry in entries),
        "folder": str(home()),
        "now": time.time(),
    }
