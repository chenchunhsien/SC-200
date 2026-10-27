"""audit_judge_progress.py 離線測試：假題庫、假證據包、假判讀紀錄；不連網、不呼叫 API。"""
import copy
import hashlib
import io
import json
import socket
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

PROJECT = Path(r"D:\SC-200-platform")
sys.path.insert(0, str(PROJECT / "scripts"))
import audit_judge_progress as ap  # noqa: E402

gj, va, eq = ap.gj, ap.va, ap.eq
PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    if not cond:
        print(f"FAIL {name} {detail}")


def single(qid, ans="A"):
    return {"id": qid, "type": "single_choice", "question": f"Question {qid}?",
            "options": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}], "answer": ans}


BANK = {"questions": [
    single("u-done"), single("u-pending"),
    {"id": "u-fail", "type": "multiple_choice", "question": "Pick two?",
     "options": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}, {"id": "C", "text": "c"}], "answer": ["A", "B"]},
    single("u-oldver"), single("u-oldpacket"), single("u-nopacket"), single("u-tamper"), single("u-stale"),
    single("u-review"), single("u-badruns"), single("u-history"), single("u-offline"), single("u-legacy"),
    single("u-dup"), single("u-dup", "B"),
    {"id": "u-broken", "type": "single_choice", "question": "Broken?", "answer": "A"},  # 缺 options
    {"id": "case-1", "type": "case_study", "caseInfo": {"title": "Contoso", "sections": []}, "questions": [
        single("c1-a"),
        {"id": "c1-b", "type": "statement_matrix", "question": "Statements?", "columns": ["Yes", "No"],
         "statements": [{"id": "s1", "text": "x", "answer": True}]}]},
]}
UNIQUE_UNITS = 17  # 16 個一般根題（u-dup 出現兩次，只計一次 → 15）＋ 2 個案例子題；案例題本身不計


def make_env(include_verification=True):
    d = Path(tempfile.mkdtemp(prefix="audit-"))
    bank = d / "bank.json"
    bank.write_text(json.dumps(BANK), encoding="utf-8")
    ws = d / "ws"
    pdir = ws / "gemini_judge" / "packets"
    pdir.mkdir(parents=True)
    if include_verification:
        (ws / "verification.json").write_text(json.dumps({"meta": {}, "questions": {}}), encoding="utf-8")
    bank_sha = eq.sha256_file(bank)
    units = {u.question_id: u for u in eq.iter_units(BANK)}
    shas = {}
    for qid in ("u-done", "u-pending", "u-fail", "u-oldver", "u-oldpacket", "u-tamper", "u-stale", "u-review", "u-badruns",
                "u-history", "u-offline", "u-legacy", "u-dup", "c1-a", "c1-b"):
        pkt = {"question_id": qid, "bank_sha256": "0" * 64 if qid == "u-stale" else bank_sha,
               "bank_answer": {"value": va.answer_of(units[qid])}, "examtopics": {}, "microsoft_learn": {"documents": []}}
        path, sha = gj.save_packet(pdir, pkt)
        shas[qid] = sha
        if qid == "u-tamper":
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["packet"]["bank_answer"]["value"] = "B"  # 竄改內容但不更新雜湊
            path.write_text(json.dumps(raw), encoding="utf-8")
    return d, bank, ws, shas


def good_run(sha, **over):
    r = {"at": "t", "kind": "api_call", "api_called": True, "status": "done", "model": "gemini-3.1-flash-lite",
         "prompt_version": gj.PROMPT_VERSION, "packet_sha256": sha, "model_output": {"suggested_answer": "A"},
         "evaluation": {"status": "evaluated", "evaluator_version": gj.EVALUATOR_VERSION},
         "final_exam_recommendation": {"source": "gemini", "answer": "A"}}
    r.update(over)
    return r


def fail_run(status, sha="x"):
    return {"at": "t", "kind": "api_call", "api_called": True, "status": status, "model": "gemini-3.1-flash-lite",
            "prompt_version": gj.PROMPT_VERSION, "packet_sha256": sha, "model_output": None, "evaluation": None,
            "final_exam_recommendation": {"source": "bank_fallback"}}


def write_results(ws, shas, extra_raw_dup=False):
    q = {
        "u-done": {"runs": [good_run(shas["u-done"])]},
        "u-fail": {"runs": [fail_run("deferred"), fail_run("invalid_output"),
                            dict(fail_run("invalid_answer"), evaluation={"status": "invalid_answer"})]},
        "u-oldver": {"runs": [good_run(shas["u-oldver"], prompt_version="judge-v2",
                                       evaluation={"status": "evaluated", "evaluator_version": "eval-v3"})]},
        "u-oldpacket": {"runs": [good_run("f" * 64)]},
        "u-nopacket": {"runs": [good_run("e" * 64)]},
        "u-tamper": {"runs": [good_run(shas["u-tamper"])]},
        "u-stale": {"runs": []},
        "u-review": {"runs": [good_run(shas["u-review"], evaluation=None)]},
        "u-badruns": {"runs": "not-a-list"},
        "u-history": {"runs": [good_run(shas["u-history"]), fail_run("deferred"), fail_run("invalid_output")]},
        "u-offline": {"runs": [dict(fail_run("invalid_answer"), evaluation={"status": "invalid_answer"}),
                               dict(good_run(shas["u-offline"]), kind="offline_reevaluation", api_called=False,
                                    source_run_index=0)]},
        "u-legacy": {"runs": [{k: v for k, v in good_run(shas["u-legacy"]).items() if k not in ("kind", "api_called")}]},
        "c1-b": {"runs": [fail_run("rate_limited")]},
        "x-orphan": {"runs": [good_run("a" * 64)]},
    }
    text = json.dumps({"meta": {}, "questions": q}, ensure_ascii=False)
    if extra_raw_dup:  # 原始 JSON 中重複出現 u-pending 鍵
        text = text.replace('"questions": {', '"questions": {"u-pending": {"runs": []}, "u-pending": {"runs": []}, ', 1)
    (ws / "gemini_judgments.json").write_text(text, encoding="utf-8")


def tree_hashes(*roots):
    out = {}
    for root in roots:
        for p in ([root] if root.is_file() else sorted(root.rglob("*"))):
            if p.is_file():
                out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def run_audit(bank, ws, out):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = ap.main(["--bank", str(bank), "--workspace", str(ws), "--out", str(out)])
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# 主要情境
# ---------------------------------------------------------------------------
d, bank, ws, shas = make_env()
write_results(ws, shas)
before = tree_hashes(bank, ws)

calls = []
real_socket, real_make_client, real_load_key = socket.socket, eq.make_client, eq.load_api_key
socket.socket = lambda *a, **k: (calls.append("socket"), (_ for _ in ()).throw(AssertionError("network")))[1]
eq.make_client = lambda *a, **k: (calls.append("client"), (_ for _ in ()).throw(AssertionError("client")))[1]
eq.load_api_key = lambda *a, **k: (calls.append("key"), (_ for _ in ()).throw(AssertionError("key")))[1]
try:
    out = d / "report"
    rc, log = run_audit(bank, ws, out)
finally:
    socket.socket, eq.make_client, eq.load_api_key = real_socket, real_make_client, real_load_key
check("audit rc 0", rc == 0, log)
check("no API / no client / no key / no network", calls == [], calls)
check("input SHA256 unchanged", tree_hashes(bank, ws) == before)

rep = json.loads((out / "progress_detail.json").read_text(encoding="utf-8"))
st = {u["id"]: u["status"] for u in rep["units"]}
why = {u["id"]: "；".join(u["reasons"]) for u in rep["units"]}
check("unit count = unique units (case root not counted, dup counted once)", rep["total"] == UNIQUE_UNITS
      and "case-1" not in st and "c1-a" in st and "c1-b" in st, (rep["total"], sorted(st)))
check("bank counts", rep["bank"]["root_questions"] == 17 and rep["bank"]["case_studies"] == 1
      and rep["bank"]["case_sub_questions"] == 2 and rep["bank"]["unit_entries"] == 18 and rep["bank"]["duplicate_unit_ids"] == ["u-dup"])
check("statuses sum to total", sum(rep["counts"].values()) == rep["total"] and len(st) == rep["total"])
expected = {
    "u-done": "completed", "u-pending": "pending", "c1-a": "pending", "u-fail": "failed_retryable", "c1-b": "failed_retryable",
    "u-oldver": "needs_rejudge", "u-oldpacket": "needs_rejudge", "u-offline": "needs_rejudge", "u-legacy": "needs_rejudge",
    "u-nopacket": "blocked", "u-tamper": "blocked", "u-stale": "blocked", "u-broken": "blocked",
    "u-review": "review_required", "u-badruns": "review_required", "u-dup": "review_required",
    "u-history": "completed",
}
for qid, s in expected.items():
    check(f"classify {qid} → {s}", st.get(qid) == s, (st.get(qid), why.get(qid)))
check("all six statuses present", set(rep["counts"]) == set(ap.STATUSES) and all(rep["counts"][s] > 0 for s in ap.STATUSES), rep["counts"])
check("old version not counted as completed", "prompt 版本 judge-v2" in why["u-oldver"] and "判定版本 eval-v3" in why["u-oldver"])
check("offline re-check differs from API run", "離線重核" in why["u-offline"])
check("legacy record without kind not completed", "未標示" in why["u-legacy"])
check("missing / tampered / stale packet reasons", "缺少證據包" in why["u-nopacket"] and "雜湊不符" in why["u-tamper"]
      and "題庫已變更" in why["u-stale"])
check("done without evaluation → review", "status=done 但沒有成功核對結果" in why["u-review"])
check("broken question data → blocked", "題目資料不完整" in why["u-broken"])
check("duplicate unit id → review", "題號重複" in why["u-dup"])
ud = next(u for u in rep["units"] if u["id"] == "u-history")
check("history: success then failures still completed, indexes recorded", ud["completed_run_index"] == 0
      and ud["effective_run_index"] == 0 and ud["run_count"] == 3)
uf = next(u for u in rep["units"] if u["id"] == "u-fail")
check("failed history lists statuses", "deferred" in why["u-fail"] and "invalid_output" in why["u-fail"]
      and "invalid_answer" in why["u-fail"] and uf["run_count"] == 3)
check("completed agrees with completed_run()", all(
    gj.completed_run(json.loads((ws / "gemini_judgments.json").read_text(encoding="utf-8"))["questions"][u["id"]]["runs"],
                     u["packet"]["sha256"], "gemini-3.1-flash-lite") is not None
    for u in rep["units"] if u["status"] == "completed"))
check("orphan record listed, not counted", rep["orphan_record_ids"] == ["x-orphan"] and "x-orphan" not in st)

pend = (out / "pending_ids.txt").read_text(encoding="utf-8").splitlines()
check("pending_ids = pending + failed_retryable only", sorted(x for x in pend if not x.startswith("#"))
      == sorted(["u-pending", "c1-a", "u-fail", "c1-b"]), pend)
rej = [x for x in (out / "needs_rejudge_ids.txt").read_text(encoding="utf-8").splitlines() if not x.startswith("#")]
check("needs_rejudge_ids", sorted(rej) == sorted(["u-oldver", "u-oldpacket", "u-offline", "u-legacy"]), rej)
br = [x for x in (out / "blocked_review_ids.txt").read_text(encoding="utf-8").splitlines() if not x.startswith("#")]
check("blocked_review_ids with reasons + orphan", len(br) == 8 and all("\t" in x for x in br)
      and any(x.startswith("x-orphan\torphan_record") for x in br), br)
summ = (out / "progress_summary.md").read_text(encoding="utf-8")
check("summary metrics", all(s in summ for s in ("有效判讀完成率", "證據包完整率", "可安全補跑", "需要重判", "受阻與人工檢視",
                                                 "✅ 一致", "不代表答案已被獨立證實正確", "與 --run 跳過條件的差異")), summ[:500])
check("summary counting explanation", "案例題本身不計" in summ and f"**{UNIQUE_UNITS}**" in summ)

# ---------------------------------------------------------------------------
# 重複鍵、工作區不可執行、空集合、輸出目錄保護
# ---------------------------------------------------------------------------
d2, bank2, ws2, shas2 = make_env()
write_results(ws2, shas2, extra_raw_dup=True)
rc, log = run_audit(bank2, ws2, d2 / "report")
rep2 = json.loads((d2 / "report" / "progress_detail.json").read_text(encoding="utf-8"))
st2 = {u["id"]: u for u in rep2["units"]}
check("duplicate JSON key in results → review", st2["u-pending"]["status"] == "review_required"
      and "u-pending" in rep2["workspace"]["results_duplicate_keys"])

d3, bank3, ws3, shas3 = make_env(include_verification=False)
write_results(ws3, shas3)
rc, log = run_audit(bank3, ws3, d3 / "report")
rep3 = json.loads((d3 / "report" / "progress_detail.json").read_text(encoding="utf-8"))
st3 = {u["id"]: u["status"] for u in rep3["units"]}
check("workspace not runnable → pending/failed become blocked", st3["u-pending"] == "blocked" and st3["u-fail"] == "blocked"
      and st3["u-done"] == "completed" and rep3["workspace"]["preflight_problems"], st3)
check("not runnable: sums still match", sum(rep3["counts"].values()) == rep3["total"] == UNIQUE_UNITS)

d4 = Path(tempfile.mkdtemp(prefix="audit-empty-"))
bank4 = d4 / "bank.json"
bank4.write_text(json.dumps({"questions": [single("only-one")]}), encoding="utf-8")
ws4 = d4 / "ws"
(ws4 / "gemini_judge" / "packets").mkdir(parents=True)
(ws4 / "verification.json").write_text(json.dumps({"questions": {}}), encoding="utf-8")
u4 = next(eq.iter_units({"questions": [single("only-one")]}))
_, sha4 = gj.save_packet(ws4 / "gemini_judge" / "packets", {"question_id": "only-one", "bank_sha256": eq.sha256_file(bank4),
                                                            "bank_answer": {"value": "A"}})
(ws4 / "gemini_judgments.json").write_text(json.dumps({"questions": {"only-one": {"runs": [good_run(sha4)]}}}), encoding="utf-8")
rc, log = run_audit(bank4, ws4, d4 / "report")
for f in ("pending_ids.txt", "needs_rejudge_ids.txt", "blocked_review_ids.txt"):
    txt = (d4 / "report" / f).read_text(encoding="utf-8")
    check(f"empty list rendered clearly: {f}", rc == 0 and "0 筆（空集合）" in txt
          and not [x for x in txt.splitlines() if x and not x.startswith("#")], txt)
check("all completed → 100%", "1／1（100.0%）" in (d4 / "report" / "progress_summary.md").read_text(encoding="utf-8"))

d5, bank5, ws5, shas5 = make_env()
(d5 / "existing").mkdir()
(d5 / "existing" / "keep.txt").write_text("keep", encoding="utf-8")
rc, log = run_audit(bank5, ws5, d5 / "existing")
check("existing output dir refused, untouched", rc == 2 and "已存在" in log
      and [p.name for p in (d5 / "existing").iterdir()] == ["keep.txt"])
rc, log = run_audit(bank5, ws5, ws5 / "report")
check("output inside workspace refused", rc == 2 and not (ws5 / "report").exists())
rc, log = run_audit(bank5, ws5, ws5)
check("output == workspace refused", rc == 2)

# 無結果檔（全新工作區）
d6, bank6, ws6, shas6 = make_env()
rc, log = run_audit(bank6, ws6, d6 / "report")
rep6 = json.loads((d6 / "report" / "progress_detail.json").read_text(encoding="utf-8"))
check("no results file → no completed, packets → pending", rc == 0 and rep6["counts"]["completed"] == 0
      and {u["id"]: u["status"] for u in rep6["units"]}["u-done"] == "pending")

print(f"{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
sys.exit(1 if FAILED else 0)
