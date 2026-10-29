"""export_review_list.py 離線測試：最小合成題庫、證據包與判讀紀錄；不連網、不呼叫 API。
另含一組唯讀的真實資料對應檢查（正式工作區存在時才執行，輸出寫到暫存目錄）。"""
import atexit
import csv
import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

PROJECT = Path(r"D:\SC-200-platform")
sys.path.insert(0, str(PROJECT / "scripts"))
import export_review_list as erl  # noqa: E402

gj, va = erl.gj, erl.va
PASSED, FAILED = [], []
NET_ATTEMPTS = []


def _no_network(*a, **k):
    NET_ATTEMPTS.append(a[1:2])
    raise AssertionError("network access attempted in test")


socket.socket.connect = _no_network
socket.create_connection = _no_network

TMP = Path(tempfile.mkdtemp(prefix="export-review-"))


def _cleanup_tmp():  # 設定 KEEP_TEST_TMP=1 可保留以便除錯
    if os.environ.get("KEEP_TEST_TMP") != "1":
        shutil.rmtree(TMP, ignore_errors=True)


atexit.register(_cleanup_tmp)


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    if not cond:
        print(f"FAIL {name} {detail}")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def tree(root, skip=(gj.LOCK_NAME,)):
    return {str(p.relative_to(root)): sha(p) for p in sorted(Path(root).rglob("*")) if p.is_file() and p.name not in skip}


def run_main(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = erl.main(argv)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# 最小合成資料
# ---------------------------------------------------------------------------

def single(qid, ans="A"):
    return {"id": qid, "type": "single_choice", "question": f"Question {qid}?",
            "options": [{"id": "A", "text": "alpha"}, {"id": "B", "text": "beta"}], "answer": ans}


BANK = {"title": "t", "chapters": [], "questions": [
    single("e-agree"),
    single("e-dis"),
    {"id": "e-conf", "type": "multiple_choice", "question": "Pick two?", "answer": ["A", "C"],
     "options": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}, {"id": "C", "text": "c"}]},
    {"id": "e-fb", "type": "dropdown_cloze", "question": "Complete the query.", "templateType": "code",
     "template": "{{select_0}}\n{{select_1}}",
     "blanks": [{"id": "select_0", "options": ["T1", "T2"], "answer": "T1"},
                {"id": "select_1", "options": ["| where x", "| project y"], "answer": "| where x"}]},
    {"id": "e-def", "type": "statement_matrix", "question": "Statements?", "columns": ["Yes", "No"],
     "statements": [{"id": "s1", "text": "first", "answer": True}, {"id": "s2", "text": "second", "answer": False}]},
    single("e-nodoc"),
    {**single("e-nopacket"), "reviewNote": "已依 PDF 作者標註更正；原題庫答案為 B。"},
    {"id": "e-case", "type": "case_study", "question": "Case study.",
     "caseInfo": {"title": "Contoso 情境", "sections": [{"title": "Overview", "content": "Contoso 是一家公司。"}]},
     "questions": [single("e-case-q1", "B")]},
]}
DOC = {"doc_id": "D1", "title": "Learn page", "url": "https://learn.microsoft.com/en-us/x/page", "passages": []}


def packet_for(pdir, qid, bank_value, docs=(DOC,), status="found"):
    p, _ = gj.save_packet(pdir, {"question_id": qid, "bank_answer": {"value": bank_value},
                                 "microsoft_learn": {"status": status, "note": "" if docs else "尚未找到官方依據",
                                                     "documents": list(docs)}})
    return p.name


def done_run(packet_file, suggested, cmp, cites=(), conflicts=(), level="低"):
    return {"at": "2026-10-28T00:00:00+00:00", "kind": "api_call", "api_called": True, "status": "done", "http_status": 200,
            "error": None, "packet_file": packet_file,
            "model_output": {"rationale": f"理由：選 {suggested}", "conflicts": [], "open_questions": ["待確認事項"]},
            "evaluation": {"status": "evaluated", "suggested_answer": suggested, "agrees_with_bank": cmp,
                           "confidence": level, "official_evidence_assessment": {"confidence": level},
                           "citations": list(cites), "official_conflicts": list(conflicts), "evidence_gaps": [],
                           "incomplete_reasons": []},
            "final_exam_recommendation": {"answer": suggested, "source": "gemini"}}


def cite(target, quote, check_="found"):
    return {"target_id": target, "passage_id": "D1-P1", "quote": quote, "quote_check": check_,
            "relation_claimed_by_model": "supports_option", "alignment": "mentions_option_and_scenario"}


def build_fixture(name):
    root = TMP / name
    bank = root / "bank.json"
    root.mkdir(parents=True)
    bank.write_text(json.dumps(BANK, ensure_ascii=False), encoding="utf-8")
    ws = root / "ws"
    pdir = ws / "gemini_judge" / "packets"
    runs = {
        "e-agree": [done_run(packet_for(pdir, "e-agree", "A"), "A", "agree", [cite("A", "alpha is right")])],
        "e-dis": [done_run(packet_for(pdir, "e-dis", "A"), "B", "disagree",
                           [cite("B", "beta is right"), cite("A", "missing quote", "not_found")])],
        "e-conf": [done_run(packet_for(pdir, "e-conf", ["A", "C"]), ["A", "C"], "agree", [cite("A", "a and c")],
                            conflicts=["可核對的官方引句被模型標為反駁建議答案中的 C"], level="中")],
        "e-fb": [{"at": "t", "kind": "api_call", "api_called": True, "status": "invalid_answer", "http_status": 200,
                  "error": None, "packet_file": packet_for(pdir, "e-fb", {"select_0": "T1", "select_1": "| where x"}),
                  "model_output": {"rationale": "填空理由"},
                  "evaluation": {"status": "invalid_answer", "error": "建議答案格式不符：select_1",
                                 "answer_parse": {"raw": '{"select_0": "T2", "select_1": ""}'}},
                  "final_exam_recommendation": {"answer": {"select_0": "T1", "select_1": "| where x"}, "source": "bank_fallback"}}],
        "e-def": [{"at": "t", "kind": "api_call", "api_called": True, "status": "deferred", "http_status": 503,
                   "error": "ServerError(503): UNAVAILABLE", "packet_file": packet_for(pdir, "e-def", {"s1": True, "s2": False}),
                   "model_output": None, "evaluation": None,
                   "final_exam_recommendation": {"answer": {"s1": True, "s2": False}, "source": "bank_fallback"}}],
        "e-nodoc": [done_run(packet_for(pdir, "e-nodoc", "A", docs=(), status="not_found"), "B", "disagree")],
        "e-nopacket": [done_run("e-nopacket.20990101-000000-000000.json", "A", "disagree")],
        "e-case-q1": [done_run(packet_for(pdir, "e-case-q1", "B"), "A", "disagree")],
    }
    (ws / "gemini_judgments.json").write_text(json.dumps({"meta": {}, "questions": {q: {"runs": r} for q, r in runs.items()}},
                                                         ensure_ascii=False), encoding="utf-8")
    # 報告由判讀程式真正的報告產生函式輸出（與正式 pipeline 報告同格式）
    ctx = va.Ctx(bank, ws)
    info = {"started_at": "t0", "limit": 8, "model": "m", "tier": "free", "ready": [], "new": [], "phase2": [],
            "stop": None, "unprocessed": [], "rc": 0, "before": gj.pipeline_state(ws)}
    report = ws / "reports" / "pipeline_summary_20990101_000000.md"
    report.parent.mkdir()
    report.write_text(gj.render_pipeline_report(info, gj.pipeline_stats(ctx, info), "t1") + "\n", encoding="utf-8")
    return root, bank, ws, report


EXPECTED = {"e-dis": ["disagree"], "e-conf": ["conflict"], "e-fb": ["fallback"], "e-def": ["deferred"],
            "e-nodoc": ["disagree"], "e-nopacket": ["disagree"], "e-case-q1": ["disagree"]}

# ---------------------------------------------------------------------------
# 1. 匯出：分類、對應、缺漏標記
# ---------------------------------------------------------------------------
root, bank, ws, report = build_fixture("main")
before = {"bank": sha(bank), "ws": tree(ws)}
out = root / "export"
rc, log = run_main(["--bank", str(bank), "--workspace", str(ws), "--report", str(report), "--out", str(out)])
check("E0 export succeeds", rc == 0 and out.is_dir(), log)
data = json.loads((out / "review_list.json").read_text(encoding="utf-8"))
qs = {q["id"]: q for q in data["questions"]}
meta = data["meta"]
check("E1 review list = the 7 questions needing review; agreeing question excluded", set(qs) == set(EXPECTED), sorted(qs))
check("E1 each question's categories", all(qs[q]["categories"] == c for q, c in EXPECTED.items()),
      {q: qs[q]["categories"] for q in qs})
check("E1 category counts (any / primary): 4 disagree, 1 conflict, 1 fallback, 1 deferred",
      meta["category_any"] == {"disagree": 4, "conflict": 1, "fallback": 1, "deferred": 1} == meta["category_primary"],
      meta)
check("E2 report ↔ current data: count, ids, reasons, outcome table, bank mapping all consistent",
      "報告宣告待人工複核 7 題；報告實際列出 7 題；依目前判讀紀錄重新計算 7 題" in meta["checks"][0]
      and "完全一致" in meta["checks"][1] and "與報告一致" in meta["checks"][2] and "與報告一致" in meta["checks"][3]
      and "全部題號都在" in meta["checks"][4], meta["checks"])
check("E2 source mapping: bank answer from the bank, Gemini answer from the evaluation, docs/quotes from the packet",
      qs["e-dis"]["bank_answer"] == "A" and qs["e-dis"]["gemini_answer"] == "B"
      and qs["e-dis"]["documents"] == [{"doc_id": "D1", "title": "Learn page", "url": DOC["url"]}]
      and qs["e-dis"]["citations"][0]["url"] == DOC["url"] and qs["e-dis"]["citations"][0]["quote"] == "beta is right"
      and qs["e-conf"]["official_conflicts"] == ["可核對的官方引句被模型標為反駁建議答案中的 C"], qs["e-dis"])
check("E2 case-study sub-question keeps its case title; existing reviewNote carried over",
      qs["e-case-q1"]["case_title"] == "Contoso 情境" and qs["e-nopacket"]["review_note"].startswith("已依 PDF"), qs["e-case-q1"])


def has_flag(q, text):
    return any(text in f for f in qs[q]["flags"])


check("E3 no Learn documents → flagged as insufficient official evidence", has_flag("e-nodoc", "沒有 Microsoft Learn 官方文件")
      and qs["e-nodoc"]["documents"] == [] and qs["e-nodoc"]["learn_status"] == "not_found", qs["e-nodoc"]["flags"])
check("E3 documents but no citations → flagged", has_flag("e-case-q1", "沒有引用任何段落"), qs["e-case-q1"]["flags"])
check("E3 quote not found in packet → flagged, not treated as evidence", has_flag("e-dis", "1 則引文在證據包中找不到原文"),
      qs["e-dis"]["flags"])
check("E3 missing packet file → flagged, no documents invented", has_flag("e-nopacket", "找不到判讀時使用的證據包")
      and qs["e-nopacket"]["documents"] == [] and qs["e-nopacket"]["citations"] == [], qs["e-nopacket"])
check("E3 invalid Gemini output → no Gemini answer, raw output shown as invalid, bank answer kept",
      qs["e-fb"]["gemini_answer"] is None and qs["e-fb"]["gemini_note"].startswith("無效輸出")
      and qs["e-fb"]["final_source"] == "bank_fallback" and has_flag("e-fb", "沒有有效的 Gemini 判讀"), qs["e-fb"])
check("E3 deferred → no Gemini answer, error and HTTP status kept, no evidence level invented",
      qs["e-def"]["gemini_answer"] is None and qs["e-def"]["gemini_note"] == "沒有模型輸出" and qs["e-def"]["http_status"] == 503
      and "503" in qs["e-def"]["error"] and qs["e-def"]["evidence_level"] == gj.NOT_ASSESSED, qs["e-def"])

# ---- 輸出格式一致 ----
md = (out / "review_list.md").read_text(encoding="utf-8")
raw_csv = (out / "review_list.csv").read_bytes()
rows = list(csv.DictReader(io.StringIO(raw_csv.decode("utf-8-sig"))))
order = [q["id"] for q in data["questions"]]
check("E4 Markdown: one section and one index row per question, same order as JSON",
      md.count("\n### ") == len(order) == 7 and [l.split("### ")[1] for l in md.splitlines() if l.startswith("### ")] == order
      and sum(1 for l in md.splitlines() if l.startswith("| ") and "](#" in l) == 7, order)
check("E4 CSV: same rows and order as JSON, expected columns", [r["id"] for r in rows] == order
      and list(rows[0]) == erl.CSV_COLUMNS, [r["id"] for r in rows])
check("E4 sorted by category order then bank order", order == ["e-dis", "e-nodoc", "e-nopacket", "e-case-q1", "e-conf", "e-fb", "e-def"],
      order)
check("E5 CSV starts with UTF-8 BOM and Chinese survives", raw_csv.startswith(b"\xef\xbb\xbf")
      and rows[0]["primary_category"] == "答案不一致／部分一致" and "理由" in rows[0]["rationale"], raw_csv[:20])
check("E5 Markdown shows question text, options, case context and BOM-free UTF-8",
      "Question e-dis?" in md and "- **B**. beta" in md and "題組情境：Contoso 情境" in md and not md.startswith("\ufeff"))

# ---- 不修改輸入；不覆寫輸出 ----
check("E6 bank, results, packets and report unchanged by export", sha(bank) == before["bank"] and tree(ws) == before["ws"])
out_before = tree(out)
rc, log = run_main(["--bank", str(bank), "--workspace", str(ws), "--report", str(report), "--out", str(out)])
check("E6 existing output directory → refused, nothing overwritten", rc == 2 and "不覆寫" in log and tree(out) == out_before, log)

# ---------------------------------------------------------------------------
# 2. 報告與資料不一致時列出差異，不自行修正
# ---------------------------------------------------------------------------
bad_report = root / "bad_report.md"
text = report.read_text(encoding="utf-8").replace("- e-conf：", "- e-ghost：")
bad_report.write_text(text, encoding="utf-8")
out2 = root / "export2"
rc, log = run_main(["--bank", str(bank), "--workspace", str(ws), "--report", str(bad_report), "--out", str(out2)])
d2 = json.loads((out2 / "review_list.json").read_text(encoding="utf-8"))
q2 = {q["id"]: q for q in d2["questions"]}
check("E7 id mismatch reported (only in report / only in data)", rc == 0 and "僅報告有 ['e-ghost']" in d2["meta"]["checks"][1]
      and "僅目前資料有 ['e-conf']" in d2["meta"]["checks"][1], d2["meta"]["checks"])
check("E7 unmappable id kept and flagged, not filled in", "e-ghost" in q2 and q2["e-ghost"]["bank_answer"] is None
      and any("不在目前正式題庫" in f for f in q2["e-ghost"]["flags"]) and q2["e-ghost"]["categories"] == ["other"]
      and q2["e-ghost"]["question"] is None, q2.get("e-ghost"))
check("E7 data-only question flagged as missing from the report", any("報告未列出此題" in f for f in q2["e-conf"]["flags"]))

# ---------------------------------------------------------------------------
# 3. 單一函式
# ---------------------------------------------------------------------------
pr = erl.parse_report(report.read_text(encoding="utf-8"))
check("U1 parse_report: declared count, ids and reasons", pr["declared"] == 7 and set(pr["review"]) == set(EXPECTED)
      and pr["review"]["e-def"][0].startswith("未取得有效判讀"), pr)
check("U1 parse_report on text without the section → empty, no crash", erl.parse_report("# nothing")["review"] == {}
      and erl.parse_report("# nothing")["declared"] is None)
ctx = va.Ctx(bank, ws)
r_none = erl.build_record("e-dis", None, ctx, ws / "gemini_judge" / "packets", [])
check("U2 build_record with no runs → unknown/other, no Gemini answer, nothing invented",
      r_none["outcome"] == "unknown" and r_none["categories"] == ["other"] and r_none["gemini_answer"] is None
      and r_none["documents"] == [] and r_none["status"] == erl.MISSING, r_none)
r_garbage = erl.build_record("e-dis", [{"kind": "api_call"}], ctx, ws / "gemini_judge" / "packets", [])
check("U2 run missing fields → unknown, missing values labelled", r_garbage["outcome"] == "unknown"
      and r_garbage["final_source"] == erl.MISSING and any("沒有記錄證據包檔名" in f for f in r_garbage["flags"]), r_garbage)
check("U3 choices_text covers options, statements and blanks",
      "A. alpha" in erl.choices_text(BANK["questions"][0]) and "s1. first" in erl.choices_text(BANK["questions"][4])
      and "select_0: T1 / T2" in erl.choices_text(BANK["questions"][3]))

# ---------------------------------------------------------------------------
# 4. 真實資料（唯讀）：最新 pipeline 報告的複核清單全部能對應；輸出寫到暫存目錄
# ---------------------------------------------------------------------------
REAL_WS = PROJECT / "output" / "gemini_run_434"
REAL_REPORT = REAL_WS / "reports" / "pipeline_summary_20261029_091759.md"
if REAL_REPORT.is_file():
    real_before = {"bank": sha(va.DEFAULT_BANK), "results": sha(REAL_WS / "gemini_judgments.json"),
                   "packets": tree(REAL_WS / "gemini_judge"), "reports": tree(REAL_WS / "reports")}
    out_r = TMP / "real_export"
    rc, log = run_main(["--workspace", str(REAL_WS), "--report", str(REAL_REPORT), "--out", str(out_r)])
    dr = json.loads((out_r / "review_list.json").read_text(encoding="utf-8"))
    declared = erl.parse_report(REAL_REPORT.read_text(encoding="utf-8"))["declared"]
    check("R1 real report: every listed id maps to the bank and current data, reasons and stats match",
          rc == 0 and len(dr["questions"]) == declared and "完全一致" in dr["meta"]["checks"][1]
          and all("與報告一致" in c for c in dr["meta"]["checks"][2:4]) and "全部題號都在" in dr["meta"]["checks"][4],
          dr["meta"]["checks"])
    check("R1 real categories sum to the declared count; no 'other'",
          sum(dr["meta"]["category_primary"].values()) == declared and "other" not in dr["meta"]["category_primary"],
          dr["meta"]["category_primary"])
    check("R2 real bank, results, packets and reports unchanged",
          real_before == {"bank": sha(va.DEFAULT_BANK), "results": sha(REAL_WS / "gemini_judgments.json"),
                          "packets": tree(REAL_WS / "gemini_judge"), "reports": tree(REAL_WS / "reports")})
else:
    print(f"NOTE real-data check skipped: {REAL_REPORT} not present")

check("zero network attempts", not NET_ATTEMPTS, NET_ATTEMPTS)
print(f"{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
sys.exit(1 if FAILED else 0)
