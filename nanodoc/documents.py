"""Turn a file that was dropped in into pages of plain text.

Everything here uses the Python standard library alone, because the person
using this app cannot run `pip install`. A PDF is parsed by hand
(`pdftext.py`); a `.docx` or `.xlsx` is a zip full of XML, which `zipfile`
and `xml.etree` open without help; everything else is decoded carefully.

A "page" is not always a sheet of paper. In a Word file it is a section, in a
spreadsheet it is a sheet, in a plain text file it is a part. `Doc.unit` holds
the right word for the format, so a citation can say "sheet Sheet1" rather
than pretending it knows about page 17 of a spreadsheet.

What this does not do: no OCR (a scan of a page is a picture, and this app
says so instead of pretending), no layout reconstruction of multi-column
pages, and no reading of formats nobody can open without an install step.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from html import unescape
from xml.etree import ElementTree

from . import pdftext

# The biggest file we will read into memory at once. A dropped file this size
# is already unusual for the documents this app is for; the limit exists so a
# mistakenly dropped video fails with a sentence instead of an out-of-memory
# crash.
MAX_BYTES = 80 * 1024 * 1024

# A single page of text longer than this is almost certainly a broken parse
# rather than a real page, and it would swamp the reader view.
MAX_PAGE_CHARS = 400_000


# --------------------------------------------------------------------------
# The shapes everything else in the app passes around
# --------------------------------------------------------------------------
@dataclass
class Page:
    """One locatable piece of a document."""

    number: int
    text: str
    label: str

    def word_count(self) -> int:
        return len(self.text.split())

    def to_dict(self) -> dict:
        return {"number": self.number, "text": self.text, "label": self.label, "words": self.word_count()}


@dataclass
class Doc:
    """A document that has been read, ready to be searched."""

    name: str
    kind: str
    unit: str
    pages: list = field(default_factory=list)
    note: str = ""
    title: str = ""
    source_name: str = ""
    added: float = 0.0
    read_ms: int = 0

    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    def word_count(self) -> int:
        return sum(page.word_count() for page in self.pages)

    def is_empty(self) -> bool:
        return self.word_count() == 0

    def to_dict(self, with_pages: bool = False) -> dict:
        data = {
            "name": self.name,
            "kind": self.kind,
            "unit": self.unit,
            "note": self.note,
            "title": self.title,
            "source_name": self.source_name,
            "added": self.added,
            "read_ms": self.read_ms,
            "words": self.word_count(),
            "pages": len(self.pages),
            "parts": [
                {"number": page.number, "label": page.label, "words": page.word_count()}
                for page in self.pages
            ],
        }
        if with_pages:
            data["full_pages"] = [page.to_dict() for page in self.pages]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Doc":
        pages = [
            Page(number=int(item.get("number", index + 1)), text=str(item.get("text", "")), label=str(item.get("label", "")))
            for index, item in enumerate(data.get("full_pages", []))
        ]
        return cls(
            name=data.get("name", ""),
            kind=data.get("kind", "document"),
            unit=data.get("unit", "page"),
            pages=pages,
            note=data.get("note", ""),
            title=data.get("title", ""),
            source_name=data.get("source_name", ""),
            added=float(data.get("added", 0.0)),
            read_ms=int(data.get("read_ms", 0)),
        )


class Unreadable(Exception):
    """Raised with a sentence a non-technical person can act on."""


# --------------------------------------------------------------------------
# Text tidy-up shared by every reader
# --------------------------------------------------------------------------
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLANK_RUN = re.compile(r"\n{3,}")
_SPACE_RUN = re.compile(r"[ \t]{2,}")
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")


def tidy(text: str) -> str:
    """Make extracted text pleasant to read and to quote.

    PDF and XML extraction both leave artefacts behind: stray control
    characters, runs of blank lines, and words split across a line by a
    hyphen. This cleans the harmless ones and leaves meaning alone.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = text.replace("\u00a0", " ").replace("\ufeff", "")
    # A word broken across a line by a hyphen is glued back together only when
    # the prefix is not itself a word we would normally hyphenate (a heuristic,
    # and the reason this is conservative).
    text = _HYPHEN_BREAK.sub(lambda m: m.group(1) + m.group(2) if len(m.group(1)) > 3 else m.group(0), text)
    text = _SPACE_RUN.sub(" ", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


def _decode(data: bytes) -> str:
    """Turn bytes into text, trying the encodings real files actually use.

    A file saved by Notepad on a machine set to a European language is cp1252,
    not utf-8, and it is not an error worth troubling the reader about —
    latin-1 is the last resort because it can decode any byte sequence at all.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if encoding in ("cp1252", "latin-1") and "\ufffd" not in text:
            return text
        if encoding in ("utf-8-sig", "utf-8"):
            return text
    return data.decode("latin-1", errors="replace")


# --------------------------------------------------------------------------
# Word documents (.docx) — a zip containing XML
# --------------------------------------------------------------------------
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# The style names Word uses for headings. A heading is the most meaningful
# place to cut a long document, because it is where a person would look.
_HEADING_STYLE = re.compile(r"^(heading|title|subtitle)\s*(\d+)?$", re.I)


def _docx_paragraph_text(node) -> str:
    pieces = []
    for child in node.iter():
        tag = child.tag
        if tag == W + "t":
            pieces.append(child.text or "")
        elif tag == W + "tab":
            pieces.append("\t")
        elif tag == W + "br":
            pieces.append("\n")
        elif tag == W + "noBreakHyphen":
            pieces.append("-")
    return tidy("".join(pieces))


def _docx_table_text(node) -> str:
    rows = []
    for row in node.findall(W + "tr"):
        cells = [_docx_paragraph_text(cell).replace("\n", " ") for cell in row.findall(W + "tc")]
        cells = [cell for cell in cells if cell != ""]
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _docx_blocks(body) -> list:
    """Walk the document body in order, yielding ("heading"|"text"|"table", value)."""
    blocks = []
    for node in body:
        if node.tag == W + "p":
            style = ""
            style_node = node.find(W + "pPr/" + W + "pStyle")
            if style_node is not None:
                style = style_node.get(W + "val", "") or ""
            text = _docx_paragraph_text(node)
            # An explicit page break is a real boundary and worth honouring.
            if node.findall(".//" + W + "br"):
                if any(br.get(W + "type") == "page" for br in node.findall(".//" + W + "br")):
                    blocks.append(("break", ""))
            if text:
                blocks.append(("heading" if _HEADING_STYLE.match(style) else "text", text))
        elif node.tag == W + "tbl":
            text = _docx_table_text(node)
            if text:
                blocks.append(("table", text))
    return blocks


def read_docx(data: bytes) -> Doc:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Unreadable("This .docx file looks damaged, so I could not open it.")
    try:
        raw = archive.read("word/document.xml")
    except KeyError:
        raise Unreadable("This file has a .docx name but is not a Word document inside.")

    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        raise Unreadable("The inside of this Word document could not be read.")

    body = root.find(W + "body")
    if body is None:
        raise Unreadable("This Word document has no text in it.")

    blocks = _docx_blocks(body)

    sections: list = [[]]
    titles: list = []
    for kind, value in blocks:
        if kind == "break" and sections[-1]:
            sections.append([])
        elif kind == "heading":
            if sections[-1]:
                sections.append([])
            titles.append(value)
            sections[-1].append(value)
        else:
            sections[-1].append(value)

    pages = []
    for index, lines in enumerate(sections):
        text = tidy("\n\n".join(lines))
        if not text:
            continue
        heading = titles[index] if index < len(titles) else ""
        pages.append(Page(number=len(pages) + 1, text=text, label=_section_label(len(pages) + 1, heading)))

    if not pages:
        raise Unreadable("This Word document has no text in it.")

    title = titles[0] if titles else ""
    return Doc(name="", kind="Word document", unit="section", pages=pages, title=title)


def _section_label(number: int, heading: str) -> str:
    if heading:
        short = heading if len(heading) <= 48 else heading[:45].rstrip() + "…"
        return "section “%s”" % short
    return "section %d" % number


# --------------------------------------------------------------------------
# Spreadsheets (.xlsx) — also a zip of XML
# --------------------------------------------------------------------------
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_SHEET_ROWS = 3000      # per sheet
_SHEET_COLS = 40        # per row


def _column_index(reference: str) -> int:
    """``"C7"`` → 2 (zero-based column)."""
    letters = "".join(ch for ch in reference if ch.isalpha())
    index = 0
    for char in letters:
        index = index * 26 + (ord(char.upper()) - 64)
    return max(0, index - 1)


def _shared_strings(archive: zipfile.ZipFile) -> list:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return []
    values = []
    for item in root.findall(S + "si"):
        # A cell's text may be split into runs when part of it is bold, etc.
        values.append("".join(node.text or "" for node in item.iter(S + "t")))
    return values


def _sheet_order(archive: zipfile.ZipFile) -> list:
    """Return [(sheet name, path inside the zip), ...] in workbook order."""
    try:
        root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    except (KeyError, ElementTree.ParseError):
        return []

    targets = {}
    try:
        rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        for rel in rels.findall(PR + "Relationship"):
            target = rel.get("Target", "")
            if target.startswith("/"):
                target = target.lstrip("/")
            elif not target.startswith("xl/"):
                target = "xl/" + target
            targets[rel.get("Id", "")] = target
    except (KeyError, ElementTree.ParseError):
        pass

    sheets = []
    for sheet in root.findall(S + "sheets/" + S + "sheet"):
        name = sheet.get("name", "Sheet")
        path = targets.get(sheet.get(R + "id", ""), "")
        if not path:
            path = "xl/worksheets/sheet%d.xml" % (len(sheets) + 1)
        sheets.append((name, path))
    return sheets


def _sheet_text(archive: zipfile.ZipFile, path: str, strings: list) -> tuple:
    """Render one sheet as lines of text. Returns (lines, truncated)."""
    try:
        root = ElementTree.fromstring(archive.read(path))
    except (KeyError, ElementTree.ParseError):
        return [], False

    lines = []
    truncated = False
    for row in root.findall(S + "sheetData/" + S + "row"):
        if len(lines) >= _SHEET_ROWS:
            truncated = True
            break
        cells = []
        for cell in row.findall(S + "c"):
            kind = cell.get("t", "")
            if kind == "s":
                value_node = cell.find(S + "v")
                index = int(value_node.text) if value_node is not None and (value_node.text or "").isdigit() else -1
                value = strings[index] if 0 <= index < len(strings) else ""
            elif kind == "inlineStr":
                value = "".join(node.text or "" for node in cell.iter(S + "t"))
            else:
                value_node = cell.find(S + "v")
                value = (value_node.text or "") if value_node is not None else ""
                if kind == "b":
                    value = "TRUE" if value.strip() == "1" else "FALSE"
            value = " ".join(value.split())
            column = _column_index(cell.get("r", "A1"))
            if column >= _SHEET_COLS:
                truncated = True
                continue
            while len(cells) < column:
                cells.append("")
            cells.append(value)
        line = " | ".join(cells).rstrip(" |")
        if line.strip():
            lines.append(line)
    return lines, truncated


def read_xlsx(data: bytes) -> Doc:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Unreadable("This .xlsx file looks damaged, so I could not open it.")

    sheets = _sheet_order(archive)
    if not sheets:
        raise Unreadable("This file has a .xlsx name but is not a spreadsheet inside.")

    strings = _shared_strings(archive)
    pages = []
    notes = []
    for name, path in sheets:
        lines, truncated = _sheet_text(archive, path, strings)
        if truncated:
            notes.append("bigger than this app reads at once")
        if not lines:
            continue
        pages.append(Page(number=len(pages) + 1, text=tidy("\n".join(lines)), label="sheet “%s”" % name))

    if not pages:
        raise Unreadable("There is nothing readable in this spreadsheet — every sheet is empty.")

    note = ""
    if notes:
        note = "One or more sheets are bigger than this app reads at once, so the bottom of them was skipped."
    return Doc(name="", kind="Spreadsheet", unit="sheet", pages=pages, note=note)


def read_csv(data: bytes) -> Doc:
    """Read a .csv as searchable text, one line per row.

    Each row is rendered as "Column: value" rather than as bare commas,
    because that is what makes a row findable and quotable later. The note
    points at the sibling app that predicts a column, since this one only
    reads.
    """
    text = _decode(data)
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        raise Unreadable("This .csv file could not be read as a table.")
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        raise Unreadable("There is nothing in this .csv file.")

    header = [cell.strip() or "column %d" % (index + 1) for index, cell in enumerate(rows[0])]
    body = rows[1:]
    lines = []
    for row in body[:20000]:
        pairs = [
            "%s: %s" % (header[index] if index < len(header) else "column %d" % (index + 1), cell.strip())
            for index, cell in enumerate(row)
            if cell.strip()
        ]
        if pairs:
            lines.append(", ".join(pairs))

    # Group rows into readable parts so a citation is not "row 812".
    parts = [lines[index:index + 25] for index in range(0, len(lines), 25)]
    pages = [
        Page(number=index + 1, text=tidy("\n".join(chunk)), label="rows %d–%d" % (index * 25 + 1, index * 25 + len(chunk)))
        for index, chunk in enumerate(parts)
        if chunk
    ]
    if not pages:
        raise Unreadable("This .csv file has headings but no rows under them.")

    note = "This is a spreadsheet, so I can only search it and quote from it. To predict a column from it, use nanoLearn."
    if len(body) > 20000:
        note += " It has more than 20,000 rows, so I only read the first 20,000."
    return Doc(name="", kind="Spreadsheet (csv)", unit="part", pages=pages, note=note, title=", ".join(header[:6]))


# --------------------------------------------------------------------------
# Web pages and plain text
# --------------------------------------------------------------------------
_TAG = re.compile(r"<[^>]+>")
_DROP_BLOCK = re.compile(r"(?is)<(script|style|noscript|head|svg)\b.*?</\1>")
_BLOCK_END = re.compile(r"(?i)</(p|div|li|tr|h[1-6]|section|article|br)\s*/?>")
_HEADING = re.compile(r"(?is)<h[12][^>]*>(.*?)</h[12]>")


def _plain(fragment: str) -> str:
    """Strip tags from an HTML fragment and turn block ends into line breaks."""
    fragment = _BLOCK_END.sub("\n", fragment)
    return unescape(_TAG.sub(" ", fragment))


def read_html(data: bytes) -> Doc:
    text = _decode(data)

    # The title has to be read before anything else is thrown away: it lives in
    # the <head>, which is the first thing dropped below.
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    title = tidy(unescape(_TAG.sub(" ", title_match.group(1)))) if title_match else ""

    text = _DROP_BLOCK.sub(" ", text)

    # Split on the headings themselves: the odd pieces of the result are the
    # heading text, and the piece after each one is the body it introduces.
    # Sections cut this way are where a reader would look for something again.
    pieces = _HEADING.split(text)
    blocks = [("", tidy(_plain(pieces[0])))] if pieces[0].strip() else []
    for index in range(1, len(pieces) - 1, 2):
        heading = tidy(unescape(_TAG.sub(" ", pieces[index])))
        blocks.append((heading, tidy(_plain(pieces[index + 1]))))

    pages = []
    for heading, chunk in blocks:
        if not chunk:
            continue
        pages.append(Page(number=len(pages) + 1, text=chunk, label=_section_label(len(pages) + 1, heading)))
    if not pages:
        raise Unreadable("There is no readable text in this web page.")

    return Doc(name="", kind="Web page", unit="section", pages=pages, title=title)


_MD_HEADING = re.compile(r"^\s{0,3}#{1,3}\s+(.*\S)\s*$")


def read_text(data: bytes) -> Doc:
    text = tidy(_decode(data))
    if not text:
        raise Unreadable("There is no text in this file.")

    lines = text.split("\n")
    marks = [index for index, line in enumerate(lines) if _MD_HEADING.match(line)]

    if marks:
        blocks = []
        for position, start in enumerate(marks):
            end = marks[position + 1] if position + 1 < len(marks) else len(lines)
            heading = _MD_HEADING.match(lines[start]).group(1)
            blocks.append((heading, tidy("\n".join(lines[start:end]))))
        unit = "section"
    else:
        # No headings: cut the text into parts of roughly a page each, so a
        # citation points at something a person can find again.
        paragraphs = [block for block in text.split("\n\n") if block.strip()]
        blocks = []
        for index in range(0, len(paragraphs), 12):
            chunk = paragraphs[index:index + 12]
            first = chunk[0][:44].replace("\n", " ").strip()
            blocks.append((first + "…" if len(chunk[0]) > 44 else first, tidy("\n\n".join(chunk))))
        unit = "part"

    pages = [
        Page(number=index + 1, text=block, label=_section_label(index + 1, heading) if unit == "section" else "part %d" % (index + 1))
        for index, (heading, block) in enumerate(blocks)
        if block
    ]
    if not pages:
        raise Unreadable("There is no text in this file.")

    title = _MD_HEADING.match(lines[marks[0]]).group(1) if marks else ""
    return Doc(name="", kind="Text file", unit=unit, pages=pages, title=title)


# --------------------------------------------------------------------------
# PDF — handed to the hand-written parser, then adapted to our shape
# --------------------------------------------------------------------------
def read_pdf(data: bytes) -> Doc:
    result = pdftext.extract(data)

    pages = []
    for page in getattr(result, "pages", []) or []:
        text = tidy(getattr(page, "text", "") or "")
        if not text:
            continue
        number = int(getattr(page, "number", len(pages) + 1))
        pages.append(Page(number=number, text=text[:MAX_PAGE_CHARS], label="page %d" % number))

    note = (getattr(result, "note", "") or "").strip()
    if getattr(result, "encrypted", False) and not pages:
        raise Unreadable(note or "This PDF is locked with a password, so I cannot read it.")

    if not pages:
        raise Unreadable(
            note
            or "There is no text inside this PDF. It is probably a scan — a picture of a page rather than a page."
        )

    return Doc(
        name="",
        kind="PDF",
        unit="page",
        pages=pages,
        note=note,
        title=(getattr(result, "title", "") or "").strip(),
    )


# --------------------------------------------------------------------------
# Choosing a reader
# --------------------------------------------------------------------------
EXTENSIONS = {
    ".pdf": "pdf", ".docx": "docx", ".doc": "doc", ".xlsx": "xlsx", ".xls": "xls",
    ".csv": "csv", ".tsv": "csv", ".txt": "text", ".md": "text", ".markdown": "text",
    ".log": "text", ".rst": "text", ".json": "text", ".yaml": "text", ".yml": "text",
    ".py": "text", ".js": "text", ".html": "html", ".htm": "html", ".xhtml": "html",
}

KIND_NAMES = {
    "pdf": "PDF", "docx": "Word document", "xlsx": "Spreadsheet", "csv": "Spreadsheet (csv)",
    "text": "Text file", "html": "Web page",
}


def sniff(name: str, data: bytes) -> str:
    """Work out what kind of file this is, from its name and its first bytes."""
    head = data[:4096]
    if pdftext.is_pdf(head):
        return "pdf"
    if head[:4] == b"PK\x03\x04":
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except zipfile.BadZipFile:
            return "unknown"
        if any(name.startswith("word/") for name in names):
            return "docx"
        if any(name.startswith("xl/") for name in names):
            return "xlsx"
        return "unknown"

    suffix = ""
    if "." in name:
        suffix = "." + name.rsplit(".", 1)[-1].lower()
    if suffix in EXTENSIONS:
        return EXTENSIONS[suffix]

    # No useful name: a file that decodes cleanly as text almost certainly is
    # one, and reading it is friendlier than refusing it.
    if head and _looks_like_text(head):
        return "text"
    return "unknown"


def _looks_like_text(data: bytes) -> bool:
    if not data:
        return False
    sample = data[:2048]
    printable = sum(1 for byte in sample if 9 <= byte <= 13 or 32 <= byte <= 126 or byte >= 160)
    return printable / len(sample) > 0.9


def read(name: str, data: bytes) -> Doc:
    """Read a dropped file into a Doc, or raise Unreadable with a plain reason."""
    started = time.time()

    if len(data) > MAX_BYTES:
        raise Unreadable(
            "This file is %d MB, which is bigger than this app reads at once (limit %d MB)."
            % (len(data) // (1024 * 1024), MAX_BYTES // (1024 * 1024))
        )
    if not data:
        raise Unreadable("That file is empty.")

    kind = sniff(name, data)
    reader = {
        "pdf": read_pdf,
        "docx": read_docx,
        "xlsx": read_xlsx,
        "csv": read_csv,
        "html": read_html,
        "text": read_text,
    }.get(kind)

    if reader is None:
        suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if suffix in (".doc", ".xls"):
            raise Unreadable(
                "This is the old-style %s format, which I cannot read. Open it in Word or Excel and "
                "save it again as .docx or .xlsx, then drop it back in." % suffix.lstrip(".").upper()
            )
        raise Unreadable(
            "I do not know how to read “%s”. I can read PDFs, Word documents (.docx), spreadsheets "
            "(.xlsx, .csv), web pages and plain text files." % (name or "that file")
        )

    doc = reader(data)
    doc.name = name
    doc.source_name = name
    doc.added = time.time()
    doc.read_ms = int((time.time() - started) * 1000)
    if not doc.kind:
        doc.kind = KIND_NAMES.get(kind, "document")
    return doc


def summary(doc: Doc) -> dict:
    """A short, honest description of a document, with no AI involved."""
    return {
        "name": doc.name,
        "kind": doc.kind,
        "unit": doc.unit,
        "words": doc.word_count(),
        "parts": len(doc.pages),
        "note": doc.note,
        "title": doc.title,
        "pages": len(doc.pages),
        "read_ms": doc.read_ms,
        "added": doc.added,
    }


def to_json(doc: Doc) -> str:
    return json.dumps(doc.to_dict(with_pages=True), ensure_ascii=False)


def from_json(raw: str) -> Doc:
    return Doc.from_dict(json.loads(raw))
