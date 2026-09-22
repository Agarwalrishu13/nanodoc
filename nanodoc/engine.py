"""Finding the AI already on this computer, and asking it about a document.

nanoDoc reads documents by itself. It only needs an AI engine to turn the
extracts it found into a written answer, and there is usually one already
installed: Ollama, LM Studio, llama.cpp, or the author's own nanollama.c.
This module finds them, talks to them over their local HTTP ports, and — if
there is nothing to find — offers to install one without sending anybody to a
terminal.

Two rules shape everything here:

* The answer must come from the document. The prompt hands the model a small
  number of numbered extracts and tells it, in as many words, to say "I could
  not find that in your document" rather than reach for anything it happens
  to know. An assistant that quietly blends outside knowledge into a quote
  from your lease is worse than no assistant.
* Nothing leaves the machine. Every address below is 127.0.0.1. The only
  requests that leave the computer are the ones a person explicitly presses a
  button for: installing an engine, or downloading a model.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# --------------------------------------------------------------------------
# The engines we know how to talk to
# --------------------------------------------------------------------------
ENGINE_CATALOG = [
    {
        "id": "ollama",
        "name": "Ollama",
        "kind": "ollama",
        "base": "http://127.0.0.1:11434",
        "port": 11434,
        "why": "The easiest one. It can also download models for you, from inside this app.",
    },
    {
        "id": "lmstudio",
        "name": "LM Studio",
        "kind": "openai",
        "base": "http://127.0.0.1:1234/v1",
        "port": 1234,
        "why": "A point-and-click app for browsing models.",
    },
    {
        "id": "llamacpp",
        "name": "llama.cpp",
        "kind": "openai",
        "base": "http://127.0.0.1:8080/v1",
        "port": 8080,
        "why": "One file, maximum speed, no extras.",
    },
    {
        "id": "nanollama",
        "name": "nanollama.c",
        "kind": "openai",
        "base": "http://127.0.0.1:8090/v1",
        "port": 8090,
        "why": "The from-scratch engine, for models trained with nanobrain.",
    },
]

ENGINES_BY_ID = {engine["id"]: engine for engine in ENGINE_CATALOG}

# Models worth suggesting to somebody who has just installed Ollama. The sizes
# are what the download costs, which is the number that actually matters when
# you are waiting for it.
SUGGESTED_MODELS = [
    {"name": "llama3.2:1b", "title": "Small and quick", "size_gb": 1.3, "needs_gb": 2.5,
     "blurb": "Answers in a second or two on any laptop. Good enough to summarise a document."},
    {"name": "qwen2.5:3b", "title": "Better writing", "size_gb": 1.9, "needs_gb": 4.5,
     "blurb": "Noticeably more fluent, still comfortable on a normal laptop."},
    {"name": "llama3.1:8b", "title": "Best quality", "size_gb": 4.7, "needs_gb": 9.0,
     "blurb": "Writes like a person. Wants 16 GB of memory and a little patience."},
]


# --------------------------------------------------------------------------
# Small HTTP helpers (standard library only, like everything else here)
# --------------------------------------------------------------------------
def _request(url: str, payload=None, method: str = "POST", timeout: float = 4.0, headers: dict | None = None):
    data = None
    head = {"Content-Type": "application/json"}
    if headers:
        head.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers=head)
    return urllib.request.urlopen(request, timeout=timeout)


def _get_json(url: str, timeout: float = 3.0):
    with _request(url, None, "GET", timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------
def probe(engine: dict, timeout: float = 2.0) -> dict:
    """Ask one engine whether it is there, and what it can run."""
    found = dict(engine)
    found["ok"] = False
    found["models"] = []
    found["error"] = ""

    if not port_open(engine["port"]):
        found["error"] = "Not running."
        return found

    try:
        if engine["kind"] == "ollama":
            data = _get_json(engine["base"].rstrip("/") + "/api/tags", timeout)
            found["models"] = sorted(item.get("name", "") for item in data.get("models", []) if item.get("name"))
        else:
            data = _get_json(engine["base"].rstrip("/") + "/models", timeout)
            entries = data.get("data", data if isinstance(data, list) else [])
            found["models"] = sorted(item.get("id", "") for item in entries if isinstance(item, dict) and item.get("id"))
        found["ok"] = True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, TimeoutError) as exc:
        found["error"] = _plain_error(exc)
    return found


def _plain_error(exc: Exception) -> str:
    text = str(exc)
    if "timed out" in text.lower():
        return "It is running but did not answer in time."
    if isinstance(exc, urllib.error.HTTPError):
        return "It answered with an error (HTTP %s)." % exc.code
    return "It is there but did not answer."


def detect(extra: dict | None = None) -> list:
    """Every engine we can find, in the order they are worth trying.

    The ports are checked at the same time rather than one after another. Each
    check can wait a couple of seconds for an engine that is not there, and
    four of those in a row is a page that sits blank long enough for somebody
    to think it is broken. In parallel the wait is the slowest single check.
    """
    catalog = list(ENGINE_CATALOG)
    custom = _custom_engine(extra)
    if custom:
        catalog.insert(0, custom)

    with ThreadPoolExecutor(max_workers=len(catalog)) as pool:
        found = list(pool.map(probe, catalog))

    if custom and not found[0].get("ok"):
        # A configured address that does not answer is still worth listing, so
        # the settings panel can say so rather than silently ignoring it.
        found.append(found.pop(0))
    return found


def _custom_engine(extra: dict | None) -> dict | None:
    """The address somebody typed into settings, if there is one."""
    if not extra or not str(extra.get("base_url") or "").strip():
        return None
    base = str(extra["base_url"]).strip().rstrip("/")
    return {
        "id": "custom",
        "name": extra.get("name") or "Your own engine",
        "kind": "openai",
        "base": base,
        "port": _port_of(base),
        "why": "The address you entered in settings.",
        "api_key": extra.get("api_key", ""),
    }


def _port_of(url: str) -> int:
    match = re.search(r":(\d{2,5})", url)
    if match:
        return int(match.group(1))
    return 443 if url.startswith("https") else 80


def working(engines: list) -> list:
    return [engine for engine in engines if engine.get("ok") and engine.get("models")]


def pick(engines: list, preferred_engine: str = "", preferred_model: str = "") -> tuple:
    """Choose which engine and model to use, honouring the saved choice."""
    usable = working(engines)
    if not usable:
        return None, ""

    engine = None
    for candidate in usable:
        if candidate["id"] == preferred_engine:
            engine = candidate
            break
    engine = engine or usable[0]

    model = preferred_model if preferred_model in engine["models"] else ""
    if not model:
        model = _best_default(engine["models"])
    return engine, model


def _best_default(models: list) -> str:
    """Pick a sensible model when the person has not chosen one.

    Small models are preferred: this app asks a model to do one narrow job —
    read six paragraphs and answer a question about them — and a small model
    does that job faster, which matters far more to somebody waiting than the
    extra polish of a big one.
    """
    if not models:
        return ""
    for hint in ("1b", "1.5b", "2b", "3b", "mini", "small", "tiny"):
        for model in models:
            if hint in model.lower():
                return model
    return sorted(models, key=len)[0]


# --------------------------------------------------------------------------
# Asking
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """You answer questions about one document that the user has on their own computer.

You are given numbered extracts from that document. These rules are strict:

1. Answer using ONLY the extracts. Never use anything you know from elsewhere, even if you are sure of it.
2. Every fact you state must come from an extract. Name where it came from at the end of the sentence, like this: (page 4).
3. Quote the document's own words when the exact wording matters, in double quotation marks.
4. If the extracts do not contain the answer, reply with exactly this sentence and nothing else: I could not find that in your document.
5. Never mention "extracts", "context", "passages" or that you were given anything. Speak as if you have read the whole document.
6. Answer in the language the question was asked in. Be brief — two or three sentences unless asked for more."""

SUMMARY_PROMPT = """You describe documents to people who have not read them.

You are given the beginning and some extracts from one document. Using only those:

1. Say what kind of document it is and what it is for, in two sentences a busy person can act on.
2. Then three to five short bullet points of the things in it that matter most — dates, amounts, names, obligations, deadlines.
3. Never invent anything. If something important is not in the extracts, leave it out.
4. Never mention "extracts" or that you were given anything."""


def extract_block(hits: list) -> str:
    """Render the found passages as the numbered extracts the prompt describes."""
    lines = []
    for position, hit in enumerate(hits, start=1):
        lines.append("[%d] (%s)\n%s" % (position, hit.passage.label, hit.passage.text.strip()))
    return "\n\n".join(lines)


def build_messages(question: str, hits: list, doc_name: str, summary: bool = False) -> list:
    system = SUMMARY_PROMPT if summary else SYSTEM_PROMPT
    if summary:
        body = (
            "Extracts from “%s” (the beginning of the document, then the parts that best "
            "represent it):\n\n%s" % (doc_name, extract_block(hits))
        )
    else:
        body = "Extracts from “%s”:\n\n%s\n\nQuestion: %s" % (doc_name, extract_block(hits), question)
    return [{"role": "system", "content": system}, {"role": "user", "content": body}]


def answer_stream(engine: dict, model: str, messages: list, temperature: float = 0.2):
    """Yield the answer as it is written, one piece at a time.

    Both engine families are handled: Ollama's own protocol, and the
    OpenAI-compatible one that everything else speaks. If an HTTP error comes
    back, the body usually explains it better than the status code does, so it
    is read and passed on.
    """
    if not engine or not model:
        return
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }
    if engine.get("api_key"):
        payload["api_key"] = engine["api_key"]

    headers = {}
    if engine.get("api_key"):
        headers["Authorization"] = "Bearer " + str(engine["api_key"])

    if engine["kind"] == "ollama":
        url = engine["base"].rstrip("/") + "/api/chat"
    else:
        url = engine["base"].rstrip("/") + "/chat/completions"

    try:
        response = _request(url, payload, "POST", timeout=180.0, headers=headers)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = exc.read().decode("utf-8", "replace")
            detail = _salvage_message(body)
        except Exception:
            pass
        raise RuntimeError(
            "%s refused the request (%s). %s" % (engine["name"], exc.code, detail or "The model may have been removed.")
        )
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError("%s stopped answering: %s" % (engine["name"], _plain_error(exc)))

    with response:
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
                if line == "[DONE]":
                    break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # Small engines sometimes write JSON by hand and get a quote
                # or a newline wrong. Dig the text out rather than failing.
                salvaged = _salvage_content(line)
                if salvaged:
                    yield salvaged
                continue
            piece = _text_of(event)
            if piece:
                yield piece


def _text_of(event: dict) -> str:
    if not isinstance(event, dict):
        return ""
    if "message" in event and isinstance(event["message"], dict):
        return event["message"].get("content") or ""
    if "response" in event and isinstance(event["response"], str):
        return event["response"]
    choices = event.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        delta = first.get("delta") or {}
        if isinstance(delta, dict) and delta.get("content"):
            return delta["content"]
        if first.get("text"):
            return first["text"]
        message = first.get("message") or {}
        if isinstance(message, dict) and message.get("content"):
            return message["content"]
    return ""


_CONTENT = re.compile(r'"(?:content|response|text)"\s*:\s*"')
_UNESCAPE = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f"}


def _salvage_content(line: str) -> str:
    match = _CONTENT.search(line)
    if not match:
        return ""
    out = []
    index = match.end()
    while index < len(line):
        char = line[index]
        if char == "\\" and index + 1 < len(line):
            out.append(_UNESCAPE.get(line[index + 1], line[index + 1]))
            index += 2
            continue
        if char == '"':
            break
        out.append(char)
        index += 1
    return "".join(out)


def _salvage_message(body: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body.strip()[:200]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message", ""))[:200]
        if isinstance(error, str):
            return error[:200]
    return body.strip()[:200]


# --------------------------------------------------------------------------
# Getting an engine without a terminal
# --------------------------------------------------------------------------
def install_command() -> list:
    """The exact command that would be run to install Ollama on this machine.

    Returned as a list so the app can show it to the person before running it.
    Being told exactly what is about to happen is the difference between a
    helpful button and a frightening one.
    """
    system = platform.system().lower()
    if system == "windows":
        if shutil.which("winget"):
            return ["winget", "install", "--id", "Ollama.Ollama", "-e",
                    "--accept-source-agreements", "--accept-package-agreements"]
        return []
    if system == "darwin":
        if shutil.which("brew"):
            return ["brew", "install", "ollama"]
        return []
    if shutil.which("curl"):
        return ["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"]
    return []


def install_stream():
    """Run the official installer, yielding its output a line at a time."""
    command = install_command()
    if not command:
        yield {
            "type": "note",
            "message": "I do not know a safe way to install this for you on this computer. "
                       "Go to ollama.com, download the installer, and run it — then press “Look again”.",
        }
        return

    yield {"type": "command", "message": " ".join(command)}
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, bufsize=1, universal_newlines=True,
            errors="replace",
        )
    except OSError as exc:
        yield {"type": "note", "message": "I could not run that: %s" % exc}
        return

    for line in iter(process.stdout.readline, ""):
        text = line.rstrip()
        if text:
            yield {"type": "log", "message": text}
    process.stdout.close()
    process.wait()

    if process.returncode == 0:
        yield {"type": "done", "message": "Ollama is installed."}
    else:
        yield {
            "type": "note",
            "message": "The installer stopped with code %d. You can also download it by hand from ollama.com."
                       % process.returncode,
        }


def pull_stream(engine: dict, model: str):
    """Download a model through Ollama, reporting progress as it goes."""
    if not engine or engine.get("kind") != "ollama":
        yield {"type": "note", "message": "Only Ollama can download models from inside this app."}
        return

    url = engine["base"].rstrip("/") + "/api/pull"
    try:
        response = _request(url, {"model": model, "stream": True}, "POST", timeout=3600.0)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        yield {"type": "note", "message": "The download could not start: %s" % _plain_error(exc)}
        return

    last = ""
    with response:
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            status = str(event.get("status", ""))
            total = event.get("total") or 0
            done = event.get("completed") or 0
            if total and done:
                percent = int(done * 100 / total)
                message = "%s — %d%%" % (status or "Downloading", percent)
            else:
                message = status
            if message and message != last:
                last = message
                yield {"type": "progress", "message": message}
    yield {"type": "done", "message": "%s is ready." % model}


# --------------------------------------------------------------------------
# Is there enough memory for the model?
# --------------------------------------------------------------------------
def total_ram_gb() -> float:
    """Total memory in GB, or 0.0 when it cannot be worked out."""
    try:
        if platform.system() == "Windows":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong), ("avail_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return round(status.total_phys / (1024 ** 3), 1)
        if platform.system() == "Darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=4)
            return round(int(out.stdout.strip()) / (1024 ** 3), 1)
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / (1024 ** 2), 1)
    except Exception:
        return 0.0
    return 0.0


def suggested_models() -> list:
    """The suggested downloads, marked with whether this machine can run them."""
    ram = total_ram_gb()
    out = []
    for model in SUGGESTED_MODELS:
        fits = True
        if ram:
            # Leave room for the operating system and the browser.
            fits = ram >= model["needs_gb"]
        out.append({**model, "fits": fits, "ram_gb": ram})
    return out


def summary(engines: list, chosen: dict, model: str) -> dict:
    """Everything the page needs to describe the AI situation in one object."""
    usable = working(engines)
    return {
        "engines": [
            {
                "id": engine["id"], "name": engine["name"], "ok": engine.get("ok", False),
                "models": engine.get("models", []), "error": engine.get("error", ""),
                "why": engine.get("why", ""), "port": engine.get("port", 0),
            }
            for engine in engines
        ],
        "ready": bool(usable),
        "engine_id": chosen["id"] if chosen else "",
        "engine_name": chosen["name"] if chosen else "",
        "model": model,
        "suggested": suggested_models(),
        "can_install": bool(install_command()),
        "install_command": " ".join(install_command()),
        "python": sys.version.split()[0],
        "machine": "%s %s" % (platform.system(), platform.release()),
        "folder": os.path.expanduser("~"),
    }
