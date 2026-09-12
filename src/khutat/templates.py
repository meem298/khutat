"""Surveying the template library.

The association's library is large and grows every term, and every template is
a file someone produced by hand.  Detection is driven by labels and drawn
geometry rather than per-file coordinates precisely so that it survives that
variety — but "should survive" is a claim, and this module is how the claim is
checked.

It walks a directory, runs the real pipeline over each template, and reports
what came back.  Two things make it worth keeping in the codebase rather than
re-writing as a throwaway script each time:

* A change to detection can be measured against the whole library in one run,
  so a fix for one template cannot quietly break the rest.
* ``--overlay`` draws the detected boxes onto a copy of each template.  Field
  counts alone cannot tell a correct box from one that landed on the wrong
  cell; only looking can, and looking needs to be one command.

A page with no fields is normal — continuation tables and the grading rubric
carry no student header.  A *template* with no fields anywhere is not, and is
reported as suspect.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from pypdf import PageObject, PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from .detect import Field, detect_fields
from .imposition import is_imposed, to_single_pages


@dataclass(frozen=True)
class TemplateReport:
    """What the pipeline made of one template."""

    path: Path
    imposed: bool
    source_pages: int
    fields_by_page: tuple[tuple[Field, ...], ...] = ()

    @property
    def logical_pages(self) -> int:
        return len(self.fields_by_page)

    @property
    def fields(self) -> tuple[Field, ...]:
        return tuple(f for page in self.fields_by_page for f in page)

    @property
    def field_count(self) -> int:
        return sum(len(page) for page in self.fields_by_page)

    @property
    def pages_with_fields(self) -> int:
        return sum(1 for page in self.fields_by_page if page)

    @property
    def is_suspect(self) -> bool:
        """No field anywhere means detection found nothing to fill."""
        return self.field_count == 0

    @property
    def layout(self) -> str:
        return "imposed" if self.imposed else "full-size"


def inspect_template(path: Path) -> TemplateReport:
    """Run the pipeline over one template and collect what it found."""
    reader = PdfReader(str(path))
    imposed = is_imposed(reader)
    pages = to_single_pages(reader).pages

    return TemplateReport(
        path=path,
        imposed=imposed,
        source_pages=len(reader.pages),
        fields_by_page=tuple(
            tuple(detect_fields(page, index)) for index, page in enumerate(pages)
        ),
    )


def inspect_library(directory: Path) -> list[TemplateReport]:
    """Inspect every PDF in ``directory``, in filename order."""
    return [inspect_template(p) for p in sorted(directory.glob("*.pdf"))]


def _overlay_page(page: PageObject, fields: list[Field], scratch: Path) -> None:
    """Stamp the detected boxes onto ``page`` in place."""
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)

    marks = canvas.Canvas(str(scratch), pagesize=(width, height))
    marks.setLineWidth(1.5)
    for field in fields:
        marks.setStrokeColorRGB(0.85, 0.1, 0.1)
        marks.rect(
            field.left,
            field.bottom,
            field.width,
            field.top - field.bottom,
            stroke=1,
            fill=0,
        )
        # The label is drawn in Latin because the annotation is a debugging aid
        # and reportlab's built-in fonts cannot shape Arabic; the box position
        # is what is being checked, not the caption.
        marks.setFillColorRGB(0.85, 0.1, 0.1)
        marks.setFont("Helvetica", 6)
        marks.drawString(field.left, field.top + 2, f"{field.width:.0f}pt")
    marks.save()

    page.merge_page(PdfReader(str(scratch)).pages[0])


def write_overlay(path: Path, destination: Path, scratch_dir: Path) -> int:
    """Write a copy of ``path`` with every detected field outlined.

    Returns the number of boxes drawn.  Use it to check by eye that a box sits
    on the cell a teacher would write in, which a field count cannot show.
    """
    pages = to_single_pages(PdfReader(str(path))).pages
    writer = PdfWriter()
    drawn = 0

    scratch_dir.mkdir(parents=True, exist_ok=True)
    scratch = scratch_dir / f"{path.stem}-marks.pdf"

    for index, page in enumerate(pages):
        fields = detect_fields(page, index)
        drawn += len(fields)
        if fields:
            _overlay_page(page, fields, scratch)
        writer.add_page(page)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "wb") as handle:
        writer.write(handle)
    return drawn


def format_report(reports: list[TemplateReport]) -> str:
    """A one-line-per-template summary, plus a total."""
    if not reports:
        return "No templates found."

    name_width = max(len(r.path.name) for r in reports)
    lines = []
    for report in reports:
        flag = "  SUSPECT: no fields detected" if report.is_suspect else ""
        lines.append(
            f"{report.path.name:<{name_width}}  "
            f"{report.layout:<9}  "
            f"{report.source_pages:>2} sheet -> {report.logical_pages:>2} page  "
            f"{report.field_count:>3} fields "
            f"on {report.pages_with_fields}/{report.logical_pages} pages"
            f"{flag}"
        )

    total = sum(r.field_count for r in reports)
    suspect = sum(1 for r in reports if r.is_suspect)
    lines.append("")
    lines.append(f"{len(reports)} templates, {total} fields, {suspect} suspect")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m khutat.templates",
        description="Survey the template library and report what detection finds.",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default="templates",
        type=Path,
        help="directory of template PDFs (default: templates)",
    )
    parser.add_argument(
        "--overlay",
        metavar="DIR",
        type=Path,
        help="also write a copy of each template with detected boxes outlined",
    )
    args = parser.parse_args(argv)

    if not args.directory.is_dir():
        print(f"Not a directory: {args.directory}", file=sys.stderr)
        return 2

    reports = inspect_library(args.directory)
    print(format_report(reports))

    if args.overlay:
        print()
        for report in reports:
            destination = args.overlay / f"{report.path.stem}-detected.pdf"
            write_overlay(report.path, destination, args.overlay / ".marks")
            print(f"wrote {destination}")

    # A suspect template is a real finding, so make it visible to a caller that
    # only checks the exit status.
    return 1 if any(r.is_suspect for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
