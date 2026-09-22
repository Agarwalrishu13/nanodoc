"""Finding the parts of a document that answer a question.

There is no AI in this file, and that is the point. It is the classic
approach — an inverted index and BM25, the same maths search engines have
used for thirty years — written out in plain Python. It means the app can
always answer "where does it say that?" even with nothing installed, and it
means every answer can point at the exact paragraph it came from.

Everything here works on one document at a time. A dropped file is not a
search engine's corpus; it is a thing somebody wants a straight answer about.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# Words that carry no meaning worth searching for. Keeping this list short
# matters: over-filtering is how a search quietly stops finding things.
STOPWORDS = set("""
a about above after again against all am an and any are aren't as at be because been before being
below between both but by can cannot could couldn't did didn't do does doesn't doing don't down
during each few for from further had hadn't has hasn't have haven't having he her here hers herself
him himself his how i if in into is isn't it its itself let's me more most mustn't my myself no nor
not of off on once only or other ought our ours ourselves out over own same shan't she should
shouldn't so some such than that the their theirs them themselves then there these they this those
through to too under until up very was wasn't we were weren't what when where which while who whom
why with won't would wouldn't you your yours yourself yourselves
please tell me show give find does do did done get got
""".split())

_TOKEN = re.compile(r"[a-z0-9][a-z0-9'’\-]*")


def tokenize(text: str, keep_stopwords: bool = False) -> list:
    """Split text into searchable words, lightly folded to their root."""
    words = []
    for match in _TOKEN.finditer(text.lower()):
        word = match.group(0).strip("'-’")
        if not word:
            continue
        if not keep_stopwords and word in STOPWORDS:
            continue
        if len(word) == 1 and not word.isdigit():
            continue
        words.append(_stem(word))
    return words


def _stem(word: str) -> str:
    """A deliberately small stemmer: enough to match "payments" to "payment".

    A full Porter stemmer would match more, and would also mangle short words
    into things a person would not recognise in a highlighted quote. This
    handles the handful of English endings that matter and leaves the rest.
    """
    if len(word) <= 3 or word.isdigit():
        return word
    for suffix, replacement in (
        ("ies", "y"), ("sses", "ss"), ("xes", "x"), ("ches", "ch"), ("shes", "sh"),
        ("ing", ""), ("ed", ""), ("ly", ""), ("s", ""),
    ):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            stem = word[: -len(suffix)] + replacement
            # "uses" -> "use", not "us"; a doubled consonant before -ing goes.
            if suffix in ("ing", "ed") and len(stem) > 3 and stem[-1] == stem[-2] and stem[-1] not in "lsz":
                stem = stem[:-1]
            return stem
    return word


# --------------------------------------------------------------------------
# Passages — the quotable unit an answer is built from
# --------------------------------------------------------------------------
@dataclass
class Passage:
    """A few sentences that can be quoted on their own."""

    doc_id: str
    number: int          # which page/section/sheet it came from
    label: str           # "page 4", as shown to the reader
    text: str
    index: int           # position within the document, in reading order

    def to_dict(self) -> dict:
        return {"number": self.number, "label": self.label, "text": self.text, "index": self.index}


PASSAGE_WORDS = 90
PASSAGE_OVERLAP = 30


def split_into_passages(doc_id: str, pages, size: int = PASSAGE_WORDS, overlap: int = PASSAGE_OVERLAP) -> list:
    """Cut every page into overlapping windows of about `size` words.

    The overlap exists so an answer that straddles a boundary is still found
    whole in one of the windows. Short pages are one passage, not zero.
    """
    passages = []
    for page in pages:
        text = (getattr(page, "text", "") or "").strip()
        if not text:
            continue
        words = text.split()
        if len(words) <= size + overlap // 2:
            passages.append(Passage(doc_id, int(page.number), page.label, text, len(passages)))
            continue
        step = max(20, size - overlap)
        for start in range(0, len(words), step):
            chunk = words[start:start + size]
            if len(chunk) < 30 and passages:
                break
            passages.append(Passage(doc_id, int(page.number), page.label, " ".join(chunk), len(passages)))
            if start + size >= len(words):
                break
    return passages


# --------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------
K1 = 1.4
B = 0.72


class Index:
    """A BM25 index over one document."""

    def __init__(self, doc_id: str, pages, size: int = PASSAGE_WORDS, overlap: int = PASSAGE_OVERLAP):
        self.doc_id = doc_id
        self.passages = split_into_passages(doc_id, pages, size, overlap)
        self.terms: dict = {}
        self.lengths: list = []
        self.total_length = 0
        self._build()

    def _build(self) -> None:
        for passage in self.passages:
            words = tokenize(passage.text)
            self.lengths.append(len(words) or 1)
            self.total_length += len(words) or 1
            seen = {}
            for word in words:
                seen[word] = seen.get(word, 0) + 1
            for word, count in seen.items():
                self.terms.setdefault(word, {})[passage.index] = count

    @property
    def average_length(self) -> float:
        return (self.total_length / len(self.lengths)) if self.lengths else 1.0

    def _idf(self, term: str) -> float:
        count = len(self.terms.get(term, {}))
        if not count:
            return 0.0
        return math.log(1.0 + (len(self.passages) - count + 0.5) / (count + 0.5))

    def _score(self, term: str, passage_index: int, idf: float, average: float) -> float:
        frequency = self.terms.get(term, {}).get(passage_index, 0)
        if not frequency:
            return 0.0
        length = self.lengths[passage_index]
        return idf * (frequency * (K1 + 1)) / (frequency + K1 * (1 - B + B * length / average))

    def search(self, query: str, limit: int = 6) -> list:
        """Return the passages that best answer `query`, best first."""
        terms = tokenize(query)
        if not terms:
            # A question made only of small words ("what is this about") still
            # has words in it; searching them unfiltered beats searching none.
            terms = tokenize(query, keep_stopwords=True)
        if not terms or not self.passages:
            return []

        unique = list(dict.fromkeys(terms))
        idfs = {term: self._idf(term) for term in unique}
        average = self.average_length or 1.0

        scores: dict = {}
        for term in unique:
            idf = idfs[term]
            if idf <= 0:
                continue
            for passage_index in self.terms.get(term, {}):
                scores[passage_index] = scores.get(passage_index, 0.0) + self._score(term, passage_index, idf, average)

        if not scores:
            return []

        # An exact phrase is a much stronger signal than the words separately,
        # especially for the way a person types: "how much is the deposit".
        phrase = " ".join(query.lower().split())
        results = []
        for passage_index, raw in scores.items():
            passage = self.passages[passage_index]
            score = raw
            haystack = " ".join(passage.text.lower().split())
            if len(phrase) > 8 and phrase in haystack:
                score *= 1.8
            present = [term for term in unique if term in self.terms and passage_index in self.terms[term]]
            # Prefer the passage that answers the whole question, not a stray
            # word of it.
            coverage = len(present) / len(unique)
            score *= 0.6 + 0.4 * coverage
            results.append((score, coverage, passage_index, present))

        results.sort(key=lambda item: -item[0])
        hits = [
            Hit(
                passage=self.passages[passage_index],
                score=round(score, 3),
                matched_terms=terms_present,
                snippet=_snippet(self.passages[passage_index].text, query),
            )
            for score, _coverage, passage_index, terms_present in results[:limit]
        ]
        return hits

    def confidence(self, query: str, hits: list) -> dict:
        """Say honestly whether the document actually answers the question.

        A search always returns *something* — that is what makes it dangerous.
        This decides whether what came back is an answer or just the least
        unrelated paragraph, so the app can say "I could not find that" rather
        than dressing up a coincidence.

        The measure is deliberately relative to the document rather than to an
        absolute score. BM25 numbers depend on how big the document is, so a
        fixed threshold that works on a 200-page report rejects everything in a
        one-page letter. What matters here is how much of the question the
        document speaks to at all:

        * if none of the question's words appear anywhere in the document, the
          answer is not in there — no score can rescue that;
        * if only one word out of six appears, the document is about something
          else nearby, which is the most common way a search lies;
        * "how much is the deposit" has one word the document lacks ("much")
          and one it is built around ("deposit"), which is a real answer.
        """
        terms = list(dict.fromkeys(tokenize(query) or tokenize(query, keep_stopwords=True)))
        if not hits or not terms:
            return {"found": False, "coverage": 0.0, "score": 0.0, "reason": "empty"}

        # How much of the question the document contains anywhere, not just in
        # the best paragraph.
        present = [term for term in terms if term in self.terms]
        coverage = len(present) / len(terms)
        best = hits[0].score
        found = bool(present) and coverage >= 0.4 and best >= 0.5

        if not present:
            reason = "nothing"
        elif not found:
            reason = "weak"
        else:
            reason = "ok"
        return {"found": found, "coverage": round(coverage, 2), "score": best, "reason": reason}


@dataclass
class Hit:
    """One passage, plus how well it matched and what to highlight."""

    passage: Passage
    score: float
    matched_terms: list = field(default_factory=list)
    snippet: str = ""

    def to_dict(self) -> dict:
        return {
            "label": self.passage.label,
            "number": self.passage.number,
            "text": self.passage.text,
            "snippet": self.snippet,
            "score": self.score,
            "matched": self.matched_terms,
        }


SNIPPET_WORDS = 46


def _snippet(text: str, query: str, width: int = SNIPPET_WORDS) -> str:
    """Pick the window of the passage that best contains the question's words.

    A passage from a dense page can be long; the reader should see the part
    that matters without having to hunt for it.
    """
    words = text.split()
    if len(words) <= width:
        return text
    wanted = set(tokenize(query, keep_stopwords=True))
    if not wanted:
        return " ".join(words[:width])

    best_start, best_count = 0, -1
    for start in range(0, max(1, len(words) - width + 1), 6):
        window = words[start:start + width]
        count = sum(1 for word in window if _stem(word.lower().strip(".,;:!?\"'()")) in wanted)
        if count > best_count:
            best_start, best_count = start, count

    snippet = " ".join(words[best_start:best_start + width])
    if best_start > 0:
        snippet = "… " + snippet
    if best_start + width < len(words):
        snippet = snippet + " …"
    return snippet


# Words that are meaningful to a search but useless as a description of a
# document. "The deposit must be protected" should be described as being about
# deposits and protection, not about "must".
_DISPLAY_NOISE = set("""
must may shall might will would can could should also however therefore hereby therein
said such using used one two three four five six seven eight nine ten first second third
new get got make made many much more most other others same each both every either neither
per via etc eg ie
""".split())


def about(doc_id: str, pages, limit: int = 12) -> dict:
    """Describe a document with no AI: its opening words and its key terms.

    This runs the moment a file is dropped in, so the reader sees that the app
    has really understood the shape of what they gave it — before any question
    is asked and whether or not an AI engine is available.

    Longer words are preferred over merely frequent ones, because in ordinary
    English the short words are the ones that carry no meaning: a document
    about a tenancy says "landlord" far more usefully than it says "must".
    """
    counter: dict = {}
    for page in pages:
        for word in tokenize(getattr(page, "text", "") or ""):
            counter[word] = counter.get(word, 0) + 1

    candidates = [
        (word, count) for word, count in counter.items()
        if count > 1 and word not in _DISPLAY_NOISE and len(word) > 3
    ]
    ranked = sorted(candidates, key=lambda item: -(item[1] * len(item[0])))
    keywords = [word for word, _count in ranked[:limit]]

    # A short document may have nothing that repeats; fall back to its longest
    # words rather than showing an empty line.
    if not keywords:
        singles = [word for word in counter if word not in _DISPLAY_NOISE and len(word) > 5]
        keywords = sorted(singles, key=len, reverse=True)[:limit]

    opening = ""
    for page in pages:
        text = (getattr(page, "text", "") or "").strip()
        if len(text) > 40:
            opening = " ".join(text.split()[:60])
            break

    first_labels = [getattr(page, "label", "") for page in pages[:6]]
    return {"keywords": keywords, "opening": opening, "labels": first_labels}
