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
* **Coordinates are only trustworthy after de-imposition.**  Run
  :mod:`khutat.imposition` first; see that module for why.

Detection is driven by label text and drawn geometry, never by hard-coded
coordinates, so the same code handles every template without per-file tuning.
"""

from __future__ import annotations

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

# Chunks whose baselines differ by less than this are treated as one line.
_BASELINE_TOLERANCE = 1.5

# Extra room a chunk may sit apart and still continue the same word.
_RUN_GAP = 4.0

# Rectangles thinner than this are borders and rules, not cells.
_MIN_CELL_WIDTH = 12.0
_MIN_CELL_HEIGHT = 6.0

# Breathing room kept between a drawn value and its cell borders.
_CELL_PADDING = 3.0

_RECT = re.compile(
    rb"([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+re(?![A-Za-z])"
)

_ARABIC_NORMALISE = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"})
_TATWEEL = "ـ"


def normalise(text: str) -> str:
    """Fold spelling variants so label matching survives small differences."""
    stripped = "".join(c for c in text if unicodedata.category(c) != "Cf")
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


def read_chunks(page: PageObject) -> list[Chunk]:
    """Extract every painted text run on ``page`` with its position."""
    chunks: list[Chunk] = []

    def visit(text, cm, tm, font_dict, font_size):
        cleaned = "".join(c for c in text if unicodedata.category(c) != "Cf")
        if cleaned.strip():
            chunks.append(
                Chunk(text=cleaned, x=tm[4], y=tm[5], size=float(font_size or 0))
            )

    page.extract_text(visitor_text=visit)
    return chunks


def read_cells(page: PageObject) -> list[Cell]:
    """Extract the table cells the page draws, de-duplicated.

    The drawing lives inside the page's Form XObject, and the de-imposed page
    places that form with an identity matrix, so rectangle coordinates are
    already page coordinates.
    """
    streams: list[bytes] = []

    contents = page.get_contents()
    if contents is not None:
        streams.append(contents.get_data())

    resources = page.get("/Resources")
    if resources is not None:
        xobjects = resources.get_object().get("/XObject")
        if xobjects is not None:
            for ref in xobjects.get_object().values():
                form = ref.get_object()
                if form.get("/Subtype") == "/Form":
                    streams.append(form.get_data())

    seen: set[tuple[float, float, float, float]] = set()
    cells: list[Cell] = []
    for stream in streams:
        for match in _RECT.finditer(stream):
            x, y, w, h = (float(v) for v in match.groups())
            if w < _MIN_CELL_WIDTH or h < _MIN_CELL_HEIGHT:
                continue
            key = (round(x, 2), round(y, 2), round(w, 2), round(h, 2))
            if key in seen:
                continue
            seen.add(key)
            cells.append(Cell(left=x, bottom=y, right=x + w, top=y + h))
    return cells


def text_by_cell(
    chunks: list[Chunk], cells: list[Cell]
) -> dict[Cell, tuple[str, float]]:
    """Reassemble each cell's text from the chunks painted inside it.

    Returns the joined text and the largest font size seen, keyed by cell.
    Chunks are concatenated right to left, which is their logical order here.
    """
    grouped: dict[Cell, list[Chunk]] = {}
    for chunk in chunks:
        cell = _smallest_cell_at(cells, chunk.x + 0.5, chunk.y + chunk.size * 0.3)
        if cell is not None:
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


def _smallest_cell_at(cells: list[Cell], x: float, y: float) -> Cell | None:
    """The tightest cell containing a point — table nesting draws larger ones too."""
    candidates = [c for c in cells if c.contains(x, y)]
    if not candidates:
        return None
    return min(candidates, key=lambda c: (c.right - c.left) * (c.top - c.bottom))


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

    for label_cell, (text, size) in contents.items():
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
