"""Reading an Injaz export.

The first group exists because of a real failure: the code was once read level
first, every student in a class got a real but wrong plan, and nothing
signalled it.  These tests pin the order down.
"""

from __future__ import annotations

import zipfile

import pytest

from khutat.roster import _read_member, parse_row, read_roster

GAP = "          "  # the run of spaces Injaz puts between name and code


# ── The plan code: curriculum first, level second ───────────────────────────


def test_first_number_is_the_curriculum():
    student = parse_row(f"فاطمة عبدالله الشمري{GAP}4 - 1")
    assert (student.manhaj, student.level) == (4, 1)
    assert student.plan_label == "منهج 4 مستوى 1"


def test_order_is_not_symmetric():
    # 6 - 5 and 5 - 6 are different plans; curriculum 5 has no source at all.
    student = parse_row(f"ريم سعد الدوسري{GAP}6 - 5")
    assert (student.manhaj, student.level) == (6, 5)


def test_recitation_sits_in_the_curriculum_slot():
    student = parse_row(f"جواهر ناصر السبيعي{GAP}تلاوة-1")
    assert student.manhaj is None
    assert student.level == 1
    assert student.track == "تلاوة"
    assert student.plan_label == "تلاوة مستوى 1"


@pytest.mark.parametrize("code", ["4-3", "4 - 3", "4 – 3", "4—3", "٤ - ٣"])
def test_separator_and_digit_variants(code):
    student = parse_row(f"لمى خالد الحربي{GAP}{code}")
    assert (student.manhaj, student.level) == (4, 3)


# ── The name ────────────────────────────────────────────────────────────────


def test_name_with_inner_double_spaces_stays_whole():
    # Splitting on runs of whitespace would cut this name at the first gap.
    student = parse_row(f"منال  عبدالعزيز   ناصر القحطاني{GAP}4 - 3")
    assert student.name == "منال عبدالعزيز ناصر القحطاني"


@pytest.mark.parametrize(
    "row", ["", "   ", "اسم الطالب", "نظام إنجاز الإلكتروني", f"{GAP}4 - 1"]
)
def test_rows_that_are_not_students(row):
    assert parse_row(row) is None


def test_filename_cannot_escape_the_output_folder():
    student = parse_row(f"../../etc/passwd{GAP}4 - 1")
    assert "/" not in student.safe_filename
    assert student.safe_filename.endswith(".pdf")


# ── The workbook ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("shared", [False, True], ids=["inline", "shared-strings"])
def test_reads_both_string_layouts(make_xlsx, shared):
    path = make_xlsx(
        [
            "نظام إنجاز الإلكتروني",
            "اسم الطالب",
            f"فاطمة عبدالله الشمري{GAP}4 - 1",
            f"جواهر ناصر السبيعي{GAP}تلاوة-1",
        ],
        shared=shared,
    )
    students = read_roster(path)
    assert [s.name for s in students] == ["فاطمة عبدالله الشمري", "جواهر ناصر السبيعي"]
    assert [(s.manhaj, s.level) for s in students] == [(4, 1), (None, 1)]


def test_refuses_a_decompression_bomb(tmp_path):
    # Small on disk, 40 MB once expanded: past the ceiling, so it must be
    # refused from the zip header without being read into memory.
    path = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"A" * (40 * 1024 * 1024))
    assert path.stat().st_size < 1024 * 1024

    with zipfile.ZipFile(path) as archive, pytest.raises(ValueError):
        _read_member(archive, "xl/worksheets/sheet1.xml")
    with pytest.raises(ValueError):
        read_roster(path)
