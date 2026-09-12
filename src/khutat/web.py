"""A local page for teachers: drop in the Injaz export, get the plans.

The command line is the wrong shape for the person this tool is for.  A
teacher preparing a term's plans should not be assembling a six-flag command
with her halaqah's name quoted inside it, and should not have to retype it
next term.

So this serves one page on localhost and opens it in her browser.  She picks
the export, her own details are already filled in from last time, she presses
one button, and the plans appear in a folder the page can open for her.

Design notes:

* **No new dependencies.**  The server is :mod:`http.server`, and the export
  is sent as base64 inside JSON rather than as a multipart upload — Python
  3.13 removed the :mod:`cgi` module that used to parse those, and a hand-
  rolled multipart parser is more code than the problem deserves.
* **Bound to localhost only.**  Nothing here is exposed to the network.
* **The teacher's own details are remembered**, because they change once a
  term at most, while the class list changes constantly.
* **Skipped students are shown as prominently as the successful ones.**  On
  the command line an ignored warning costs a scroll; here it would cost a
  teacher four missing plans discovered in front of her class.

The bundled Noto face is this page's default.  That is a deployment choice and
does not belong in the library, where the font stays a required argument.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import socket
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .roster import generate, read_roster

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_FONT = PROJECT_ROOT / "assets" / "fonts" / "NotoNaskhArabic-Regular.ttf"
SETTINGS_PATH = PROJECT_ROOT / ".cache" / "khutat" / "settings.json"
DEFAULT_OUTPUT = Path.home() / "Documents" / "خطط الطالبات"

# The header values a teacher supplies once; the student's name comes from the
# roster.  Both spellings of the teacher's label are sent because templates
# disagree about which they use, and each takes the one it has.
TEACHER_FIELDS = (
    ("الحلقة", "حلقة النور"),
    ("المجمع/الدار", "الفيحاء"),
    ("اسم المعلم", ""),
    ("العام والفصل", ""),
)

# Roughly the largest export worth accepting; a class list is a few tens of KB.
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024


def load_settings() -> dict[str, str]:
    if not SETTINGS_PATH.is_file():
        return {}
    try:
        stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return stored if isinstance(stored, dict) else {}


def save_settings(values: dict[str, str]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(values, ensure_ascii=False, indent=1), encoding="utf-8"
    )


@dataclass
class RunOutcome:
    written: list[str]
    skipped: list[dict[str, str]]
    out_dir: str


def run_batch(
    roster_bytes: bytes, values: dict[str, str], out_dir: Path, compact: bool
) -> RunOutcome:
    """Write the class's plans from an in-memory export."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # read_roster reads a file and the export arrived as bytes, so it has to be
    # written somewhere — but not into the output folder.  A teacher selects
    # everything there and prints it, and a spreadsheet in with the plans would
    # go to the printer too.
    staged = SETTINGS_PATH.parent / "last-roster.xlsx"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(roster_bytes)

    students = read_roster(staged)
    if not students:
        raise ValueError("لم يُعثر على طالبات في هذا الملف")

    supplied = {k: v for k, v in values.items() if v.strip()}
    if "اسم المعلم" in supplied:
        supplied.setdefault("المعلم", supplied["اسم المعلم"])

    result = generate(
        students,
        supplied,
        out_dir,
        BUNDLED_FONT,
        compact=compact,
    )
    return RunOutcome(
        written=[student.name for student, _ in result.written],
        skipped=[
            {"name": student.name, "plan": student.plan_label, "reason": reason}
            for student, reason in result.skipped
        ],
        out_dir=str(out_dir),
    )


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
  .wrap { max-width:760px; margin:0 auto; padding:32px 20px 64px; }
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
  input[type=text] { width:100%; padding:10px 12px; font:inherit; font-size:15px;
         border:1px solid var(--line); border-radius:9px; background:#fff; }
  input[type=text]:focus { outline:2px solid var(--accent); border-color:transparent; }
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
</style>
</head>
<body>
<div class="wrap">
  <h1>خطط الطالبات</h1>
  <p class="sub">رفع كشف إنجاز، ثم زر واحد.</p>

  <div class="card">
    <p class="step"><span class="num">١</span> كشف الطالبات</p>
    <div id="drop">
      <strong id="dropTitle">سحب ملف إنجاز إلى هنا</strong>
      <span id="dropHint">أو النقر للاختيار — ملف Excel المصدَّر من النظام</span>
    </div>
    <input type="file" id="file" accept=".xlsx" hidden>
  </div>

  <div class="card">
    <p class="step"><span class="num">٢</span> البيانات الثابتة</p>
    <div class="grid" id="fields"></div>
    <p class="hint">تُحفظ تلقائيًّا، فلا حاجة إلى إعادة كتابتها في المرة القادمة.</p>
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
  <div id="out" style="margin-top:18px"></div>
</div>

<script>
const FIELDS = __FIELDS__;
const drop = document.getElementById('drop');
const picker = document.getElementById('file');
const go = document.getElementById('go');
const out = document.getElementById('out');
let chosen = null;

const box = document.getElementById('fields');
for (const [label, value] of FIELDS) {
  const id = 'f_' + encodeURIComponent(label);
  const w = document.createElement('div');
  const l = document.createElement('label');
  l.textContent = label; l.htmlFor = id;
  const i = document.createElement('input');
  i.type = 'text'; i.id = id; i.dataset.label = label; i.value = value;
  i.addEventListener('change', saveFields);
  w.append(l, i); box.append(w);
}
function values() {
  const v = {};
  box.querySelectorAll('input').forEach(i => v[i.dataset.label] = i.value);
  return v;
}
function saveFields() {
  fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(values())});
}

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
  html += '<button class="ghost" id="reveal">فتح مجلد الخطط</button>';
  out.innerHTML = html;
  document.getElementById('reveal').addEventListener('click', () => {
    fetch('/api/reveal', {method:'POST'});
  });
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    output_dir = DEFAULT_OUTPUT

    def log_message(self, *args) -> None:  # keep the terminal quiet
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(
            code,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > _MAX_UPLOAD_BYTES * 2:
            raise ValueError("حجم الطلب غير مقبول")
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:
        if self.path not in ("/", "/index.html"):
            self._json(404, {"error": "not found"})
            return
        stored = load_settings()
        fields = [[label, stored.get(label, default)] for label, default in TEACHER_FIELDS]
        page = PAGE.replace("__FIELDS__", json.dumps(fields, ensure_ascii=False))
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        try:
            if self.path == "/api/settings":
                payload = self._read_json()
                save_settings({str(k): str(v) for k, v in payload.items()})
                self._json(200, {"ok": True})
                return

            if self.path == "/api/reveal":
                subprocess.run(["open", str(self.output_dir)], check=False)
                self._json(200, {"ok": True})
                return

            if self.path != "/api/generate":
                self._json(404, {"error": "not found"})
                return

            payload = self._read_json()
            try:
                data = base64.b64decode(payload.get("roster", ""), validate=True)
            except (binascii.Error, ValueError):
                raise ValueError("تعذّرت قراءة الملف المرفوع")
            if not data:
                raise ValueError("لم يصل أي ملف")
            if len(data) > _MAX_UPLOAD_BYTES:
                raise ValueError("الملف أكبر مما ينبغي لكشف طالبات")

            values = {str(k): str(v) for k, v in (payload.get("values") or {}).items()}
            save_settings(values)

            outcome = run_batch(
                data,
                values,
                self.output_dir,
                compact=payload.get("size") == "compact",
            )
            self._json(
                200,
                {
                    "written": outcome.written,
                    "skipped": outcome.skipped,
                    "out_dir": outcome.out_dir,
                },
            )
        except ValueError as error:
            self._json(400, {"error": str(error)})
        except Exception as error:  # surfaced in the page rather than the terminal
            self._json(500, {"error": f"{type(error).__name__}: {error}"})


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


def serve(output_dir: Path, port: int = 8731, open_browser: bool = True) -> None:
    if not BUNDLED_FONT.is_file():
        print(f"الخط غير موجود عند {BUNDLED_FONT}", file=sys.stderr)
        raise SystemExit(2)

    Handler.output_dir = Path(output_dir)
    port = _free_port(port)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"

    print("خطط الطالبات جاهزة.")
    print(f"  العنوان: {url}")
    print(f"  المخرجات: {output_dir}")
    print("  للإغلاق: إغلاق هذي النافذة، أو Control+C")

    if open_browser:
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
        description="Serve the teacher-facing page on localhost.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--port", type=int, default=8731)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    serve(args.output, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
