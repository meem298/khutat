"""Imposition and field detection against the two templates in the repository.

The field counts are the known-good values from CLAUDE.md.  A count cannot show
that a box landed in the right cell — that still needs a look at a rendered
page — but a change that makes a count move has certainly broken something.
"""

from __future__ import annotations

from collections import Counter

import pytest
from pypdf import PdfReader

from conftest import TEMPLATES
from khutat.detect import compact, detect_fields, match_label, normalise
from khutat.imposition import impose, is_imposed, to_single_pages

IMPOSED = TEMPLATES / "manhaj-3-level-6.pdf"
FULL = TEMPLATES / "manhaj-6-level-9.pdf"


def _fields(path):
    pages = to_single_pages(PdfReader(str(path))).pages
    return [f for i, page in enumerate(pages) for f in detect_fields(page, i)]


# ── Layout ──────────────────────────────────────────────────────────────────


def test_layouts_are_recognised():
    assert is_imposed(PdfReader(str(IMPOSED)))
    assert not is_imposed(PdfReader(str(FULL)))


def test_imposed_template_splits_into_eight_pages():
    assert len(to_single_pages(PdfReader(str(IMPOSED))).pages) == 8


def test_full_size_template_passes_through_unchanged():
    assert len(to_single_pages(PdfReader(str(FULL))).pages) == 6


def test_impose_puts_four_pages_on_a_sheet_of_the_same_size():
    pages = list(to_single_pages(PdfReader(str(IMPOSED))).pages)
    sheets = impose(pages).pages
    assert len(sheets) == 2
    assert float(sheets[0].mediabox.width) == pytest.approx(float(pages[0].mediabox.width))


# ── Detection ───────────────────────────────────────────────────────────────


def test_imposed_template_field_count():
    assert len(_fields(IMPOSED)) == 21


def test_full_size_template_field_count():
    assert len(_fields(FULL)) == 19


def test_wrapped_labels_are_found():
    # On the full-size template "الحلقة" and "المجمع/الدار" wrap onto two
    # stacked line-boxes; before nested cells were handled, only two of the
    # four header fields on pages 3-5 were found.
    labels = Counter(f.label for f in _fields(FULL))
    assert labels["الحلقة"] >= 4
    assert labels["المجمع/الدار"] >= 4


def test_fields_have_room_to_write_in():
    for field in _fields(FULL):
        assert field.width > 20
        assert field.top > field.bottom


# ── Label matching ──────────────────────────────────────────────────────────


def test_presentation_forms_fold_to_base_letters():
    # Google Docs exports store shaped glyphs; they must match plain text.
    assert compact("اﺳم اﻟطﺎﻟب") == compact("اسم الطالب")


def test_spelling_variants_fold():
    assert normalise("الحلقـــة") == normalise("الحلقه")


def test_longer_label_wins():
    # "المعلم" is inside "اسم المعلم"; the longer one must be chosen.
    assert match_label("اسم المعلم") == "اسم المعلم"
    assert match_label("المعلم") == "المعلم"


def test_unrelated_text_is_not_a_label():
    assert match_label("الأسبوع الأول") is None
