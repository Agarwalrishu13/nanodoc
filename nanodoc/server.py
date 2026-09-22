"""Every address the page can ask for, each one commented.

The page is a single HTML file; this file is the whole back end. Requests
arrive only from 127.0.0.1, because the server binds the loopback address and
nothing else.

The one idea worth knowing before reading: *finding* and *writing* are two
separate steps. Finding is a search over the document and always works, with
nothing installed. Writing sends the few paragraphs that were found to a local
AI to be turned into a sentence. If there is no AI, the search still happens
and the page says so plainly instead of failing.
"""

from __future__ import annotations

import re
import threading
import uuid
from pathlib import Path

from . import documents, engine, httpbase, search, store
from .httpbase import App, Error, Json, Stream

VERSION = "0.1.0"
PORT = 8781

# One in-flight upload per id. Small and process-local on purpose: the file is
# arriving from this machine, so there is never more than one of them at once.
_uploads: dict = {}
_uploads_lock = threading.Lock()

# Search indexes are held in memory, because rebuilding one on every question
# would be felt on a long document. Four is enough for the way this app is
# used — one document open, occasionally comparing two — and it means a
# document's index never outlives the app by much.
_indexes: dict = {}
_indexes_lock = threading.Lock()
INDEX_CACHE = 4

MAX_QUESTION = 500

# The sentence the prompt tells the model to use when the extracts do not
# contain the answer. Recognised so it is not mistaken for a bad answer.
_REFUSAL = re.compile(r"could not find that in your document", re.I)

# "(page 4)" and its relatives, which the model is told to write. They are
# references to the document, not claims about it, so they are removed before
# an answer's words are judged.
_CITATION = re.compile(r"\((?:page|part|section|sheet)\s+[^)]{1,60}\)", re.I)

# How much of a written answer must be traceable to the paragraphs it was given
# before the app is willing to present it without a warning. See
# `_answer_support` for why this check exists at all.
SUPPORT_THRESHOLD = 0.4


def build_app(web_dir=None) -> App:
    app = App("nanoDoc", web_dir or (Path(__file__).parent / "web"), version=VERSION)

    # ------------------------------------------------------------------ state
    @app.get("/api/state")
    def state(_request):
        """Everything the page needs to draw itself on load."""
        settings = store.load_settings()
        engines = engine.detect({
            "base_url": settings.get("custom_base_url", ""),
            "api_key": settings.get("custom_api_key", ""),
        })
        chosen, model = engine.pick(engines, settings.get("engine_id", ""), settings.get("model", ""))
        return Json({
            "documents": store.load_library(),
            "engine": engine.summary(engines, chosen, model),
            "stats": store.stats(),
            "settings": {
                "engine_id": chosen["id"] if chosen else "",
                "model": model,
                "custom_base_url": settings.get("custom_base_url", ""),
            },
            "version": VERSION,
        })

    @app.post("/api/settings")
    def save_settings(request):
        body = request.json()
        patch = {}
        if "engine_id" in body:
            patch["engine_id"] = str(body["engine_id"])[:40]
        if "model" in body:
            patch["model"] = str(body["model"])[:120]
        if "custom_base_url" in body:
            patch["custom_base_url"] = str(body["custom_base_url"])[:300].strip()
        if "custom_api_key" in body:
            patch["custom_api_key"] = str(body["custom_api_key"])[:300].strip()
        saved = store.save_settings(patch)
        return Json({"ok": True, "settings": saved})

    # ---------------------------------------------------------------- upload
    @app.post("/api/upload/start")
    def upload_start(request):
        name = str(request.json().get("name", "document"))[:200]
        upload_id = uuid.uuid4().hex
        target = store.uploads_dir() / (upload_id + ".part")
        target.write_bytes(b"")
        with _uploads_lock:
            _uploads[upload_id] = {"name": name, "path": target, "received": 0}
        return Json({"id": upload_id, "name": name, "chunk_bytes": 8 * 1024 * 1024})

    @app.post("/api/upload/chunk")
    def upload_chunk(request):
        upload_id = request.q("id", "")
        with _uploads_lock:
            entry = _uploads.get(upload_id)
        if not entry:
            return Error("That upload expired. Start again.")
        try:
            offset = int(request.q("offset", "-1"))
        except ValueError:
            return Error("Bad offset.")
        if offset != entry["received"]:
            return Error("The pieces arrived out of order.", 409, expected=entry["received"])
        with open(entry["path"], "ab") as handle:
            handle.write(request.body)
        entry["received"] += len(request.body)
        return Json({"received": entry["received"]})

    @app.post("/api/upload/finish")
    def upload_finish(request):
        """Read the file that has just arrived and add it to the library.

        Reading happens here, in the request that ends the upload, so the page
        can report "this is a 14-page PDF, 6,200 words" immediately rather than
        after another round trip.
        """
        upload_id = str(request.json().get("id", ""))
        with _uploads_lock:
            entry = _uploads.pop(upload_id, None)
        if not entry:
            return Error("That upload expired. Start again.")

        path = entry["path"]
        name = entry["name"]
        try:
            data = path.read_bytes()
        finally:
            try:
                path.unlink()
            except OSError:
                pass

        try:
            doc = documents.read(name, data)
        except documents.Unreadable as exc:
            return Error(str(exc), 400, unreadable=True)
        except Exception as exc:  # a reader bug must not look like a crash
            return Error("I could not read “%s”. (%s)" % (name, exc), 400, unreadable=True)

        saved = store.save_document(doc)
        with _indexes_lock:
            _indexes.pop(saved["id"], None)

        return Json({
            "document": saved,
            "about": search.about(saved["id"], doc.pages),
            "replaced": bool(saved.get("replaced")),
            "message": (
                "“%s” was already here, so I read it again." % saved["name"]
                if saved.get("replaced")
                else "“%s” is ready — %s, %s words."
                % (saved["name"], saved["kind"].lower(), "{:,}".format(int(saved.get("words", 0))))
            ),
        })

    # ------------------------------------------------------------- documents
    # Route parameters arrive on the request (`request.params`), because the
    # toolkit hands every handler the same single argument.
    @app.get("/api/doc/{doc_id}")
    def get_document(request):
        doc_id = request.params.get("doc_id", "")
        doc = store.load_document(doc_id)
        if not doc:
            return Error("That document is not in your library any more.", 404)
        entry = store.entry_for(doc_id) or {}
        return Json({
            "document": doc.to_dict(with_pages=True),
            "entry": entry,
            "about": search.about(doc_id, doc.pages),
        })

    @app.delete("/api/doc/{doc_id}")
    def delete_document(request):
        doc_id = request.params.get("doc_id", "")
        if not store.delete_document(doc_id):
            return Error("That document is not in your library any more.", 404)
        with _indexes_lock:
            _indexes.pop(doc_id, None)
        return Json({"ok": True, "documents": store.load_library()})

    # -------------------------------------------------------------- searching
    @app.post("/api/search")
    def find_passages(request):
        """Find the parts of a document that match, with no AI involved.

        The page calls this to show what it found before any answer arrives,
        and it is also the whole answer when there is no engine installed.
        """
        body = request.json()
        doc_id = str(body.get("id", ""))
        question = str(body.get("question", ""))[:MAX_QUESTION].strip()
        if not question:
            return Error("Type a question first.")

        doc = store.load_document(doc_id)
        if not doc:
            return Error("That document is not in your library any more.", 404)

        index = _index_for(doc_id, doc)
        hits = index.search(question, limit=6)
        return Json({
            "hits": [hit.to_dict() for hit in hits],
            "confidence": index.confidence(question, hits),
            "question": question,
        })

    # -------------------------------------------------------------- answering
    @app.post("/api/ask")
    def ask(request):
        """Answer a question about a document, streaming as it is written."""
        body = request.json()
        doc_id = str(body.get("id", ""))
        question = str(body.get("question", ""))[:MAX_QUESTION].strip()
        want_summary = bool(body.get("summary"))

        doc = store.load_document(doc_id)
        if not doc:
            return Error("That document is not in your library any more.", 404)

        index = _index_for(doc_id, doc)
        settings = store.load_settings()
        engines = engine.detect({
            "base_url": settings.get("custom_base_url", ""),
            "api_key": settings.get("custom_api_key", ""),
        })
        chosen, model = engine.pick(engines, settings.get("engine_id", ""), settings.get("model", ""))

        return Stream.sse(_answer_events(doc, index, question, want_summary, chosen, model))

    @app.post("/api/summary")
    def summarise(request):
        body = request.json()
        doc_id = str(body.get("id", ""))
        doc = store.load_document(doc_id)
        if not doc:
            return Error("That document is not in your library any more.", 404)

        index = _index_for(doc_id, doc)
        settings = store.load_settings()
        engines = engine.detect({
            "base_url": settings.get("custom_base_url", ""),
            "api_key": settings.get("custom_api_key", ""),
        })
        chosen, model = engine.pick(engines, settings.get("engine_id", ""), settings.get("model", ""))
        return Stream.sse(_answer_events(doc, index, "", True, chosen, model))

    # --------------------------------------------------------------- engines
    @app.get("/api/engines")
    def engines(_request):
        settings = store.load_settings()
        found = engine.detect({
            "base_url": settings.get("custom_base_url", ""),
            "api_key": settings.get("custom_api_key", ""),
        })
        chosen, model = engine.pick(found, settings.get("engine_id", ""), settings.get("model", ""))
        return Json(engine.summary(found, chosen, model))

    @app.post("/api/engine/install")
    def install_engine(_request):
        """Install an engine for somebody who does not use a terminal.

        The exact command is shown in the log before it runs, because a button
        that silently installs software is not a button anybody should press.
        """
        return Stream.sse(engine.install_stream())

    @app.post("/api/engine/pull")
    def pull_model(request):
        model = str(request.json().get("model", ""))[:120].strip()
        if not model:
            return Error("Which model?")
        settings = store.load_settings()
        found = engine.detect({
            "base_url": settings.get("custom_base_url", ""),
            "api_key": settings.get("custom_api_key", ""),
        })
        ollama = next((item for item in found if item["id"] == "ollama" and item.get("ok")), None)
        if not ollama:
            return Error("Ollama needs to be running before I can download a model.")
        return Stream.sse(engine.pull_stream(ollama, model))

    @app.get("/api/doctor")
    def doctor(_request):
        """A plain-language account of what this computer has. No tokens needed."""
        settings = store.load_settings()
        found = engine.detect({
            "base_url": settings.get("custom_base_url", ""),
            "api_key": settings.get("custom_api_key", ""),
        })
        chosen, model = engine.pick(found, settings.get("engine_id", ""), settings.get("model", ""))
        lines = []
        for item in found:
            if item.get("ok") and item.get("models"):
                lines.append("%s is running with %d model(s)." % (item["name"], len(item["models"])))
            elif item.get("ok"):
                lines.append("%s is running, but has no models yet." % item["name"])
            else:
                lines.append("%s: %s" % (item["name"], item.get("error") or "not found"))
        return Json({
            "summary": engine.summary(found, chosen, model),
            "lines": lines,
            "library": store.stats(),
        })

    return app


# --------------------------------------------------------------------------
# Answering, step by step
# --------------------------------------------------------------------------
def _index_for(doc_id: str, doc: documents.Doc) -> search.Index:
    with _indexes_lock:
        cached = _indexes.get(doc_id)
        if cached is not None:
            return cached
    index = search.Index(doc_id, doc.pages)
    with _indexes_lock:
        _indexes[doc_id] = index
        while len(_indexes) > INDEX_CACHE:
            _indexes.pop(next(iter(_indexes)))
    return index


def _answer_events(doc, index, question, want_summary, chosen, model):
    """The whole answer as a series of events the page understands.

    Order matters: the passages are found and sent first, so the reader sees
    where the answer comes from before reading the answer. Then either the
    text streams in, or the page is told why no text is coming.
    """
    if want_summary:
        keywords = search.about(doc.name, doc.pages)["keywords"]
        hits = index.search(" ".join(keywords[:8]), limit=5) or index.search(doc.title or doc.name, limit=5)
        if not hits and index.passages:
            # Nothing matched the document's own vocabulary; the opening pages
            # still describe it better than an empty answer would.
            hits = [search.Hit(passage=passage, score=0.0, matched_terms=[], snippet=passage.text)
                    for passage in index.passages[:3]]
        confidence = {"found": True, "coverage": 1.0, "reason": "summary"}
        question_text = "What is this document about?"
    else:
        hits = index.search(question, limit=6)
        confidence = index.confidence(question, hits)
        question_text = question

    yield {
        "type": "found",
        "hits": [hit.to_dict() for hit in hits],
        "confidence": confidence,
        "question": question_text,
    }

    if not hits:
        yield {
            "type": "note",
            "message": "I could not find anything in “%s” about that. Try different words — "
                       "or check that it is the document you meant." % doc.name,
        }
        yield {"type": "done", "answered": False}
        return

    if not chosen or not model:
        yield {
            "type": "no_engine",
            "message": "I found these parts of your document, and I can point at exactly where. "
                       "Writing them into a sentence needs a local AI engine, and there is not one "
                       "running on this computer yet.",
        }
        yield {"type": "done", "answered": False}
        return

    messages = engine.build_messages(question_text, hits, doc.name, summary=want_summary)
    yield {"type": "engine", "name": chosen["name"], "model": model}

    written = []
    try:
        for piece in engine.answer_stream(chosen, model, messages):
            written.append(piece)
            yield {"type": "answer", "text": piece}
        if not written:
            yield {
                "type": "note",
                "message": "%s started answering but sent nothing back. Try again, or pick a "
                           "different model." % chosen["name"],
            }
    except RuntimeError as exc:
        yield {"type": "note", "message": str(exc)}
    except Exception as exc:  # a broken engine must not take the page down
        yield {"type": "note", "message": "The engine stopped part-way through: %s" % exc}

    support = _answer_support("".join(written), hits)
    if support.get("checked") and not support.get("supported"):
        yield {
            "type": "unsupported",
            "ratio": support.get("ratio"),
            "message": "The AI wrote something that does not appear in your document. A small "
                       "model does this — it ignores what it was given and carries on by itself. "
                       "Read the paragraphs below instead; they are your document's own words.",
        }

    yield {"type": "done", "answered": True}


def _answer_support(answer: str, hits: list) -> dict:
    """How much of a written answer can actually be traced to the document.

    This exists because of a real failure. Asked about the SELECT statement in
    a database assignment, a 17M-parameter model trained from scratch on
    children's stories replied "Hello! This is the most amazing day." Nothing in
    the app could stop it, but presenting that as an answer about somebody's
    document would be a lie by omission.

    So the answer is measured against the extracts it was given: every
    meaningful word in it that also occurs in those paragraphs is counted. A
    faithful answer scores high even when it paraphrases, because it is using
    the document's vocabulary; an invented one scores near zero. The check is
    deliberately lenient and only ever adds a warning — it never suppresses
    what the model said, and it stays quiet for short answers and for the
    refusal sentence, where there is nothing to judge.
    """
    text = _CITATION.sub(" ", answer or "").strip()
    if _REFUSAL.search(text):
        return {"checked": False}

    source = set()
    for hit in hits:
        source.update(search.tokenize(hit.passage.text))
    words = [word for word in search.tokenize(text) if len(word) > 3]
    if not words or not source:
        return {"checked": False}

    covered = sum(1 for word in words if word in source)
    ratio = covered / len(words)
    if len(words) < 3:
        # Too short to judge by proportion — but "Yes (page 1)" has no
        # substance to judge, while a short answer made only of words the
        # document never uses is still a made-up answer.
        return {"checked": True, "ratio": round(ratio, 2), "supported": covered > 0}
    return {"checked": True, "ratio": round(ratio, 2), "supported": ratio >= SUPPORT_THRESHOLD}


def main(argv=None) -> int:
    """Kept here as well as in ``__main__`` so the module can be run directly."""
    from .__main__ import main as entry

    return entry(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
