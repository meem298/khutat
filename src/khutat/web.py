"""A page for teachers: drop in the Injaz export, get the plans back.

The command line is the wrong shape for the person this tool is for.  A
teacher preparing a term's plans should not be assembling a six-flag command
with her halaqah's name quoted inside it, and should not have to retype it
next term.  So this serves one page: she picks the export, her own details are
already filled in from last time, one button generates, and the plans come
back as a zip.

The page is **stateless by construction**, and that is the whole design.  It
started as a convenience for one teacher on her own Mac, where a settings file
and a fixed output folder were fine.  The moment a second teacher can reach
it, both become faults rather than shortcuts:

* A settings file on the server is one file.  The last teacher to type would
  overwrite everyone, and the next would open the page to find another
  woman's halaqah and name already filled in — and could generate a whole
  class under the wrong teacher's name before noticing.  Her details live in
  her own browser instead.
* A fixed output folder is one folder.  Two teachers generating at the same
  moment would write into it together and their students would mix.  Each
  request gets a temporary directory of its own, zipped and then deleted.

Nothing about a class is written where it outlives the request: the upload and
the filled plans live in a temporary directory removed in a ``finally``, the
finished zip waits in memory for a short while under a one-time token, and no
student's name is ever logged.

The bundled Noto face is this page's default.  That is a deployment choice and
does not belong in the library, where the font stays a required argument.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import io
import json
import os
import re
import secrets
import shutil
import socket
import tempfile
import threading
import time
import webbrowser
import zipfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .roster import Student, generate, read_roster

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_FONT = PROJECT_ROOT / "assets" / "fonts" / "NotoNaskhArabic-Regular.ttf"

# A dedicated work number the owner is happy to publish; overridable so the
# number can change, or a colleague can host her own copy under her own name.
WHATSAPP_NUMBER = os.environ.get("KHUTAT_WHATSAPP", "966552350036")
CREDIT = os.environ.get("KHUTAT_CREDIT", "by MeeM")

# The header values a teacher supplies once; the student's name comes from the
# roster.  Both spellings of the teacher's label are sent because templates
# disagree about which they use, and each takes the one it has.
TEACHER_FIELDS = ("الحلقة", "المجمع/الدار", "اسم المعلم", "العام والفصل")

# The two digit scripts the pipeline actually reads a plan code in (see
# roster.py's own translation table) — Latin 0-9, and the Arabic-Indic digits
# Injaz itself writes.  Anything else (Persian ۰-۹, fullwidth, …) is rejected
# here rather than handed to int(), which would silently accept scripts this
# tool never sees in a real class list.
_DIGITS = re.compile(r"^[0-9٠-٩]+$")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _digits_to_int(raw: object, label: str) -> int:
    text = str(raw if raw is not None else "").strip()
    if not _DIGITS.match(text):
        raise ValueError(f"{label} يجب أن يكون أرقامًا فقط (٠-٩ أو 0-9)")
    value = int(text.translate(_ARABIC_DIGITS))
    if value <= 0:
        raise ValueError(f"{label} يجب أن يكون أكبر من صفر")
    return value


# Roughly the largest export worth accepting; a class list is a few tens of KB.
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024

# Most students one shared request will build plans for; see run_batch.
_MAX_STUDENTS = 200

# How long a finished zip waits to be collected, and how many may wait at once.
_DOWNLOAD_TTL_SECONDS = 15 * 60
_MAX_PENDING = 8


class _Pending:
    """Finished archives waiting to be downloaded, held only in memory.

    A batch is handed back under a single-use token rather than written
    anywhere, so a class list never touches the server's disk beyond the
    temporary directory that produced it.  Entries expire, and the oldest is
    dropped when too many pile up, so an abandoned batch cannot keep a
    class's names in memory indefinitely.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, bytes, str, str]] = {}

    def _expire(self, now: float) -> None:
        stale = [k for k, (created, _, _, _) in self._items.items()
                 if now - created > _DOWNLOAD_TTL_SECONDS]
        for key in stale:
            del self._items[key]
        while len(self._items) > _MAX_PENDING:
            oldest = min(self._items, key=lambda k: self._items[k][0])
            del self._items[oldest]

    def put(self, payload: bytes, content_type: str, filename: str) -> str:
        token = secrets.token_urlsafe(16)
        with self._lock:
            now = time.time()
            self._items[token] = (now, payload, content_type, filename)
            self._expire(now)
        return token

    def take(self, token: str) -> tuple[bytes, str, str] | None:
        with self._lock:
            entry = self._items.pop(token, None)
        if entry is None:
            return None
        created, payload, content_type, filename = entry
        if time.time() - created > _DOWNLOAD_TTL_SECONDS:
            return None
        return payload, content_type, filename


PENDING = _Pending()


@dataclass
class RunOutcome:
    written: list[str]
    skipped: list[dict[str, str]]
    archive: bytes


def run_batch(roster_bytes: bytes, values: dict[str, str], compact: bool) -> RunOutcome:
    """Fill a class's plans and return them zipped, leaving nothing behind."""
    workspace = Path(tempfile.mkdtemp(prefix="khutat-"))
    try:
        staged = workspace / "roster.xlsx"
        staged.write_bytes(roster_bytes)

        students = read_roster(staged)
        if not students:
            raise ValueError("لم يُعثر على طالبات في هذا الملف")
        if len(students) > _MAX_STUDENTS:
            # Each plan is about a megabyte and the archive is assembled in
            # memory, so an implausibly long roster would exhaust a small host
            # and take the page down for everyone.  A halaqah is tens of
            # students; the command line stays unbounded for real bulk work.
            raise ValueError(
                f"الكشف يحوي {len(students)} طالبة، والحد على الصفحة {_MAX_STUDENTS}. "
                "للأعداد الأكبر يُستعمل الأمر المحلي."
            )

        plans_dir = workspace / "plans"
        plans_dir.mkdir()

        supplied = {k: v for k, v in values.items() if v.strip()}
        if "اسم المعلم" in supplied:
            supplied.setdefault("المعلم", supplied["اسم المعلم"])

        result = generate(students, supplied, plans_dir, BUNDLED_FONT, compact=compact)

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for _, path in result.written:
                archive.write(path, arcname=path.name)
            if result.skipped:
                # The teacher will print the zip's contents and file them; a
                # note inside it travels with the plans, where a message shown
                # once in a browser does not.
                lines = ["طالبات بلا خطة — تحتاج تعبئة يدوية:", ""]
                lines += [
                    f"- {student.name} — {student.plan_label} — {reason}"
                    for student, reason in result.skipped
                ]
                archive.writestr(
                    "الطالبات المتعذرات.txt", "\n".join(lines) + "\n"
                )

        return RunOutcome(
            written=[student.name for student, _ in result.written],
            skipped=[
                {"name": student.name, "plan": student.plan_label, "reason": reason}
                for student, reason in result.skipped
            ],
            archive=buffer.getvalue(),
        )
    finally:
        # Runs even when filling raised: nothing about a class survives the
        # request on disk.
        shutil.rmtree(workspace, ignore_errors=True)


def run_single(student: Student, values: dict[str, str], compact: bool) -> tuple[bytes, str]:
    """Fill one student's plan and return its bytes and a filename.

    Reuses ``generate`` with a one-element list rather than a parallel
    single-student path, so a missing digital plan (curriculum 5, the
    recitation track, an unconfigured drive folder) is reported with the
    same message a roster upload would give for that student.
    """
    workspace = Path(tempfile.mkdtemp(prefix="khutat-"))
    try:
        plans_dir = workspace / "plans"
        plans_dir.mkdir()

        supplied = {k: v for k, v in values.items() if v.strip()}
        if "اسم المعلم" in supplied:
            supplied.setdefault("المعلم", supplied["اسم المعلم"])

        result = generate([student], supplied, plans_dir, BUNDLED_FONT, compact=compact)
        if result.skipped:
            _, reason = result.skipped[0]
            raise ValueError(f"{student.plan_label} — {reason}")

        _, path = result.written[0]
        return path.read_bytes(), student.safe_filename
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


PAGE = """<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>خطط الطالبات</title>
<style>
  :root { --ink:#1b2230; --muted:#6b7280; --line:#e2e5ea; --bg:#f6f7f9;
          --accent:#1f6f54; --accent-dark:#17553f; --warn:#8a5a00;
          --warn-bg:#fff8e6; --ok-bg:#eef6f2; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:16px/1.7 -apple-system, "SF Arabic", "Geeza Pro", sans-serif; }
  .wrap { max-width:760px; margin:0 auto; padding:32px 20px 40px; }
  h1 { font-size:26px; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 28px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:14px;
          padding:22px; margin-bottom:18px; }
  .step { display:flex; align-items:baseline; gap:10px; margin:0 0 14px;
          font-weight:600; font-size:17px; }
  .num { background:var(--accent); color:#fff; border-radius:50%;
         width:26px; height:26px; min-width:26px; display:inline-flex;
         align-items:center; justify-content:center; font-size:14px; }
  #drop { border:2px dashed #c7ccd4; border-radius:12px; padding:30px 18px;
          text-align:center; cursor:pointer; transition:.15s; background:#fcfcfd; }
  #drop:hover, #drop.over { border-color:var(--accent); background:var(--ok-bg); }
  #drop strong { display:block; font-size:17px; margin-bottom:4px; }
  #drop span { color:var(--muted); font-size:14px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  @media (max-width:560px) { .grid { grid-template-columns:1fr; } }
  label { display:block; font-size:14px; color:var(--muted); margin-bottom:5px; }
  input[type=text], select { width:100%; padding:10px 12px; font:inherit; font-size:15px;
         border:1px solid var(--line); border-radius:9px; background:#fff; }
  input[type=text]:focus, select:focus { outline:2px solid var(--accent); border-color:transparent; }
  .tabs { display:flex; gap:8px; margin-bottom:18px; }
  .tabs button { flex:1; background:#fff; color:var(--muted); font-weight:600;
         border:1px solid var(--line); }
  .tabs button.active { background:var(--accent); color:#fff; border-color:var(--accent); }
  .tabs button:hover:not(.active) { background:var(--ok-bg); color:var(--ink); }
  [hidden] { display:none !important; }
  .sizes { display:flex; gap:10px; flex-wrap:wrap; }
  .sizes label { flex:1; min-width:210px; border:1px solid var(--line);
         border-radius:11px; padding:13px 15px; cursor:pointer; margin:0;
         color:var(--ink); display:flex; gap:10px; align-items:flex-start; }
  .sizes label:has(input:checked) { border-color:var(--accent);
         background:var(--ok-bg); }
  .sizes b { display:block; font-size:15px; }
  .sizes em { font-style:normal; color:var(--muted); font-size:13px; }
  button { width:100%; padding:15px; font:inherit; font-size:17px; font-weight:600;
           color:#fff; background:var(--accent); border:0; border-radius:11px;
           cursor:pointer; }
  button:hover { background:var(--accent-dark); }
  button:disabled { background:#9aa3ad; cursor:default; }
  .ghost { background:#fff; color:var(--accent); border:1px solid var(--accent);
           margin-top:12px; }
  .ghost:hover { background:var(--ok-bg); }
  .hint { font-size:13px; color:var(--muted); margin-top:9px; }
  .result h2 { font-size:18px; margin:0 0 10px; }
  .ok { background:var(--ok-bg); border:1px solid #cfe3d9; border-radius:11px;
        padding:15px 17px; margin-bottom:14px; }
  .warn { background:var(--warn-bg); border:1px solid #f0dfae; border-radius:11px;
          padding:15px 17px; }
  .warn h3 { margin:0 0 8px; font-size:16px; color:var(--warn); }
  ul { margin:6px 0 0; padding-inline-start:20px; }
  li { margin-bottom:4px; }
  .err { background:#fdeeee; border:1px solid #f2c9c9; color:#8a1f1f;
         border-radius:11px; padding:15px 17px; }
  .names { columns:2; font-size:14.5px; }
  @media (max-width:560px) { .names { columns:1; } }
  .privacy { font-size:13px; color:var(--muted); text-align:center;
             margin:22px 0 0; }
  footer { text-align:center; padding:26px 20px 40px; color:var(--muted);
           font-size:14px; }
  footer .mark { font-weight:600; color:var(--ink); letter-spacing:.3px; }
  footer a { color:var(--accent); text-decoration:none; font-weight:600;
             border:1px solid var(--accent); border-radius:999px;
             padding:6px 15px; display:inline-block; margin-top:10px; }
  footer a:hover { background:var(--ok-bg); }
</style>
</head>
<body>
<div class="wrap">
  <h1>خطط الطالبات</h1>
  <p class="sub" id="sub">رفع كشف إنجاز، ثم زر واحد.</p>

  <div class="tabs">
    <button type="button" id="tabBatch" class="active">كشف طالبات</button>
    <button type="button" id="tabOne">طالبة واحدة</button>
  </div>

  <div class="card" id="batchCard">
    <p class="step"><span class="num">١</span> كشف الطالبات</p>
    <div id="drop">
      <strong id="dropTitle">سحب ملف إنجاز إلى هنا</strong>
      <span id="dropHint">أو النقر للاختيار — ملف Excel المصدَّر من النظام</span>
    </div>
    <input type="file" id="file" accept=".xlsx" hidden>
  </div>

  <div class="card" id="oneCard" hidden>
    <p class="step"><span class="num">١</span> بيانات الطالبة</p>
    <div class="grid">
      <div>
        <label for="oneName">اسم الطالبة</label>
        <input type="text" id="oneName">
      </div>
      <div>
        <label for="oneTrack">المسار</label>
        <select id="oneTrack">
          <option value="حفظ">حفظ</option>
          <option value="تلاوة">تلاوة</option>
        </select>
      </div>
      <div id="oneManhajWrap">
        <label for="oneManhaj">المنهج</label>
        <select id="oneManhaj">
          <option value="1">١</option>
          <option value="2">٢</option>
          <option value="3">٣</option>
          <option value="4">٤</option>
          <option value="6">٦</option>
        </select>
      </div>
      <div>
        <label for="oneLevel">المستوى</label>
        <input type="text" id="oneLevel" inputmode="numeric" pattern="[0-9٠-٩]*">
      </div>
    </div>
  </div>

  <div class="card">
    <p class="step"><span class="num">٢</span> البيانات الثابتة</p>
    <div class="grid" id="fields"></div>
    <p class="hint">تُحفظ في هذا المتصفح وحده، فلا حاجة إلى إعادة كتابتها في المرة القادمة.</p>
  </div>

  <div class="card">
    <p class="step"><span class="num">٣</span> حجم الطباعة</p>
    <div class="sizes">
      <label><input type="radio" name="size" value="full" checked>
        <span><b>مكبّرة</b><em>صفحة واحدة بالورقة</em></span></label>
      <label><input type="radio" name="size" value="compact">
        <span><b>مصغّرة</b><em>أربع صفحات بالورقة — ورق أقل</em></span></label>
    </div>
  </div>

  <button id="go" disabled>توليد الخطط</button>
  <button id="goOne" hidden>توليد الخطة</button>
  <div id="out" style="margin-top:18px"></div>

  <p class="privacy">أسماء الطالبات تُستعمل لتعبئة الخطط ثم تُحذف فور انتهاء الطلب.
    لا تُحفظ ولا تُسجَّل.</p>
</div>

<footer>
  <div class="mark">__CREDIT__</div>
  __CONTACT__
</footer>

<script>
const FIELDS = __FIELDS__;
const STORE = 'khutat.fields';
const drop = document.getElementById('drop');
const picker = document.getElementById('file');
const go = document.getElementById('go');
const out = document.getElementById('out');
let chosen = null;

// Kept in this browser, never on the server: a shared settings file would let
// one teacher's details appear prefilled for the next.
let saved = {};
try { saved = JSON.parse(localStorage.getItem(STORE) || '{}'); } catch (e) { saved = {}; }

const box = document.getElementById('fields');
for (const label of FIELDS) {
  const id = 'f_' + encodeURIComponent(label);
  const w = document.createElement('div');
  const l = document.createElement('label');
  l.textContent = label; l.htmlFor = id;
  const i = document.createElement('input');
  i.type = 'text'; i.id = id; i.dataset.label = label;
  i.value = saved[label] || '';
  i.addEventListener('change', saveFields);
  w.append(l, i); box.append(w);
}
function values() {
  const v = {};
  box.querySelectorAll('input').forEach(i => v[i.dataset.label] = i.value);
  return v;
}
function saveFields() {
  try { localStorage.setItem(STORE, JSON.stringify(values())); } catch (e) {}
}

const tabBatch = document.getElementById('tabBatch');
const tabOne = document.getElementById('tabOne');
const batchCard = document.getElementById('batchCard');
const oneCard = document.getElementById('oneCard');
const goOne = document.getElementById('goOne');
const sub = document.getElementById('sub');

function showBatch() {
  tabBatch.classList.add('active'); tabOne.classList.remove('active');
  batchCard.hidden = false; oneCard.hidden = true;
  go.hidden = false; goOne.hidden = true;
  sub.textContent = 'رفع كشف إنجاز، ثم زر واحد.';
  out.innerHTML = '';
}
function showOne() {
  tabOne.classList.add('active'); tabBatch.classList.remove('active');
  oneCard.hidden = false; batchCard.hidden = true;
  goOne.hidden = false; go.hidden = true;
  sub.textContent = 'خطة طالبة واحدة، بلا كشف.';
  out.innerHTML = '';
}
tabBatch.addEventListener('click', showBatch);
tabOne.addEventListener('click', showOne);

const oneTrack = document.getElementById('oneTrack');
const oneManhajWrap = document.getElementById('oneManhajWrap');
function syncTrack() {
  oneManhajWrap.hidden = oneTrack.value === 'تلاوة';
}
oneTrack.addEventListener('change', syncTrack);
syncTrack();

// Only the two digit scripts the tool actually reads a plan code in —
// Latin and Arabic-Indic — same as what the server accepts.
const oneLevel = document.getElementById('oneLevel');
oneLevel.addEventListener('input', () => {
  const kept = oneLevel.value.match(/[0-9٠-٩]/g);
  oneLevel.value = kept ? kept.join('') : '';
});

drop.addEventListener('click', () => picker.click());
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', () => drop.classList.remove('over'));
drop.addEventListener('drop', e => {
  e.preventDefault(); drop.classList.remove('over');
  if (e.dataTransfer.files.length) take(e.dataTransfer.files[0]);
});
picker.addEventListener('change', () => { if (picker.files.length) take(picker.files[0]); });

function take(f) {
  if (!f.name.toLowerCase().endsWith('.xlsx')) {
    out.innerHTML = '<div class="err">هذا ليس ملف Excel. المطلوب كشف مُصدَّر من إنجاز بصيغة xlsx.</div>';
    return;
  }
  chosen = f;
  document.getElementById('dropTitle').textContent = '✓ ' + f.name;
  document.getElementById('dropHint').textContent = 'نقر لاختيار ملف آخر';
  go.disabled = false; out.innerHTML = '';
}

go.addEventListener('click', async () => {
  if (!chosen) return;
  go.disabled = true; go.textContent = 'جارٍ التوليد…';
  out.innerHTML = '';
  try {
    const b64 = await new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(r.result.split(',')[1]);
      r.onerror = () => rej(new Error('تعذّرت قراءة الملف'));
      r.readAsDataURL(chosen);
    });
    const size = document.querySelector('input[name=size]:checked').value;
    const r = await fetch('/api/generate', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({roster: b64, values: values(), size})});
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'خطأ غير متوقع');
    render(data);
  } catch (err) {
    out.innerHTML = '<div class="err">' + escape_(err.message) + '</div>';
  } finally {
    go.disabled = false; go.textContent = 'توليد الخطط';
  }
});

goOne.addEventListener('click', async () => {
  goOne.disabled = true; goOne.textContent = 'جارٍ التوليد…';
  out.innerHTML = '';
  try {
    const size = document.querySelector('input[name=size]:checked').value;
    const r = await fetch('/api/generate-one', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        name: document.getElementById('oneName').value,
        track: oneTrack.value,
        manhaj: document.getElementById('oneManhaj').value,
        level: document.getElementById('oneLevel').value,
        values: values(), size,
      })});
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'خطأ غير متوقع');
    renderOne(data);
  } catch (err) {
    out.innerHTML = '<div class="err">' + escape_(err.message) + '</div>';
  } finally {
    goOne.disabled = false; goOne.textContent = 'توليد الخطة';
  }
});

function renderOne(d) {
  out.innerHTML = '<div class="ok"><h2>وُلّدت خطة ' + escape_(d.name) + '</h2></div>' +
    '<button class="ghost" id="dl">تنزيل الخطة</button>';
  document.getElementById('dl').addEventListener('click', () => {
    location.href = '/api/download?token=' + d.token;
  });
  document.getElementById('dl').click();
}

function escape_(s) {
  const d = document.createElement('div'); d.textContent = s; return d.innerHTML;
}

function render(d) {
  let html = '<div class="result">';
  html += '<div class="ok"><h2>وُلّدت ' + d.written.length + ' خطة</h2>';
  if (d.written.length) {
    html += '<div class="names"><ul>' +
      d.written.map(n => '<li>' + escape_(n) + '</li>').join('') + '</ul></div>';
  }
  html += '</div>';
  if (d.skipped.length) {
    html += '<div class="warn"><h3>⚠ ' + d.skipped.length +
      ' طالبة بلا خطة — تحتاج تعبئة يدوية</h3><ul>' +
      d.skipped.map(s => '<li><b>' + escape_(s.name) + '</b> — ' +
        escape_(s.plan) + ' — ' + escape_(s.reason) + '</li>').join('') +
      '</ul></div>';
  }
  html += '</div>';
  if (d.token) {
    html += '<button class="ghost" id="dl">تنزيل الخطط</button>';
  }
  out.innerHTML = html;
  const dl = document.getElementById('dl');
  if (dl) {
    dl.addEventListener('click', () => { location.href = '/api/download?token=' + d.token; });
    dl.click();
  }
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        # Deliberately silent: the default log line would record every request,
        # and nothing about a class should reach a server log.
        pass

    def _send(self, code: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(
            code,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            contact = ""
            if WHATSAPP_NUMBER:
                message = "استفسار عن أداة خطط الطالبات"
                contact = (
                    f'<a href="https://wa.me/{WHATSAPP_NUMBER}'
                    f'?text={_quote(message)}" target="_blank" rel="noopener">'
                    "للاستفسار عبر واتساب</a>"
                )
            page = (
                PAGE.replace("__FIELDS__", json.dumps(TEACHER_FIELDS, ensure_ascii=False))
                .replace("__CREDIT__", CREDIT)
                .replace("__CONTACT__", contact)
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return

        if parsed.path == "/api/download":
            token = (parse_qs(parsed.query).get("token") or [""])[0]
            entry = PENDING.take(token)
            if entry is None:
                self._json(404, {"error": "انتهت صلاحية الرابط — أعيدي التوليد"})
                return
            payload, content_type, filename = entry
            fallback = "khutat.zip" if content_type == "application/zip" else "khutat.pdf"
            self._send(
                200,
                payload,
                content_type,
                {
                    "Content-Disposition":
                        f"attachment; filename={fallback}; "
                        f"filename*=UTF-8''{_quote(filename)}"
                },
            )
            return

        if parsed.path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > _MAX_UPLOAD_BYTES * 2:
                raise ValueError("حجم الطلب غير مقبول")
            payload = json.loads(self.rfile.read(length))

            if path == "/api/generate":
                self._generate(payload)
            elif path == "/api/generate-one":
                self._generate_one(payload)
            else:
                self._json(404, {"error": "not found"})
        except ValueError as error:
            self._json(400, {"error": str(error)})
        except Exception as error:  # surfaced in the page, not the terminal
            self._json(500, {"error": f"{type(error).__name__}: {error}"})

    def _generate(self, payload: dict) -> None:
        try:
            data = base64.b64decode(payload.get("roster", ""), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("تعذّرت قراءة الملف المرفوع")
        if not data:
            raise ValueError("لم يصل أي ملف")
        if len(data) > _MAX_UPLOAD_BYTES:
            raise ValueError("الملف أكبر مما ينبغي لكشف طالبات")

        values = {str(k): str(v) for k, v in (payload.get("values") or {}).items()}
        outcome = run_batch(data, values, compact=payload.get("size") == "compact")

        token = (
            PENDING.put(outcome.archive, "application/zip", "khutat.zip")
            if outcome.written else None
        )
        self._json(
            200,
            {"written": outcome.written, "skipped": outcome.skipped, "token": token},
        )

    def _generate_one(self, payload: dict) -> None:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("اسم الطالبة مطلوب")

        track = str(payload.get("track") or "")
        if track not in ("حفظ", "تلاوة"):
            raise ValueError("المسار غير معروف")

        level = _digits_to_int(payload.get("level"), "المستوى")

        manhaj: int | None = None
        if track == "حفظ":
            manhaj = _digits_to_int(payload.get("manhaj"), "المنهج")

        student = Student(name=name, manhaj=manhaj, level=level, track=track)
        values = {str(k): str(v) for k, v in (payload.get("values") or {}).items()}
        pdf_bytes, filename = run_single(
            student, values, compact=payload.get("size") == "compact"
        )

        token = PENDING.put(pdf_bytes, "application/pdf", filename)
        self._json(200, {"token": token, "name": student.name})


def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text)


def _free_port(preferred: int) -> int:
    """``preferred`` if it is free, otherwise whatever the OS hands out."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def serve(host: str = "127.0.0.1", port: int = 8731, open_browser: bool = True) -> None:
    if not BUNDLED_FONT.is_file():
        raise SystemExit(f"الخط غير موجود عند {BUNDLED_FONT}")

    local = host in ("127.0.0.1", "localhost")
    if local:
        port = _free_port(port)

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'127.0.0.1' if local else host}:{port}/"

    print("خطط الطالبات جاهزة.")
    print(f"  العنوان: {url}")
    print("  للإغلاق: إغلاق هذي النافذة، أو Control+C")

    if open_browser and local:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nتم الإغلاق.")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m khutat.web",
        description="Serve the teacher-facing page.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="127.0.0.1 for this machine only; 0.0.0.0 when hosted",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", 8731))
    )
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    serve(args.host, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
