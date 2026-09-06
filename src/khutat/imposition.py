"""Splitting and re-assembling imposed plan PDFs.

The association ships plans in two layouts, and a template may arrive as either:

* **Imposed.**  Every printed sheet is A4 landscape and carries four half-scale
  copies of full-size pages, drawn as Form XObjects.  A plan that is logically
  8 pages arrives as 2 sheets.
* **Already full-size.**  One logical page per PDF page, nothing nested.

The imposed layout is why naive coordinate extraction fails.  ``pypdf`` reports
text positions inside the *form's* own coordinate space, and the page-level
matrix that shrinks and moves the form is not applied, so all four panels of a
sheet collapse onto identical coordinates.

De-imposing removes the problem entirely: once each form is its own full-size
page, the form space *is* the page space and reported coordinates are correct
with no matrix arithmetic anywhere else in the codebase.

:func:`to_single_pages` is the entry point and covers both layouts — it splits
an imposed file and passes an already-full-size one through untouched, so the
rest of the pipeline never asks which kind it was handed.  The layout is decided
by inspecting the file (:func:`is_imposed`), never by a per-file setting: the
library is large and growing, and a setting is one more thing to get wrong.

Panels are emitted in the source file's own imposition order — top row before
bottom, left to right within a row — so page 1 of the returned list is the
plan's cover.  That order was read off the folio numbers printed inside the
document, not assumed from the language's reading direction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
)

# Share of the sheet a form must cover to count as a page panel rather than a
# logo or a decorative rule.  Four-up panels each cover about a quarter.
_MIN_PANEL_AREA_SHARE = 0.15

# Matches "a b c d e f cm" optionally followed by "/Name Do" in a content stream.
_PLACEMENT = re.compile(
    rb"([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+cm"
    rb"(?:\s*/(\w+)\s+Do)?"
)


@dataclass(frozen=True)
class Placement:
    """Where one form sits on its sheet, in PDF user space."""

    name: str
    scale_x: float
    scale_y: float
    offset_x: float
    offset_y: float

    @property
    def reading_key(self) -> tuple[float, float]:
        """Sort key for panel order: top row first, then left to right.

        Verified against the printed folio numbers inside the source file — the
        association's imposition runs left-to-right even though the document is
        Arabic, so do not "correct" this to right-to-left.
        """
        return (-self.offset_y, self.offset_x)


def find_placements(page: PageObject) -> list[Placement]:
    """Return every Form XObject drawn on ``page``, in reading order."""
    contents = page.get_contents()
    if contents is None:
        return []

    placements = []
    for match in _PLACEMENT.finditer(contents.get_data()):
        name = match.group(7)
        if name is None:
            continue
        a, b, c, d, e, f = (float(v) for v in match.groups()[:6])
        placements.append(
            Placement(
                name=name.decode("ascii"),
                scale_x=a,
                scale_y=d,
                offset_x=e,
                offset_y=f,
            )
        )
    return sorted(placements, key=lambda p: p.reading_key)


def _page_xobjects(page: PageObject) -> DictionaryObject:
    resources = page.get("/Resources")
    if resources is None:
        return DictionaryObject()
    resources = resources.get_object()
    xobjects = resources.get("/XObject")
    return DictionaryObject() if xobjects is None else xobjects.get_object()


def panel_placements(page: PageObject) -> list[Placement]:
    """Placements large enough to be page panels, in reading order.

    A form is a panel only if it covers a real share of the sheet.  Templates
    also draw logos and decorations as Form XObjects, and those are placed at a
    small scale like a panel is — size is what separates them.

    Images are skipped outright.  ``Do`` paints images and forms alike, and the
    full-size templates draw their letterhead as one, so subtype is checked
    before the dictionary is read as a form.
    """
    xobjects = _page_xobjects(page)
    sheet_area = float(page.mediabox.width) * float(page.mediabox.height)
    if sheet_area <= 0:
        return []

    panels = []
    for placement in find_placements(page):
        form_ref = xobjects.get(f"/{placement.name}")
        if form_ref is None:
            continue
        form = form_ref.get_object()
        if form.get("/Subtype") != "/Form":
            continue
        bbox = [float(v) for v in form["/BBox"]]
        drawn = abs(placement.scale_x * (bbox[2] - bbox[0])) * abs(
            placement.scale_y * (bbox[3] - bbox[1])
        )
        if drawn / sheet_area >= _MIN_PANEL_AREA_SHARE:
            panels.append(placement)
    return panels


def is_imposed(reader: PdfReader) -> bool:
    """Whether the file nests several pages onto each sheet.

    True when any sheet carries more than one panel, or carries a single panel
    that has been scaled down — a lone shrunken panel is still a page inside a
    form, so its coordinates need the same unwrapping.
    """
    for page in reader.pages:
        panels = panel_placements(page)
        if len(panels) > 1:
            return True
        if panels and (abs(panels[0].scale_x) < 0.99 or abs(panels[0].scale_y) < 0.99):
            return True
    return False


def to_single_pages(reader: PdfReader) -> PdfWriter:
    """One full-size page per logical plan page, whatever the source layout.

    Imposed files are split; files that are already one page per page are
    copied through unchanged rather than rebuilt, so a template that needs no
    work is not put at risk of being altered by the code that fixes one.
    """
    if not is_imposed(reader):
        writer = PdfWriter()
        writer.append(reader)
        return writer
    return deimpose(reader)


def deimpose(reader: PdfReader) -> PdfWriter:
    """Split an imposed plan into one full-size page per panel.

    Each panel is re-drawn at its natural size — identity matrix, no scaling —
    so nothing is resampled and text stays vector.
    """
    writer = PdfWriter()

    for page in reader.pages:
        xobjects = _page_xobjects(page)
        for placement in panel_placements(page):
            form_ref = xobjects.get(f"/{placement.name}")
            if form_ref is None:
                continue
            form = form_ref.get_object()

            bbox = [float(v) for v in form["/BBox"]]
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]

            new_page = PageObject.create_blank_page(width=width, height=height)
            new_page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/XObject"): DictionaryObject(
                        {NameObject("/Form0"): form_ref}
                    )
                }
            )

            stream = DecodedStreamObject()
            stream.set_data(
                f"q 1 0 0 1 {-bbox[0]} {-bbox[1]} cm /Form0 Do Q".encode("ascii")
            )
            new_page[NameObject("/Contents")] = writer._add_object(stream)

            writer.add_page(new_page)

    return writer


def write_single_pages(source: str, destination: str) -> int:
    """Write ``source`` to ``destination`` as full-size pages; returns the count."""
    writer = to_single_pages(PdfReader(source))
    with open(destination, "wb") as handle:
        writer.write(handle)
    return len(writer.pages)
