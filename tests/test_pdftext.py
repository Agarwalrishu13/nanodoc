"""Tests for nanodoc.pdftext.

Every fixture here is built byte by byte in this file. Nothing is downloaded,
nothing is read from disk, and no third-party PDF writer is involved - which
is the point: the reader under test is the only thing that knows the format.

The fixtures are deliberately written the way real files are, not the way a
library would write them, so a parser that only handles one tidy dialect
fails here.
"""

import os
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nanodoc import pdftext  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
_CONTENT_FONT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"


class _Builder:
    """Assemble a PDF with a classic xref table.

    Object numbers are handed out in insertion order, starting at 1, which is
    how the fixtures below can reference objects before they are added.
    """

    def __init__(self, version=b"1.4"):
        self.version = version
        self.bodies = []
        self.trailer_extra = b""

    def add(self, body):
        self.bodies.append(body)
        return len(self.bodies)

    def stream(self, data, extra=b""):
        body = b"<< /Length %d %s >>\nstream\n" % (len(data), extra)
        return self.add(body + data + b"\nendstream")

    def build(self, root=1, info=None):
        out = bytearray(b"%PDF-" + self.version + b"\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(self.bodies, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % number
            out += body
            out += b"\nendobj\n"
        xref_at = len(out)
        out += b"xref\n0 %d\n" % (len(self.bodies) + 1)
        out += b"0000000000 65535 f \n"
        for offset in offsets:
            out += b"%010d 00000 n \n" % offset
        trailer = b"<< /Size %d /Root %d 0 R" % (len(self.bodies) + 1, root)
        if info is not None:
            trailer += b" /Info %d 0 R" % info
        trailer += self.trailer_extra + b" >>"
        out += b"trailer\n" + trailer + b"\nstartxref\n%d\n%%%%EOF\n" % xref_at
        return bytes(out)


def _document(contents, compress=False, font=_CONTENT_FONT, title=None, extra_trailer=b""):
    """A one-or-more page document whose pages each show the given content.

    Object layout: 1 catalog, 2 pages, then a page and a content stream per
    page, then the font, then the info dictionary when a title is wanted.
    """
    builder = _Builder()
    builder.add(b"")  # 1, the catalog
    builder.add(b"")  # 2, the page tree
    page_numbers = []
    content_numbers = []
    for _ in contents:
        page_numbers.append(len(builder.bodies) + 1)
        content_numbers.append(len(builder.bodies) + 2)
        builder.add(b"")  # placeholder, filled in below
        builder.add(b"")
    font_number = builder.add(font)
    info_number = builder.add(b"<< /Title (%s) >>" % title) if title else None

    for index, content in enumerate(contents):
        resources = b"<< /Font << /F1 %d 0 R >> >>" % font_number
        builder.bodies[page_numbers[index] - 1] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            + resources
            + b" /Contents %d 0 R >>" % content_numbers[index]
        )
        body = content
        extra = b""
        if compress:
            body = zlib.compress(content)
            extra = b"/Filter /FlateDecode "
        builder.bodies[content_numbers[index] - 1] = (
            b"<< /Length %d %s>>\nstream\n" % (len(body), extra) + body + b"\nendstream"
        )

    kids = b" ".join(b"%d 0 R" % n for n in page_numbers)
    builder.bodies[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    builder.bodies[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(contents))
    if extra_trailer:
        builder.trailer_extra = extra_trailer
    return builder.build(info=info_number)


def _xref_stream_pdf(objs, root, size, xref_body, xref_extra=b"", predictor=False):
    """Glue objects together, then append a hand-written XRef stream.

    `objs` is a list of raw object bodies (object 1 first). The xref stream is
    written last and becomes the object numbered `len(objs) + 1`; `xref_body`
    is called with the object offsets, that number, and the xref's own offset.
    """
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_number = len(objs) + 1
    xref_at = len(out)
    payload = xref_body(offsets, xref_number, xref_at)
    if predictor:
        payload = _png_up_encode(payload, 4)
        xref_extra = b"/Filter /FlateDecode /DecodeParms << /Predictor 12 /Columns 4 >> " + xref_extra
        payload = zlib.compress(payload)
    header = b"<< /Type /XRef /Size %d /W [1 2 1] /Root %d 0 R %s>>\nstream\n" % (
        size,
        root,
        xref_extra,
    )
    out += b"%d 0 obj\n" % xref_number + header + payload + b"\nendstream\nendobj\n"
    out += b"startxref\n%d\n%%%%EOF\n" % xref_at
    return bytes(out)


def _png_up_encode(data, width):
    """Encode rows with PNG filter type 2 (Up), the way xref streams do."""
    row = width
    out = bytearray()
    previous = bytearray(row)
    for start in range(0, len(data), row):
        chunk = bytearray(data[start : start + row])
        encoded = bytearray([2])
        for k in range(len(chunk)):
            encoded.append((chunk[k] - previous[k]) & 0xFF)
        out += encoded
        previous = chunk
    return bytes(out)


def _minimal_page():
    """Catalog, Pages node, Page, content stream and font, in that order."""
    content = b"BT /F1 24 Tf 72 700 Td (Cross reference stream text) Tj ET"
    return [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
        b"<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        _CONTENT_FONT,
    ]


def _xref_rows(offsets, _number, xref_at):
    """Rows for a /W [1 2 1] cross-reference stream."""
    rows = bytearray(bytes([0, 0, 0, 0]))  # object 0 is free
    for offset in offsets:
        rows += bytes([1]) + offset.to_bytes(2, "big") + b"\x00"
    rows += bytes([1]) + xref_at.to_bytes(2, "big") + b"\x00"
    return bytes(rows)


# ---------------------------------------------------------------------------
# Simple, well-formed documents
# ---------------------------------------------------------------------------
class TestSimpleDocuments(unittest.TestCase):
    def test_uncompressed_text_round_trips(self):
        data = _document([b"BT /F1 24 Tf 72 700 Td (Hello, world.) Tj ET"])
        result = pdftext.extract(data)
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.pages[0].text, "Hello, world.")
        self.assertEqual(result.pages[0].word_count(), 2)
        self.assertEqual(result.note, "")
        self.assertFalse(result.is_empty())

    def test_flate_compressed_matches_plain(self):
        content = b"BT /F1 24 Tf 72 700 Td (Hello, world.) Tj ET"
        plain = pdftext.extract(_document([content]))
        packed = pdftext.extract(_document([content], compress=True))
        self.assertEqual(packed.pages[0].text, plain.pages[0].text)
        self.assertEqual(packed.pages[0].text, "Hello, world.")
        self.assertEqual(packed.note, "")

    def test_truncated_flate_stream_still_yields_text(self):
        # A stream whose tail is missing must still give up what was written.
        content = b"BT /F1 24 Tf 72 700 Td (Recovered from a broken stream) Tj ET"
        packed = zlib.compress(content)
        builder = _Builder()
        builder.add(b"<< /Type /Catalog /Pages 2 0 R >>")
        builder.add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        builder.add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        )
        broken = packed[: len(packed) - 4]  # lose the check bytes and the tail
        builder.add(
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(broken)
            + broken
            + b"\nendstream"
        )
        builder.add(_CONTENT_FONT)
        result = pdftext.extract(builder.build())
        self.assertIn("Recovered from a broken stream", result.text())

    def test_escapes_and_parentheses(self):
        content = (
            b"BT /F1 12 Tf 72 700 Td (Costs \\(total\\) 50%) Tj"
            b" 0 -20 Td (back\\\\slash \\101 \\t tab) Tj ET"
        )
        result = pdftext.extract(_document([content]))
        text = result.pages[0].text
        self.assertIn("Costs (total) 50%", text)
        self.assertIn("back\\slash A", text)

    def test_tj_array_kerning(self):
        content = b"BT /F1 12 Tf 72 700 Td [(Big) -250 (Gap) -40 (Small)] TJ ET"
        result = pdftext.extract(_document([content]))
        text = result.pages[0].text
        self.assertEqual(text, "Big GapSmall")
        self.assertNotIn("-250", text)
        self.assertNotIn("-40", text)

    def test_hex_string_winansi_accent(self):
        content = b"BT /F1 12 Tf 72 700 Td <636166e9> Tj ET"
        result = pdftext.extract(_document([content]))
        self.assertEqual(result.pages[0].text, "caf\xe9")

    def test_literal_backslash_separated_letters(self):
        content = b"BT /F1 12 Tf 72 700 Td (A\\050B\\051) Tj ET"
        result = pdftext.extract(_document([content]))
        self.assertEqual(result.pages[0].text, "A(B)")

    def test_multi_page_numbers_and_placement(self):
        contents = [
            b"BT /F1 12 Tf 72 700 Td (First page text) Tj ET",
            b"BT /F1 12 Tf 72 700 Td (Second page text) Tj ET",
            b"BT /F1 12 Tf 72 700 Td (Third page text) Tj ET",
        ]
        result = pdftext.extract(_document(contents))
        self.assertEqual([p.number for p in result.pages], [1, 2, 3])
        self.assertEqual(result.pages[0].text, "First page text")
        self.assertEqual(result.pages[1].text, "Second page text")
        self.assertEqual(result.pages[2].text, "Third page text")
        self.assertEqual(result.word_count(), 9)

    def test_line_and_paragraph_breaks(self):
        content = (
            b"BT /F1 12 Tf 72 700 Td (Line one) Tj"
            b" 0 -14 Td (Line two) Tj"
            b" 0 -40 Td (New paragraph) Tj ET"
        )
        result = pdftext.extract(_document([content]))
        self.assertEqual(result.pages[0].text, "Line one\nLine two\n\nNew paragraph")

    def test_apostrophe_operator_starts_a_line(self):
        content = b"BT /F1 12 Tf 14 TL 72 700 Td (One) Tj (Two) ' (Three) ' ET"
        result = pdftext.extract(_document([content]))
        self.assertEqual(result.pages[0].text, "One\nTwo\nThree")

    def test_separate_text_objects_on_one_line_are_joined(self):
        content = (
            b"BT /F1 12 Tf 72 700 Td (Left) Tj ET "
            b"BT /F1 12 Tf 300 700 Td (Right) Tj ET"
        )
        result = pdftext.extract(_document([content]))
        self.assertEqual(result.pages[0].text, "Left Right")

    def test_control_characters_are_removed(self):
        content = b"BT /F1 12 Tf 72 700 Td (Clean\\000text\\007here) Tj ET"
        result = pdftext.extract(_document([content]))
        self.assertEqual(result.pages[0].text, "Cleantexthere")

    def test_word_split_across_two_show_operations_is_rejoined(self):
        # Real files break words across Tj runs; a space here would be wrong.
        content = (
            b"BT /F1 12 Tf 72 700 Td (Hel) Tj [(lo) -8 (there)] TJ ET"
        )
        result = pdftext.extract(_document([content]))
        self.assertEqual(result.pages[0].text, "Hellothere")

    def test_title_from_info_dictionary(self):
        data = _document([b"BT /F1 12 Tf 72 700 Td (Body) Tj ET"], title=b"My Lease")
        result = pdftext.extract(data)
        self.assertEqual(result.title, "My Lease")

    def test_utf16_title(self):
        title = b"\xfe\xff\x00M\x00y\x00 \x00D\x00o\x00c"
        builder = _Builder()
        builder.add(b"<< /Type /Catalog /Pages 2 0 R >>")
        builder.add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        builder.add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        )
        builder.add(b"<< /Length 5 >>\nstream\nBT ET\nendstream")
        builder.add(_CONTENT_FONT)
        info = builder.add(b"<< /Title <" + title.hex().encode("ascii") + b"> >>")
        result = pdftext.extract(builder.build(info=info))
        self.assertEqual(result.title, "My Doc")


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
def _cid_font_objects(builder, cid_table):
    """Add a Type0/CIDFontType2 font with a /W width table, return its number.

    `cid_table` maps a character to (cid, width in thousandths of an em).
    Returns (font number, widths-as-written-to-/W).
    """
    groups = {}
    for _char, (cid, width) in cid_table.items():
        groups[cid] = width
    ordered = sorted(groups)
    table = b"[" + b" ".join(
        b"%d [%d]" % (cid, groups[cid]) for cid in ordered
    ) + b"]"
    descendant = builder.add(
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Test /DW 500 "
        b"/W " + table + b" >>"
    )
    cmap = b"beginbfchar\n" + b"".join(
        b"<%04X> <%s>\n" % (cid, _HEX_TEXT[_char])
        for _char, (cid, _width) in sorted(cid_table.items(), key=lambda kv: kv[1][0])
    ) + b"endbfchar\n"
    cmap_number = builder.add(
        b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream"
    )
    font = builder.add(
        b"<< /Type /Font /Subtype /Type0 /BaseFont /Test /Encoding /Identity-H "
        b"/DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>" % (descendant, cmap_number)
    )
    return font


def _utf16_hex(text):
    return text.encode("utf-16-be").hex().encode("ascii")


_HEX_TEXT = {
    " ": _utf16_hex(" "),
    "M": _utf16_hex("M"),
    "u": _utf16_hex("u"),
    "k": _utf16_hex("k"),
    "e": _utf16_hex("e"),
    "s": _utf16_hex("s"),
    "h": _utf16_hex("h"),
    "a": _utf16_hex("a"),
    "n": _utf16_hex("n"),
    "g": _utf16_hex("g"),
    "m": _utf16_hex("m"),
    "t": _utf16_hex("t"),
}

# Character: (CID, width in thousandths of an em). The widths are the real
# Times New Roman ones, which is the point: a capital M is nearly twice as
# wide as the average letter, so a reader that guesses an average width for
# it decides the next letter must be a new word.
_GLYPHS = {
    " ": (3, 250),
    "M": (40, 889),
    "u": (41, 500),
    "k": (42, 500),
    "e": (43, 444),
    "s": (44, 389),
    "h": (45, 500),
    "a": (46, 444),
    "n": (47, 500),
    "g": (48, 500),
    "m": (49, 778),
    "t": (50, 278),
}


def _positioned_by_td(text, font_name=b"F1", size=13.0):
    """One Tj per glyph, each placed with its own Td - how Word writes text.

    The numbers are real: each Td moves by the previous glyph's own width, so
    the glyphs are butted up against each other with no gap at all between
    letters and exactly a space glyph where the word breaks are.
    """
    parts = [b"BT /%s %.4f Tf 1 0 0 1 72 700 Tm" % (font_name, size)]
    advance = 0.0
    for char in text:
        cid = _GLYPHS[char][0]
        parts.append(b"%.6f 0 Td <%04X> Tj" % (advance, cid))
        advance = _GLYPHS[char][1] / 1000.0 * size
    parts.append(b"ET")
    return b" ".join(parts)


class TestFonts(unittest.TestCase):
    def _word_fixture(self, text):
        """A Type0 font with /W widths and glyph-by-glyph positioning."""
        builder = _Builder()
        builder.add(b"<< /Type /Catalog /Pages 2 0 R >>")
        builder.add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        content = _positioned_by_td(text)
        builder.add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        )
        builder.add(
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        )
        font = _cid_font_objects(builder, _GLYPHS)
        assert font == 5
        return builder.build()

    def test_glyph_by_glyph_positioning_keeps_words_whole(self):
        # A capital M is 0.889 em wide. Guessing 0.45 em for it makes the next
        # letter look like a new word, which is how "Mukesh" became "M ukesh"
        # and "Management" became "M anagement" in a real Word-made PDF.
        result = pdftext.extract(self._word_fixture("Mukesh Management"))
        self.assertEqual(result.pages[0].text, "Mukesh Management")
        self.assertNotIn("M ukesh", result.text())
        self.assertNotIn("M anagement", result.text())
        self.assertEqual(result.word_count(), 2)

    def test_simple_font_widths_keep_words_whole(self):
        # The same trap on a simple font, where /Widths is indexed from
        # /FirstChar: entry 0 belongs to code 32.
        text = "Mukesh Management"
        first = 32
        widths = [
            _GLYPHS[chr(code)][1] if chr(code) in _GLYPHS else 500
            for code in range(first, 127)
        ]
        parts = [b"BT /F1 13 Tf 1 0 0 1 72 700 Tm"]
        advance = 0.0
        for char in text:
            parts.append(b"%.6f 0 Td (%s) Tj" % (advance, char.encode("ascii")))
            advance = widths[ord(char) - first] / 1000.0 * 13.0
        parts.append(b"ET")
        content = b" ".join(parts)

        builder = _Builder()
        builder.add(b"<< /Type /Catalog /Pages 2 0 R >>")
        builder.add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        builder.add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        )
        builder.add(
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        )
        builder.add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Test /FirstChar 32 "
            b"/LastChar 126 /Widths [%s] >>"
            % b" ".join(b"%d" % w for w in widths)
        )
        result = pdftext.extract(builder.build())
        self.assertEqual(result.pages[0].text, text)

    def test_tj_kerning_between_letters_does_not_split_words(self):
        # A kerning-happy writer puts a small negative adjustment between
        # every pair of letters. Those are letter spacing, not word gaps.
        content = (
            b"BT /F1 12 Tf 72 700 Td "
            b"[(M) -10 (u) -25 (k) -40 (e) -10 (s) -30 (h)] TJ "
            b"[(and) -500 (Patel)] TJ ET"
        )
        result = pdftext.extract(_document([content]))
        text = result.pages[0].text
        self.assertIn("Mukesh", text)
        self.assertIn("and Patel", text)
        self.assertNotIn("M ukesh", text)
        self.assertNotIn("-10", text)

    def _with_tounicode(self, cmap, content, subtype=b"/Type1", encoding=b"/WinAnsiEncoding"):
        font = (
            b"<< /Type /Font /Subtype %s /BaseFont /Embedded /Encoding %s "
            b"/ToUnicode 6 0 R >>" % (subtype, encoding)
        )
        builder = _Builder()
        builder.add(b"<< /Type /Catalog /Pages 2 0 R >>")
        builder.add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        builder.add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        )
        builder.add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        builder.add(font)
        cmap_data = cmap
        builder.add(
            b"<< /Length %d >>\nstream\n" % len(cmap_data) + cmap_data + b"\nendstream"
        )
        return builder.build()

    def test_tounicode_bfchar_wins_over_latin1(self):
        cmap = (
            b"/CIDInit /ProcSet findresource begin\n"
            b"beginbfchar\n"
            b"<41> <005A>\n"
            b"<42> <0065>\n"
            b"endbfchar\n"
        )
        data = self._with_tounicode(
            cmap, b"BT /F1 12 Tf 72 700 Td (AB) Tj ET"
        )
        self.assertEqual(pdftext.extract(data).pages[0].text, "Ze")

    def test_tounicode_bfrange(self):
        cmap = (
            b"beginbfrange\n"
            b"<41> <43> <0061>\n"
            b"<44> <44> [<00e9>]\n"
            b"endbfrange\n"
        )
        data = self._with_tounicode(cmap, b"BT /F1 12 Tf 72 700 Td (ABCD) Tj ET")
        self.assertEqual(pdftext.extract(data).pages[0].text, "abc\xe9")

    def test_two_byte_identity_font(self):
        cmap = b"beginbfchar\n<0048> <0048>\n<0069> <0069>\n<0021> <0021>\nendbfchar\n"
        font = (
            b"<< /Type /Font /Subtype /Type0 /BaseFont /Embedded "
            b"/Encoding /Identity-H /DescendantFonts [7 0 R] /ToUnicode 6 0 R >>"
        )
        content = b"BT /F1 12 Tf 72 700 Td <004800690021> Tj ET"
        builder = _Builder()
        builder.add(b"<< /Type /Catalog /Pages 2 0 R >>")
        builder.add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        builder.add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        )
        builder.add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        builder.add(font)
        builder.add(
            b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream"
        )
        builder.add(b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /Embedded >>")
        result = pdftext.extract(builder.build())
        self.assertEqual(result.pages[0].text, "Hi!")

    def test_two_byte_font_without_map_yields_nothing_and_says_so(self):
        font = (
            b"<< /Type /Font /Subtype /Type0 /BaseFont /Embedded "
            b"/Encoding /Identity-H /DescendantFonts [6 0 R] >>"
        )
        content = b"BT /F1 12 Tf 72 700 Td <00480069> Tj ET"
        builder = _Builder()
        builder.add(b"<< /Type /Catalog /Pages 2 0 R >>")
        builder.add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        builder.add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        )
        builder.add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        builder.add(font)
        builder.add(b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /Embedded >>")
        result = pdftext.extract(builder.build())
        self.assertTrue(result.is_empty())
        self.assertIn("font", result.note.lower())


# ---------------------------------------------------------------------------
# Modern documents: cross-reference streams and object streams
# ---------------------------------------------------------------------------
class TestCrossReferenceStreams(unittest.TestCase):
    def test_xref_stream_plain(self):
        data = _xref_stream_pdf(_minimal_page(), root=1, size=6, xref_body=_xref_rows)
        result = pdftext.extract(data)
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.pages[0].text, "Cross reference stream text")

    def test_xref_stream_flate_with_png_predictor(self):
        data = _xref_stream_pdf(
            _minimal_page(), root=1, size=6, xref_body=_xref_rows, predictor=True
        )
        result = pdftext.extract(data)
        self.assertEqual(result.pages[0].text, "Cross reference stream text")

    def test_objects_inside_an_object_stream(self):
        inner = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        ]
        offsets = []
        running = 0
        for body in inner:
            offsets.append(running)
            running += len(body) + 1
        header = b"".join(
            b"%d %d " % (index + 1, offsets[index]) for index in range(len(inner))
        )
        packed = header + b" ".join(inner)

        content = b"BT /F1 24 Tf 72 700 Td (Text from an object stream) Tj ET"
        bodies = {
            4: b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            5: _CONTENT_FONT,
            10: b"<< /Type /ObjStm /N 3 /First %d /Length %d >>\nstream\n"
            % (len(header), len(packed))
            + packed
            + b"\nendstream",
        }

        out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
        written = {}
        for number in (4, 5, 10):  # 1-3 live inside object stream 10
            written[number] = len(out)
            out += b"%d 0 obj\n" % number + bodies[number] + b"\nendobj\n"
        xref_at = len(out)
        rows = bytearray(bytes([0, 0, 0, 0]))
        for index in range(3):
            rows += bytes([2]) + (10).to_bytes(2, "big") + bytes([index])
        for number in (4, 5):
            rows += bytes([1]) + written[number].to_bytes(2, "big") + b"\x00"
        rows += bytes([1]) + written[10].to_bytes(2, "big") + b"\x00"
        rows += bytes([1]) + xref_at.to_bytes(2, "big") + b"\x00"
        header = (
            b"<< /Type /XRef /Size 12 /W [1 2 1] /Index [0 6 10 2] "
            b"/Root 1 0 R >>\nstream\n"
        )
        out += b"11 0 obj\n" + header + bytes(rows) + b"\nendstream\nendobj\n"
        out += b"startxref\n%d\n%%%%EOF\n" % xref_at

        result = pdftext.extract(bytes(out))
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.pages[0].text, "Text from an object stream")


# ---------------------------------------------------------------------------
# Damaged, encrypted and scanned files
# ---------------------------------------------------------------------------
class TestAwkwardFiles(unittest.TestCase):
    def test_garbage_is_reported_not_raised(self):
        result = pdftext.extract(b"not a pdf at all")
        self.assertIsInstance(result, pdftext.PdfResult)
        self.assertTrue(result.is_empty())
        self.assertTrue(result.note)
        self.assertFalse(result.encrypted)
        self.assertEqual(result.word_count(), 0)

    def test_assorted_junk_never_raises(self):
        junk = [
            b"",
            b"%PDF-",
            b"%PDF-1.7\n",
            b"%PDF-1.4\n" + bytes(range(256)) * 8,
            b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog\n",  # half an object
            b"%PDF-1.4\ntrailer\n<< /Root 1 0 R >>\nstartxref\n999999\n%%EOF\n",
            b"stream\nendstream\n" * 20,
            b"%PDF-1.4\n" + b"x" * 50000,
        ]
        for data in junk:
            result = pdftext.extract(data)
            self.assertIsInstance(result, pdftext.PdfResult)
            self.assertIsInstance(result.note, str)

    def test_header_after_leading_junk_is_accepted(self):
        data = b"junk before the header\n" + _document(
            [b"BT /F1 12 Tf 72 700 Td (Found it) Tj ET"]
        )
        self.assertTrue(pdftext.is_pdf(data))
        self.assertIn("Found it", pdftext.extract(data).text())

    def test_truncated_file_keeps_the_text(self):
        full = _document(
            [b"BT /F1 24 Tf 72 700 Td (Survives truncation) Tj ET"]
        )
        cut = full.index(b"xref\n")  # the file map is simply gone
        trimmed = full[:cut]
        result = pdftext.extract(trimmed)
        self.assertIn("Survives truncation", result.text())
        self.assertTrue(result.note)

    def test_truncated_in_half_never_raises(self):
        full = _document([b"BT /F1 12 Tf 72 700 Td (Half) Tj ET"] * 2)
        half = full[: len(full) // 2]
        result = pdftext.extract(half)  # must not raise
        self.assertIsInstance(result, pdftext.PdfResult)
        self.assertIsInstance(result.text(), str)

    def test_encrypted_document_is_reported(self):
        data = _document(
            [b"BT /F1 12 Tf 72 700 Td (Secret) Tj ET"],
            extra_trailer=b" /Encrypt 9 0 R",
        )
        result = pdftext.extract(data)
        self.assertTrue(result.encrypted)
        self.assertTrue(result.is_empty())
        self.assertIn("password", result.note.lower())

    def test_scanned_document_is_reported_as_pictures(self):
        # A page that draws an image and no text at all.
        builder = _Builder()
        builder.add(b"<< /Type /Catalog /Pages 2 0 R >>")
        builder.add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        builder.add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>"
        )
        draw = b"q 612 0 0 792 0 0 cm /Im1 Do Q"
        builder.add(
            b"<< /Length %d >>\nstream\n" % len(draw) + draw + b"\nendstream"
        )
        image = bytes(range(256)) * 12
        builder.add(
            b"<< /Type /XObject /Subtype /Image /Width 32 /Height 32 "
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length %d >>\nstream\n"
            % len(image)
            + image
            + b"\nendstream"
        )
        result = pdftext.extract(builder.build())
        self.assertTrue(result.is_empty())
        self.assertIn("picture", result.note.lower())
        self.assertFalse(result.encrypted)

    def test_image_page_with_a_caption_still_reads_the_caption(self):
        builder = _Builder()
        builder.add(b"<< /Type /Catalog /Pages 2 0 R >>")
        builder.add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        builder.add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 6 0 R >> /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>"
        )
        draw = (
            b"q 612 0 0 400 0 300 cm /Im1 Do Q\n"
            b"BT /F1 12 Tf 72 200 Td (Figure 1: a scanned page) Tj ET"
        )
        builder.add(
            b"<< /Length %d >>\nstream\n" % len(draw) + draw + b"\nendstream"
        )
        image = bytes(range(256)) * 12
        builder.add(
            b"<< /Type /XObject /Subtype /Image /Width 32 /Height 32 "
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length %d >>\nstream\n"
            % len(image)
            + image
            + b"\nendstream"
        )
        builder.add(_CONTENT_FONT)
        result = pdftext.extract(builder.build())
        self.assertEqual(result.pages[0].text, "Figure 1: a scanned page")

    def test_missing_pages_are_reported(self):
        # The page tree claims three pages but only one is reachable.
        content = b"BT /F1 12 Tf 72 700 Td (Only one) Tj ET"
        builder = _Builder()
        builder.add(b"<< /Type /Catalog /Pages 2 0 R >>")
        builder.add(b"<< /Type /Pages /Kids [3 0 R] /Count 3 >>")
        builder.add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        )
        builder.add(
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        )
        builder.add(_CONTENT_FONT)
        result = pdftext.extract(builder.build())
        self.assertEqual(result.pages[0].text, "Only one")
        self.assertIn("missing", result.note.lower())

    def test_broken_xref_falls_back_to_a_whole_file_scan(self):
        # Valid content, valid header, and a completely wrong file map.
        good = _document([b"BT /F1 24 Tf 72 700 Td (Brute force found this) Tj ET"])
        broken = good.replace(b"startxref\n", b"startxref\n999", 1)
        result = pdftext.extract(broken)
        self.assertIn("Brute force found this", result.text())

    def test_stream_with_wrong_length_is_recovered(self):
        # /Length that lies is common in files written by broken tools.
        content = b"BT /F1 12 Tf 72 700 Td (Wrong length declared) Tj ET"
        builder = _Builder()
        builder.add(b"<< /Type /Catalog /Pages 2 0 R >>")
        builder.add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        builder.add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        )
        builder.add(
            b"<< /Length 12 >>\nstream\n" + content + b"\nendstream"
        )
        builder.add(_CONTENT_FONT)
        result = pdftext.extract(builder.build())
        self.assertEqual(result.pages[0].text, "Wrong length declared")

    def test_form_xobject_text_is_found(self):
        builder = _Builder()
        builder.add(b"<< /Type /Catalog /Pages 2 0 R >>")
        builder.add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        builder.add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 6 0 R >> /XObject << /Fx1 5 0 R >> >> /Contents 4 0 R >>"
        )
        builder.add(b"<< /Length 12 >>\nstream\n/Fx1 Do\nendstream")
        form = b"BT /F1 12 Tf 72 700 Td (Inside a form) Tj ET"
        builder.add(
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 612 792] /Length %d >>\nstream\n"
            % len(form)
            + form
            + b"\nendstream"
        )
        builder.add(_CONTENT_FONT)
        result = pdftext.extract(builder.build())
        self.assertIn("Inside a form", result.text())

    def test_incremental_update_uses_the_newest_objects(self):
        # A second revision appends a changed page and a second xref table.
        data = _document([b"BT /F1 12 Tf 72 700 Td (Original text) Tj ET"])
        first_eof = data.index(b"%%EOF") + 6
        updated = bytearray(data[:first_eof] + b"\n")
        new_page_at = len(updated)
        updated += (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>\nendobj\n"
        )
        new_content_at = len(updated)
        content = b"BT /F1 12 Tf 72 700 Td (Revised text) Tj ET"
        updated += (
            b"6 0 obj\n<< /Length %d >>\nstream\n" % len(content)
            + content
            + b"\nendstream\nendobj\n"
        )
        xref_at = len(updated)
        updated += b"xref\n0 1\n0000000000 65535 f \n3 1\n%010d 00000 n \n6 1\n%010d 00000 n \n" % (
            new_page_at,
            new_content_at,
        )
        updated += b"trailer\n<< /Size 7 /Root 1 0 R /Prev %d >>\n" % data.index(b"xref\n")
        updated += b"startxref\n%d\n%%%%EOF\n" % xref_at
        result = pdftext.extract(bytes(updated))
        self.assertIn("Revised text", result.text())
        self.assertNotIn("Original text", result.text())


# ---------------------------------------------------------------------------
# The public surface
# ---------------------------------------------------------------------------
class TestPublicSurface(unittest.TestCase):
    def test_is_pdf(self):
        self.assertTrue(pdftext.is_pdf(b"%PDF-1.4\n"))
        self.assertTrue(pdftext.is_pdf(b"\n\n%PDF-1.7\n"))
        self.assertTrue(pdftext.is_pdf(_document([b"BT ET"])))
        self.assertFalse(pdftext.is_pdf(b"not a pdf at all"))
        self.assertFalse(pdftext.is_pdf(b""))
        self.assertFalse(pdftext.is_pdf("a string, not bytes"))

    def test_result_shape(self):
        result = pdftext.extract(_document([b"BT /F1 12 Tf 72 700 Td (Shape) Tj ET"]))
        self.assertIsInstance(result.pages, list)
        self.assertIsInstance(result.pages[0], pdftext.Page)
        self.assertIsInstance(result.pages[0].text, str)
        self.assertIsInstance(result.pages[0].number, int)
        self.assertIsInstance(result.note, str)
        self.assertIsInstance(result.title, (str, type(None)))
        self.assertIsInstance(result.encrypted, bool)
        self.assertIsInstance(result.is_empty(), bool)
        self.assertIsInstance(result.word_count(), int)
        self.assertIsInstance(result.text(), str)

    def test_page_separator_is_present(self):
        result = pdftext.extract(
            _document(
                [
                    b"BT /F1 12 Tf 72 700 Td (One) Tj ET",
                    b"BT /F1 12 Tf 72 700 Td (Two) Tj ET",
                ]
            )
        )
        self.assertIn("\f", result.text())

    def test_no_runs_of_three_blank_lines(self):
        content = b"BT /F1 12 Tf 72 700 Td (A) Tj 0 -60 Td (B) Tj 0 -60 Td (C) Tj ET"
        result = pdftext.extract(_document([content]))
        self.assertNotIn("\n\n\n", result.pages[0].text)

    def test_bytes_like_inputs_are_accepted(self):
        data = _document([b"BT /F1 12 Tf 72 700 Td (Bytes) Tj ET"])
        self.assertIn("Bytes", pdftext.extract(bytearray(data)).text())
        self.assertIn("Bytes", pdftext.extract(memoryview(data)).text())


if __name__ == "__main__":
    unittest.main()
