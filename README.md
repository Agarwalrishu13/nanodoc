<div align="center">

# nanoDoc

**Drop in a document. Ask it anything about it.**

No code. No terminal. No account. No internet. Your file never leaves your computer —
and every answer shows you the exact paragraph it came from.

[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.9+-58a6ff.svg)]()
[![dependencies](https://img.shields.io/badge/runtime%20deps-0-f0883e.svg)]()
[![tests](https://img.shields.io/badge/tests-119%20passing-3ddc97.svg)]()

</div>

---

## What this is, in one paragraph

Most people have documents they cannot get a straight answer out of: a tenancy
agreement, a warranty, a 40-page manual, a spreadsheet of receipts, a folder of
lecture notes. Asking an AI about them normally means uploading the file to
somebody's server. nanoDoc is the other way round. You drag the file onto the
window, ask your question in your own words, and read the answer next to the
paragraphs it came from — with the page number on each one. It reads PDFs,
Word documents, spreadsheets and web pages by itself, using nothing but the
Python standard library. It needs **no install, no account, no API key**, and
it works with the internet switched off.

**Zero dependencies.** The whole app — including a PDF parser written from
scratch — is the Python standard library plus three static files. There is
nothing to `pip install`, ever.

---

## Use it

1. Install Python if you do not have it — [python.org/downloads](https://www.python.org/downloads/).
   On Windows, tick **“Add python.exe to PATH”** during setup.
2. Download this repo (green **Code** button → *Download ZIP*) and unzip it.
3. **Windows:** double-click `run.bat`. **macOS / Linux:** `./run.sh`.
4. Your browser opens at `http://127.0.0.1:8781`. Drag a document onto it.

<details>
<summary>Prefer the command line? (you do not need to)</summary>

```bash
python start.py                  # start and open the browser
python -m nanodoc doctor         # print what this computer has, then exit
python -m nanodoc --port 9000 --no-browser
```

</details>

---

## What it does when you drop a file in

| step | what happens |
|---|---|
| **1. It reads it** | The text is pulled out of the file on your machine, page by page. Scanned PDFs, which are pictures of text rather than text, are recognised and reported instead of silently returning nothing. |
| **2. It tells you what it found** | *"Lease agreement · PDF · 14 pages · 6,200 words"*, plus the words the document uses most, so you can see it has understood the shape of the thing. |
| **3. It finds the answer** | A search over the whole document — the classic BM25 ranking, no AI involved — picks the handful of paragraphs that answer your question. **This part needs nothing installed.** |
| **4. It says whether it found it** | If your question is not answered in the document, it says so. A search always returns *something*; this app is careful to tell the difference between an answer and the least unrelated paragraph. |
| **5. It writes the answer** | If there is a local AI engine on your computer, the found paragraphs are handed to it with strict instructions to use only those, and to name the page. The answer types itself out. |
| **6. You check it** | Every answer lists its sources with the matching words highlighted. Clicking `(page 4)` in the answer opens page 4 beside it. |

### The bit that matters

**The AI is not allowed to answer from memory.** The prompt it is given says,
in as many words: answer using only these extracts, never use anything you know
from elsewhere, and if the extracts do not contain the answer, reply
*"I could not find that in your document."* For a question about your lease,
a model quietly blending in what it remembers about leases is worse than no
answer at all. There is a test asserting those instructions are in the prompt
that is actually sent.

**And it is checked afterwards.** A prompt is a request, not a guarantee. So
when the answer arrives, the app compares the words in it with the words of the
paragraphs it was given. A faithful answer scores high even when it
paraphrases, because it is using the document's vocabulary. When the overlap is
poor, you are told plainly:

> *The AI wrote something that does not appear in your document. A small model
> does this — it ignores what it was given and carries on by itself. Read the
> paragraphs below instead; they are your document's own words.*

This is not hypothetical. Tested against a 17M-parameter model trained from
scratch (the kind `nanobrain` produces), asking about the `SELECT` statement in
a database assignment produced **"Hello! This is the most amazing day."** The
app cannot stop a model doing that. It can refuse to hand it to you as an
answer about your document. That is what this check is for.

---

## What it can read

| format | how | what you get |
|---|---|---|
| **PDF** (`.pdf`) | parsed by hand, byte by byte — `pdftext.py` | text with page numbers, including modern PDFs that use compressed object streams |
| **Word** (`.docx`) | it is a zip of XML | text split at the headings you wrote |
| **Excel** (`.xlsx`) | also a zip of XML | one citable unit per sheet |
| **Spreadsheets** (`.csv`) | | rows written out as *Name: Ada, City: London* so a row is findable |
| **Text and Markdown** (`.txt`, `.md`, …) | | sections from your headings, or parts of about a page |
| **Saved web pages** (`.html`) | tags stripped | sections from the headings |

<details>
<summary>Why a hand-written PDF parser?</summary>

Because the alternative is a `pip install`, and the people this is for cannot
run one. It is roughly 700 lines: the cross-reference tables, the compressed
object streams that every PDF made since 2006 uses, the text-showing operators,
the string escapes, and the character maps that make accented letters come out
as `café` rather than `cafÃ©`. When a PDF defeats it, it falls back to scanning
every stream in the file for text, and it reports what it could not read rather
than pretending.

</details>

---

## The AI engine (optional)

nanoDoc finds whatever is already running and uses it. Nothing is bundled and
nothing is required.

| engine | address it looks on |
|---|---|
| **Ollama** | `127.0.0.1:11434` — the easy one, and the only one that can download models |
| **LM Studio** | `127.0.0.1:1234` |
| **llama.cpp** (`llama-server`) | `127.0.0.1:8080` |
| **nanollama.c** | `127.0.0.1:8090` — models trained by [nanobrain](https://github.com/Agarwalrishu13/nanobrain) |
| **your own** | any OpenAI-compatible address, typed into settings |

**If none of them is there, nanoDoc still works.** It reads the document, finds
the paragraphs, quotes them, and tells you plainly that writing them into a
sentence needs an engine. The settings panel can install Ollama for you with
one button — showing you the exact command in the log before it runs it — and
download a model that fits your memory, with a progress line.

---

## Where your stuff lives

```
~/.nanodoc/
├── settings.json     your choices (which engine, which model)
├── library.json      the documents you have added
├── docs/<id>.json    the text that was read out of each one
└── uploads/          scratch space while a file is arriving
```

Delete that folder and the app forgets everything. Your original files are
never moved, never modified, and never deleted — and because a document's text
is stored once, reopening an old one is instant.

---

## Under the hood

```
nanodoc/
├── nanodoc/
│   ├── server.py      # every address the page can ask for, each one commented
│   ├── documents.py   # choosing a reader, and reading: docx, xlsx, csv, html, text
│   ├── pdftext.py     # the hand-written PDF parser (stdlib only)
│   ├── search.py      # passages, BM25, and the honesty about not finding things
│   ├── engine.py      # finding a local AI, the grounded prompt, install, download
│   ├── store.py       # your library, saved as JSON, never overwriting a name
│   ├── httpbase.py    # the mini web toolkit (routing, JSON, SSE, uploads)
│   └── web/           # the whole page: index.html, style.css, app.js
└── tests/             # 119 tests across two files
    ├── test_smoke.py  # readers, search, store, the server end to end
    └── test_pdftext.py# the PDF parser on its own
```

- **Finding and writing are separate.** `/api/search` finds and always works;
  `/api/ask` streams the writing. The page calls the first, shows you the
  paragraphs, and then starts the second — which is why an answer never appears
  before you can see where it came from.
- **Big files arrive in slices.** The page sends a dropped file in 8 MB pieces,
  so a 200 MB PDF does not have to fit in memory.
- **Nothing is trusted to be well-formed.** Files are corrupt, encodings are
  wrong, and small engines write JSON by hand and get it subtly wrong. Every
  one of those paths ends in a plain sentence rather than a stack trace.
- **The server is local-only.** It binds `127.0.0.1`, and the tests talk to it
  over HTTP exactly as the page does.

## Tests

```bash
python -m unittest discover tests -v
```

The tests build real files — a valid PDF written byte by byte with a correct
cross-reference table, a compressed one, a `.docx` and an `.xlsx` zipped up in
the test itself — read them back, and compare. Then they start the real server
on a spare port, upload a file in pieces the way the page does, and ask a
question over Server-Sent Events. One of those runs against **a fake engine**,
so the whole path from question to streamed answer to cited page is exercised
without any AI being installed.

CI runs on Windows, macOS and Linux, on Python 3.9 and 3.13.

---

## Honest limitations

- **Scanned PDFs are pictures.** If your PDF came out of a scanner, there is no
  text inside it to find. nanoDoc says so plainly. OCR is not built in.
- **PDF text is imperfect by nature.** A PDF contains no paragraphs, only
  instructions for placing glyphs on a page. This parser reconstructs reading
  order and does it well, but a multi-column newsletter or a table will come
  out less tidy than the original.
- **Old Office formats are refused with advice.** `.doc` and `.xls` are a
  different, pre-2007 binary format; you are told to save as `.docx`/`.xlsx`
  and drop it back in, which takes ten seconds in Word.
- **Spreadsheets are searched, not understood.** If you want to *predict* a
  column from a spreadsheet, that is [nanoLearn](https://github.com/Agarwalrishu13/nanolearn),
  and the app tells you so.
- **A big PDF takes a moment to read the first time.** Reading happens once,
  when you drop the file in; after that it is instant.
- **The written answer is only as good as your model.** A 1 GB model is quick
  and useful; it is not GPT-4. A *very* small model — a 17M-parameter one
  trained from scratch, say — cannot follow instructions at all and will reply
  with something unrelated; the app detects that and tells you so rather than
  dressing it up. The paragraphs and page numbers underneath are always exact,
  and they are the part you can rely on.
- **Search finds words, not meanings.** If your document says "the lessee shall
  remit" and you ask about "paying rent", the retrieval may miss it. The AI
  engine can bridge that gap when it is given the right paragraphs, but it can
  only work with what the search brings it.

## Family

| repo | role |
|---|---|
| [nanollama.c](https://github.com/Agarwalrishu13/nanollama.c) | the engine — from-scratch C inference |
| [nanobrain](https://github.com/Agarwalrishu13/nanobrain) | the brain factory — trains the model from scratch |
| [nanoforge](https://github.com/Agarwalrishu13/nanoforge) | the offline studio for building your own tiny model |
| [nanolaama](https://github.com/Agarwalrishu13/nanolaama) | **talk to an AI** — a friendly window onto local engines |
| [nanolearn](https://github.com/Agarwalrishu13/nanolearn) | **teach an AI** — drop a spreadsheet, get an answer machine |
| [nonoForge](https://github.com/Agarwalrishu13/nonoforge) | **build an app** — pick a card, press one button, it exists |
| **nanoDoc** (this repo) | **ask your documents** — drag a file in, get an answer you can check |

The same idea runs through all of them: the hard thing should be built
properly, and then it should be handed to somebody who has never had it before.

## License

MIT © Priyanshu Agarwal
