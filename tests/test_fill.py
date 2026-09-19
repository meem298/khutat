"""Shaping Arabic and writing it into a template."""

from __future__ import annotations

import unicodedata

import pytest
from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics

from conftest import FONT, TEMPLATES
from khutat.detect import Field
from khutat.fill import _shorten_to_fit, fill_template, fit_size, register_font, shape


@pytest.fixture(scope="module")
def font():
    return register_font(FONT)


def _is_presentation_form(char: str) -> bool:
    return 0xFB50 <= ord(char) <= 0xFDFF or 0xFE70 <= ord(char) <= 0xFEFF


def test_shaping_produces_joined_forms():
    shaped = shape("مها")
    assert shaped != "مها"
    assert all(_is_presentation_form(c) for c in shaped)


def test_shaping_reverses_into_visual_order():
    # Visual order puts the last logical letter first; NFKC folds each form
    # back to its base letter so the comparison is about order alone.
    folded = unicodedata.normalize("NFKC", shape("مها"))
    assert folded == "اهم"


def test_digits_keep_their_own_order():
    assert "12" in shape("دار 12 الفيحاء")


def _field(width: float, height: float = 40) -> Field:
    return Field(label="اسم الطالب", page_index=0, left=0, right=width,
                 bottom=0, top=height, size=18)


def test_a_short_name_keeps_its_size(font):
    assert fit_size(shape("هدى"), font, _field(200), 18) == 18


def test_a_short_row_caps_the_size(font):
    # Width is not the only limit: a value sized to the width alone can still
    # be taller than its row, so the size is capped at 72% of the height.
    assert fit_size(shape("هدى"), font, _field(200, height=20), 18) == pytest.approx(14.4)


def test_a_long_name_shrinks_to_fit(font):
    field = _field(80)
    display = shape("منال عبدالعزيز ناصر القحطاني")
    size = fit_size(display, font, field, 18)
    assert size < 18
    assert pdfmetrics.stringWidth(display, font, size) <= field.width + 0.01 or size == 7.0


def test_truncation_cuts_the_end_of_the_name_not_the_start(font):
    field = _field(40)
    kept = _shorten_to_fit("منال عبدالعزيز ناصر القحطاني", font, field, 7.0)
    assert kept.startswith("منال")
    assert kept.endswith("…")
    assert pdfmetrics.stringWidth(shape(kept), font, 7.0) <= field.width


def test_fill_writes_every_matching_field(tmp_path):
    out = tmp_path / "plan.pdf"
    result = fill_template(
        TEMPLATES / "manhaj-6-level-9.pdf",
        {"اسم الطالب": "فاطمة عبدالله الشمري", "الحلقة": "حلقة النور"},
        out,
        FONT,
    )
    assert result.fields_seen == 19
    assert result.written > 0
    assert result.unused_labels == ()
    assert len(PdfReader(str(out)).pages) == 6


def test_a_label_that_matches_nothing_is_reported(tmp_path):
    result = fill_template(
        TEMPLATES / "manhaj-6-level-9.pdf",
        {"اسم الطالبة": "فاطمة"},
        tmp_path / "plan.pdf",
        FONT,
    )
    assert result.written == 0
    assert result.unused_labels == ("اسم الطالبة",)
