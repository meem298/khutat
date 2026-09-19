"""Shared fixtures.

Every test here runs offline and uses invented names.  The two templates in
``templates/`` are the only real documents involved, and they are the
association's own published plans with blank headers.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
FONT = ROOT / "assets" / "fonts" / "NotoNaskhArabic-Regular.ttf"

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


@pytest.fixture
def make_xlsx(tmp_path):
    """Build a minimal workbook whose first column holds ``rows``.

    ``shared=False`` stores cells as inline strings, which is how Injaz writes
    them; ``shared=True`` uses a shared-strings table, which is how Excel
    writes them if a teacher re-saves the export.  The reader must take both.
    """

    def build(rows: list[str], shared: bool = False, name: str = "roster.xlsx") -> Path:
        path = tmp_path / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            if shared:
                strings = "".join(f"<si><t>{r}</t></si>" for r in rows)
                archive.writestr(
                    "xl/sharedStrings.xml",
                    f'<sst xmlns="{_NS}">{strings}</sst>',
                )
                cells = "".join(
                    f'<row r="{i}"><c r="A{i}" t="s"><v>{i - 1}</v></c></row>'
                    for i in range(1, len(rows) + 1)
                )
            else:
                cells = "".join(
                    f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>{r}</t></is></c></row>'
                    for i, r in enumerate(rows, 1)
                )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                f'<worksheet xmlns="{_NS}"><sheetData>{cells}</sheetData></worksheet>',
            )
        return path

    return build
