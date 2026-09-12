"""Finding the association's plan templates and fetching them on demand.

Templates live in two places, and neither is a tidy library:

* **The association's site** (``utq.org.sa/mnahig/``) publishes the daily plans
  for the four released memorisation curricula as PDFs on Google Drive — 70
  plans, one link per level, grouped under a heading per curriculum.
* **A shared Drive folder** holds the working files, including curriculum 6,
  which the site does not publish at all.  That folder is a workspace rather
  than a library: names are inconsistent (``منهج 6 مستوى 9``, ``منهج6-13.pdf``,
  ``منهج٦ مستوى٣٠.docx``, ``نسخة منهج 6 مستوى 16``) and most files are Word
  documents rather than PDFs.

:func:`parse_plan_name` is therefore deliberately forgiving about separators,
Arabic-Indic digits and stray prefixes, and deliberately strict about one
thing: the digit must follow the word منهج directly, so ``منهج تعاهد 3`` and
``بيانات توزيع مستويات المنهج`` are not mistaken for plans.

PDFs and Google Docs are usable; a bare Word upload is not.  Docs exports to
PDF through a plain URL, and since :mod:`khutat.detect` applies transformation
matrices those exports read correctly.  A ``.docx`` sitting in Drive has no
such export URL — converting it needs the Drive API and an account — so
:func:`ensure_template` refuses it and says which formats it found, which is a
better failure than a blank page.

Fetching is explicit and cached.  The catalogue is read from the network only
on ``--refresh``, and a template is downloaded once and reused, so a batch of
thirty students costs one download per distinct template.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from html import unescape
from pathlib import Path

SITE_URL = "https://utq.org.sa/mnahig/"

# The association's shared "توزيع المستويات" folder, which carries curriculum 6.
DRIVE_FOLDER_ID = "<KHUTAT_DRIVE_FOLDER>"

DEFAULT_CACHE = Path(".cache/khutat")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# "منهج" then its number, then the level number.  The separator between them
# may be the word مستوى, punctuation, whitespace, several of those, or nothing.
_PLAN_NAME = re.compile(r"منهج\s*([0-9]+)\s*[-_.\s]*(?:مستوى)?\s*[-_.\s]*([0-9]+)")

# Headings on the site ("المنهج رقم (٣) — خمسة أسطر يومياً") and the level
# links beneath them, matched together so document order gives the grouping.
_SITE_TOKEN = re.compile(
    r'(?is)(?:<button[^>]*class="[^"]*levels-toggle[^"]*"[^>]*>\s*<span[^>]*>(.*?)</span>)'
    r'|(?:<a\b([^>]*class="[^"]*level-link[^"]*"[^>]*)>(.*?)</a>)'
)

# Drive renders each row's name in aria-label and hides the file id in ssk.
_DRIVE_ROW = re.compile(
    r'aria-label="([^"]{3,160})"[^>]{0,240}?ssk=\'[^\']*?:([0-9A-Za-z_-]{25,44})-',
    re.S,
)


def _digits(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_ARABIC_DIGITS)


def _plain(markup: str) -> str:
    return unescape(re.sub(r"(?s)<[^>]+>", "", markup)).strip()


def parse_plan_name(text: str) -> tuple[int, int] | None:
    """Return ``(manhaj, level)`` read out of a file or link name, or ``None``."""
    normalised = re.sub(r"\s+", " ", _digits(text))
    match = _PLAN_NAME.search(normalised)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True)
class Plan:
    """One template the association publishes, and where to get it."""

    manhaj: int
    level: int
    kind: str  # "pdf", "docx" or "gdoc"
    source: str  # "site" or "drive"
    url: str
    title: str

    @property
    def usable(self) -> bool:
        """Whether a PDF can be obtained from this entry without an account.

        A Google Doc exports to PDF through a plain URL, so it counts; a Word
        upload would need the Drive API to convert, so it does not.
        """
        return self.kind in ("pdf", "gdoc")

    @property
    def filename(self) -> str:
        return f"manhaj-{self.manhaj}-level-{self.level}.pdf"


def _get(url: str, timeout: float = 120) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_site_catalogue(url: str = SITE_URL) -> list[Plan]:
    """Read the published plans off the association's curricula page."""
    html = _get(url).decode("utf-8", "replace")

    plans: list[Plan] = []
    manhaj: int | None = None
    for token in _SITE_TOKEN.finditer(html):
        if token.group(1) is not None:
            heading = _digits(_plain(token.group(1)))
            number = re.search(r"[0-9]+", heading)
            # Groups without a number ("منهج مدارس تحفيظ القرآن", "نموذج خطة
            # مفرغ") are not level-indexed plans, so drop whatever follows.
            manhaj = int(number.group()) if number else None
            continue

        if manhaj is None:
            continue
        level_text = _digits(_plain(token.group(3)))
        level = re.search(r"[0-9]+", level_text)
        href = re.search(r'href\s*=\s*"([^"]*)"', token.group(2))
        if level and href:
            plans.append(
                Plan(
                    manhaj=manhaj,
                    level=int(level.group()),
                    kind="pdf",
                    source="site",
                    url=unescape(href.group(1)),
                    title=_plain(token.group(3)),
                )
            )
    return plans


def _drive_rows(folder_id: str) -> list[tuple[str, str]]:
    """Return ``(name, id)`` for every row Drive renders in a public folder."""
    html = _get(f"https://drive.google.com/drive/folders/{folder_id}").decode(
        "utf-8", "replace"
    )
    seen: set[tuple[str, str]] = set()
    rows: list[tuple[str, str]] = []
    for match in _DRIVE_ROW.finditer(html):
        name, ident = match.group(1), match.group(2).removesuffix("-0")
        if (name, ident) in seen:
            continue
        seen.add((name, ident))
        rows.append((name, ident))
    return rows


def _drive_kind(label: str) -> str:
    if "PDF" in label:
        return "pdf"
    if "Google Docs" in label:
        return "gdoc"
    return "docx"


def _drive_url(kind: str, ident: str) -> str:
    if kind == "gdoc":
        return f"https://docs.google.com/document/d/{ident}/export?format=pdf"
    return f"https://drive.google.com/uc?export=download&id={ident}"


def fetch_drive_catalogue(folder_id: str = DRIVE_FOLDER_ID) -> list[Plan]:
    """Walk the shared folder one level deep and collect anything plan-shaped.

    The folder mixes subfolders per curriculum with loose files, so both levels
    are scanned and every row is tested by name rather than by position.
    """
    plans: list[Plan] = []
    seen: set[tuple[int, int, str]] = set()

    def collect(rows: list[tuple[str, str]]) -> None:
        for label, ident in rows:
            parsed = parse_plan_name(label)
            if parsed is None:
                continue
            manhaj, level = parsed
            kind = _drive_kind(label)
            key = (manhaj, level, kind)
            if key in seen:
                continue
            seen.add(key)
            plans.append(
                Plan(
                    manhaj=manhaj,
                    level=level,
                    kind=kind,
                    source="drive",
                    url=_drive_url(kind, ident),
                    title=label,
                )
            )

    top = _drive_rows(folder_id)
    collect(top)
    for label, ident in top:
        # Subfolder rows carry no format word; plan files always do.
        if any(word in label for word in ("PDF", "Microsoft Word", "Google Docs")):
            continue
        try:
            collect(_drive_rows(ident))
        except urllib.error.URLError:
            continue
    return plans


def build_catalogue() -> list[Plan]:
    """Everything findable, site first so its PDFs win ties."""
    return fetch_site_catalogue() + fetch_drive_catalogue()


def save_catalogue(plans: list[Plan], cache_dir: Path = DEFAULT_CACHE) -> Path:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "catalogue.json"
    path.write_text(
        json.dumps([asdict(p) for p in plans], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return path


def load_catalogue(cache_dir: Path = DEFAULT_CACHE) -> list[Plan]:
    path = Path(cache_dir) / "catalogue.json"
    if not path.is_file():
        return []
    return [Plan(**row) for row in json.loads(path.read_text(encoding="utf-8"))]


def catalogue(cache_dir: Path = DEFAULT_CACHE, refresh: bool = False) -> list[Plan]:
    """The catalogue, from cache unless asked to refresh or the cache is empty."""
    if not refresh:
        cached = load_catalogue(cache_dir)
        if cached:
            return cached
    plans = build_catalogue()
    save_catalogue(plans, cache_dir)
    return plans


def candidates(plans: list[Plan], manhaj: int, level: int) -> list[Plan]:
    """Every entry for one plan, usable formats first."""
    matches = [p for p in plans if p.manhaj == manhaj and p.level == level]
    order = {"pdf": 0, "gdoc": 1, "docx": 2}
    return sorted(matches, key=lambda p: (order.get(p.kind, 3), p.source != "site"))


class TemplateUnavailable(Exception):
    """No template that detection can read exists for this plan."""


# Serialises downloads into the shared cache; see ensure_template.
_DOWNLOAD_LOCK = threading.Lock()


def ensure_template(
    manhaj: int,
    level: int,
    plans: list[Plan] | None = None,
    cache_dir: Path = DEFAULT_CACHE,
) -> Path:
    """Return a local PDF for one plan, downloading it once if needed."""
    cache_dir = Path(cache_dir)
    plans = catalogue(cache_dir) if plans is None else plans

    found = candidates(plans, manhaj, level)
    if not found:
        raise TemplateUnavailable(f"منهج {manhaj} مستوى {level}: لا مصدر معروف")

    best = found[0]
    if not best.usable:
        kinds = ", ".join(sorted({p.kind for p in found}))
        raise TemplateUnavailable(
            f"منهج {manhaj} مستوى {level}: متاح بصيغة {kinds} فقط، ويحتاج تحويلًا يدويًّا"
        )

    destination = cache_dir / "templates" / best.filename
    if destination.is_file() and destination.stat().st_size > 0:
        return destination

    # Two teachers asking for the same new plan at the same moment would
    # otherwise download and write it concurrently, and a reader could pick up
    # a half-written file.  One downloader at a time, and the second finds the
    # file already there.
    with _DOWNLOAD_LOCK:
        if destination.is_file() and destination.stat().st_size > 0:
            return destination

        destination.parent.mkdir(parents=True, exist_ok=True)
        data = _get(best.url)
        if not data.startswith(b"%PDF"):
            raise TemplateUnavailable(
                f"منهج {manhaj} مستوى {level}: المنزَّل ليس PDF ({best.url})"
            )
        # Written under a temporary name and moved into place, so a reader
        # never sees a partial file even if this process dies mid-write.
        staging = destination.with_suffix(".part")
        staging.write_bytes(data)
        staging.replace(destination)
    return destination


def format_catalogue(plans: list[Plan]) -> str:
    by_manhaj: dict[int, list[Plan]] = {}
    for plan in plans:
        by_manhaj.setdefault(plan.manhaj, []).append(plan)

    lines = []
    for manhaj in sorted(by_manhaj):
        entries = by_manhaj[manhaj]
        levels = sorted({p.level for p in entries})
        usable = sorted({p.level for p in entries if p.usable})
        missing = [lv for lv in levels if lv not in usable]
        lines.append(
            f"منهج {manhaj}: {len(levels)} مستوى، منها {len(usable)} جاهز"
        )
        if missing:
            lines.append(f"    يحتاج تحويلًا يدويًّا: {', '.join(str(lv) for lv in missing)}")
    lines.append("")
    lines.append(
        f"المجموع: {len({(p.manhaj, p.level) for p in plans})} خطة، "
        f"{len({(p.manhaj, p.level) for p in plans if p.usable})} منها جاهزة"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m khutat.catalogue",
        description="Index the association's plan templates and fetch them.",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="re-read the catalogue from the network"
    )
    parser.add_argument(
        "--cache", type=Path, default=DEFAULT_CACHE, help="where to keep the index"
    )
    parser.add_argument(
        "--get",
        nargs=2,
        type=int,
        metavar=("MANHAJ", "LEVEL"),
        help="download one plan and print its local path",
    )
    args = parser.parse_args(argv)

    try:
        plans = catalogue(args.cache, refresh=args.refresh)
    except urllib.error.URLError as error:
        print(f"تعذّر الوصول إلى الشبكة: {error}", file=sys.stderr)
        return 2

    if args.get:
        try:
            print(ensure_template(args.get[0], args.get[1], plans, args.cache))
        except (TemplateUnavailable, urllib.error.URLError) as error:
            print(error, file=sys.stderr)
            return 1
        return 0

    print(format_catalogue(plans))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
