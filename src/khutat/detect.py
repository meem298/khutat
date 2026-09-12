"""Finding the fillable header fields inside a plan template.

A PDF has no form fields here — the header is a drawn table and the labels
("اسم الطالب", "الحلقة", …) are text painted at coordinates.  To know where a
value belongs we locate its label, find the table cell containing it, and take
the neighbouring cell to its left.

The templates draw every table cell as a filled rectangle, so cell geometry is
read straight from the document rather than estimated from text widths.  That
matters: an estimate would have to be re-tuned per template and would still put
text near the borders, while the real rectangles are exact for all of them.

Two other properties of the source documents shape this module:

* **Text chunks are not words.**  The producer breaks runs wherever it adjusts
  spacing, so "الحلقة" can arrive as ``الحل`` + ``قة`` and "العام والفصل" as
  ``العام و`` + ``الفصل``.  Chunks are reassembled by the cell they fall inside
  rather than by guessing at gap widths — a gap threshold wide enough to rejoin
  "العام و" + "الفصل" also wrongly swallows the neighbouring value, whereas cell
  membership is exact.
* **Coordinates mean nothing on their own.**  A rectangle is drawn in whatever
  space the transformation matrix establishes at that moment, so the content
  stream is walked with the matrix tracked rather than scanned for ``re``.  The
  association's own PDFs happen to set no transformation at all, but a plan
  converted from Word flips the y axis and scales by 0.75, and reading raw
  coordinates there puts every cell in the wrong place.
* **De-impose first.**  Run :mod:`khutat.imposition` before detecting; see that
  module for why.

Detection is driven by label text and drawn geometry, never by hard-coded
coordinates, so the same code handles every template without per-file tuning.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

from pypdf import PageObject

# Header labels, longest first so "اسم المعلم" wins over "المعلم".
FIELD_LABELS: tuple[str, ...] = (
    "اسم الطالب",
    "اسم المعلم",
    "العام والفصل",
    "المجمع/الدار",
    "الحلقة",
    "المعلم",
)

# Rectangles thinner than this are borders and rules, not cells.
_MIN_CELL_WIDTH = 12.0
_MIN_CELL_HEIGHT = 6.0

# Breathing room kept between a drawn value and its cell borders.
_CELL_PADDING = 3.0

_ARABIC_NORMALISE = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"})
_TATWEEL = "ـ"


def normalise(text: str) -> str:
    """Fold spelling variants so label matching survives small differences.

    NFKC is what makes this work across producers.  Some writers store Arabic
    as abstract letters and let the font shape them; others — a Google Docs PDF
    export, for one — store the shaped glyphs themselves, so the text comes back
    as presentation forms (U+FE70-U+FEFF) and "اسم الطالب" never matches
    "اﺳم اﻟطﺎﻟب" as a string.  NFKC maps every presentation form back to its
    base letter, and splits the lam-alef ligature, so both kinds of document
    compare equal.
    """
    stripped = "".join(c for c in text if unicodedata.category(c) != "Cf")
    stripped = unicodedata.normalize("NFKC", stripped)
    stripped = stripped.replace(_TATWEEL, "")
    stripped = stripped.translate(_ARABIC_NORMALISE)
    return re.sub(r"\s+", " ", stripped).strip()


def compact(text: str) -> str:
    """Normalise and drop every space, for whitespace-blind label matching.

    The producer's chunking leaves stray spaces inside labels — "العام والفصل"
    comes back as "العام و الفصل" — so a single space must not decide a match.
    """
    return normalise(text).replace(" ", "")


@dataclass(frozen=True)
class Chunk:
    """One run of text as the PDF painted it."""

    text: str
    x: float  # left edge in PDF user space
    y: float  # baseline
    size: float


@dataclass(frozen=True)
class Cell:
    """A rectangle the template drew as part of a table."""

    left: float
    bottom: float
    right: float
    top: float

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x <= self.right and self.bottom <= y <= self.top

    def shares_row(self, other: "Cell") -> bool:
        overlap = min(self.top, other.top) - max(self.bottom, other.bottom)
        return overlap > (self.top - self.bottom) * 0.5


@dataclass(frozen=True)
class Field:
    """A header label and the empty cell that belongs to it."""

    label: str
    page_index: int
    left: float
    right: float
    bottom: float
    top: float
    size: float

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def centre_x(self) -> float:
        return (self.left + self.right) / 2

    @property
    def centre_y(self) -> float:
        return (self.bottom + self.top) / 2


Matrix = tuple[float, float, float, float, float, float]

_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _multiply(first: Matrix, second: Matrix) -> Matrix:
    """``first`` then ``second`` — PDF's row-vector order."""
    a1, b1, c1, d1, e1, f1 = first
    a2, b2, c2, d2, e2, f2 = second
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _apply(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return (a * x + c * y + e, b * x + d * y + f)


def _vertical_scale(matrix: Matrix) -> float:
    """How much the matrix stretches the y axis, for scaling a font size."""
    _, _, c, d, _, _ = matrix
    return math.hypot(c, d) or 1.0


def read_chunks(page: PageObject) -> list[Chunk]:
    """Extract every painted text run on ``page`` with its position.

    A glyph's position is its text matrix *then* the transformation in force,
    so the two are combined rather than reading the text matrix alone.  The
    association's own PDFs set no transformation at all, which is why the text
    matrix was enough for them; a PDF exported from Word or Google Docs flips
    the y axis and scales by 0.75, and ignoring that puts every chunk in the
    wrong place.
    """
    chunks: list[Chunk] = []

    def visit(text, cm, tm, font_dict, font_size):
        cleaned = "".join(c for c in text if unicodedata.category(c) != "Cf")
        if not cleaned.strip():
            return
        combined = _multiply(tuple(tm), tuple(cm))
        x, y = combined[4], combined[5]
        size = float(font_size or 0) * _vertical_scale(tuple(cm))
        chunks.append(Chunk(text=cleaned, x=x, y=y, size=size))

    page.extract_text(visitor_text=visit)
    return chunks


# Numbers, operators, and the literal forms that must not be read as either.
_CONTENT_TOKEN = re.compile(
    rb"(?P<num>[-+]?(?:\d+\.?\d*|\.\d+))"
    rb"|(?P<name>/[^\s/\[\]<>(){}%]*)"
    rb"|(?P<string>\((?:\\.|[^()\\])*\))"
    rb"|(?P<hex><[0-9A-Fa-f\s]*>)"
    rb"|(?P<op>[A-Za-z'\"*]+)",
    re.S,
)

# Inline images carry raw bytes between ID and EI that must not be tokenised.
_INLINE_IMAGE = re.compile(rb"\bBI\b.*?\bEI\b", re.S)


def _xobjects_of(resources) -> dict:
    if resources is None:
        return {}
    xobjects = resources.get_object().get("/XObject")
    return {} if xobjects is None else xobjects.get_object()


def _collect_rectangles(
    data: bytes, resources, ctm: Matrix, out: list[Cell], depth: int = 0
) -> None:
    """Walk a content stream, tracking the matrix, and record every rectangle.

    ``re`` gives a rectangle in the space current at that moment, so the four
    corners are mapped through the transformation in force before they mean
    anything on the page.  ``q``/``Q`` save and restore it, and ``Do`` on a form
    enters a nested space that is the form's own matrix followed by whatever
    placed it.
    """
    if depth > 8:
        return

    data = _INLINE_IMAGE.sub(b" ", data)
    xobjects = _xobjects_of(resources)

    stack: list[Matrix] = []
    operands: list[float] = []
    last_name: bytes | None = None

    for token in _CONTENT_TOKEN.finditer(data):
        if token.lastgroup == "num":
            operands.append(float(token.group()))
            continue
        if token.lastgroup == "name":
            last_name = token.group()
            continue
        if token.lastgroup in ("string", "hex"):
            continue

        operator = token.group()
        if operator == b"q":
            stack.append(ctm)
        elif operator == b"Q":
            if stack:
                ctm = stack.pop()
        elif operator == b"cm" and len(operands) >= 6:
            ctm = _multiply(tuple(operands[-6:]), ctm)
        elif operator == b"re" and len(operands) >= 4:
            x, y, width, height = operands[-4:]
            corners = [
                _apply(ctm, x, y),
                _apply(ctm, x + width, y),
                _apply(ctm, x, y + height),
                _apply(ctm, x + width, y + height),
            ]
            xs = [p[0] for p in corners]
            ys = [p[1] for p in corners]
            out.append(Cell(left=min(xs), bottom=min(ys), right=max(xs), top=max(ys)))
        elif operator == b"Do" and last_name is not None:
            reference = xobjects.get(last_name.decode("latin-1"))
            form = reference.get_object() if reference is not None else None
            if form is not None and form.get("/Subtype") == "/Form":
                inner = ctm
                matrix = form.get("/Matrix")
                if matrix is not None:
                    inner = _multiply(tuple(float(v) for v in matrix), ctm)
                _collect_rectangles(
                    form.get_data(), form.get("/Resources"), inner, out, depth + 1
                )

        operands.clear()

    return


def read_cells(page: PageObject) -> list[Cell]:
    """Extract the table cells the page draws, in page coordinates."""
    contents = page.get_contents()
    if contents is None:
        return []

    found: list[Cell] = []
    _collect_rectangles(contents.get_data(), page.get("/Resources"), _IDENTITY, found)

    seen: set[tuple[float, float, float, float]] = set()
    cells: list[Cell] = []
    for cell in found:
        if (
            cell.right - cell.left < _MIN_CELL_WIDTH
            or cell.top - cell.bottom < _MIN_CELL_HEIGHT
        ):
            continue
        key = (
            round(cell.left, 2),
            round(cell.bottom, 2),
            round(cell.right, 2),
            round(cell.top, 2),
        )
        if key in seen:
            continue
        seen.add(key)
        cells.append(cell)
    return cells


def text_by_cell(
    chunks: list[Chunk], cells: list[Cell]
) -> dict[Cell, tuple[str, float]]:
    """Reassemble each cell's text from the chunks painted inside it.

    A chunk counts towards *every* cell that contains it, not just the tightest
    one.  Table cells nest: when a label is too wide for its column the producer
    wraps it onto two stacked line-boxes inside the real cell, so "الحلقة"
    arrives as ``الحلق`` in one box and ``ة`` in the box beneath.  Neither box
    matches the label alone; their shared parent does.  Counting a chunk once,
    at its tightest cell, therefore loses every wrapped label — which is exactly
    how the full-size templates differ from the imposed ones, where the same
    labels happen to fit on one line.

    Returns the joined text and the largest font size seen, keyed by cell.
    Chunks are concatenated right to left, then top to bottom, which is their
    logical order here.
    """
    grouped: dict[Cell, list[Chunk]] = {}
    for chunk in chunks:
        x, y = chunk.x + 0.5, chunk.y + chunk.size * 0.3
        for cell in cells:
            if cell.contains(x, y):
                grouped.setdefault(cell, []).append(chunk)

    assembled: dict[Cell, tuple[str, float]] = {}
    for cell, members in grouped.items():
        members.sort(key=lambda c: (-c.y, -c.x))
        assembled[cell] = (
            "".join(c.text for c in members),
            max(c.size for c in members),
        )
    return assembled


def match_label(run_text: str) -> str | None:
    """Return the header label a run represents, or ``None``.

    ``FIELD_LABELS`` is ordered longest first so "اسم المعلم" is tested before
    the "المعلم" that sits inside it.
    """
    candidate = compact(run_text)
    if not candidate:
        return None
    for label in FIELD_LABELS:
        if compact(label) in candidate:
            return label
    return None


def _area(cell: Cell) -> float:
    return (cell.right - cell.left) * (cell.top - cell.bottom)


def _cell_to_left(cells: list[Cell], anchor: Cell) -> Cell | None:
    """The nearest same-row cell whose right edge meets the anchor's left edge."""
    candidates = [
        c
        for c in cells
        if c.shares_row(anchor) and c.right <= anchor.left + 1.0 and c is not anchor
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.right)


def detect_fields(page: PageObject, page_index: int) -> list[Field]:
    """Return the fillable header fields found on one de-imposed page."""
    cells = read_cells(page)
    if not cells:
        return []

    contents = text_by_cell(read_chunks(page), cells)

    fields: list[Field] = []
    claimed: set[tuple[float, float]] = set()

    # Tightest cell first: a label's own cell should win over any ancestor that
    # merely contains it, and over the page-sized rectangle that contains
    # everything.  Ancestors that resolve to an already-claimed value cell are
    # dropped below, so the tight match is the one that survives.
    for label_cell in sorted(contents, key=_area):
        text, size = contents[label_cell]
        label = match_label(text)
        if label is None:
            continue

        value_cell = _cell_to_left(cells, label_cell)
        if value_cell is None:
            continue

        # A value cell that already holds text is pre-filled by the association
        # (the level range beside "الجديد", for one) and must be left alone.
        if value_cell in contents:
            continue

        key = (round(value_cell.left, 1), round(value_cell.bottom, 1))
        if key in claimed:
            continue
        claimed.add(key)

        fields.append(
            Field(
                label=label,
                page_index=page_index,
                left=value_cell.left + _CELL_PADDING,
                right=value_cell.right - _CELL_PADDING,
                bottom=value_cell.bottom + _CELL_PADDING,
                top=value_cell.top - _CELL_PADDING,
                size=size,
            )
        )

    return fields
