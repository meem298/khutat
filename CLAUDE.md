# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Quran-memorisation teachers at the Unaizah association hand every student a
printed daily study plan. The association publishes those plans as PDF
templates with a blank header — student name, halaqah, centre, teacher — which
teachers currently fill in by hand, once per student per term.

This tool reads a teacher's class export, fetches each student's template, and
writes the filled plans. The whole job is one command (`khutat.roster`).

The user is a beginner programmer and the repository's owner; she is learning
from this project as well as using it. Explain *why* a change is shaped the way
it is, not just what it does.

## Setup

```bash
.venv/bin/python -m pip install -e .
```

The package must be installed editable or `python -m khutat.*` will not
resolve. Always invoke `.venv/bin/python` explicitly.

## Commands

```bash
# Survey every template in a directory: layout, page counts, fields found
.venv/bin/python -m khutat.templates templates

# Same, plus a copy of each template with the detected boxes outlined
.venv/bin/python -m khutat.templates templates --overlay out/detected

# Index the association's plan sources (network); --refresh re-reads them
.venv/bin/python -m khutat.catalogue --refresh
.venv/bin/python -m khutat.catalogue --get 1 4     # fetch one plan, print its path

# Fill one template by hand
.venv/bin/python -m khutat.fill TEMPLATE OUT.pdf \
  --font assets/fonts/NotoNaskhArabic-Regular.ttf \
  --set "اسم الطالب=فاطمة الشمري"

# The real entry point: one plan per student from an Injaz export
.venv/bin/python -m khutat.roster ROSTER.xlsx out/plans \
  --font assets/fonts/NotoNaskhArabic-Regular.ttf \
  --size مصغرة \
  --set "الحلقة=حلقة النور" --set "اسم المعلم=نورة القحطاني"
```

`--size مكبرة` (default) is one page per sheet; `مصغرة` is four to a sheet.

```bash
# The page teachers actually use; --host 0.0.0.0 only when hosted
.venv/bin/python -m khutat.web
```

## Verifying a change

**There is no test suite.** `pytest` is in the dev extra and `tests/` is empty.
Verification is done by running the pipeline over real templates and looking:

1. `khutat.templates templates` and `khutat.templates .cache/khutat/templates` —
   field counts must not drop. Known-good: the imposed template yields 21
   fields, the full-size one 19, each plan downloaded from the site 21.
2. `--overlay`, then `pdftoppm -png -r 80 FILE.pdf OUT` and read the image.
   **A field count never proves correctness** — it cannot distinguish a box in
   the right cell from one that landed on the neighbour. Look at the page.
3. For fill changes, also try a short name, an over-long one, a value with
   digits, and a mixed Arabic/Latin value.

Never claim a detection or fill change works without having looked at a
rendered page.

## Pipeline

```
roster.py     reads the Injaz .xlsx, one Student per row
  └─ catalogue.py   finds and downloads that student's template (cached)
      └─ imposition.to_single_pages   normalises to one logical page per page
          └─ detect.py                finds the header fields
              └─ fill.py              draws the values
                  └─ imposition.impose   optional, four pages to a sheet
```

Two ordering rules are load-bearing:

- **De-impose before detecting.** Coordinates inside a nested form collapse
  onto each other otherwise.
- **Impose last.** `impose` inlines content rather than nesting forms, so its
  output is a finished artefact that `is_imposed` will not recognise and
  cannot be split again.

## What is non-obvious

**Templates arrive in two layouts.** Some are imposed — one A4 sheet carrying
four half-scale pages as Form XObjects; others are already one page per page.
`is_imposed` decides by inspecting the file, never by configuration: the
library is ~120 plans and growing, and a per-file setting is one more thing to
get wrong.

**Coordinates mean nothing without the matrix.** `detect.read_cells` walks the
content stream tracking `q`/`Q`/`cm` and maps every rectangle's corners.
The association's own PDFs set no transformation at all, so raw coordinates
happened to work for years of them — but a plan converted from Word flips the
y axis and scales by 0.75. Never reintroduce a regex scan for `re`.

**Arabic needs two passes before drawing.** `arabic_reshaper.reshape` then
`bidi.get_display`, in that order. After them the string is presentation-form
glyphs in visual order: draw it or measure it, nothing else. Every text
operation — trimming, truncating, normalising — belongs *before* the reshape.
Cutting a shaped string removes the name's first letters, not its last.

**Label matching folds presentation forms.** `detect.normalise` applies NFKC
because some producers store shaped glyphs rather than abstract letters, so
`"اسم الطالب"` and `"اﺳم اﻟطﺎﻟب"` must compare equal.

**Fields are found by geometry, never by coordinates.** A label is located by
text, its cell by the rectangles the template draws, and its value cell is the
one to its left. Cells nest — a label too wide for its column wraps onto two
stacked line-boxes — so a chunk counts towards every cell containing it and
label cells are tried tightest-first. A value cell that already holds text is
pre-filled by the association and must be left alone.

**The font is always a parameter.** macOS ships Arabic faces that work for
development but cannot be redistributed. `assets/fonts/NotoNaskhArabic-Regular.ttf`
(SIL OFL 1.1, with its `OFL.txt`) is bundled, but nothing defaults to it.

## Data sources and their state

- **`utq.org.sa/mnahig/`** — curricula 1–4, 70 plans, all PDF. Clean.
- **A shared Drive folder ("توزيع المستويات")** — curriculum 6, which the site
  does not publish. A workspace, not a library: names are inconsistent
  (`منهج 6 مستوى 9`, `منهج6-13.pdf`, `منهج٦ مستوى٣٠.docx`, `نسخة منهج 6 مستوى 16`)
  and most files are Word. Google Docs entries export to PDF by URL and are
  usable; bare `.docx` uploads need the Drive API and are refused.
- **Curriculum 5 and the recitation (تلاوة) track have no digital plans.**
  Students on them are skipped by name. This is not a bug to fix in code.

**The Injaz export** packs the student's name and plan code into column A
separated by a run of spaces: `فاطمة عبدالله الشمري    4 - 3` means level 4
of curriculum 3 — level first. Match the code by its pattern at the end of the
string; splitting on whitespace truncates names that contain double spaces.
Injaz also writes a stylesheet `openpyxl` refuses to load and stores cells as
inline strings, which is why the sheet XML is read directly.

## The web page is stateless on purpose

`web.py` began as a convenience for one teacher on her own Mac, where a
settings file and a fixed output folder were fine. Both became faults the
moment a second teacher could reach it, and the fix is the design:

- **Her details live in her browser** (`localStorage`), never on the server. A
  server-side settings file is one file: the last teacher to type would
  overwrite everyone, and the next would find another woman's halaqah and name
  prefilled — and could generate a whole class under the wrong teacher's name.
- **Each request gets its own temp directory**, zipped and removed in a
  `finally`. A fixed output folder is one folder, and two teachers generating
  at once would mix their students into it.
- **The finished zip waits in memory under a single-use token** with a TTL, so
  a class list never reaches the server's disk beyond the temp directory that
  produced it. Request logging is disabled for the same reason.

Do not add a server-side store, a fixed output path, or an endpoint that runs
a command on the host — an earlier `/api/reveal` called `open` and had to go.

**Never put a real student's name in this repository.** Examples in docs and
docstrings use invented ones (فاطمة عبدالله الشمري); the roster that exercises
the real pipeline stays outside the repo, and `out/`, `.cache/` and `sandbox/`
are ignored.

## Conventions

- **Exit status 1 means something needs a human.** A template where detection
  found nothing, a student who was skipped, a `--set` label that matched no
  field. Keep new failure modes visible this way rather than silent.
- **Commit messages explain why.** Imperative subject line, then a body giving
  the reasoning and the verification performed — the code already shows what
  changed. Commits are authored by the repository owner with a
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` trailer.
- `out/`, `.cache/`, and `sandbox/` are ignored; `sandbox/` is the owner's
  scratch space for experiments.
