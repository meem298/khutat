"""Writing student values into a template's detected fields.

Scaffold: the contracts are described, the bodies are not written yet.

Arabic cannot be handed to ReportLab as-is.  A PDF draws glyphs in the order
given, left to right, and knows nothing about language, so two things have to
happen to a string before it is drawn:

* **Shaping** — an Arabic letter has up to four contextual forms and Unicode
  stores only the abstract letter.  ReportLab does not run the font's shaping
  tables, so unshaped text comes out as disconnected letters.
* **Reordering** — even shaped, the glyphs would be laid out left to right.

``arabic_reshaper.reshape`` then ``bidi.get_display``, in that order.  After
those two calls the string is in *visual* order and made of presentation-form
codepoints: draw it, measure it, and do nothing else with it.  Every text
operation — trimming, truncating, normalising — belongs before the reshape.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.pdfgen.canvas import Canvas

from .detect import Field

# Smallest the value may shrink to before it is truncated instead.  Below this
# a printed name stops being readable, which defeats the point of fitting it.
MIN_FONT_SIZE = 7.0


def register_font(path: Path) -> str:
    """Register the Arabic TTF with ReportLab; return the name to draw with.

    Call once, not per field.  The returned name is what ``setFont`` and
    ``stringWidth`` expect.
    """
    raise NotImplementedError


def shape(text: str) -> str:
    """Return ``text`` shaped and reordered, ready to draw.

    The result is visual-order presentation forms.  Treat it as opaque.
    """
    raise NotImplementedError


def fit_size(display: str, font: str, field: Field, start: float) -> float:
    """Largest size at or below ``start`` that fits ``display`` in ``field``.

    Never returns less than ``MIN_FONT_SIZE``; a value that still does not fit
    at that floor is the caller's problem to truncate.
    """
    raise NotImplementedError


def draw_value(canvas: Canvas, field: Field, value: str, font: str) -> None:
    """Draw ``value`` centred inside ``field``.

    ``field.bottom`` is the cell's edge, not the text baseline — placing the
    baseline there sits the text on the border.
    """
    raise NotImplementedError


def fill_template(
    source: Path, values: dict[str, str], destination: Path, font_path: Path
) -> int:
    """Fill every detected field whose label appears in ``values``.

    Returns the number of fields written.  Labels repeat across pages, and a
    single page can carry two headers, so iterate over the detected fields and
    look each label up — not the other way round.
    """
    raise NotImplementedError
