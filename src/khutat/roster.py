"""Turning a teacher's Injaz export into one filled plan per student.

The association's Injaz system exports a teacher's class as a spreadsheet whose
first column carries two things at once — the student's name and her plan code,
separated by a run of spaces:

    فاطمة عبدالله الشمري          4 - 3

The remaining columns are the web page's own buttons ("غائب", "جديد",
"الانضباط") serialised as text, and carry nothing.

The code reads level first, curriculum second: ``4 - 3`` is level 4 of
curriculum 3.  Some students are on the recitation track instead, written
``تلاوة-1``, which has no numeric level.

Two details of the export shape the reader:

* **The code is found by its pattern, not by the gap.**  Splitting on runs of
  whitespace looks obvious and is wrong: several names contain double spaces,
  so ``منال عبدالعزيز ناصر القحطاني`` comes back truncated.  Matching the code
  at the end of the string leaves the name whole however it is spaced.
* **The file is read without openpyxl.**  Injaz writes a stylesheet that
  openpyxl refuses to load, and the cells are inline strings rather than a
  shared table, so the sheet XML is read directly.

Students whose template has no usable source are skipped and named.  Curriculum
5 and the recitation track have no digital plans at all today, so a teacher
would otherwise be left wondering which of her class came out and which did
not.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
import urllib.error
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader

from .catalogue import TemplateUnavailable, catalogue, ensure_template
from .fill import fill_template
from .imposition import impose, to_single_pages

_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# The plan code sits at the end: a level (a number, or the word تلاوة) then the
# curriculum number.
_CODE = re.compile(r"(?:([0-9]+)|(تلاوة))\s*[-–—]\s*([0-9]+)\s*$")

# Characters a filename cannot carry, plus the separators that would nest it.
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass(frozen=True)
class Student:
    """One row of the export: who she is and which plan she is on."""

    name: str
    manhaj: int
    level: int | None
    track: str  # "حفظ" or "تلاوة"

    @property
    def plan_label(self) -> str:
        where = self.level if self.level is not None else self.track
        return f"منهج {self.manhaj} مستوى {where}"

    @property
    def safe_filename(self) -> str:
        cleaned = _UNSAFE.sub("_", self.name).strip() or "طالبة"
        return f"{cleaned}.pdf"


def _cell_texts(path: Path) -> list[str]:
    """First-column text of every row, however the workbook stores strings."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()

        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall(f"{_SHEET_NS}si"):
                shared.append("".join(t.text or "" for t in item.iter(f"{_SHEET_NS}t")))

        sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))
        if not sheets:
            return []
        sheet = ElementTree.fromstring(archive.read(sheets[0]))

    texts: list[str] = []
    for row in sheet.iter(f"{_SHEET_NS}row"):
        cell = row.find(f"{_SHEET_NS}c")
        if cell is None:
            continue
        inline = cell.find(f"{_SHEET_NS}is/{_SHEET_NS}t")
        if inline is not None:
            texts.append(inline.text or "")
            continue
        value = cell.find(f"{_SHEET_NS}v")
        if value is None or value.text is None:
            continue
        if cell.get("t") == "s" and shared:
            index = int(value.text)
            texts.append(shared[index] if index < len(shared) else "")
        else:
            texts.append(value.text)
    return texts


def parse_row(raw: str) -> Student | None:
    """Read one export row into a :class:`Student`, or ``None`` if it is not one."""
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw)).strip()
    if not text:
        return None

    match = _CODE.search(text.translate(_ARABIC_DIGITS))
    if match is None:
        return None

    # Offsets survive the digit translation because it is one-to-one.
    name = text[: match.start()].strip()
    if not name:
        return None

    level = int(match.group(1)) if match.group(1) else None
    return Student(
        name=name,
        manhaj=int(match.group(3)),
        level=level,
        track="حفظ" if level is not None else "تلاوة",
    )


def read_roster(path: Path) -> list[Student]:
    """Every student in an Injaz export, in sheet order."""
    students = []
    for raw in _cell_texts(Path(path)):
        student = parse_row(raw)
        if student is not None:
            students.append(student)
    return students


@dataclass
class RosterResult:
    """What one batch produced, and what it could not."""

    written: list[tuple[Student, Path]] = field(default_factory=list)
    skipped: list[tuple[Student, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.written) + len(self.skipped)


def _compact_sheets(source: Path) -> None:
    """Rewrite ``source`` in place as four pages to a sheet."""
    pages = list(to_single_pages(PdfReader(str(source))).pages)
    writer = impose(pages)
    with open(source, "wb") as handle:
        writer.write(handle)


def generate(
    students: list[Student],
    constants: dict[str, str],
    out_dir: Path,
    font_path: Path,
    cache_dir: Path | None = None,
    compact: bool = False,
) -> RosterResult:
    """Fill one plan per student, fetching each template at most once.

    ``constants`` are the values that do not vary across the class — the
    teacher's name, her halaqah, the centre — and the student's own name is
    added per plan.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    plans = catalogue() if cache_dir is None else catalogue(cache_dir)
    kwargs = {} if cache_dir is None else {"cache_dir": cache_dir}

    result = RosterResult()
    templates: dict[tuple[int, int | None], Path | str] = {}

    for student in students:
        key = (student.manhaj, student.level)
        if key not in templates:
            if student.level is None:
                templates[key] = f"{student.track}: لا خطط رقمية"
            else:
                try:
                    templates[key] = ensure_template(
                        student.manhaj, student.level, plans, **kwargs
                    )
                except (TemplateUnavailable, urllib.error.URLError) as error:
                    templates[key] = str(error)

        template = templates[key]
        if isinstance(template, str):
            result.skipped.append((student, template))
            continue

        destination = out_dir / student.safe_filename
        values = dict(constants)
        values["اسم الطالب"] = student.name
        fill_template(template, values, destination, font_path)
        if compact:
            _compact_sheets(destination)
        result.written.append((student, destination))

    return result


def format_result(result: RosterResult) -> str:
    lines = [f"وُلّدت {len(result.written)} خطة من {result.total} طالبة"]
    if result.skipped:
        lines.append("")
        lines.append("لم تُولَّد:")
        for student, reason in result.skipped:
            lines.append(f"  • {student.name} — {student.plan_label} — {reason}")
    return "\n".join(lines)


def _parse_assignment(raw: str) -> tuple[str, str]:
    label, separator, value = raw.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError(f"Expected LABEL=VALUE, got {raw!r}")
    return label.strip(), value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m khutat.roster",
        description="Generate one filled plan per student from an Injaz export.",
    )
    parser.add_argument("roster", type=Path, help="the Injaz .xlsx export")
    parser.add_argument("output", type=Path, help="directory to write the plans into")
    parser.add_argument("--font", required=True, type=Path, help="Arabic TTF to draw with")
    parser.add_argument(
        "--set",
        dest="assignments",
        metavar="LABEL=VALUE",
        action="append",
        default=[],
        type=_parse_assignment,
        help="a value shared by the whole class, repeatable",
    )
    parser.add_argument(
        "--size",
        choices=("مكبرة", "مصغرة", "full", "compact"),
        default="مكبرة",
        help="مكبرة: one page per sheet. مصغرة: four pages per sheet",
    )
    parser.add_argument("--list", action="store_true", help="list students and exit")
    args = parser.parse_args(argv)

    if not args.roster.is_file():
        print(f"لا ملف عند {args.roster}", file=sys.stderr)
        return 2

    students = read_roster(args.roster)
    if not students:
        print("لم يُعثر على طالبات في الملف", file=sys.stderr)
        return 2

    if args.list:
        for student in students:
            print(f"{student.name}  —  {student.plan_label}")
        print(f"\n{len(students)} طالبة")
        return 0

    result = generate(
        students,
        dict(args.assignments),
        args.output,
        args.font,
        compact=args.size in ("مصغرة", "compact"),
    )
    print(format_result(result))

    # Skipped students need the teacher's attention, so say so in the status.
    return 1 if result.skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
