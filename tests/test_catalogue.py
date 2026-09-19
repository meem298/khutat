"""Recognising plans by name, and choosing between sources.

The names below are the real spellings found in the association's working
folder: every separator, digit style and stray prefix it actually uses.
"""

from __future__ import annotations

import pytest

from khutat.catalogue import Plan, candidates, parse_plan_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("منهج 6 مستوى 9", (6, 9)),
        ("منهج 6 مستوى 3.docx", (6, 3)),
        ("منهج 6 مستوى5", (6, 5)),
        ("منهج6 مستوى 6 docx", (6, 6)),
        ("منهج6مستوى28.docx", (6, 28)),
        ("منهج6_مستوى 12.pdf", (6, 12)),
        ("منهج6-13.pdf", (6, 13)),
        ("منهج 6-15..pdf", (6, 15)),
        ("منهج6 -4.pdf", (6, 4)),
        ("منهج٦ مستوى٣٠.docx", (6, 30)),
        ("نسخة منهج 6 مستوى 16", (6, 16)),
        ("منهج 6 مستوى 26 .docx", (6, 26)),
    ],
)
def test_parses_every_spelling_in_the_working_folder(name, expected):
    assert parse_plan_name(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "بيانات توزيع مستويات المنهج.docx",
        "منهج الحفظ والتلاوة لمدارس التحفيظ .jpg",
        "نموذج خطة مفرغ.pdf",
        "منهج تعاهد 3- الدورات المكثفة",
    ],
)
def test_ignores_names_that_are_not_plans(name):
    assert parse_plan_name(name) is None


def _plan(kind: str, source: str) -> Plan:
    return Plan(manhaj=6, level=5, kind=kind, source=source, url="u", title="t")


def test_usable_formats_come_first():
    found = candidates(
        [_plan("docx", "drive"), _plan("gdoc", "drive"), _plan("pdf", "drive")], 6, 5
    )
    assert [p.kind for p in found] == ["pdf", "gdoc", "docx"]


def test_the_site_wins_a_tie():
    found = candidates([_plan("pdf", "drive"), _plan("pdf", "site")], 6, 5)
    assert found[0].source == "site"


def test_other_plans_are_not_candidates():
    other = Plan(manhaj=5, level=6, kind="pdf", source="site", url="u", title="t")
    assert candidates([other], 6, 5) == []


def test_usability_follows_format():
    assert _plan("pdf", "site").usable
    assert _plan("gdoc", "drive").usable
    assert not _plan("docx", "drive").usable
