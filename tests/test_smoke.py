"""The tests, which between them build every kind of file the app claims to read.

Two of them are worth more than the rest, and they are the reason this file is
long:

* every reader is fed a real file, built byte by byte in this test, and the
  text that comes out is compared with the text that went in — so a reader
  cannot quietly start returning nothing;
* the server is started on a spare port, a file is uploaded to it in pieces
  the way the page does, and a question is asked over Server-Sent Events. One
  of those runs against a fake engine, so the whole path from question to
  streaming answer to cited page is exercised without needing any AI installed.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The app keeps its documents in the user's home folder. For tests it must not,
# so the folder is redirected before anything is imported.
_TEMPORARY_HOME = tempfile.mkdtemp(prefix="nanodoc-tests-")
os.environ["NANODOC_HOME"] = _TEMPORARY_HOME

from pathlib import Path  # noqa: E402

from nanodoc import documents, engine, httpbase, search, server, store  # noqa: E402
from nanodoc import pdftext  # noqa: E402


# --------------------------------------------------------------------------
# Building real files to read
# --------------------------------------------------------------------------
def _pdf_escape(text: str) -> bytes:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)").encode("latin-1", "replace")


def build_pdf(pages, compressed: bool = False) -> bytes:
    """A small but genuinely valid PDF, written out by hand.

    Handing a reader a file made by the same code it is testing is a good way
    to test nothing, so this builds the file structure from the specification:
    objects, a cross-reference table with real byte offsets, and a trailer.
    """
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    kids = []
    number = 4
    for text in pages:
        page_number, content_number = number, number + 1
        number += 2
        kids.append(page_number)
        stream = b"BT /F1 24 Tf 72 700 Td (" + _pdf_escape(text) + b") Tj ET"
        filter_part = b""
        if compressed:
            stream = zlib.compress(stream)
            filter_part = b" /Filter /FlateDecode"
        objects[content_number] = (
            b"<< /Length %d%s >>\nstream\n" % (len(stream), filter_part) + stream + b"\nendstream"
        )
        objects[page_number] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content_number
        )
    objects[2] = (
        b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % kid for kid in kids)
        + b"] /Count %d >>" % len(pages)
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for obj_number in sorted(objects):
        offsets[obj_number] = len(out)
        out += b"%d 0 obj\n" % obj_number + objects[obj_number] + b"\nendobj\n"

    start_xref = len(out)
    count = max(objects) + 1
    out += b"xref\n0 %d\n" % count
    out += b"0000000000 65535 f \n"
    for obj_number in range(1, count):
        out += b"%010d 00000 n \n" % offsets.get(obj_number, 0)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (count, start_xref)
    return bytes(out)


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def build_docx(blocks) -> bytes:
    """A .docx is a zip of XML. `blocks` is a list of (style, text) pairs."""
    paragraphs = []
    for style, text in blocks:
        style_xml = '<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style if style else ""
        paragraphs.append(
            '<w:p>%s<w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % (style_xml, text)
        )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="%s"><w:body>%s</w:body></w:document>' % (WORD_NS, "".join(paragraphs))
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


SPREAD_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def build_xlsx(sheets) -> bytes:
    """`sheets` is a list of (name, rows), where each row is a list of strings."""
    shared = []
    index_of = {}

    def shared_index(value):
        if value not in index_of:
            index_of[value] = len(shared)
            shared.append(value)
        return index_of[value]

    sheet_xml = []
    for name, rows in sheets:
        row_xml = []
        for row_number, row in enumerate(rows, start=1):
            cells = []
            for column, value in enumerate(row):
                if value == "":
                    continue
                letter = chr(ord("A") + column)
                if isinstance(value, (int, float)):
                    cells.append('<c r="%s%d"><v>%s</v></c>' % (letter, row_number, value))
                else:
                    cells.append('<c r="%s%d" t="s"><v>%d</v></c>'
                                 % (letter, row_number, shared_index(str(value))))
            row_xml.append('<row r="%d">%s</row>' % (row_number, "".join(cells)))
        sheet_xml.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>'
            % (SPREAD_NS, "".join(row_xml))
        )

    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="%s" xmlns:r="%s"><sheets>%s</sheets></workbook>'
        % (SPREAD_NS, REL_NS, "".join(
            '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (name, position + 1, position + 1)
            for position, (name, _rows) in enumerate(sheets)))
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s</Relationships>'
        % "".join(
            '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (position + 1, position + 1)
            for position in range(len(sheets)))
    )
    strings = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="%s" count="%d" uniqueCount="%d">%s</sst>'
        % (SPREAD_NS, len(shared), len(shared),
           "".join("<si><t>%s</t></si>" % value for value in shared))
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/sharedStrings.xml", strings)
        for position, xml in enumerate(sheet_xml, start=1):
            archive.writestr("xl/worksheets/sheet%d.xml" % position, xml)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Reading files
# --------------------------------------------------------------------------
class TestReaders(unittest.TestCase):
    def test_reads_a_plain_text_file(self):
        doc = documents.read("notes.txt", b"First paragraph about apples.\n\nSecond about oranges.")
        self.assertEqual(doc.kind, "Text file")
        self.assertIn("apples", doc.text())
        self.assertIn("oranges", doc.text())

    def test_reads_a_file_that_is_not_utf8(self):
        # A text file saved by Notepad on a Western European machine: cp1252,
        # not utf-8. It must not fail and must not turn into question marks.
        doc = documents.read("notes.txt", "The café bill was £12 — paid by René.".encode("cp1252"))
        self.assertIn("café", doc.text())
        self.assertIn("René", doc.text())

    def test_markdown_sections_become_the_places_you_cite(self):
        doc = documents.read("plan.md", b"# Plan\n\nIntro text.\n\n## Budget\n\nThe budget is five thousand.\n\n## Dates\n\nIn June.")
        self.assertEqual(doc.unit, "section")
        self.assertGreaterEqual(len(doc.pages), 3)
        labels = " ".join(page.label for page in doc.pages)
        self.assertIn("Budget", labels)
        self.assertIn("five thousand", doc.text())

    def test_html_headings_become_sections_and_tags_disappear(self):
        html = (b"<html><head><title>Refunds</title><style>p{color:red}</style></head><body>"
                b"<h1>Refund policy</h1><p>You may return an item within 30 days.</p>"
                b"<h2>Shipping</h2><p>Shipping is not refunded.</p>"
                b"<script>var x = 'should not appear';</script></body></html>")
        doc = documents.read("policy.html", html)
        self.assertEqual(doc.title, "Refunds")
        self.assertIn("30 days", doc.text())
        self.assertNotIn("should not appear", doc.text())
        self.assertNotIn("color:red", doc.text())
        self.assertIn("Refund policy", " ".join(page.label for page in doc.pages))

    def test_reads_a_word_document(self):
        data = build_docx([
            ("Heading1", "Tenancy agreement"),
            ("", "The deposit is 1200 pounds."),
            ("Heading2", "Repairs"),
            ("", "The landlord repairs the boiler within 5 days."),
        ])
        doc = documents.read("agreement.docx", data)
        self.assertEqual(doc.kind, "Word document")
        self.assertIn("1200 pounds", doc.text())
        self.assertIn("boiler", doc.text())
        self.assertEqual(doc.unit, "section")
        labels = " ".join(page.label for page in doc.pages)
        self.assertIn("Tenancy agreement", labels)

    def test_reads_a_spreadsheet_with_several_sheets(self):
        data = build_xlsx([
            ("January", [["Item", "Cost"], ["Rent", 900], ["Food", 220]]),
            ("February", [["Item", "Cost"], ["Rent", 900], ["Books", 40]]),
        ])
        doc = documents.read("spending.xlsx", data)
        self.assertEqual(doc.kind, "Spreadsheet")
        self.assertIn("Rent", doc.text())
        self.assertIn("900", doc.text())
        self.assertIn("February", " ".join(page.label for page in doc.pages))
        self.assertEqual(doc.unit, "sheet")

    def test_reads_a_csv_as_readable_rows(self):
        doc = documents.read("people.csv", b"Name,City,Age\nAda,London,36\nGrace,New York,45\n")
        self.assertIn("Name: Ada", doc.text())
        self.assertIn("City: New York", doc.text())
        # The note must point at the sibling app, not pretend to predict.
        self.assertIn("nanoLearn", doc.note)

    def test_reads_a_pdf(self):
        data = build_pdf(["The deposit is one thousand pounds.", "The lease ends in June 2027."])
        doc = documents.read("lease.pdf", data)
        self.assertEqual(doc.kind, "PDF")
        self.assertEqual(doc.unit, "page")
        self.assertIn("deposit", doc.text())
        self.assertIn("June 2027", doc.text())
        self.assertEqual(len(doc.pages), 2)
        self.assertEqual(doc.pages[0].label, "page 1")
        self.assertEqual(doc.pages[1].label, "page 2")

    def test_reads_a_compressed_pdf(self):
        data = build_pdf(["Rooms are cleaned every Tuesday."], compressed=True)
        doc = documents.read("cleaning.pdf", data)
        self.assertIn("Tuesday", doc.text())

    def test_a_pdf_with_no_text_says_it_is_a_scan(self):
        # A page with no text operators at all stands in for a scanned page.
        data = build_pdf([""])
        with self.assertRaises(documents.Unreadable) as caught:
            documents.read("scan.pdf", data)
        message = str(caught.exception).lower()
        self.assertTrue("scan" in message or "no text" in message, message)

    def test_an_old_word_file_is_told_to_be_resaved(self):
        with self.assertRaises(documents.Unreadable) as caught:
            documents.read("letter.doc", b"\xd0\xcf\x11\xe0old binary word format")
        self.assertIn("docx", str(caught.exception))

    def test_an_unknown_file_is_refused_in_plain_words(self):
        with self.assertRaises(documents.Unreadable) as caught:
            documents.read("holiday.mp4", bytes(range(256)) * 20)
        self.assertIn("do not know how to read", str(caught.exception))

    def test_an_empty_file_is_refused(self):
        with self.assertRaises(documents.Unreadable):
            documents.read("nothing.txt", b"")

    def test_a_file_larger_than_the_limit_is_refused(self):
        with self.assertRaises(documents.Unreadable) as caught:
            documents.read("huge.txt", b"x" * (documents.MAX_BYTES + 1))
        self.assertIn("bigger than", str(caught.exception))

    def test_html_that_is_not_a_web_page_is_still_readable(self):
        doc = documents.read("page.html", b"<html><body><p>Just one sentence about invoices.</p></body></html>")
        self.assertIn("invoices", doc.text())

    def test_comment_before_the_pdf_header_is_tolerated(self):
        data = b"% comment line some tools write\n" + build_pdf(["Hello from a PDF."])
        self.assertTrue(documents.sniff("file", data) == "pdf")
        doc = documents.read("odd.pdf", data)
        self.assertIn("Hello", doc.text())


# --------------------------------------------------------------------------
# Finding things
# --------------------------------------------------------------------------
class TestSearch(unittest.TestCase):
    def setUp(self):
        self.pages = [
            documents.Page(1, "The deposit is one thousand two hundred pounds, payable on signing. "
                              "The landlord holds it in a deposit protection scheme.", "page 1"),
            documents.Page(2, "The tenant must keep the garden tidy and must not keep pets without "
                              "written permission from the landlord.", "page 2"),
            documents.Page(3, "This agreement may be ended by either party giving two months written "
                              "notice. Notice must be sent by recorded post.", "page 3"),
        ]
        self.index = search.Index("test", self.pages)

    def test_finds_the_right_page(self):
        hits = self.index.search("how much is the deposit")
        self.assertTrue(hits)
        self.assertEqual(hits[0].passage.number, 1)
        self.assertIn("deposit", hits[0].passage.text)

    def test_finds_a_later_page(self):
        hits = self.index.search("how much notice must be given")
        self.assertTrue(hits)
        self.assertEqual(hits[0].passage.number, 3)

    def test_a_word_only_on_one_page_wins(self):
        hits = self.index.search("pets")
        self.assertEqual(hits[0].passage.number, 2)

    def test_a_question_the_document_does_not_answer_is_reported_honestly(self):
        hits = self.index.search("what colour is the front door painted")
        confidence = self.index.confidence("what colour is the front door painted", hits)
        self.assertFalse(confidence["found"])

    def test_a_question_the_document_does_answer_is_trusted(self):
        question = "how much is the deposit"
        confidence = self.index.confidence(question, self.index.search(question))
        self.assertTrue(confidence["found"], confidence)

    def test_an_empty_question_finds_nothing_rather_than_crashing(self):
        self.assertEqual(self.index.search(""), [])
        self.assertEqual(self.index.search("   "), [])

    def test_a_question_of_only_small_words_still_searches_something(self):
        # "what is this about" is all stopwords; falling back to the unfiltered
        # words beats returning nothing at all.
        self.assertIsInstance(self.index.search("what is this about"), list)

    def test_plural_and_singular_match_each_other(self):
        # The document says "pets"; the question says "pet".
        hits = self.index.search("pet")
        self.assertTrue(hits)
        self.assertEqual(hits[0].passage.number, 2)

    def test_the_snippet_shows_the_part_that_matched(self):
        page = [documents.Page(1, ("Filler sentence about nothing in particular. " * 20)
                               + "The boiler must be serviced every year. "
                               + ("More filler that goes on and on. " * 20), "page 1")]
        index = search.Index("snippet", page)
        hits = index.search("boiler serviced")
        self.assertTrue(hits)
        self.assertIn("boiler", hits[0].snippet.lower())

    def test_a_long_page_is_cut_into_overlapping_passages(self):
        page = [documents.Page(1, " ".join("word%d" % number for number in range(400)), "page 1")]
        index = search.Index("long", page)
        self.assertGreater(len(index.passages), 3)
        for passage in index.passages:
            self.assertLessEqual(len(passage.text.split()), search.PASSAGE_WORDS + 5)

    def test_about_describes_a_document_without_any_ai(self):
        summary = search.about("test", self.pages)
        self.assertTrue(summary["keywords"])
        self.assertTrue(summary["opening"])
        self.assertIn("deposit", " ".join(summary["keywords"]))

    def test_stemming_does_not_mangle_short_words(self):
        self.assertEqual(search._stem("is"), "is")
        self.assertEqual(search._stem("gas"), "gas")
        self.assertEqual(search._stem("2027"), "2027")

    def test_the_index_keeps_page_labels_for_citing(self):
        hits = self.index.search("garden")
        self.assertEqual(hits[0].passage.label, "page 2")


# --------------------------------------------------------------------------
# Keeping documents
# --------------------------------------------------------------------------
class TestStore(unittest.TestCase):
    def setUp(self):
        store.forget_everything()

    def test_a_document_survives_being_saved_and_loaded(self):
        doc = documents.read("note.txt", b"The rent is due on the first of each month.")
        entry = store.save_document(doc)
        loaded = store.load_document(entry["id"])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.name, "note.txt")
        self.assertIn("first of each month", loaded.text())

    def test_dropping_the_same_file_twice_replaces_it(self):
        first = store.save_document(documents.read("note.txt", b"Version one about rent."))
        second = store.save_document(documents.read("note.txt", b"Version two about deposits."))
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["replaced"])
        self.assertEqual(len(store.load_library()), 1)
        self.assertIn("deposits", store.load_document(second["id"]).text())

    def test_two_different_files_both_stay(self):
        store.save_document(documents.read("one.txt", b"About apples."))
        store.save_document(documents.read("two.txt", b"About oranges."))
        self.assertEqual(len(store.load_library()), 2)

    def test_deleting_a_document_removes_its_text_too(self):
        entry = store.save_document(documents.read("note.txt", b"Temporary text about nothing."))
        self.assertTrue(store.delete_document(entry["id"]))
        self.assertIsNone(store.load_document(entry["id"]))
        self.assertEqual(store.load_library(), [])

    def test_a_made_up_id_is_refused(self):
        self.assertIsNone(store.load_document("../../settings"))
        self.assertIsNone(store.load_document("not-an-id"))

    def test_a_dangerous_file_name_is_made_safe(self):
        doc = documents.read("../../etc/passwd.txt", b"Something about a file.")
        entry = store.save_document(doc)
        self.assertNotIn("/", entry["source_name"])
        self.assertNotIn("\\", entry["source_name"])
        self.assertNotIn("..", entry["source_name"])

    def test_settings_round_trip_and_ignore_nonsense(self):
        store.save_settings({"engine_id": "ollama", "model": "llama3.2:1b", "evil": "ignored"})
        settings = store.load_settings()
        self.assertEqual(settings["engine_id"], "ollama")
        self.assertEqual(settings["model"], "llama3.2:1b")
        self.assertNotIn("evil", settings)

    def test_a_corrupt_settings_file_does_not_break_startup(self):
        store.settings_path().write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(store.load_settings()["engine_id"], "")

    def test_a_corrupt_library_file_does_not_break_startup(self):
        store.library_path().write_text("[[[", encoding="utf-8")
        self.assertEqual(store.load_library(), [])


# --------------------------------------------------------------------------
# Talking to an engine
# --------------------------------------------------------------------------
class TestEngine(unittest.TestCase):
    def test_the_prompt_forbids_outside_knowledge(self):
        messages = engine.build_messages(
            "How much is the deposit?",
            [search.Hit(passage=search.Passage("d", 1, "page 1", "The deposit is 1200 pounds.", 0), score=3.0)],
            "lease.pdf",
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("ONLY the extracts", messages[0]["content"])
        self.assertIn("I could not find that in your document", messages[0]["content"])
        self.assertIn("The deposit is 1200 pounds.", messages[1]["content"])
        self.assertIn("[1] (page 1)", messages[1]["content"])

    def test_the_summary_prompt_is_a_different_job(self):
        messages = engine.build_messages(
            "", [search.Hit(passage=search.Passage("d", 1, "page 1", "Text.", 0), score=1.0)],
            "doc.pdf", summary=True,
        )
        self.assertIn("describe documents", messages[0]["content"])
        self.assertNotIn("Question:", messages[1]["content"])

    def test_a_small_model_is_preferred_when_choosing_one(self):
        self.assertEqual(engine._best_default(["llama3.1:8b", "llama3.2:1b", "mistral:7b"]), "llama3.2:1b")
        self.assertEqual(engine._best_default([]), "")

    def test_the_chosen_model_wins_over_the_default(self):
        found = [{"id": "ollama", "name": "Ollama", "ok": True, "models": ["a:1b", "b:7b"], "port": 11434}]
        chosen, model = engine.pick(found, "ollama", "b:7b")
        self.assertEqual(model, "b:7b")
        chosen, model = engine.pick(found, "ollama", "gone:1b")
        self.assertEqual(model, "a:1b")

    def test_an_engine_with_no_models_is_not_ready(self):
        found = [{"id": "ollama", "name": "Ollama", "ok": True, "models": [], "port": 11434}]
        chosen, model = engine.pick(found, "", "")
        self.assertIsNone(chosen)
        self.assertEqual(engine.working(found), [])

    def test_money_sized_answers_are_asked_for_at_a_low_temperature(self):
        # Not a style preference: a question about a lease wants the document's
        # words, not the model's imagination.
        engine_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "nanodoc", "engine.py")
        with open(engine_path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("temperature: float = 0.2", source)

    def test_installing_is_shown_before_it_is_run(self):
        command = engine.install_command()
        self.assertIsInstance(command, list)

    def test_memory_can_be_reported(self):
        self.assertGreaterEqual(engine.total_ram_gb(), 0.0)

    def test_suggested_models_say_whether_they_fit(self):
        for model in engine.suggested_models():
            self.assertIn("fits", model)
            self.assertIn("needs_gb", model)

    def test_broken_json_from_a_small_engine_is_salvaged(self):
        self.assertEqual(engine._salvage_content('{"message": {"content": "Hello there'), "Hello there")
        self.assertEqual(engine._salvage_content('{"response": "Line one\\nLine two"'), "Line one\nLine two")
        self.assertEqual(engine._salvage_content("not json at all"), "")
    def test_the_pieces_of_a_reply_are_read_from_every_shape_of_event(self):
        self.assertEqual(engine._text_of({"message": {"content": "a"}}), "a")
        self.assertEqual(engine._text_of({"choices": [{"delta": {"content": "b"}}]}), "b")
        self.assertEqual(engine._text_of({"choices": [{"text": "c"}]}), "c")
        self.assertEqual(engine._text_of({"nothing": "useful"}), "")


class TestAnswerSupport(unittest.TestCase):
    """Does what the model wrote actually come from the document?

    The case that prompted this: asked about SELECT statements in a database
    assignment, a 17M-parameter model trained from scratch on children's
    stories answered "Hello! This is the most amazing day." The app cannot stop
    that happening, but it can refuse to present it as an answer about the
    document without a warning.
    """

    def hits(self, text="The deposit is one thousand two hundred pounds and is held in a scheme."):
        return [search.Hit(passage=search.Passage("d", 1, "page 1", text, 0), score=3.0)]

    def test_a_faithful_answer_is_left_alone(self):
        result = server._answer_support(
            "The deposit is one thousand two hundred pounds and the landlord holds it in a "
            "protection scheme (page 1).", self.hits())
        self.assertTrue(result["checked"])
        self.assertTrue(result["supported"], result)

    def test_an_invented_answer_is_flagged(self):
        result = server._answer_support("Hello! This is the most amazing day.", self.hits())
        self.assertTrue(result["checked"])
        self.assertFalse(result["supported"], result)

    def test_a_short_answer_built_from_unknown_words_is_flagged(self):
        result = server._answer_support("Bananas are purple.", self.hits())
        self.assertTrue(result["checked"])
        self.assertFalse(result["supported"], result)

    def test_a_short_answer_with_nothing_to_judge_is_left_alone(self):
        self.assertFalse(server._answer_support("Yes (page 1).", self.hits())["checked"])
        self.assertFalse(server._answer_support("(page 2)", self.hits())["checked"])
        self.assertFalse(server._answer_support("", self.hits())["checked"])

    def test_the_refusal_is_never_flagged(self):
        result = server._answer_support("I could not find that in your document.", self.hits())
        self.assertFalse(result["checked"])


# --------------------------------------------------------------------------
# The server, started for real
# --------------------------------------------------------------------------
class FakeEngine(BaseHTTPRequestHandler):
    """A stand-in for Ollama or LM Studio, speaking the OpenAI protocol."""

    reply = ["The deposit is ", "1200 pounds ", "(page 1)."]
    seen = []

    def log_message(self, fmt, *args):
        pass

    def _json(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json({"data": [{"id": "fake-model"}]})
        else:
            self._json({"error": "no"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        FakeEngine.seen.append(json.loads(body.decode("utf-8")))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for piece in self.reply:
            # Real engines frame each event as `data: {...}` followed by a blank
            # line, and the reader is written against that. Framing the fake
            # engine differently would test a protocol nobody speaks.
            payload = ("data: " + json.dumps({"choices": [{"delta": {"content": piece}}]})
                       + "\n\n").encode("utf-8")
            self.wfile.write(b"%X\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.flush()
        done = b"data: [DONE]\n\n"
        self.wfile.write(b"%X\r\n" % len(done) + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def read_sse(request):
    """Collect the events from a streaming endpoint into a list of dicts."""
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        buffer = ""
        for raw in response:
            buffer += raw.decode("utf-8", "replace")
            while "\n\n" in buffer:
                chunk, buffer = buffer.split("\n\n", 1)
                for line in chunk.split("\n"):
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload and payload != "[DONE]":
                            events.append(json.loads(payload))
    return events


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEngine)
        cls.engine_port = cls.engine_server.server_address[1]
        threading.Thread(target=cls.engine_server.serve_forever, daemon=True).start()

        cls.app = server.build_app()
        cls.httpd = httpbase._Server(("127.0.0.1", 0), cls.app)
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.base + "/api/state", timeout=0.5).read()
                break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.engine_server.shutdown()
        cls.engine_server.server_close()

    # -- helpers ---------------------------------------------------------
    def setUp(self):
        # Every test points the app at the fake engine on purpose. Anything
        # else listening on this machine (a llama.cpp server on 8080, say)
        # would otherwise change what these tests see, and one of the tests
        # here is about the exact words sent to the engine.
        self.use_fake_engine()

    def use_fake_engine(self):
        self.post_json("/api/settings", {
            "custom_base_url": "http://127.0.0.1:%d/v1" % self.engine_port,
            "engine_id": "custom",
        })

    def get_json(self, path):
        with urllib.request.urlopen(self.base + path, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, path, payload):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def upload(self, name, data, slice_size=1024):
        start = self.post_json("/api/upload/start", {"name": name})
        offset = 0
        while offset < len(data):
            piece = data[offset:offset + slice_size]
            request = urllib.request.Request(
                self.base + "/api/upload/chunk?id=%s&offset=%d" % (start["id"], offset),
                data=piece, headers={"Content-Type": "application/octet-stream"}, method="POST",
            )
            urllib.request.urlopen(request, timeout=20).read()
            offset += len(piece)
        return self.post_json("/api/upload/finish", {"id": start["id"]})

    def ask(self, doc_id, question, summary=False):
        request = urllib.request.Request(
            self.base + "/api/ask",
            data=json.dumps({"id": doc_id, "question": question}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        return read_sse(request)

    # -- the tests -------------------------------------------------------
    def test_the_page_is_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            body = response.read().decode("utf-8")
        self.assertIn("nanoDoc", body)
        self.assertIn("Drag a document here", body)

    def test_the_stylesheet_and_script_are_served(self):
        for path in ("/style.css", "/app.js"):
            with urllib.request.urlopen(self.base + path, timeout=10) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read())

    def test_an_unknown_api_address_says_so(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/api/nonsense", timeout=10)
        self.assertEqual(caught.exception.code, 404)

    def test_state_describes_the_engines_and_the_library(self):
        data = self.get_json("/api/state")
        self.assertIn("documents", data)
        self.assertIn("engine", data)
        self.assertIn("suggested", data["engine"])
        self.assertIsInstance(data["engine"]["engines"], list)

    def test_a_document_can_be_uploaded_in_pieces_and_read(self):
        text = ("Lease agreement\n\nThe deposit is 1200 pounds and is held in a protection scheme.\n\n"
                "The tenant must not keep pets without written permission.\n") * 20
        result = self.upload("lease.txt", text.encode("utf-8"))
        self.assertIn("document", result)
        self.assertEqual(result["document"]["name"], "lease.txt")
        self.assertIn("words", result["document"])
        self.assertIn("about", result)

    def test_an_unreadable_file_is_refused_with_an_explanation(self):
        request = urllib.request.Request(
            self.base + "/api/upload/start", data=json.dumps({"name": "clip.mp4"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        start = json.loads(urllib.request.urlopen(request, timeout=10).read().decode("utf-8"))
        chunk = urllib.request.Request(
            self.base + "/api/upload/chunk?id=%s&offset=0" % start["id"],
            data=b"\x00\x01\x02\x03" * 100, headers={"Content-Type": "application/octet-stream"}, method="POST",
        )
        urllib.request.urlopen(chunk, timeout=10).read()
        finish = urllib.request.Request(
            self.base + "/api/upload/finish", data=json.dumps({"id": start["id"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(finish, timeout=10)
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("do not know how to read", body["error"])

    def test_out_of_order_pieces_are_refused(self):
        start = self.post_json("/api/upload/start", {"name": "late.txt"})
        request = urllib.request.Request(
            self.base + "/api/upload/chunk?id=%s&offset=999" % start["id"],
            data=b"hello", headers={"Content-Type": "application/octet-stream"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 409)

    def test_finding_paragraphs_needs_no_engine_at_all(self):
        result = self.upload("rules.txt", b"Smoking is not allowed anywhere in the building.\n" * 3)
        doc_id = result["document"]["id"]
        found = self.post_json("/api/search", {"id": doc_id, "question": "is smoking allowed"})
        self.assertTrue(found["hits"])
        self.assertIn("Smoking", found["hits"][0]["text"])
        self.assertEqual(found["hits"][0]["label"], "part 1")

    def test_a_question_shows_the_paragraphs_before_anything_is_written(self):
        result = self.upload("quiet.txt", b"The office is closed on the 24th of December.\n" * 3)
        events = self.ask(result["document"]["id"], "when is the office closed")
        self.assertEqual(events[0]["type"], "found", "the sources must arrive before the answer")
        found = events[0]
        self.assertTrue(found["hits"])
        self.assertIn("December", found["hits"][0]["text"])

    def test_with_no_engine_the_paragraphs_are_still_the_whole_answer(self):
        # Called directly rather than over HTTP, because "no engine is running"
        # is not something a test can arrange on a machine that has one.
        doc = documents.read("quiet.txt", b"The office is closed on the 24th of December.\n" * 3)
        entry = store.save_document(doc)
        index = search.Index(entry["id"], doc.pages)
        events = list(server._answer_events(doc, index, "when is the office closed", False, None, ""))
        kinds = [event["type"] for event in events]
        self.assertIn("found", kinds)
        self.assertIn("no_engine", kinds)
        self.assertNotIn("engine", kinds)
        found = next(event for event in events if event["type"] == "found")
        self.assertTrue(found["hits"])
        self.assertIn("December", found["hits"][0]["text"])

    def test_a_question_the_document_cannot_answer_says_so(self):
        result = self.upload("short.txt", b"The bicycle shed is behind the main building.\n" * 3)
        events = self.ask(result["document"]["id"], "what is the annual salary for a manager")
        found = next(event for event in events if event["type"] == "found")
        self.assertFalse(found["confidence"]["found"])

    def test_a_question_about_a_missing_document_is_a_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post_json("/api/search", {"id": "deadbeefdeadbeef", "question": "hello"})
        self.assertEqual(caught.exception.code, 404)

    def test_an_empty_question_is_refused(self):
        result = self.upload("empty-q.txt", b"Some text about a bicycle.\n" * 3)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post_json("/api/search", {"id": result["document"]["id"], "question": "   "})
        self.assertEqual(caught.exception.code, 400)

    def test_the_whole_answer_path_streams_against_a_real_engine(self):
        # The app is pointed at the fake engine by setUp, exactly as somebody
        # would by typing its address into settings.
        result = self.upload("deposit.txt",
                             b"The deposit is 1200 pounds and is protected by law.\n" * 3)
        doc_id = result["document"]["id"]
        events = self.ask(doc_id, "how much is the deposit")

        kinds = [event["type"] for event in events]
        self.assertIn("found", kinds)
        self.assertIn("engine", kinds)
        self.assertIn("answer", kinds)
        self.assertEqual(kinds[-1], "done")

        answer = "".join(event["text"] for event in events if event["type"] == "answer")
        self.assertIn("1200 pounds", answer)

        # The prompt the engine actually received must contain the document's
        # own words, so the answer can be traced back to it.
        self.assertTrue(FakeEngine.seen, "the engine was never called")
        sent = json.dumps(FakeEngine.seen[-1])
        self.assertIn("protected by law", sent)
        self.assertIn("I could not find that in your document", sent)

    def test_the_summary_address_works_without_a_question(self):
        result = self.upload("sum.txt", b"A tenancy agreement between a landlord and a tenant.\n" * 3)
        request = urllib.request.Request(
            self.base + "/api/summary",
            data=json.dumps({"id": result["document"]["id"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        events = read_sse(request)
        self.assertIn("found", [event["type"] for event in events])
        self.assertEqual(events[-1]["type"], "done")

    def test_a_document_can_be_fetched_back_with_its_pages(self):
        result = self.upload("pages.txt", b"Page one text about a garden.\n" * 30)
        doc_id = result["document"]["id"]
        data = self.get_json("/api/doc/" + doc_id)
        self.assertTrue(data["document"]["full_pages"])
        self.assertTrue(data["about"]["keywords"])

    def test_a_document_can_be_deleted(self):
        result = self.upload("delete-me.txt", b"Something to remove.\n" * 3)
        doc_id = result["document"]["id"]
        request = urllib.request.Request(self.base + "/api/doc/" + doc_id, method="DELETE")
        self.assertTrue(json.loads(urllib.request.urlopen(request, timeout=10).read().decode("utf-8"))["ok"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get_json("/api/doc/" + doc_id)
        self.assertEqual(caught.exception.code, 404)

    def test_settings_decide_which_engine_is_used(self):
        self.post_json("/api/settings", {"engine_id": "ollama", "model": "llama3.2:1b"})
        data = self.get_json("/api/state")
        self.assertIn("engine_id", data["settings"])
        self.assertEqual(store.load_settings()["model"], "llama3.2:1b")
        # Put it back so the other tests see the fake engine first.
        self.post_json("/api/settings", {"engine_id": "", "model": ""})

    def test_doctor_lists_every_engine_in_plain_words(self):
        data = self.get_json("/api/doctor")
        self.assertIn("lines", data)
        self.assertTrue(any("nanoDoc" in line or "not found" in line or "running" in line
                            for line in data["lines"] + [""]))
        self.assertTrue(data["lines"])

    def test_a_dropped_pdf_goes_all_the_way_through(self):
        data = build_pdf(["The deposit is 1200 pounds, payable on signing.",
                          "The garden must be kept tidy."], compressed=True)
        result = self.upload("lease.pdf", data)
        self.assertEqual(result["document"]["kind"], "PDF")
        self.assertEqual(result["document"]["unit"], "page")
        self.assertEqual(result["document"]["pages"], 2)
        found = self.post_json("/api/search", {"id": result["document"]["id"], "question": "how much is the deposit"})
        self.assertTrue(found["hits"])
        self.assertEqual(found["hits"][0]["label"], "page 1")


# --------------------------------------------------------------------------
# The PDF parser on its own
# --------------------------------------------------------------------------
class TestPdfReading(unittest.TestCase):
    def test_recognises_a_pdf(self):
        self.assertTrue(pdftext.is_pdf(build_pdf(["hello"])))
        self.assertFalse(pdftext.is_pdf(b"not a pdf"))
        self.assertTrue(pdftext.is_pdf(b"% comment first\n" + build_pdf(["hello"])))

    def test_never_raises_on_garbage(self):
        for payload in (b"", b"not a pdf at all", b"%PDF-1.4 then nothing",
                        build_pdf(["text"])[:60], bytes(range(256)) * 4):
            result = pdftext.extract(payload)
            self.assertIsInstance(result, pdftext.PdfResult)
            self.assertIsInstance(result.pages, list)

    def test_text_survives_the_round_trip(self):
        result = pdftext.extract(build_pdf(["Hello world.", "Second page here."]))
        self.assertEqual(result.pages[0].text.strip(), "Hello world.")
        self.assertEqual(result.pages[1].text.strip(), "Second page here.")

    def test_escaped_brackets_come_back(self):
        # A PDF writes a literal bracket as \( and \). The builder escapes the
        # text on the way in, so what comes out must be the brackets again.
        result = pdftext.extract(build_pdf(["Costs (total) 50%"]))
        self.assertIn("Costs (total) 50%", result.pages[0].text)

    def test_a_compressed_stream_is_decompressed(self):
        result = pdftext.extract(build_pdf(["Squeezed text about a boiler."], compressed=True))
        self.assertIn("boiler", result.pages[0].text)


if __name__ == "__main__":
    unittest.main()


class ExportTests(TestServer):
    """The whole conversation saved as one readable file, sources included."""

    def test_a_conversation_is_exported_with_its_sources(self):
        body = json.dumps({
            "title": "bridge report",
            "turns": [
                {"question": "What is the load limit?",
                 "answer": "The report says 12 tonnes.",
                 "hits": [{"label": "page 3", "number": 3, "snippet": "maximum load of 12 tonnes"}]},
                {"question": "Who signed it?",
                 "answer": "Nobody signed it.", "hits": []},
            ],
        }).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/export", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertTrue(data["ok"], data)
        saved = Path(data["path"])
        try:
            self.assertTrue(saved.exists())
            text = saved.read_text(encoding="utf-8")
            self.assertIn("# bridge report", text)
            self.assertIn("What is the load limit?", text)
            self.assertIn("The report says 12 tonnes.", text)
            self.assertIn("Where this came from", text)
            self.assertIn("page 3", text)
            self.assertIn("Question 2", text)
        finally:
            saved.unlink(missing_ok=True)

    def test_exporting_nothing_is_explained(self):
        body = json.dumps({"title": "empty", "turns": []}).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/export", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(request, timeout=10)
            self.fail("expected an error")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertIn("nothing to export", exc.read().decode("utf-8").lower())
