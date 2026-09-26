"""Read the text out of a PDF using nothing but the Python standard library.

nanodoc is meant to be usable by people who cannot install anything: no pip,
no wheels, nothing beyond the Python that is already on the machine. That
rules out every existing PDF library, so this module implements the small
part of the format that matters for reading text:

    * the file map at the end of the document, both the classic `xref` table
      and the cross-reference streams used by PDF 1.5 and later;
    * objects kept inside compressed object streams (`/ObjStm`);
    * FlateDecode (with PNG and TIFF predictors), ASCIIHex, ASCII85 and LZW;
    * the text operators `Tj`, `TJ`, `'` and `"`, including kerning arrays,
      string escapes and hex strings;
    * enough font information to turn bytes back into letters, including
      `/ToUnicode` CMaps, so accented text arrives intact.

What this does not do
---------------------

    * No OCR and no image handling. A scanned page is a picture of text and
      is reported as such instead of guessed at.
    * No password cracking. An encrypted document is reported, not opened.
    * No exotic CID font CMaps beyond a best effort: a `/ToUnicode` map is
      used when the file has one, and without it two-byte fonts are left
      unread rather than filled with plausible nonsense.
    * No layout reconstruction. Text comes out in the order the file draws
      it, which is reading order for letters, contracts and manuals, but not
      for magazine columns or tables with side-by-side cells.

`extract()` never raises. Whatever could be recovered is returned together
with a one-sentence `note` written for someone who does not know what a PDF
is, and an empty note when the read was clean.
"""

from __future__ import annotations

import math
import re
import zlib

__all__ = ["Page", "PdfResult", "extract", "is_pdf"]


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------
class Page:
    """One page of text."""

    __slots__ = ("number", "text")

    def __init__(self, number: int, text: str) -> None:
        self.number = number
        self.text = text

    def word_count(self) -> int:
        """Number of words on this page."""
        return len(self.text.split())

    def __repr__(self) -> str:
        return "Page(number=%d, words=%d)" % (self.number, self.word_count())


class PdfResult:
    """Everything the reader could find in one file."""

    __slots__ = ("pages", "title", "encrypted", "note")

    def __init__(self, pages, title, encrypted: bool, note: str) -> None:
        self.pages = list(pages or [])
        self.title = title
        self.encrypted = bool(encrypted)
        self.note = note or ""

    def text(self) -> str:
        """All pages, separated by a form feed so page breaks stay visible."""
        return "\n\f\n".join(p.text for p in self.pages)

    def word_count(self) -> int:
        """Number of words in the whole document."""
        return sum(p.word_count() for p in self.pages)

    def is_empty(self) -> bool:
        """True when there is essentially nothing to read."""
        return len(self.text().strip()) < 4

    def __len__(self) -> int:
        return len(self.pages)

    def __repr__(self) -> str:
        return "PdfResult(pages=%d, words=%d, encrypted=%s, note=%r)" % (
            len(self.pages),
            self.word_count(),
            self.encrypted,
            self.note,
        )


# The note is the only thing a non-technical reader sees when something is
# wrong, so these are written in plain language and name the effect, never
# the mechanism.
_NOTE_NOT_PDF = (
    "This does not look like a PDF file, so there was nothing to read from it."
)
_NOTE_LOCKED = (
    "This document is locked with a password, so the text inside it could not "
    "be read."
)
_NOTE_DAMAGED = (
    "Part of this file could not be read, so some pages may be missing."
)
_NOTE_RECOVERED = (
    "The file's internal map was damaged, so the text was recovered by "
    "searching the whole file. Page numbers may not be exact."
)
_NOTE_PARTIAL_PAGES = (
    "Part of this file could not be read, so one or more pages may be missing."
)
_NOTE_FONT = (
    "Some of the text uses a font this reader could not decode, so parts of "
    "the document may be missing."
)
_NOTE_SCANNED = (
    "This looks like a scanned document, so there is no text inside it - only "
    "pictures of text."
)
_NOTE_UNREADABLE = (
    "This file could not be read. It may be damaged or not really a PDF."
)


# ---------------------------------------------------------------------------
# Limits
#
# Real documents never come close to these; a corrupted or hostile file can
# easily try to make us build a four-gigabyte string, so every unbounded loop
# gets a ceiling.
# ---------------------------------------------------------------------------
_MAX_DEPTH = 48
_MAX_DICT_ITEMS = 200000
_MAX_OBJECTS = 400000
_MAX_STREAMS = 40000
_MAX_DECOMPRESSED = 64 * 1024 * 1024
_MAX_TEXT = 8 * 1024 * 1024
_MAX_PIECES = 400000


# ---------------------------------------------------------------------------
# Lexing
#
# PDF syntax is small: whitespace, comments, delimited names and strings, and
# bare tokens for numbers and operators. Names come back as `str` (latin-1
# decoded), strings as `bytes`, dictionaries as `dict` keyed by name.
# ---------------------------------------------------------------------------
_WS = b"\x00\t\n\x0c\r "
_DELIM = b"()<>[]{}/%"

_WS_AT = re.compile(rb"[\x00\t\n\x0c\r ]+")
_NUM_AT = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")
_TOK_AT = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]+")
_COMMENT_AT = re.compile(rb"%[^\r\n]*")

class _Ref:
    """An indirect reference such as `12 0 R`."""

    __slots__ = ("num", "gen")

    def __init__(self, num: int, gen: int) -> None:
        self.num = num
        self.gen = gen

    def __repr__(self) -> str:
        return "%d %d R" % (self.num, self.gen)


class _Stream:
    """A stream object: a dictionary plus raw bytes, decoded on demand."""

    __slots__ = ("dict", "raw", "_doc", "_data", "_tried")

    def __init__(self, d, doc, raw: bytes) -> None:
        self.dict = d
        self.raw = raw
        self._doc = doc
        self._data = None
        self._tried = False

    def data(self):
        """Decoded stream bytes, or None when a filter cannot be undone."""
        if not self._tried:
            self._tried = True
            self._data = _decode_stream(self.dict, self.raw, self._doc)
        return self._data

    def __repr__(self) -> str:
        return "<stream %d bytes>" % len(self.raw)


def _skip_ws(buf: bytes, i: int) -> int:
    """Step over whitespace and comments."""
    n = len(buf)
    while i < n:
        c = buf[i]
        if c in _WS:
            i += 1
        elif c == 0x25:  # '%' starts a comment that runs to the end of the line
            m = _COMMENT_AT.match(buf, i)
            i = m.end() if m else i + 1
        else:
            break
    return i


def _read_name(buf: bytes, i: int) -> tuple:
    """Read `/Name`, decoding `#xx` escapes. Returns (str, new position)."""
    i += 1
    out = bytearray()
    n = len(buf)
    while i < n:
        c = buf[i]
        if c in _WS or c in _DELIM:
            break
        if c == 0x23 and i + 2 < n:  # '#' escape, e.g. `/A#20B` is `A B`
            try:
                out.append(int(buf[i + 1 : i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(c)
        i += 1
    return out.decode("latin-1"), i


def _read_literal_string(buf: bytes, i: int) -> tuple:
    """Read `(a string)` with all its escapes. Returns (bytes, new position)."""
    i += 1
    out = bytearray()
    depth = 1
    n = len(buf)
    while i < n:
        c = buf[i]
        if c == 0x5C:  # backslash
            i += 1
            if i >= n:
                break
            e = buf[i]
            if e == 0x6E:  # n
                out.append(0x0A)
                i += 1
            elif e == 0x72:  # r
                out.append(0x0D)
                i += 1
            elif e == 0x74:  # t
                out.append(0x09)
                i += 1
            elif e == 0x62:  # b
                out.append(0x08)
                i += 1
            elif e == 0x66:  # f
                out.append(0x0C)
                i += 1
            elif 0x30 <= e <= 0x37:  # 1-3 octal digits
                value = 0
                k = 0
                while k < 3 and i < n and 0x30 <= buf[i] <= 0x37:
                    value = value * 8 + (buf[i] - 0x30)
                    i += 1
                    k += 1
                out.append(value & 0xFF)
            elif e == 0x0D:  # line continuation
                i += 1
                if i < n and buf[i] == 0x0A:
                    i += 1
            elif e == 0x0A:
                i += 1
            else:  # an escaped delimiter or anything else stands for itself
                out.append(e)
                i += 1
        elif c == 0x28:  # '(' - parentheses nest inside strings
            depth += 1
            out.append(c)
            i += 1
        elif c == 0x29:  # ')'
            depth -= 1
            i += 1
            if depth == 0:
                break
            out.append(c)
        else:
            out.append(c)
            i += 1
    return bytes(out), i


def _read_hex_string(buf: bytes, i: int) -> tuple:
    """Read `<48656C6C6F>`. Returns (bytes, new position)."""
    i += 1
    digits = bytearray()
    n = len(buf)
    while i < n:
        c = buf[i]
        i += 1
        if c == 0x3E:  # '>'
            break
        if 0x30 <= c <= 0x39 or 0x41 <= c <= 0x46 or 0x61 <= c <= 0x66:
            digits.append(c)
    if len(digits) % 2:
        digits.append(0x30)  # an odd final digit is padded with zero
    try:
        return bytes.fromhex(digits.decode("ascii")), i
    except ValueError:
        return b"", i


def _parse_number(buf: bytes, i: int) -> tuple:
    """Read an integer or real, and the `R` lookahead that makes it a ref."""
    m = _NUM_AT.match(buf, i)
    if not m:
        return None, i
    tok = m.group(0)
    end = m.end()
    try:
        value = int(tok)
    except ValueError:
        try:
            return float(tok), end
        except ValueError:
            return None, i
    # `12 0 R` is a reference, not the two integers 12 and 0.
    j = _skip_ws(buf, end)
    k = j
    while k < len(buf) and 0x30 <= buf[k] <= 0x39:
        k += 1
    if k > j:
        gen = int(buf[j:k])
        m2 = _skip_ws(buf, k)
        if buf[m2 : m2 + 1] == b"R" and (
            m2 + 1 >= len(buf) or buf[m2 + 1] in _WS or buf[m2 + 1] in _DELIM
        ):
            return _Ref(value, gen), m2 + 1
    return value, end


def _parse_object(buf: bytes, i: int, depth: int = 0) -> tuple:
    """Parse one object. Returns (value, new position).

    On anything unparseable it returns (None, i) without moving, so callers
    can detect the lack of progress and stop instead of spinning.
    """
    if depth > _MAX_DEPTH:
        return None, i
    i = _skip_ws(buf, i)
    if i >= len(buf):
        return None, i
    c = buf[i]
    if c == 0x2F:  # '/'
        return _read_name(buf, i)
    if c == 0x28:  # '('
        return _read_literal_string(buf, i)
    if c == 0x3C:  # '<'
        if buf[i + 1 : i + 2] == b"<":
            return _parse_dict(buf, i, depth)
        return _read_hex_string(buf, i)
    if c == 0x5B:  # '['
        return _parse_array(buf, i, depth)
    if c == 0x2B or c == 0x2D or c == 0x2E or 0x30 <= c <= 0x39:
        return _parse_number(buf, i)
    m = _TOK_AT.match(buf, i)
    if not m:
        return None, i
    w = m.group(0)
    if w == b"true":
        return True, m.end()
    if w == b"false":
        return False, m.end()
    if w == b"null":
        return None, m.end()
    return w.decode("latin-1"), m.end()


def _parse_array(buf: bytes, i: int, depth: int = 0) -> tuple:
    """Parse `[ ... ]`. Returns (list, new position)."""
    out = []
    i += 1
    n = len(buf)
    while i < n:
        i = _skip_ws(buf, i)
        if i >= n:
            break
        if buf[i] == 0x5D:  # ']'
            return out, i + 1
        value, j = _parse_object(buf, i, depth + 1)
        if j <= i:
            i += 1  # unparseable byte: step over it and carry on
            continue
        out.append(value)
        i = j
    return out, i


def _parse_dict(buf: bytes, i: int, depth: int = 0) -> tuple:
    """Parse `<< ... >>`. Returns (dict, new position)."""
    out = {}
    i += 2
    n = len(buf)
    for _ in range(_MAX_DICT_ITEMS):
        i = _skip_ws(buf, i)
        if i >= n:
            break
        if buf[i : i + 2] == b">>":
            return out, i + 2
        if buf[i] != 0x2F:  # not a name where a key belongs: skip it
            _, j = _parse_object(buf, i, depth + 1)
            i = j if j > i else i + 1
            continue
        key, i = _read_name(buf, i)
        value, j = _parse_object(buf, i, depth + 1)
        out[key] = value
        if j <= i:
            i += 1
        else:
            i = j
    return out, i


# ---------------------------------------------------------------------------
# Stream filters
# ---------------------------------------------------------------------------
def _inflate(data: bytes) -> bytes:
    """Inflate as much of a Flate stream as possible, never raising.

    Files in the wild end in a truncated stream, or start with a byte or two
    of junk, or use raw deflate without the zlib header. Feeding the data in
    blocks means a corrupt tail still leaves the output produced before it.
    """
    if not data:
        return b""
    best = b""
    for wbits in (15, -15, 47):
        obj = None
        try:
            obj = zlib.decompressobj(wbits)
        except zlib.error:
            continue
        out = bytearray()
        for start in range(0, len(data), 65536):
            try:
                out += obj.decompress(data[start : start + 65536], 1048576)
            except zlib.error:
                break
            if len(out) >= _MAX_DECOMPRESSED:
                break
        else:
            try:
                out += obj.flush()
            except zlib.error:
                pass
        if len(out) > len(best):
            best = bytes(out)
        if best:
            break
    return best


def _apply_predictor(data: bytes, parms) -> bytes:
    """Undo the PNG/TIFF predictor that some streams are written with."""
    if not isinstance(parms, dict) or not data:
        return data
    try:
        predictor = int(parms.get("Predictor") or 1)
    except (TypeError, ValueError):
        return data
    if predictor <= 1:
        return data
    try:
        colors = int(parms.get("Colors") or 1)
        bits = int(parms.get("BitsPerComponent") or 8)
        columns = int(parms.get("Columns") or 1)
    except (TypeError, ValueError):
        return data
    if colors < 1 or columns < 1 or bits < 1:
        return data
    row_len = (columns * colors * bits + 7) // 8
    if row_len <= 0:
        return data
    bpp = max(1, (colors * bits + 7) // 8)

    if predictor == 2:  # TIFF: each row is the difference from the last
        if bits != 8:
            return data
        out = bytearray(data)
        rows = len(out) // row_len
        for r in range(1, rows):
            base = r * row_len
            for k in range(bpp, row_len):
                out[base + k] = (out[base + k] + out[base + k - bpp]) & 0xFF
        return bytes(out)

    # PNG predictors. Some writers forget the per-row filter byte; if the
    # length divides evenly without it, assume it is missing.
    if len(data) % (row_len + 1) != 0 and len(data) % row_len == 0:
        return data
    out = bytearray()
    prev = bytearray(row_len)
    pos = 0
    total = len(data)
    while pos + 1 <= total:
        ft = data[pos]
        pos += 1
        row = bytearray(data[pos : pos + row_len])
        pos += row_len
        if len(row) < row_len:
            break
        if ft == 1:
            for k in range(bpp, row_len):
                row[k] = (row[k] + row[k - bpp]) & 0xFF
        elif ft == 2:
            for k in range(row_len):
                row[k] = (row[k] + prev[k]) & 0xFF
        elif ft == 3:
            for k in range(row_len):
                left = row[k - bpp] if k >= bpp else 0
                row[k] = (row[k] + ((left + prev[k]) >> 1)) & 0xFF
        elif ft == 4:
            for k in range(row_len):
                a = row[k - bpp] if k >= bpp else 0
                b = prev[k]
                c = prev[k - bpp] if k >= bpp else 0
                p = a + b - c
                pa = abs(p - a)
                pb = abs(p - b)
                pc = abs(p - c)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                row[k] = (row[k] + pr) & 0xFF
        # ft == 0 means the row is already plain
        out += row
        prev = row
        if len(out) >= _MAX_DECOMPRESSED:
            break
    return bytes(out)


def _ascii_hex_decode(data: bytes) -> bytes:
    end = data.find(b">")
    if end >= 0:
        data = data[:end]
    digits = re.sub(rb"[^0-9A-Fa-f]", b"", data)
    if len(digits) % 2:
        digits += b"0"
    try:
        return bytes.fromhex(digits.decode("ascii"))
    except ValueError:
        return b""


def _ascii85_decode(data: bytes) -> bytes:
    start = data.find(b"<~")
    if start >= 0:
        data = data[start + 2 :]
    end = data.find(b"~>")
    if end >= 0:
        data = data[:end]
    data = re.sub(rb"\s", b"", data)
    out = bytearray()
    group = []
    for byte in data:
        if byte == 0x7A and not group:  # 'z' is four zero bytes
            out += b"\x00\x00\x00\x00"
            continue
        if byte < 0x21 or byte > 0x75:
            continue
        group.append(byte - 33)
        if len(group) == 5:
            value = 0
            for g in group:
                value = value * 85 + g
            out += value.to_bytes(4, "big")
            group = []
    if group:  # a short final group pads with the highest digit
        n = len(group)
        for _ in range(5 - n):
            group.append(84)
        value = 0
        for g in group:
            value = value * 85 + g
        out += value.to_bytes(4, "big")[: n - 1]
    return bytes(out)


def _lzw_decode(data: bytes, early: int = 1) -> bytes:
    """PDF's LZW variant: 9-12 bit codes, clear 256, end-of-data 257."""
    out = bytearray()
    table = None
    codelen = 9
    bitpos = 0
    total_bits = len(data) * 8
    prev = None
    while True:
        if bitpos + codelen > total_bits:
            break
        if table is None:
            table = [bytes([i]) for i in range(256)] + [b"", b""]
        byte_index = bitpos >> 3
        shift = bitpos & 7
        need = (shift + codelen + 7) >> 3
        chunk = data[byte_index : byte_index + need]
        if len(chunk) < need:
            break
        value = 0
        for byte in chunk:
            value = (value << 8) | byte
        value >>= len(chunk) * 8 - shift - codelen
        value &= (1 << codelen) - 1
        bitpos += codelen
        if value == 256:
            table = [bytes([i]) for i in range(256)] + [b"", b""]
            codelen = 9
            prev = None
            continue
        if value == 257:
            break
        if value < len(table):
            entry = table[value]
        elif value == len(table) and prev is not None:
            entry = prev + prev[:1]
        else:
            break
        out += entry
        if prev is not None and len(table) < 4096:
            table.append(prev + entry[:1])
            if len(table) + early >= (1 << codelen) and codelen < 12:
                codelen += 1
        prev = entry
        if len(out) >= _MAX_DECOMPRESSED:
            break
    return bytes(out)


def _decode_stream(stream_dict, raw: bytes, doc):
    """Apply a stream's filters. Returns None when one cannot be undone."""
    filters = stream_dict.get("Filter")
    if filters is None:
        return raw
    if isinstance(filters, str):
        filters = [filters]
    if not isinstance(filters, list):
        return raw
    parms = stream_dict.get("DecodeParms")
    if parms is None:
        parms = stream_dict.get("DP")
    if isinstance(parms, dict):
        parms = [parms]
    if not isinstance(parms, list):
        parms = []
    data = raw
    for index, name in enumerate(filters):
        if doc is not None:
            name = doc.resolve(name)
        if not isinstance(name, str):
            return None
        parm = parms[index] if index < len(parms) else None
        if doc is not None:
            parm = doc.resolve(parm)
        if name in ("FlateDecode", "Fl"):
            data = _apply_predictor(_inflate(data), parm)
        elif name in ("ASCIIHexDecode", "AHx"):
            data = _ascii_hex_decode(data)
        elif name in ("ASCII85Decode", "A85"):
            data = _ascii85_decode(data)
        elif name in ("LZWDecode", "LZW"):
            early = 1
            if isinstance(parm, dict):
                try:
                    early = int(parm.get("EarlyChange", 1))
                except (TypeError, ValueError):
                    early = 1
            data = _apply_predictor(_lzw_decode(data, early), parm)
        elif name in ("Crypt", "Identity"):
            continue
        else:
            return None  # an image codec, or something we do not know
    return data


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
def _hex_to_text(digits: bytes) -> str:
    """Turn the hex payload of a CMap entry into text."""
    if not digits:
        return ""
    if len(digits) % 2:
        digits = digits + b"0"
    try:
        raw = bytes.fromhex(digits.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if len(raw) >= 2 and len(raw) % 2 == 0:
        try:
            return raw.decode("utf-16-be")
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1", "replace")


def _bump_hex_text(digits: bytes, delta: int) -> str:
    """`bfrange` destinations count up from the low code, one code unit each."""
    if not digits:
        return ""
    if len(digits) % 2:
        digits = digits + b"0"
    try:
        raw = bytearray(bytes.fromhex(digits.decode("ascii")))
    except (ValueError, UnicodeDecodeError):
        return ""
    if not raw:
        return ""
    if len(raw) >= 2:
        unit = (int.from_bytes(raw[-2:], "big") + delta) & 0xFFFF
        raw[-2:] = unit.to_bytes(2, "big")
    else:
        raw[0] = (raw[0] + delta) & 0xFF
    return _hex_to_text(raw.hex().encode("ascii"))


def _parse_tounicode(data: bytes) -> tuple:
    """Read a `/ToUnicode` CMap. Returns (mapping, source code length).

    Only `bfchar` and `bfrange` are handled, which is what real writers emit.
    """
    mapping = {}
    lengths = {}
    for block in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for entry in re.finditer(
            rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block.group(1)
        ):
            src = entry.group(1)
            mapping[int(src, 16)] = _hex_to_text(entry.group(2))
            n = len(src) // 2
            lengths[n] = lengths.get(n, 0) + 1
    for block in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.S):
        pattern = re.compile(
            rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])"
        )
        for lo_h, hi_h, target in pattern.findall(block.group(1)):
            lo = int(lo_h, 16)
            hi = int(hi_h, 16)
            n = len(lo_h) // 2
            lengths[n] = lengths.get(n, 0) + 1
            if hi < lo or hi - lo > 65535:
                continue
            if target.startswith(b"["):
                items = re.findall(rb"<([0-9A-Fa-f]*)>", target)
                for k, item in enumerate(items):
                    if lo + k > hi:
                        break
                    mapping[lo + k] = _hex_to_text(item)
            else:
                base = target[1:-1]
                for code in range(lo, hi + 1):
                    mapping[code] = _bump_hex_text(base, code - lo)
    if lengths:
        code_len = max(lengths.items(), key=lambda kv: (kv[1], -kv[0]))[0]
    else:
        code_len = 1
    return mapping, max(1, code_len)


class _Font:
    """Enough of a font to turn content-stream bytes into characters."""

    __slots__ = (
        "map",
        "clen",
        "readable",
        "codec",
        "_single",
        "_two_byte",
        "widths",
        "default_width",
        "has_widths",
    )

    def __init__(self, d, doc) -> None:
        self.map = {}
        self.clen = 1
        self.readable = True
        self._two_byte = False
        self.codec = "latin-1"
        self.widths = {}
        self.default_width = None
        self.has_widths = False
        if not isinstance(d, dict):
            self._single = bytes(range(256)).decode("latin-1", "replace")
            return
        subtype = doc.resolve(d.get("Subtype")) if doc else d.get("Subtype")
        encoding = doc.resolve(d.get("Encoding")) if doc else d.get("Encoding")
        if subtype == "Type0":
            self._two_byte = True
            self.clen = 2
        base = ""
        if isinstance(encoding, dict):
            base = encoding.get("BaseEncoding") or ""
        elif isinstance(encoding, str):
            base = encoding
        if base == "WinAnsiEncoding":
            self.codec = "cp1252"
        elif base == "MacRomanEncoding":
            self.codec = "mac_roman"
        elif base in ("Identity-H", "Identity-V"):
            self._two_byte = True
            self.clen = 2
        # A ToUnicode map is the only reliable way to get accents, ligatures
        # and symbols right, so it wins over any built-in encoding.
        to_unicode = doc.resolve(d.get("ToUnicode")) if doc else d.get("ToUnicode")
        if isinstance(to_unicode, _Stream):
            data = to_unicode.data()
            if data:
                mapping, clen = _parse_tounicode(data)
                if mapping:
                    self.map = mapping
                    if self._two_byte:
                        self.clen = max(2, clen)
        try:
            self._single = bytes(range(256)).decode(self.codec, "replace")
        except LookupError:
            self.codec = "latin-1"
            self._single = bytes(range(256)).decode("latin-1", "replace")
        if self._two_byte:
            self._read_cid_widths(d, doc)
        else:
            self._read_simple_widths(d, doc)
        # A two-byte font with no map would decode to invented characters,
        # which is worse than admitting we cannot read it.
        self.readable = bool(self.map) or not self._two_byte

    def _read_simple_widths(self, d, doc) -> None:
        """A simple font carries `/FirstChar` and `/Widths`, one per code."""
        widths = doc.resolve(d.get("Widths")) if doc else d.get("Widths")
        first = doc.resolve(d.get("FirstChar")) if doc else d.get("FirstChar")
        if not isinstance(widths, list) or not isinstance(first, int):
            return
        for index, width in enumerate(widths[:65536]):
            if isinstance(width, (int, float)) and not isinstance(width, bool):
                self.widths[first + index] = float(width)
        descriptor = doc.resolve(d.get("FontDescriptor")) if doc else None
        if isinstance(descriptor, dict):
            missing = doc.resolve(descriptor.get("MissingWidth"))
            if isinstance(missing, (int, float)) and not isinstance(missing, bool):
                self.default_width = float(missing)
        self.has_widths = bool(self.widths)

    def _read_cid_widths(self, d, doc) -> None:
        """A CID font carries `/DW` and a `/W` array keyed by CID.

        With an Identity encoding the CID is the code we read from the stream,
        which is the case for every CID font we meet in practice.
        """
        descendants = doc.resolve(d.get("DescendantFonts")) if doc else None
        if isinstance(descendants, list) and descendants:
            descendant = doc.resolve(descendants[0])
        else:
            descendant = d
        if not isinstance(descendant, dict):
            return
        default = doc.resolve(descendant.get("DW"))
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            self.default_width = float(default)
        table = doc.resolve(descendant.get("W"))
        if not isinstance(table, list):
            return
        index = 0
        entries = 0
        while index < len(table) - 1 and entries < 200000:
            first = doc.resolve(table[index])
            nxt = doc.resolve(table[index + 1])
            if not isinstance(first, (int, float)) or isinstance(first, bool):
                break
            if isinstance(nxt, list):
                # `c [w1 w2 ...]`: widths for consecutive CIDs from `c`.
                for offset, width in enumerate(nxt):
                    if isinstance(width, (int, float)) and not isinstance(width, bool):
                        self.widths[int(first) + offset] = float(width)
                        entries += 1
                index += 2
            elif index + 2 < len(table):
                # `first last w`: one width for a run of CIDs.
                last = int(nxt) if isinstance(nxt, (int, float)) else int(first)
                width = doc.resolve(table[index + 2])
                if not isinstance(width, (int, float)) or isinstance(width, bool):
                    break
                if 0 <= last - first <= 65535:
                    for code in range(int(first), last + 1):
                        self.widths[code] = float(width)
                        entries += 1
                index += 3
            else:
                break
        self.has_widths = bool(self.widths)

    def advance(self, data: bytes):
        """Width of a string in ems, or None when the font has no metrics.

        The width table is what keeps a word from being torn in half: without
        it we would have to guess how wide each letter is, and a guess that is
        too small for a capital M turns it into a separate word.
        """
        if not self.has_widths or not data:
            return None
        default = self.default_width if self.default_width is not None else 500.0
        if self.clen > 1:
            total = 0.0
            step = self.clen
            for k in range(0, len(data) - step + 1, step):
                code = int.from_bytes(data[k : k + step], "big")
                total += self.widths.get(code, default)
            return total / 1000.0
        total = 0.0
        for byte in data:
            total += self.widths.get(byte, default)
        return total / 1000.0

    def decode(self, data: bytes) -> str:
        if not data:
            return ""
        if self.clen > 1:
            out = []
            step = self.clen
            for k in range(0, len(data) - step + 1, step):
                code = int.from_bytes(data[k : k + step], "big")
                ch = self.map.get(code)
                if ch is not None:
                    out.append(ch)
            return "".join(out)
        if self.map:
            return "".join(self.map.get(b, self._single[b]) for b in data)
        return "".join(self._single[b] for b in data)


def _decode_pdf_text(value) -> str:
    """Decode a text string from the document information dictionary."""
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes):
        return ""
    if value[:2] == b"\xfe\xff":
        return value[2:].decode("utf-16-be", "replace")
    if value[:2] == b"\xff\xfe":
        return value[2:].decode("utf-16-le", "replace")
    if value[:3] == b"\xef\xbb\xbf":
        return value[3:].decode("utf-8", "replace")
    try:
        return value.decode("cp1252")
    except UnicodeDecodeError:
        return value.decode("latin-1", "replace")


# ---------------------------------------------------------------------------
# Content streams
#
# A page is drawn by a small stack language. We only need the text operators,
# but we must still track the graphics state (q/Q/cm) and the text matrices
# so that we can tell a new line from a new word.
# ---------------------------------------------------------------------------
_IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mul(m, n) -> tuple:
    """Matrix product, PDF convention (row vectors, m applied first)."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _device(x: float, y: float, m) -> tuple:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _advance(tm, dx: float) -> tuple:
    return (tm[0], tm[1], tm[2], tm[3], tm[4] + dx, tm[5])


class _Stop(Exception):
    """Raised internally when a stream is pathologically large."""


class _Piece:
    """One run of text as it was drawn, in device coordinates.

    `size` is the em size in those same device units and `width` the measured
    width when the font had metrics, so the spacing rules in `_assemble` can
    compare like with like.
    """

    __slots__ = ("text", "x", "y", "size", "brk", "width")

    def __init__(
        self,
        text: str,
        x: float,
        y: float,
        size: float,
        brk: bool = False,
        width=None,
    ):
        self.text = text
        self.x = x
        self.y = y
        self.size = size if size and size > 0 else 1.0
        self.brk = brk
        self.width = width

    def end(self) -> float:
        """Where this run finishes, as far as we can tell."""
        if self.width is not None:
            return self.x + self.width
        return self.x + len(self.text) * _GUESSED_GLYPH_EM * self.size


def _scales(tm, ctm) -> tuple:
    """Device units per text-space unit, east and north of the text origin."""
    x0, y0 = _device(tm[4], tm[5], ctm)
    x1, y1 = _device(tm[4] + 1.0, tm[5], ctm)
    x2, y2 = _device(tm[4], tm[5] + 1.0, ctm)
    sx = math.hypot(x1 - x0, y1 - y0)
    sy = math.hypot(x2 - x0, y2 - y0)
    return (sx if sx > 1e-9 else 1.0, sy if sy > 1e-9 else 1.0)


def _scan_content(data: bytes, fonts, doc, resources, stats, depth: int = 0, ctm0=None):
    """Walk a content stream and return the text pieces it draws."""
    pieces = []
    stack = []
    ctm = ctm0 if ctm0 is not None else _IDENT
    gstack = []
    tm = tlm = _IDENT
    leading = 0.0
    size = 0.0
    font = None
    i = 0
    n = len(data)

    def position():
        return _device(tm[4], tm[5], ctm)

    def show(raw: bytes, brk: bool = False, advance: bool = True) -> None:
        nonlocal tm, font
        if not isinstance(raw, bytes):
            return
        if not raw:
            return
        if font is None:
            text = raw.decode("latin-1", "replace")
        elif not font.readable:
            if stats is not None:
                stats["font"] = True
            return
        else:
            text = font.decode(raw)
        if not text:
            return
        x, y = position()
        scale_x, scale_y = _scales(tm, ctm)
        em = (size or 1.0) * scale_y
        width = None
        measured = font.advance(raw) if font is not None else None
        if measured is not None:
            width = measured * (size or 1.0) * scale_x
        parts = text.split("\n")
        for index, part in enumerate(parts):
            piece_break = brk or index > 0
            if part:
                pieces.append(_Piece(part, x, y, em, piece_break, width))
            elif piece_break and index > 0:
                pieces.append(_Piece("", x, y, em, True))
        if advance:
            # Real metrics when the font has them; otherwise the best we can
            # do is an average letter width, which is only good enough to
            # spot large gaps.
            if measured is not None:
                tm = _advance(tm, measured * (size or 1.0))
            else:
                tm = _advance(tm, len(text) * _GUESSED_GLYPH_EM * (size or 1.0))
        if len(pieces) > _MAX_PIECES:
            raise _Stop()

    while i < n:
        c = data[i]
        if c in _WS:
            m = _WS_AT.match(data, i)
            i = m.end() if m else i + 1
            continue
        if c == 0x25:  # comment
            m = _COMMENT_AT.match(data, i)
            i = m.end() if m else i + 1
            continue
        if c == 0x2F:  # '/Name'
            name, i = _read_name(data, i)
            stack.append(name)
            continue
        if c == 0x28:  # '(string)'
            raw, i = _read_literal_string(data, i)
            stack.append(raw)
            continue
        if c == 0x3C:  # '<hex>' or '<<dict>>'
            if data[i + 1 : i + 2] == b"<":
                value, i = _parse_dict(data, i)
                stack.append(value)
            else:
                raw, i = _read_hex_string(data, i)
                stack.append(raw)
            continue
        if c == 0x5B:  # '['
            value, i = _parse_array(data, i)
            stack.append(value)
            continue
        if c == 0x5D or c == 0x3E:  # ']' or '>': stray, ignore
            i += 1
            continue
        if c == 0x2B or c == 0x2D or c == 0x2E or 0x30 <= c <= 0x39:
            value, j = _parse_number(data, i)
            if j <= i:
                i += 1
                continue
            stack.append(value)
            i = j
            continue
        m = _TOK_AT.match(data, i)
        if not m:
            i += 1
            continue
        op = m.group(0)
        i = m.end()

        # --- text state and positioning
        if op == b"BT":
            tm = tlm = _IDENT
        elif op == b"Tf":
            if len(stack) >= 2:
                name = stack[-2]
                font = fonts.get(name) if isinstance(name, str) else None
                try:
                    size = abs(float(stack[-1]))
                except (TypeError, ValueError):
                    size = 0.0
        elif op == b"Td" or op == b"TD":
            if len(stack) >= 2:
                try:
                    tx = float(stack[-2])
                    ty = float(stack[-1])
                except (TypeError, ValueError):
                    tx = ty = 0.0
                tlm = _mul((1, 0, 0, 1, tx, ty), tlm)
                tm = tlm
                if op == b"TD":
                    leading = -ty
        elif op == b"Tm":
            if len(stack) >= 6:
                try:
                    tlm = tm = tuple(float(v) for v in stack[-6:])
                except (TypeError, ValueError):
                    pass
        elif op == b"T*":
            tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
            tm = tlm
        elif op == b"TL":
            if stack:
                try:
                    leading = float(stack[-1])
                except (TypeError, ValueError):
                    pass

        # --- showing text
        elif op == b"Tj":
            if stack:
                show(stack[-1])
        elif op == b"'":
            tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
            tm = tlm
            if stack:
                show(stack[-1], brk=True)
        elif op == b'"':
            if len(stack) >= 3:
                try:
                    leading = float(stack[-2])
                except (TypeError, ValueError):
                    pass
            tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
            tm = tlm
            if stack:
                show(stack[-1], brk=True)
        elif op == b"TJ":
            if stack and isinstance(stack[-1], list):
                run = []
                total = 0.0
                for item in stack[-1]:
                    if isinstance(item, bool):
                        continue
                    if isinstance(item, (int, float)):
                        # A big negative kern is a word gap; small kerns are
                        # just letter spacing and must not become spaces.
                        if item < -100:
                            run.append(" ")
                        total += -float(item) / 1000.0 * (size or 1.0)
                    elif isinstance(item, bytes):
                        if font is None:
                            run.append(item.decode("latin-1", "replace"))
                            total += len(item) * _GUESSED_GLYPH_EM * (size or 1.0)
                        elif font.readable:
                            run.append(font.decode(item))
                            measured = font.advance(item)
                            if measured is None:
                                total += len(item) * _GUESSED_GLYPH_EM * (size or 1.0)
                            else:
                                total += measured * (size or 1.0)
                        elif stats is not None:
                            stats["font"] = True
                text = "".join(run)
                if text:
                    x, y = position()
                    scale_x, scale_y = _scales(tm, ctm)
                    pieces.append(
                        _Piece(
                            text,
                            x,
                            y,
                            (size or 1.0) * scale_y,
                            False,
                            total * scale_x,
                        )
                    )
                    tm = _advance(tm, total)

        # --- graphics state, so the CTM stays meaningful
        elif op == b"q":
            gstack.append(ctm)
        elif op == b"Q":
            if gstack:
                ctm = gstack.pop()
        elif op == b"cm":
            if len(stack) >= 6:
                try:
                    ctm = _mul(tuple(float(v) for v in stack[-6:]), ctm)
                except (TypeError, ValueError):
                    pass

        # --- form XObjects can hold the real page text
        elif op == b"Do":
            if stack and isinstance(stack[-1], str) and depth < 3 and doc is not None:
                xobjects = _lookup(doc, resources, "XObject")
                target = doc.resolve(_dict_get(xobjects, stack[-1]))
                if isinstance(target, _Stream):
                    if target.dict.get("Subtype") == "Form":
                        body = target.data()
                        if body:
                            form_res = doc.resolve(target.dict.get("Resources"))
                            sub_fonts = fonts
                            if isinstance(form_res, dict):
                                sub_fonts = _page_fonts(doc, form_res, None, stats)
                            matrix = (1, 0, 0, 1, 0, 0)
                            raw_matrix = doc.resolve(target.dict.get("Matrix"))
                            if isinstance(raw_matrix, list) and len(raw_matrix) == 6:
                                try:
                                    matrix = tuple(float(v) for v in raw_matrix)
                                except (TypeError, ValueError):
                                    pass
                            pieces.extend(
                                _scan_content(
                                    body,
                                    sub_fonts,
                                    doc,
                                    form_res if isinstance(form_res, dict) else resources,
                                    stats,
                                    depth + 1,
                                    _mul(matrix, ctm),
                                )
                            )
        if len(stack) > 64:
            stack = stack[-64:]
        stack = []
    return pieces


def _dict_get(d, key):
    if isinstance(d, dict):
        return d.get(key)
    return None


def _lookup(doc, resources, key):
    if not isinstance(resources, dict):
        return None
    value = resources.get(key)
    if doc is not None:
        value = doc.resolve(value)
    return value


def _page_fonts(doc, resources, cache, stats) -> dict:
    """Build {resource name: _Font} for a page or form."""
    fonts = {}
    font_dict = _lookup(doc, resources, "Font")
    if not isinstance(font_dict, dict):
        return fonts
    for name, value in font_dict.items():
        if not isinstance(name, str):
            continue
        if cache is not None and isinstance(value, _Ref):
            hit = cache.get(value.num)
            if hit is not None:
                fonts[name] = hit
                continue
            entry = _Font(doc.resolve(value), doc)
            cache[value.num] = entry
            fonts[name] = entry
        else:
            fonts[name] = _Font(doc.resolve(value), doc)
    return fonts


# ---------------------------------------------------------------------------
# Turning pieces back into readable text
# ---------------------------------------------------------------------------
_SPACE_RUN = re.compile(r"[ \t]+")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Fallback average glyph width in ems, used only for fonts that ship no width
# table at all (the standard fourteen, mostly). Real metrics are far better:
# this guess is too small for a capital M, which is exactly how a word gets
# torn into "M ukesh".
_GUESSED_GLYPH_EM = 0.45

# A gap this many ems wide between two runs on one line reads as a word space.
# With measured widths the only gaps left are real ones, so the bar can sit
# just below the narrowest real space; with the guessed width it has to be
# looser or every wide letter sprouts a space.
_GAP_EM_MEASURED = 0.15
_GAP_EM_GUESSED = 0.18


def _squeeze(line: str) -> str:
    return _SPACE_RUN.sub(" ", line).strip()


def _median(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _assemble(pieces) -> str:
    """Group drawn runs into lines, then lines into paragraphs."""
    lines = []  # [y, size, [(x, text, size, width), ...], forced break]
    for piece in pieces:
        if not piece.text:
            if piece.brk and lines:
                lines.append([piece.y, piece.size, [], True])
            continue
        if lines and not piece.brk and not lines[-1][3]:
            current = lines[-1]
            tolerance = 0.4 * max(piece.size, current[1])
            if abs(piece.y - current[0]) <= tolerance:
                current[2].append((piece.x, piece.text, piece.size, piece.width))
                continue
        lines.append(
            [piece.y, piece.size, [(piece.x, piece.text, piece.size, piece.width)], False]
        )

    # Join the runs on one line, inserting a space only when the gap between
    # them is too wide to be the middle of a word. The width of the previous
    # run comes from the font's own metrics whenever it has any, so a letter
    # that happens to be wide does not push the next one away.
    rows = []
    for y, size, parts, _break in lines:
        buf = ""
        prev_end = None
        prev_measured = False
        for x, text, psize, width in parts:
            if not text:
                continue
            if buf and prev_end is not None:
                gap = x - prev_end
                limit = (_GAP_EM_MEASURED if prev_measured else _GAP_EM_GUESSED) * psize
                if not buf.endswith(" ") and not text.startswith(" ") and gap > limit:
                    buf += " "
            buf += text
            if width is not None:
                prev_end = x + width
                prev_measured = True
            else:
                prev_end = x + len(text) * _GUESSED_GLYPH_EM * psize
                prev_measured = False
        text = _squeeze(buf)
        if text:
            rows.append((y, text))

    if not rows:
        return ""
    gaps = []
    for index in range(1, len(rows)):
        gap = abs(rows[index - 1][0] - rows[index][0])
        if gap > 1e-6:
            gaps.append(gap)
    # The normal line spacing is the smallest gap that is not a fluke: a
    # superscript or a decorative mark can produce a tiny one. Anything
    # noticeably larger than the usual spacing is taken as a paragraph break.
    leading = 0.0
    if gaps:
        middle = _median(gaps)
        ordinary = [g for g in gaps if g >= 0.5 * middle] if middle else []
        leading = min(ordinary) if ordinary else middle
    threshold = leading * 1.6 if leading else None

    out = []
    previous_y = None
    for y, text in rows:
        if previous_y is not None:
            gap = abs(previous_y - y)
            if threshold and gap > threshold and gap > 1e-6:
                out.append("")
        out.append(text)
        previous_y = y
    return "\n".join(out)


def _tidy(text: str) -> str:
    """Final human-facing clean-up: no control characters, no blank runs."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL.sub("", text)
    out = []
    blanks = 0
    for line in text.split("\n"):
        line = _squeeze(line)
        if line:
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks <= 1 and out:
                out.append("")
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# The document: file map, objects, page tree
# ---------------------------------------------------------------------------
class _Document:
    """A parsed PDF, read as lazily as possible."""

    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.xref = {}
        self.trailer = {}
        self.encrypted = False
        self.recovered = False
        self.failed = False
        self.page_count_hint = None
        self.missing_pages = False
        self.undecodable_text = False
        self._cache = {}
        self._objstm = {}
        self._fonts = {}

    # --- object access
    def resolve(self, value, depth: int = 0):
        while isinstance(value, _Ref) and depth < 32:
            value = self.get(value.num)
            depth += 1
        return value

    def get(self, num: int):
        if num in self._cache:
            return self._cache[num]
        self._cache[num] = None  # also a cycle guard
        value = None
        entry = self.xref.get(num)
        if entry is not None:
            kind = entry[0]
            try:
                if kind == "n":
                    value = self._object_at(entry[1])
                elif kind == "c":
                    value = self._object_in_stream(entry[1], entry[2])
            except Exception:
                value = None
        self._cache[num] = value
        return value

    def _object_at(self, offset: int):
        """Parse `num gen obj ... endobj` at a file offset."""
        buf = self.buf
        if offset < 0 or offset >= len(buf):
            return None
        j = _skip_ws(buf, offset)
        m = re.match(rb"(\d{1,10})\s+(\d{1,5})\s+obj\b", buf[j : j + 64])
        if not m:
            return None
        i = j + m.end()
        value, i = _parse_object(buf, i)
        if not isinstance(value, dict):
            return value
        j = _skip_ws(buf, i)
        if buf[j : j + 6] != b"stream":
            return value
        k = j + 6
        if buf[k : k + 2] == b"\r\n":
            k += 2
        elif buf[k : k + 1] in (b"\r", b"\n"):
            k += 1
        length = self.resolve(value.get("Length"))
        raw = None
        if isinstance(length, int) and 0 <= length and k + length <= len(buf):
            candidate = buf[k : k + length]
            # Only trust /Length when `endstream` really is next; broken
            # writers get it wrong and lose the rest of the page.
            after = buf[k + length : k + length + 24].lstrip(b"\r\n")
            if after.startswith(b"endstream"):
                raw = candidate
        if raw is None:
            end = buf.find(b"endstream", k)
            if end < 0:
                end = len(buf)
            raw = buf[k:end]
            if raw.endswith(b"\r\n"):
                raw = raw[:-2]
            elif raw.endswith(b"\n") or raw.endswith(b"\r"):
                raw = raw[:-1]
        return _Stream(value, self, raw)

    def _object_in_stream(self, container: int, index: int):
        """Fetch the object at position `index` inside object stream `container`.

        The stream starts with a table of `object number, offset` pairs, where
        each offset is counted from `/First`, and the xref stream refers to an
        object by its *position* in that table, not by its number.
        """
        table = self._objstm.get(container)
        if table is None:
            table = []
            holder = self.get(container)
            if isinstance(holder, _Stream):
                body = holder.data()
                count = self.resolve(holder.dict.get("N"))
                first = self.resolve(holder.dict.get("First"))
                if body and isinstance(count, int) and isinstance(first, int):
                    if 0 <= first <= len(body):
                        offsets = []
                        pos = 0
                        for _ in range(min(count, _MAX_OBJECTS)):
                            # The pairs are whitespace-separated ("1 0 2 34 …"),
                            # so skip runs of space before each number.
                            pos = _skip_ws(body, pos)
                            m = _NUM_RE.match(body, pos)
                            if not m:
                                break
                            pos = _skip_ws(body, m.end())
                            m2 = _NUM_RE.match(body, pos)
                            if not m2:
                                break
                            pos = m2.end()
                            offsets.append(int(m2.group(0)))
                        for offset in offsets:
                            table.append(self._parse_inner(body, first + offset))
            self._objstm[container] = table
        if 0 <= index < len(table):
            return table[index]
        return None

    def _parse_inner(self, body: bytes, offset: int):
        if offset < 0 or offset > len(body):
            return None
        value, _ = _parse_object(body, offset)
        return value

    # --- the file map
    def load(self) -> None:
        start = _find_startxref(self.buf)
        if start is not None:
            self._read_xref_chain(start)
        if not self.xref:
            self._reconstruct()
        if "Encrypt" in self.trailer:
            self.encrypted = True

    def _merge_trailer(self, d) -> None:
        if not isinstance(d, dict):
            return
        for key, value in d.items():
            if key not in self.trailer:
                self.trailer[key] = value

    def _read_xref_chain(self, offset) -> None:
        seen = set()
        for _ in range(64):
            if not isinstance(offset, int) or offset in seen:
                break
            seen.add(offset)
            if offset < 0 or offset >= len(self.buf):
                break
            if self.buf[offset : offset + 4] == b"xref":
                entries, trailer = _read_classic_xref(self.buf, offset)
                for num, entry in entries.items():
                    self.xref.setdefault(num, entry)
                self._merge_trailer(trailer)
                hybrid = self.resolve(trailer.get("XRefStm"))
                if isinstance(hybrid, int):
                    # A hybrid file keeps the objects that live in streams
                    # only in the cross-reference stream.
                    for num, entry in self._read_xref_stream(hybrid):
                        self.xref.setdefault(num, entry)
                nxt = self.resolve(trailer.get("Prev"))
                offset = nxt if isinstance(nxt, int) else None
            else:
                entries, trailer = self._read_xref_stream(offset)
                if not entries and not trailer:
                    # Some writers are a few bytes out with startxref.
                    found = self._nearby_xref(offset)
                    if found is None or found == offset:
                        break
                    offset = found
                    continue
                for num, entry in entries.items():
                    self.xref.setdefault(num, entry)
                self._merge_trailer(trailer)
                nxt = self.resolve(trailer.get("Prev"))
                offset = nxt if isinstance(nxt, int) else None

    def _nearby_xref(self, offset: int):
        window_start = max(0, offset - 300)
        window = self.buf[window_start : offset + 300]
        index = window.find(b"xref")
        if index >= 0:
            return window_start + index
        return None

    def _read_xref_stream(self, offset: int):
        """Read a `/Type /XRef` stream. Returns ({num: entry}, trailer dict)."""
        holder = self._object_at(offset)
        if not isinstance(holder, _Stream):
            return {}, {}
        d = holder.dict
        if d.get("Type") not in ("XRef", None):
            return {}, {}
        try:
            widths = [int(v) for v in (d.get("W") or [])]
        except (TypeError, ValueError):
            return {}, {}
        if len(widths) < 3:
            return {}, {}
        body = holder.data()
        if not body:
            return {}, {}
        size = d.get("Size")
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = 0
        index = d.get("Index")
        if not isinstance(index, list) or not index:
            index = [0, size]
        try:
            pairs = [int(v) for v in index]
        except (TypeError, ValueError):
            return {}, {}
        entries = {}
        pos = 0
        row_len = sum(widths[:3])
        if row_len <= 0:
            return {}, {}
        for k in range(0, len(pairs) - 1, 2):
            first, count = pairs[k], pairs[k + 1]
            for n in range(count):
                if pos + row_len > len(body):
                    return entries, d
                fields = []
                for width in widths[:3]:
                    if width == 0:
                        fields.append(None)
                    else:
                        fields.append(int.from_bytes(body[pos : pos + width], "big"))
                        pos += width
                kind = fields[0] if fields[0] is not None else 1
                f2 = fields[1] or 0
                f3 = fields[2] or 0
                num = first + n
                if kind == 1:
                    entries[num] = ("n", f2)
                elif kind == 2:
                    entries[num] = ("c", f2, f3)
                # kind 0 is a free entry: nothing to record
        return entries, d

    def _reconstruct(self) -> None:
        """Rebuild the file map by scanning for `N G obj` markers.

        This is what rescues a file whose xref table was destroyed or never
        finished being written. Later definitions win, which matches how
        incremental updates work.
        """
        self.recovered = True
        count = 0
        for m in re.finditer(rb"(?<![0-9])(\d{1,9})[\x00\t\r\n ]+(\d{1,5})[\x00\t\r\n ]+obj\b", self.buf):
            num = int(m.group(1))
            self.xref[num] = ("n", m.start())
            count += 1
            if count > _MAX_OBJECTS:
                break
        for m in re.finditer(rb"trailer", self.buf):
            d, _ = _parse_object(self.buf, m.end())
            if isinstance(d, dict):
                self._merge_trailer(d)
        if not self.trailer.get("Root"):
            root = self._find_catalog()
            if root is not None:
                self.trailer["Root"] = root

    def _find_catalog(self):
        for num in sorted(self.xref):
            value = self.get(num)
            if isinstance(value, dict) and value.get("Type") == "Catalog":
                return _Ref(num, 0)
        return None

    # --- document level
    @property
    def root(self):
        root = self.resolve(self.trailer.get("Root"))
        if isinstance(root, dict):
            return root
        found = self._find_catalog()
        return self.resolve(found)

    def title(self):
        info = self.resolve(self.trailer.get("Info"))
        if isinstance(info, dict):
            title = _decode_pdf_text(info.get("Title")).strip()
            if title:
                return title
        return None

    def pages(self):
        """[(page dictionary, inherited resources)], in document order."""
        out = []
        root = self.root
        if isinstance(root, dict):
            node = self.resolve(root.get("Pages"))
            # The top page-tree node knows how many pages there should be,
            # which is how we notice the ones we could not reach.
            hint = None
            if isinstance(node, dict):
                hint = self.resolve(node.get("Count"))
            if isinstance(hint, int) and hint > 0:
                self.page_count_hint = hint
            self._walk(node, {}, set(), out, 0)
            if isinstance(hint, int) and hint > len(out):
                self.missing_pages = True
        if not out:
            out = self._pages_by_scan()
        return out

    def _walk(self, node, inherited, seen, out, depth) -> None:
        if depth > 64:
            return
        if isinstance(node, _Ref):
            if node.num in seen:
                return
            seen.add(node.num)
        node = self.resolve(node)
        if not isinstance(node, dict):
            return
        inherited = dict(inherited)
        for key in ("Resources", "MediaBox", "CropBox", "Rotate"):
            if key in node and key not in inherited:
                inherited[key] = node[key]
        kids = self.resolve(node.get("Kids"))
        if isinstance(kids, list) and node.get("Type") != "Page":
            for kid in kids[:50000]:
                self._walk(kid, inherited, seen, out, depth + 1)
            return
        if node.get("Type") == "Page" or "Contents" in node:
            resources = self.resolve(node.get("Resources"))
            if not isinstance(resources, dict):
                resources = self.resolve(inherited.get("Resources"))
            out.append((node, resources if isinstance(resources, dict) else {}))

    def _pages_by_scan(self):
        """Last resort: any object that looks like a page, in object order."""
        out = []
        for num in sorted(self.xref):
            value = self.get(num)
            if isinstance(value, dict) and value.get("Type") == "Page":
                resources = self.resolve(value.get("Resources"))
                out.append((value, resources if isinstance(resources, dict) else {}))
        return out

    def note(self) -> str:
        if self.missing_pages:
            return _NOTE_PARTIAL_PAGES
        if self.undecodable_text:
            return _NOTE_FONT
        if self.recovered:
            return _NOTE_RECOVERED
        return ""


_NUM_RE = re.compile(rb"[+-]?\d+")


def _read_classic_xref(buf: bytes, pos: int) -> tuple:
    """Read an `xref` table and the `trailer` that follows it."""
    entries = {}
    trailer = {}
    i = pos + 4
    n = len(buf)
    for _ in range(20000):
        i = _skip_ws(buf, i)
        if i >= n:
            break
        if buf[i : i + 7] == b"trailer":
            value, _ = _parse_object(buf, i + 7)
            if isinstance(value, dict):
                trailer = value
            break
        m = re.match(rb"(\d{1,9})\s+(\d{1,9})", buf[i : i + 48])
        if not m:
            break
        first = int(m.group(1))
        count = int(m.group(2))
        i += m.end()
        if count < 0 or count > _MAX_OBJECTS:
            break
        for k in range(count):
            m2 = re.match(rb"\s*(\d{1,12})\s+(\d{1,6})\s+([nf])", buf[i : i + 48])
            if not m2:
                break
            i += m2.end()
            if m2.group(3) == b"n":
                offset = int(m2.group(1))
                entries[first + k] = ("n", offset)
            # free entries are simply left out
        else:
            continue
        break
    return entries, trailer


def _find_startxref(buf: bytes) -> int:
    index = buf.rfind(b"startxref", max(0, len(buf) - 4096))
    if index < 0:
        index = buf.rfind(b"startxref")
    if index < 0:
        return None
    m = re.search(rb"\d+", buf[index + 9 : index + 64])
    if not m:
        return None
    offset = int(m.group(0))
    if 0 <= offset < len(buf):
        return offset
    return None


def _has_pdf_header(buf: bytes) -> bool:
    return b"%PDF-" in buf[:8192]


def is_pdf(data) -> bool:
    """True when the bytes carry a `%PDF-` signature near the start."""
    try:
        if isinstance(data, memoryview):
            data = data.tobytes()
        if not isinstance(data, (bytes, bytearray)):
            return False
        return _has_pdf_header(bytes(data))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Brute force recovery
# ---------------------------------------------------------------------------
_TEXT_OP_MARKERS = (b"Tj", b"TJ", b"BT")


def _plausible_text(text: str) -> bool:
    """Reject the noise that comes out of non-text streams.

    A whole-file scan cannot tell a page description from an image, so the
    bar for keeping a chunk is a couple of real words rather than an
    accidental run of letters in compressed bytes.
    """
    if len(text) < 6 or len(text.split()) < 2:
        return False
    letters = 0
    printable = 0
    for ch in text:
        if ch.isalpha() or ch.isdigit():
            letters += 1
        if ch.isprintable() or ch in "\n\t ":
            printable += 1
    return letters >= 2 and printable * 10 >= len(text) * 9


def _brute_force(buf: bytes) -> str:
    """Scan the whole file for streams and read any text they contain.

    A destroyed file map does not stop the text being there; the bytes are
    still in the file. This finds them, at the cost of losing page structure.
    """
    chunks = []
    pos = 0
    scanned = 0
    total = 0
    while scanned < _MAX_STREAMS:
        index = buf.find(b"stream", pos)
        if index < 0:
            break
        pos = index + 6
        if index and buf[index - 1] not in _WS and buf[index - 1] not in _DELIM:
            continue  # part of `endstream`
        k = pos
        if buf[k : k + 2] == b"\r\n":
            k += 2
        elif buf[k : k + 1] in (b"\r", b"\n"):
            k += 1
        end = buf.find(b"endstream", k)
        if end < 0:
            end = len(buf)
        raw = buf[k:end]
        if len(raw) > 32 * 1024 * 1024:
            continue
        scanned += 1
        data = _inflate(raw)
        if not data:
            data = raw
        if not any(marker in data for marker in _TEXT_OP_MARKERS):
            continue
        pieces = _scan_content(data, {}, None, {}, None)
        text = _tidy(_assemble(pieces))
        if not _plausible_text(text):
            continue
        total += len(text)
        if total > _MAX_TEXT:
            break
        chunks.append(text)
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Reading a page
# ---------------------------------------------------------------------------
def _page_text(doc: _Document, page: dict, resources: dict, stats: dict) -> str:
    contents = doc.resolve(page.get("Contents"))
    if contents is None:
        return ""
    if not isinstance(contents, list):
        contents = [contents]
    streams = []
    for item in contents[:10000]:
        value = doc.resolve(item)
        if isinstance(value, _Stream):
            body = value.data()
            if body:
                streams.append(body)
    if not streams:
        return ""
    fonts = _page_fonts(doc, resources, doc._fonts, stats)
    try:
        pieces = _scan_content(b"\n".join(streams), fonts, doc, resources, stats)
    except _Stop:
        stats["truncated"] = True
        pieces = []
    except Exception:
        return ""
    return _tidy(_assemble(pieces))


def _extract(buf: bytes) -> PdfResult:
    doc = _Document(buf)
    try:
        doc.load()
    except Exception:
        doc.failed = True
    if doc.encrypted:
        return PdfResult([], None, True, _NOTE_LOCKED)

    stats = {}
    pages = []
    try:
        found = doc.pages()
    except Exception:
        found = []
    for index, (page, resources) in enumerate(found):
        try:
            text = _page_text(doc, page, resources, stats)
        except Exception:
            text = ""
        pages.append(Page(index + 1, text))
    if stats.get("font"):
        doc.undecodable_text = True

    words = sum(p.word_count() for p in pages)
    title = None
    try:
        title = doc.title()
    except Exception:
        title = None

    # Only pay for the whole-file scan when the structured read looks thin or
    # damaged: it is the sledgehammer, not the first tool.
    if words == 0 or doc.recovered or doc.failed:
        blob = _brute_force(buf)
        if blob and len(blob.split()) > words:
            return PdfResult([Page(1, blob)], title, False, _NOTE_RECOVERED)

    if pages:
        note = doc.note()
        if not note and words == 0:
            # Real pages, real content streams, no letters: a scan.
            note = _NOTE_SCANNED
        return PdfResult(pages, title, False, note)
    if words:
        return PdfResult(pages, title, False, "")
    if doc.failed:
        return PdfResult([], title, False, _NOTE_UNREADABLE)
    return PdfResult([], title, False, _NOTE_UNREADABLE)


def extract(data) -> PdfResult:
    """Read the text from PDF bytes.

    Never raises. A damaged, encrypted, scanned or non-PDF file comes back as
    a `PdfResult` with whatever was recovered and a plain-language `note`.
    """
    try:
        if isinstance(data, memoryview):
            buf = data.tobytes()
        elif isinstance(data, str):
            buf = data.encode("utf-8", "ignore")
        elif isinstance(data, (bytes, bytearray)):
            buf = bytes(data)
        else:
            return PdfResult([], None, False, _NOTE_NOT_PDF)
    except Exception:
        return PdfResult([], None, False, _NOTE_NOT_PDF)

    if not buf:
        return PdfResult([], None, False, _NOTE_NOT_PDF)
    if not _has_pdf_header(buf):
        return PdfResult([], None, False, _NOTE_NOT_PDF)
    try:
        return _extract(buf)
    except Exception:
        pass
    try:  # last resort: forget the file structure entirely
        blob = _brute_force(buf)
        if blob:
            return PdfResult([Page(1, blob)], None, False, _NOTE_RECOVERED)
    except Exception:
        pass
    return PdfResult([], None, False, _NOTE_UNREADABLE)
