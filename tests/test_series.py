"""系列題測試：scripts/add_series.py（暫存題庫）與前端 src/lib/series.js、mockExam.js（Node 直接載入真正的程式）。
不連網、不呼叫 API；另含正式題庫的唯讀檢查（存在時才執行）。"""
import atexit
import copy
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
import add_series as ads  # noqa: E402

PASSED, FAILED, NET_ATTEMPTS = [], [], []


def _no_network(*a, **k):
    NET_ATTEMPTS.append(a[1:2])
    raise AssertionError("network access attempted in test")


socket.socket.connect = _no_network
socket.create_connection = _no_network
TMP = Path(tempfile.mkdtemp(prefix="series-"))
atexit.register(lambda: os.environ.get("KEEP_TEST_TMP") == "1" or shutil.rmtree(TMP, ignore_errors=True))


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    if not cond:
        print(f"FAIL {name} {detail}")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run_main(argv, series=None):
    saved = ads.SERIES
    if series is not None:
        ads.SERIES = series
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = ads.main(argv)
    finally:
        ads.SERIES = saved
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# 1. add_series.py（暫存題庫）
# ---------------------------------------------------------------------------
NOTE = ("Note: This question is part of a series of questions that present the same scenario. "
        "After you answer, these questions will not appear in the review screen.\n\n")


def sol(qid, scen, solution, ans="B", chapter="d1"):
    return {"id": qid, "type": "single_choice", "chapter": chapter,
            "question": f"{NOTE}{scen}\n\nSolution: {solution}\n\nDoes this meet the goal?",
            "options": [{"id": "A", "text": "Yes"}, {"id": "B", "text": "No"}], "answer": ans, "explanation": ""}


BANK = {"$schema": "./questions.schema.json", "id": "t", "title": "t", "chapters": [{"id": "d1", "title": "D1"}], "questions": [
    sol("s-1", "You need to do X.", "Use tool 1.", "A"),
    {"id": "plain", "type": "single_choice", "chapter": "d1", "question": "Plain?", "answer": "A",
     "options": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}]},
    sol("s-2", "You need to do X.", "Use tool 2."),
    sol("other", "You need to do Y.", "Use tool 3."),
    sol("s-3", "You need  to do\nX.", "Use tool 4."),   # 空白差異視為相同情境
]}
GOOD = {"series-x": ["s-1", "s-2", "s-3"]}


def make_bank(name, newline="\r\n"):
    p = TMP / name / "bank.json"
    p.parent.mkdir(parents=True)
    p.write_bytes((json.dumps(BANK, ensure_ascii=False, indent=2) + "\n").replace("\n", newline).encode("utf-8"))
    return p


p = make_bank("dry")
before = sha(p)
rc, log = run_main(["--bank", str(p), "--backup-dir", str(TMP / "dry" / "bk"), "--dry-run"], GOOD)
check("A1 --dry-run lists the plan and writes nothing", rc == 0 and "需要新增 series 的題目 3 題" in log and sha(p) == before
      and not (TMP / "dry" / "bk").exists(), log)

p = make_bank("write")
orig = json.loads(p.read_text(encoding="utf-8"))
rc, log = run_main(["--bank", str(p), "--backup-dir", str(TMP / "write" / "bk")], GOOD)
new = json.loads(p.read_text(encoding="utf-8"))
backups = list((TMP / "write" / "bk").glob("*.json"))
check("A2 writes series on exactly the listed questions", rc == 0 and {q["id"]: q.get("series") for q in new["questions"] if q.get("series")}
      == {"s-1": "series-x", "s-2": "series-x", "s-3": "series-x"}, log)
check("A2 nothing else changes (answers, text, order, other questions)", ads.strip_series(new) == orig)
check("A2 series inserted right after chapter (readable diff)", list(new["questions"][0])[:4] == ["id", "type", "chapter", "series"])
check("A2 original format kept (indent 2, CRLF, trailing newline)",
      p.read_bytes() == (json.dumps(new, ensure_ascii=False, indent=2) + "\n").replace("\n", "\r\n").encode("utf-8"))
check("A2 backup made before writing and equals the original", len(backups) == 1 and json.loads(backups[0].read_text(encoding="utf-8")) == orig)
after = sha(p)
rc, log = run_main(["--bank", str(p), "--backup-dir", str(TMP / "write" / "bk")], GOOD)
check("A3 second run is a no-op: no write, no new backup", rc == 0 and "全部已標記，未寫入" in log and sha(p) == after
      and len(list((TMP / "write" / "bk").glob("*.json"))) == 1, log)

for label, series, expect in (
        ("different scenario", {"series-x": ["s-1", "other"]}, "情境文字不同"),
        ("missing id", {"series-x": ["s-1", "nope"]}, "不是正式題庫的頂層題目"),
        ("not a series question", {"series-x": ["s-1", "plain"]}, "沒有 Solution:"),
        ("id in two series", {"a": ["s-1", "s-2"], "b": ["s-2", "s-3"]}, "同時列在"),
        ("single-member series", {"a": ["s-1"]}, "至少需要 2 題")):
    p = make_bank(f"bad-{label.replace(' ', '-')}")
    b = sha(p)
    rc, log = run_main(["--bank", str(p), "--backup-dir", str(p.parent / "bk")], series)
    check(f"A4 refused ({label}): nothing written, no backup", rc == 2 and expect in log and sha(p) == b and not (p.parent / "bk").exists(), log)
p = make_bank("conflict")
data = json.loads(p.read_text(encoding="utf-8"))
data["questions"][0]["series"] = "series-old"
p.write_bytes((json.dumps(data, ensure_ascii=False, indent=2) + "\n").replace("\n", "\r\n").encode("utf-8"))
b = sha(p)
rc, log = run_main(["--bank", str(p), "--backup-dir", str(p.parent / "bk")], GOOD)
check("A4 refused (already in another series)", rc == 2 and "已屬於其他系列" in log and sha(p) == b, log)
p = make_bank("oddformat")
p.write_bytes(json.dumps(BANK, ensure_ascii=False, indent=4).encode("utf-8"))
b = sha(p)
rc, log = run_main(["--bank", str(p), "--backup-dir", str(p.parent / "bk")], GOOD)
check("A5 file whose formatting can't be reproduced → refused (no unrelated diff)", rc == 2 and "無法原樣重現" in log and sha(p) == b, log)
check("A6 the real SERIES list: 6 groups, 20 ids, no duplicates, qd4-qs2-q013 not included",
      len(ads.SERIES) == 6 and sum(len(v) for v in ads.SERIES.values()) == 20
      and len({i for v in ads.SERIES.values() for i in v}) == 20 and "qd4-qs2-q013" not in {i for v in ads.SERIES.values() for i in v})

# ---------------------------------------------------------------------------
# 2. 前端（Node）：series.js、mockExam.js
# ---------------------------------------------------------------------------
NODE = shutil.which("node")
LIB = TMP / "lib"
LIB.mkdir()
for name in ("grading.js", "customBank.js", "mockExam.js", "series.js"):
    src = (PROJECT / "src" / "lib" / name).read_text(encoding="utf-8")
    (LIB / name).write_text(re.sub(r"from '\./(\w+)'", r"from './\1.js'", src), encoding="utf-8")
JS = r"""
import { bindSeries, seriesBlocks, shuffleKeepingSeries } from './lib/series.js'
import { drawMockExam, pickByNumbers } from './lib/mockExam.js'
const s = (id, series) => ({ id, type: 'single_choice', series, answer: 'A', options: [] })
const c = (id, n) => ({ id, type: 'case_study', questions: Array.from({ length: n }, (_, i) => ({ id: `${id}-${i}` })) })
// 題庫順序：a1 x a2 y [case 3 子題] z a3 b1 w b2
const bank = [s('a1', 'A'), s('x'), s('a2', 'A'), s('y'), c('cs', 3), s('z'), s('a3', 'A'), s('b1', 'B'), s('w'), s('b2', 'B')]
const ids = (l) => l.map((q) => q.id)
const adjacent = (l, sid) => { const p = l.map((q, i) => (q.series === sid ? i : -1)).filter((i) => i >= 0); return p.every((v, i) => i === 0 || v === p[i - 1] + 1) }
const out = {}
out.blocks = seriesBlocks(bank).map(ids)
out.bindAll = ids(bindSeries(bank, bank))
out.expandOne = ids(bindSeries([s('y'), bank[2]], bank, { expand: true }))
out.noExpand = ids(bindSeries([bank[6], s('y'), bank[0]], bank))
out.plain = ids(bindSeries([s('p'), s('q')], [s('p'), s('q')], { expand: true }))
let shuffleOk = true
for (let i = 0; i < 300; i++) { const l = shuffleKeepingSeries(bank); if (!adjacent(l, 'A') || !adjacent(l, 'B') || l.length !== bank.length) shuffleOk = false }
out.shuffleOk = shuffleOk
let mockOk = true, sawSeries = false, sawNoSeries = false
for (let i = 0; i < 500; i++) {
  const { questions, total } = drawMockExam(bank, 4)
  const units = questions.reduce((n, q) => n + (q.type === 'case_study' ? q.questions.length : 1), 0)
  for (const sid of ['A', 'B']) {
    const k = questions.filter((q) => q.series === sid).length
    if (k && k !== bank.filter((q) => q.series === sid).length) mockOk = false
    if (!adjacent(questions, sid)) mockOk = false
    if (k) sawSeries = true; else sawNoSeries = true
  }
  if (units !== total || new Set(ids(questions)).size !== questions.length) mockOk = false
}
out.mockOk = mockOk && sawSeries && sawNoSeries
out.mockAll = drawMockExam(bank, 100).total
// 題號（以子題計）：a1=1 x=2 a2=3 y=4 cs=5-7 z=8 a3=9 b1=10 w=11 b2=12
const r1 = pickByNumbers(bank, new Set([3]))
out.range1 = { ids: ids(r1.questions), total: r1.total, added: r1.added }
const r2 = pickByNumbers(bank, new Set([2, 6]))
out.range2 = { ids: ids(r2.questions), total: r2.total, added: r2.added }
const plainBank = [s('p'), c('cq', 2), s('q')]
const r3 = pickByNumbers(plainBank, new Set([1, 3]))
out.range3 = { ids: ids(r3.questions), total: r3.total, added: r3.added }
console.log(JSON.stringify(out))
"""
if NODE:
    (TMP / "series_check.mjs").write_text(JS, encoding="utf-8")
    r = subprocess.run([NODE, str(TMP / "series_check.mjs")], capture_output=True, text=True, encoding="utf-8")
    o = json.loads(r.stdout) if r.returncode == 0 else {}
    check("N0 node script ran", r.returncode == 0, r.stderr)
    if o:
        check("N1 seriesBlocks: series members grouped at the first member's position", o["blocks"] ==
              [["a1", "a2", "a3"], ["x"], ["y"], ["cs"], ["z"], ["b1", "b2"], ["w"]], o["blocks"])
        check("N2 bindSeries on the whole bank: members consecutive, everything else in order",
              o["bindAll"] == ["a1", "a2", "a3", "x", "y", "cs", "z", "b1", "b2", "w"], o["bindAll"])
        check("N3 expand: one member pulls in the whole series (bank order), at its position", o["expandOne"] == ["y", "a1", "a2", "a3"], o["expandOne"])
        check("N4 no expand (mistakes/starred): nothing added, members made adjacent", o["noExpand"] == ["a3", "a1", "y"], o["noExpand"])
        check("N5 bank without series: unchanged", o["plain"] == ["p", "q"])
        check("N6 shuffle keeps each series adjacent and complete (300 runs)", o["shuffleOk"])
        check("N7 mock exam: series all-or-none, adjacent, unit count right, no duplicates (500 runs)", o["mockOk"])
        check("N7 mock exam larger than bank → all 12 units", o["mockAll"] == 12, o["mockAll"])
        check("N8 range hitting one member → whole series, added counts the extra members",
              o["range1"] == {"ids": ["a1", "a2", "a3"], "total": 3, "added": 2}, o["range1"])
        check("N8 range with plain + case sub-question: case kept whole, no series added",
              o["range2"] == {"ids": ["x", "cs"], "total": 4, "added": 2}, o["range2"])
        check("N9 bank without series: pickByNumbers same as before (case completeness only)",
              o["range3"] == {"ids": ["p", "cq"], "total": 3, "added": 1}, o["range3"])
else:
    print("NOTE frontend checks skipped: node not found")

# ---------------------------------------------------------------------------
# 3. 正式題庫與 AI 版（唯讀；已加入 series 後才檢查）
# ---------------------------------------------------------------------------
REAL = PROJECT / "public" / "data" / "sc200.json"
AI = PROJECT / "public" / "data" / "sc200.ai.json"
if REAL.is_file() and any(q.get("series") for q in json.loads(REAL.read_text(encoding="utf-8"))["questions"]):
    real = json.loads(REAL.read_text(encoding="utf-8"))
    got = {q["id"]: q["series"] for q in real["questions"] if q.get("series")}
    check("R1 real bank: exactly the 20 listed questions carry their series",
          got == {i: n for n, v in ads.SERIES.items() for i in v}, got)
    check("R1 real bank: plan() finds nothing left to add and no problems", ads.plan(real, ads.SERIES) == ({}, []))
    if AI.is_file():
        ai = json.loads(AI.read_text(encoding="utf-8"))
        check("R2 AI bank carries the same series", {q["id"]: q["series"] for q in ai["questions"] if q.get("series")} == got)
else:
    print("NOTE real-bank checks skipped: series not yet added to sc200.json")

check("zero network attempts", not NET_ATTEMPTS, NET_ATTEMPTS)
print(f"{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
sys.exit(1 if FAILED else 0)
