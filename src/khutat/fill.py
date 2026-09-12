"""Writing student values into a template's detected fields.

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
operation — trimming, truncating, normalising — belongs before the reshape,
which is why :func:`_shorten_to_fit` shortens the logical string and reshapes
again rather than cutting the shaped one.

The font is a parameter, never a constant.  macOS ships Arabic faces that are
fine for development but cannot be redistributed, so the face this runs with is
a deployment decision and the code must not bake one in.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import arabic_reshaper
from bidi import get_display
from pypdf import PageObject, PdfReader, PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from .detect import Field, compact, detect_fields
from .imposition import to_single_pages

# Smallest the value may shrink to before it is truncated instead.  Below this
# a printed name stops being readable, which defeats the point of fitting it.
MIN_FONT_SIZE = 7.0

# Share of the cell's height a value may occupy.  A value sized to the width
# alone can still be taller than the row it sits in.
_MAX_HEIGHT_SHARE = 0.72

# Used when a field carries no usable label size to start from.
_FALLBACK_SIZE_SHARE = 0.6

_ELLIPSIS = "…"


def register_font(path: Path) -> str:
    """Register the Arabic TTF with ReportLab; return the name to draw with.

    Call once, not per field.  The returned name is what ``setFont`` and
    ``stringWidth`` expect.  Registering the same file twice is a no-op, so
    callers may call this freely rather than tracking whether they have.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"No font file at {path}")

    name = re.sub(r"[^A-Za-z0-9]+", "", path.stem) or "PlanFont"
    try:
        pdfmetrics.getFont(name)
    except KeyError:
        pdfmetrics.registerFont(TTFont(name, str(path)))
    return name


def shape(text: str) -> str:
    """Return ``text`` shaped and reordered, ready to draw.

    The result is visual-order presentation forms.  Treat it as opaque.
    """
    return get_display(arabic_reshaper.reshape(text))


def fit_size(display: str, font: str, field: Field, start: float) -> float:
    """Largest size at or below ``start`` that fits ``display`` in ``field``.

    Never returns less than ``MIN_FONT_SIZE``; a value that still does not fit
    at that floor is the caller's problem to truncate.

    Width scales linearly with font size, so the size that exactly fills the
    cell is one division rather than a search.
    """
    ceiling = min(start, (field.top - field.bottom) * _MAX_HEIGHT_SHARE)
    ceiling = max(ceiling, MIN_FONT_SIZE)

    width = pdfmetrics.stringWidth(display, font, ceiling)
    if width <= field.width or width <= 0:
        return ceiling

    return max(MIN_FONT_SIZE, ceiling * field.width / width)


def _shorten_to_fit(text: str, font: str, field: Field, size: float) -> str:
    """Trim ``text`` until its shaped form fits ``field`` at ``size``.

    Shortening happens on the logical string, before reshaping.  Cutting the
    shaped string instead would remove the *first* letters of the name — the
    shaped form is in visual order — and would strand the joining forms of
    whatever survived.
    """
    if not text:
        return text

    kept = text
    while kept:
        candidate = kept if kept == text else kept.rstrip() + _ELLIPSIS
        if pdfmetrics.stringWidth(shape(candidate), font, size) <= field.width:
            return candidate
        kept = kept[:-1]
    return ""


def _starting_size(field: Field) -> float:
    """A sensible size to try first for this field's value.

    The label's own size is the best available hint at what the template's
    author intended; when detection could not recover one, fall back to a share
    of the cell's height.
    """
    if field.size and field.size > 0:
        return float(field.size)
    return max(MIN_FONT_SIZE, (field.top - field.bottom) * _FALLBACK_SIZE_SHARE)


def draw_value(canvas: Canvas, field: Field, value: str, font: str) -> None:
    """Draw ``value`` centred inside ``field``.

    ``field.bottom`` is the cell's edge, not the text baseline — placing the
    baseline there sits the text on the border, so the baseline is derived from
    the font's own ascent and descent instead.
    """
    text = value.strip()
    if not text:
        return

    display = shape(text)
    size = fit_size(display, font, field, _starting_size(field))

    if pdfmetrics.stringWidth(display, font, size) > field.width:
        text = _shorten_to_fit(text, font, field, size)
        if not text:
            return
        display = shape(text)

    width = pdfmetrics.stringWidth(display, font, size)
    ascent, descent = pdfmetrics.getAscentDescent(font, size)

    canvas.setFont(font, size)
    canvas.drawString(
        field.centre_x - width / 2,
        field.centre_y - (ascent + descent) / 2,
        display,
    )


@dataclass(frozen=True)
class FillResult:
    """What one fill run wrote."""

    written: int
    fields_seen: int
    unused_labels: tuple[str, ...]


def _values_by_compact_label(values: dict[str, str]) -> dict[str, str]:
    """Key the caller's values the way labels are compared.

    Callers type labels by hand, so "العام والفصل" must match however they
    spaced or spelled it; :func:`khutat.detect.compact` is the same folding
    detection uses to recognise the label in the document.
    """
    return {compact(label): value for label, value in values.items()}


def _stamp(page: PageObject, fields: list[Field], values: dict[str, str], font: str) -> int:
    """Draw every matching value onto ``page``; return how many were written."""
    written = 0
    buffer = io.BytesIO()
    canvas = Canvas(
        buffer, pagesize=(float(page.mediabox.width), float(page.mediabox.height))
    )

    for field in fields:
        value = values.get(compact(field.label))
        if value is None or not value.strip():
            continue
        draw_value(canvas, field, value, font)
        written += 1

    if not written:
        return 0

    canvas.save()
    buffer.seek(0)
    page.merge_page(PdfReader(buffer).pages[0])
    return written


def fill_template(
    source: Path, values: dict[str, str], destination: Path, font_path: Path
) -> FillResult:
    """Fill every detected field whose label appears in ``values``.

    Labels repeat across pages, and a single page can carry two headers, so
    this iterates over the detected fields and looks each label up — not the
    other way round.  A value therefore lands on every copy of its field
    without the caller saying so.
    """
    font = register_font(font_path)
    wanted = _values_by_compact_label(values)

    pages = to_single_pages(PdfReader(str(source))).pages
    writer = PdfWriter()

    written = 0
    seen = 0
    matched: set[str] = set()

    for index, page in enumerate(pages):
        fields = detect_fields(page, index)
        seen += len(fields)
        for field in fields:
            key = compact(field.label)
            if key in wanted:
                matched.add(key)
        written += _stamp(page, fields, wanted, font)
        writer.add_page(page)

    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "wb") as handle:
        writer.write(handle)

    unused = tuple(
        label for label in values if compact(label) not in matched
    )
    return FillResult(written=written, fields_seen=seen, unused_labels=unused)


def _parse_assignment(raw: str) -> tuple[str, str]:
    label, separator, value = raw.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError(f"Expected LABEL=VALUE, got {raw!r}")
    return label.strip(), value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m khutat.fill",
        description="Fill a plan template's detected header fields.",
    )
    parser.add_argument("template", type=Path, help="template PDF to fill")
    parser.add_argument("output", type=Path, help="where to write the filled copy")
    parser.add_argument(
        "--font",
        required=True,
        type=Path,
        help="path to an Arabic TTF to draw with",
    )
    parser.add_argument(
        "--set",
        dest="assignments",
        metavar="LABEL=VALUE",
        action="append",
        default=[],
        type=_parse_assignment,
        help="a value to write, repeatable (e.g. --set 'اسم الطالب=مها')",
    )
    args = parser.parse_args(argv)

    if not args.assignments:
        print("Nothing to write: pass at least one --set LABEL=VALUE", file=sys.stderr)
        return 2

    try:
        result = fill_template(
            args.template, dict(args.assignments), args.output, args.font
        )
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 2

    print(f"wrote {result.written} of {result.fields_seen} detected fields -> {args.output}")
    for label in result.unused_labels:
        print(f"  no field found for {label!r}", file=sys.stderr)

    # An unused label is nearly always a typo in the caller's label, so make it
    # visible to a script that only checks the exit status.
    return 1 if result.unused_labels else 0


if __name__ == "__main__":
    raise SystemExit(main())
