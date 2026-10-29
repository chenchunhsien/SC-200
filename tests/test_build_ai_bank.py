"""build_ai_bank.py 離線測試：最小合成題庫、判讀紀錄與證據包；不連網、不呼叫 API。
前端部分以 Node 直接載入真正的 src/lib/grading.js、validateBank.js、aiAnswer.js 驗證（找不到 node 時略過並提示）。
另含一組唯讀的真實資料檢查（正式資料存在時才執行，輸出寫到暫存目錄）。"""
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

PROJECT = Path(r"D:\SC-200-platform")
sys.path.insert(0, str(PROJECT / "scripts"))
import build_ai_bank as bab  # noqa: E402

gj, va = bab.gj, bab.va
PASSED, FAILED = [], []
NET_ATTEMPTS = []


def _no_network(*a, **k):
    NET_ATTEMPTS.append(a[1:2])
    raise AssertionError("network access attempted in test")


socket.socket.connect = _no_network
socket.create_connection = _no_network
TMP = Path(tempfile.mkdtemp(prefix="ai-bank-"))


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


def tree(root):
    return {str(p.relative_to(root)): sha(p) for p in sorted(Path(root).rglob("*")) if p.is_file() and p.name != gj.LOCK_NAME}


def run_main(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = bab.main(argv)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# 合成資料
# ---------------------------------------------------------------------------
def single(qid, ans="A", stem=None):
    return {"id": qid, "type": "single_choice", "question": stem or f"Question {qid}?", "answer": ans,
            "options": [{"id": "A", "text": "alpha"}, {"id": "B", "text": "beta"}, {"id": "C", "text": "gamma"}]}


def drag(qid, ans, stem):
    return {"id": qid, "type": "drag_drop_order", "question": stem, "answer": ans,
            "items": [{"id": x, "text": f"step {x}"} for x in "abcdef"]}


def matrix(qid, answers):
    return {"id": qid, "type": "statement_matrix", "question": "For each statement select Yes or No.",
            "statements": [{"id": f"s{i + 1}", "text": f"statement {i + 1}", "answer": a} for i, a in enumerate(answers)]}


BANK = {"$schema": "./questions.schema.json", "id": "sc200", "title": "測試題庫", "chapters": [{"id": "d1", "title": "D1"}],
        "questions": [
            {**single("t-agree"), "chapter": "d1", "reviewNote": "既有備註保留", "explanation": ""},
            single("t-differ"),
            {"id": "t-multi", "type": "multiple_choice", "question": "Which two options apply?", "answer": ["A", "B"],
             "options": [{"id": x, "text": f"opt {x}"} for x in "ABCD"]},
            {"id": "t-drop", "type": "dropdown_cloze", "question": "Complete.", "templateType": "code",
             "template": "{{select_0}} {{select_1}}",
             "blanks": [{"id": "select_0", "options": ["T1", "T2"], "answer": "T1"},
                        {"id": "select_1", "options": ["| where", "| project"], "answer": "| where"}]},
            matrix("t-matrix", [True, False]),
            drag("t-drag", ["a", "b", "c"], "Which three actions should you perform in sequence?"),
            drag("t-drag-count", ["b", "f", "c"], "Which three actions should you perform in sequence?"),
            drag("t-drag-nocount", ["a", "b"], "Arrange the actions in the correct order."),
            {"id": "t-case", "type": "case_study", "question": "Case study.", "chapter": "d1",
             "caseInfo": {"title": "Contoso", "sections": [{"title": "Overview", "content": "Contoso 情境全文。"}]},
             "questions": [single("t-case-q1", "A"), matrix("t-case-q2", [False, False])]},
            single("t-deferred"), single("t-fallback"), single("t-community", "C"), single("t-stale"),
            single("t-conflict"), single("t-nopacket"), single("t-badsingle"), single("t-norun"),
            {"id": "t-baddrop", "type": "dropdown_cloze", "question": "Complete.", "templateType": "code", "template": "{{select_0}}",
             "blanks": [{"id": "select_0", "options": ["X", "Y"], "answer": "X"}]},
            matrix("t-badmatrix", [True]),
        ]}
DOC1 = {"doc_id": "D1", "title": "Cited doc", "url": "https://learn.microsoft.com/en-us/x/cited"}
DOC2 = {"doc_id": "D2", "title": "Uncited doc", "url": "https://learn.microsoft.com/en-us/x/uncited"}


def natural_of(q):
    return va.answer_of(bab.SimpleUnit(q))


def build_fixture(name, mutate_bank=None):
    root = TMP / name
    root.mkdir(parents=True)
    bank = copy.deepcopy(BANK)
    if mutate_bank:
        mutate_bank(bank)
    bank_path = root / "bank.json"
    bank_path.write_text(json.dumps(bank, ensure_ascii=False), encoding="utf-8")
    ws = root / "ws"
    pdir = ws / "gemini_judge" / "packets"
    by_id = {u["id"]: u for u in bab.answerable(BANK["questions"])}  # 判讀時的題庫（未變更版本）

    def packet(qid, docs=(DOC1, DOC2), bank_value=None):
        p, _ = gj.save_packet(pdir, {"question_id": qid, "bank_sha256": "judging-time-sha",
                                     "bank_answer": {"value": natural_of(by_id[qid]) if bank_value is None else bank_value},
                                     "microsoft_learn": {"status": "found", "documents": list(docs)}})
        return p.name

    def done(qid, suggested, cmp="disagree", conflicts=(), cites=None, pf=None):
        cites = cites if cites is not None else [
            {"target_id": "A", "passage_id": "D1-P1", "quote": f"quote for {qid}", "quote_check": "found",
             "relation_claimed_by_model": "supports_option"},
            {"target_id": "B", "passage_id": "D2-P1", "quote": "fabricated quote", "quote_check": "not_found",
             "relation_claimed_by_model": "supports_option"}]
        return [{"at": "2026-10-28T00:00:00+00:00", "kind": "api_call", "api_called": True, "status": "done",
                 "packet_file": pf or packet(qid), "model_output": {"rationale": f"{qid} 的 AI 理由"},
                 "evaluation": {"status": "evaluated", "suggested_answer": suggested, "agrees_with_bank": cmp,
                                "official_evidence_assessment": {"confidence": "低"}, "citations": cites,
                                "official_conflicts": list(conflicts)},
                 "final_exam_recommendation": {"answer": suggested, "source": "gemini"}}]

    runs = {
        "t-agree": done("t-agree", "A", "agree"),
        "t-differ": done("t-differ", "B"),
        "t-multi": done("t-multi", ["A", "C"], "partial"),
        "t-drop": done("t-drop", {"select_0": "T2", "select_1": "| where"}, "partial"),
        "t-matrix": done("t-matrix", {"s1": "No", "s2": "Yes"}),
        "t-drag": done("t-drag", ["c", "b", "a"], "partial"),
        "t-drag-count": done("t-drag-count", ["b", "f", "e", "a"], "partial"),   # 題幹要求 three，判讀 4 個
        "t-drag-nocount": done("t-drag-nocount", ["a", "b", "c"], "partial"),   # 題幹沒寫數量 → 以原答案長度 2 為準
        "t-case-q1": done("t-case-q1", "C"),
        "t-case-q2": done("t-case-q2", {"s1": "No", "s2": "No"}, "agree"),
        "t-deferred": [{"at": "t", "kind": "api_call", "api_called": True, "status": "deferred", "http_status": 503,
                        "packet_file": packet("t-deferred"), "model_output": None, "evaluation": None,
                        "final_exam_recommendation": {"answer": "A", "source": "bank_fallback"}}],
        "t-fallback": [{"at": "t", "kind": "api_call", "api_called": True, "status": "invalid_answer",
                        "packet_file": packet("t-fallback"), "model_output": {"rationale": "無效輸出的理由"},
                        "evaluation": {"status": "invalid_answer", "answer_parse": {"raw": "Z"}},
                        "final_exam_recommendation": {"answer": "A", "source": "bank_fallback"}}],
        "t-community": [{"at": "t", "kind": gj.COMMUNITY_KIND, "api_called": False, "status": gj.COMMUNITY_KIND,
                         "evaluator_version": gj.EVALUATOR_VERSION, "packet_rules_version": gj.PACKET_RULES_VERSION,
                         "model_output": None, "packet_file": packet("t-community"),
                         "community_qualification": {"qualifies": True},
                         "final_exam_recommendation": {"answer": "C", "source": "community"}}],
        "t-stale": done("t-stale", "B", pf=packet("t-stale", bank_value="C")),       # 判讀時題庫答案是 C，目前是 A
        "t-conflict": done("t-conflict", "A", "agree", conflicts=["可核對的官方引句被模型標為反駁建議答案中的 A"]),
        "t-nopacket": done("t-nopacket", "B", pf="t-nopacket.20990101-000000-000000.json"),
        "t-badsingle": done("t-badsingle", "Z"),
        "t-baddrop": done("t-baddrop", {"select_0": "W"}),
        "t-badmatrix": done("t-badmatrix", {"s1": "Maybe"}),
    }
    (ws / "gemini_judgments.json").write_text(json.dumps({"meta": {}, "questions": {q: {"runs": r} for q, r in runs.items()}},
                                                         ensure_ascii=False), encoding="utf-8")
    review = root / "review_list.json"
    review.write_text(json.dumps({"meta": {}, "questions": [{"id": "t-differ"}, {"id": "t-case-q1"}, {"id": "t-deferred"}]}),
                      encoding="utf-8")
    return root, bank_path, ws, review


EXPECTED_STATUS = {
    "t-agree": "agree", "t-differ": "differ", "t-multi": "differ", "t-drop": "differ", "t-matrix": "differ",
    "t-drag": "differ", "t-drag-count": "invalid_spec", "t-drag-nocount": "invalid_spec", "t-case-q1": "differ",
    "t-case-q2": "agree", "t-deferred": "deferred", "t-fallback": "fallback", "t-community": "community",
    "t-stale": "stale", "t-conflict": "agree", "t-nopacket": "basis_missing", "t-badsingle": "invalid_spec",
    "t-norun": "unavailable", "t-baddrop": "invalid_spec", "t-badmatrix": "invalid_spec"}

# ---------------------------------------------------------------------------
# 1. 產生與規則
# ---------------------------------------------------------------------------
root, bank_path, ws, review = build_fixture("main")
src_before = {"bank": sha(bank_path), "ws": tree(ws), "review": sha(review)}
out_path = root / "public" / "sc200.ai.json"
rc, log = run_main(["--bank", str(bank_path), "--workspace", str(ws), "--review", str(review), "--out", str(out_path)])
check("B0 build succeeds", rc == 0 and out_path.is_file(), log)
out = json.loads(out_path.read_text(encoding="utf-8"))
U = {u["id"]: u for u in bab.answerable(out["questions"])}
S = {u["id"]: u for u in bab.answerable(BANK["questions"])}
check("B1 every question gets the expected answerStatus", {k: U[k]["ai"]["answerStatus"] for k in EXPECTED_STATUS} == EXPECTED_STATUS,
      {k: U[k]["ai"]["answerStatus"] for k in EXPECTED_STATUS})
check("B1 practiceSource: ai only for agree/differ", all(
    U[k]["ai"]["practiceSource"] == ("ai" if s in ("agree", "differ") else "original") for k, s in EXPECTED_STATUS.items()))

# ---- 題型轉換 ----
check("T1 single: answer field = AI answer", U["t-differ"]["answer"] == "B" and U["t-differ"]["ai"]["answer"] == "B")
check("T1 multiple: answer array = AI answer", U["t-multi"]["answer"] == ["A", "C"])
check("T1 dropdown: each blanks[].answer set, options untouched",
      [b["answer"] for b in U["t-drop"]["blanks"]] == ["T2", "| where"]
      and [b["options"] for b in U["t-drop"]["blanks"]] == [b["options"] for b in S["t-drop"]["blanks"]])
check("T1 matrix: Yes/No → true/false in statements[].answer",
      [s["answer"] for s in U["t-matrix"]["statements"]] == [False, True] and U["t-matrix"]["ai"]["answer"] == {"s1": False, "s2": True})
check("T1 drag: item-id array", U["t-drag"]["answer"] == ["c", "b", "a"])
case = next(q for q in out["questions"] if q["id"] == "t-case")
check("T2 case study: sub-questions updated in place, outer structure and case info unchanged",
      [s["id"] for s in case["questions"]] == ["t-case-q1", "t-case-q2"] and case["questions"][0]["answer"] == "C"
      and case["caseInfo"] == BANK["questions"][8]["caseInfo"] and "ai" not in case and "originalAnswer" not in case
      and [s["answer"] for s in case["questions"][1]["statements"]] == [False, False])

# ---- 原題庫答案 ----
check("B2 originalAnswer = current source-bank answer (frontend format) for every question",
      all(U[k]["originalAnswer"] == bab.original_answer(S[k], S[k]["type"]) for k in S))
check("B2 differing answer: practice uses AI, original kept separately",
      U["t-differ"]["answer"] == "B" and U["t-differ"]["originalAnswer"] == "A"
      and U["t-matrix"]["originalAnswer"] == {"s1": True, "s2": False})
check("B2 existing reviewNote / explanation / chapter preserved; explanation not overwritten by AI",
      U["t-agree"]["reviewNote"] == "既有備註保留" and U["t-agree"]["explanation"] == "" and U["t-agree"]["chapter"] == "d1"
      and U["t-agree"]["ai"]["explanation"] == "t-agree 的 AI 理由")

# ---- 無有效判讀 ----
for k in ("t-deferred", "t-fallback", "t-community", "t-norun", "t-nopacket"):
    check(f"B3 {k}: original answer used, ai.answer null, no explanation or references invented",
          U[k]["answer"] == S[k]["answer"] and U[k]["ai"]["answer"] is None and U[k]["ai"]["explanation"] is None
          and U[k]["ai"]["references"] == [] and U[k]["ai"]["note"], U[k]["ai"])
check("B3 community: source info kept", U["t-community"]["ai"]["community"] == {"answer": "C", "source": "community",
                                                                                "agreesWithOriginal": True}, U["t-community"]["ai"])

# ---- 排序題步驟數（一般規則）----
check("B4 drag with 'Which three' but 4 steps → invalid_spec, original kept, rejected answer traceable",
      U["t-drag-count"]["answer"] == ["b", "f", "c"] and U["t-drag-count"]["ai"]["answer"] is None
      and U["t-drag-count"]["ai"]["rejectedAnswer"] == ["b", "f", "e", "a"]
      and "答案規格異常，已退回原題庫答案" in U["t-drag-count"]["ai"]["note"] and "要求排列 3 個步驟" in U["t-drag-count"]["ai"]["note"])
check("B4 same rule without a count in the stem (uses original length) — not tied to a question id",
      U["t-drag-nocount"]["answer"] == ["a", "b"] and "要求排列 2 個步驟" in U["t-drag-nocount"]["ai"]["note"])
check("B4 required_count: stem number wins; otherwise original length; other types None",
      bab.required_count(S["t-drag-count"], "drag_drop_order") == 3 and bab.required_count(S["t-drag-nocount"], "drag_drop_order") == 2
      and bab.required_count(S["t-multi"], "multiple_choice") == 2 and bab.required_count(S["t-differ"], "single_choice") is None)
check("B4 multiple choice with wrong count rejected", bab.to_frontend(S["t-multi"], "multiple_choice", ["A", "B", "C"])[0] is None)

# ---- 過期 ----
check("B5 stale basis → current original answer, stale AI answer not applied",
      U["t-stale"]["answer"] == "A" and U["t-stale"]["ai"]["answer"] is None and U["t-stale"]["ai"]["rejectedAnswer"] == "B"
      and "判讀依據已過期" in U["t-stale"]["ai"]["note"] and U["t-stale"]["ai"]["basis"]["bankAnswerAtJudging"] == "C")

# ---- 衝突、引用 ----
check("B6 conflict kept, AI answer still used", U["t-conflict"]["ai"]["conflicts"] == ["可核對的官方引句被模型標為反駁建議答案中的 A"]
      and U["t-conflict"]["ai"]["practiceSource"] == "ai")
check("B6 references: only docs with quotes found in the packet; not-found quote dropped",
      U["t-differ"]["ai"]["references"] == [{"title": "Cited doc", "url": DOC1["url"],
                                            "quotes": [{"target": "A", "quote": "quote for t-differ", "relation": "supports_option"}]}])
check("B6 other packet docs listed as 檢索到、未引用", U["t-differ"]["ai"]["unreferencedSources"] ==
      [{"title": "Uncited doc", "url": DOC2["url"], "label": "檢索到、未引用"}])
check("B6 no valid AI answer → all packet docs are unreferenced, none presented as support",
      U["t-deferred"]["ai"]["references"] == [] and len(U["t-deferred"]["ai"]["unreferencedSources"]) == 2)

# ---- 複核狀態 ----
check("B7 reviewStatus only 待複核 / 不在複核清單, matching the review list",
      {u["ai"]["reviewStatus"] for u in U.values()} == {"待複核", "不在複核清單"}
      and {k for k, u in U.items() if u["ai"]["reviewStatus"] == "待複核"} == {"t-differ", "t-case-q1", "t-deferred"})

# ---- 無法安全轉換 ----
for k in ("t-badsingle", "t-baddrop", "t-badmatrix"):
    check(f"B8 {k}: unconvertible AI answer not applied, nothing invented", U[k]["answer" if k == "t-badsingle" else "originalAnswer"]
          == bab.original_answer(S[k], S[k]["type"]) and bab.original_answer(U[k], U[k]["type"]) == bab.original_answer(S[k], S[k]["type"])
          and U[k]["ai"]["answer"] is None and U[k]["ai"]["explanation"] is None, U[k]["ai"])

# ---- 結構、不覆寫、來源不變 ----
check("B9 bank id sc200-ai, same top-level ids/order, same answerable count",
      out["id"] == "sc200-ai" and [q["id"] for q in out["questions"]] == [q["id"] for q in BANK["questions"]] and len(U) == len(S) == 20)
check("B9 all non-answer fields identical to the source bank", [bab.strip(q) for q in out["questions"]] == [bab.strip(q) for q in BANK["questions"]])
check("B9 validate_output passes on the result", bab.validate_output(BANK, out, 20) == [])
broken = copy.deepcopy(out)
broken["questions"][0]["question"] = "changed"
check("B9 validate_output catches a changed question text", any("非答案欄位" in p for p in bab.validate_output(BANK, broken, 20)))
out_sha = sha(out_path)
rc, log = run_main(["--bank", str(bank_path), "--workspace", str(ws), "--review", str(review), "--out", str(out_path)])
check("B10 existing output → stop, not overwritten", rc == 2 and "不覆寫" in log and sha(out_path) == out_sha, log)
check("B11 source bank, results, packets and review list unchanged",
      src_before == {"bank": sha(bank_path), "ws": tree(ws), "review": sha(review)})
check("B11 aiBuild records source hashes", out["aiBuild"]["sourceBankSha256"] == src_before["bank"]
      and out["aiBuild"]["resultsSha256"] == sha(ws / "gemini_judgments.json"))

# ---------------------------------------------------------------------------
# 2. Git 與部署清單
# ---------------------------------------------------------------------------
env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
ign = subprocess.run(["git", "-C", str(PROJECT), "check-ignore", "-q", "public/data/sc200.ai.json"], env=env)
check("G1 public/data/sc200.ai.json is ignored by Git", ign.returncode == 0)
index = json.loads((PROJECT / "public" / "data" / "index.json").read_text(encoding="utf-8"))
check("G2 AI bank not in the deployed index.json", all("ai" not in b.get("file", "") for b in index["banks"]), index)
vite_cfg = (PROJECT / "vite.config.js").read_text(encoding="utf-8")
m = re.search(r"const PRIVATE_BANK = /(.+)/", vite_cfg)
check("G3 vite build removes the AI bank from dist (PRIVATE_BANK covers sc200.ai.json, still covers sc200.json)",
      m is not None and re.match(m.group(1), "sc200.ai.json") and re.match(m.group(1), "sc200.json")
      and not re.match(m.group(1), "mock_questions.json"), m and m.group(1))

# ---------------------------------------------------------------------------
# 3. 前端（Node）：真正的 validateBank / gradeQuestion / formatAnswer
# ---------------------------------------------------------------------------
NODE = shutil.which("node")
LIB = PROJECT / "src" / "lib"


def node_check(bank_file: Path):
    vb_src = (LIB / "validateBank.js").read_text(encoding="utf-8").replace("from './grading'", f"from '{(LIB / 'grading.js').as_uri()}'")
    vb = TMP / "validateBank.mjs"
    vb.write_text(vb_src, encoding="utf-8")
    script = TMP / "check.mjs"
    script.write_text(f"""
import {{ readFileSync }} from 'node:fs'
import {{ validateBank }} from '{vb.as_uri()}'
import {{ gradeQuestion }} from '{(LIB / 'grading.js').as_uri()}'
import {{ formatAnswer, AI_STATUS_LABELS }} from '{(LIB / 'aiAnswer.js').as_uri()}'
const bank = JSON.parse(readFileSync(process.argv[2], 'utf8'))
const units = bank.questions.flatMap((q) => (q.type === 'case_study' ? q.questions : [q]))
const practice = (q) => q.type === 'dropdown_cloze' ? Object.fromEntries(q.blanks.map((b) => [b.id, b.answer]))
  : q.type === 'statement_matrix' ? Object.fromEntries(q.statements.map((s) => [s.id, s.answer])) : q.answer
const out = {{ errors: validateBank(bank), units: units.length, practiceFull: 0, aiMatchesField: 0, originalNotFullWhenDiffer: 0,
  differ: 0, labelsMissing: [], formatted: {{}} }}
for (const q of units) {{
  const g = gradeQuestion(q, practice(q))
  if (g.correct) out.practiceFull++
  if (q.ai.practiceSource === 'ai' && JSON.stringify(practice(q)) === JSON.stringify(q.ai.answer)) out.aiMatchesField++
  if (q.ai.answerStatus === 'differ') {{ out.differ++; if (!gradeQuestion(q, q.originalAnswer).correct) out.originalNotFullWhenDiffer++ }}
  if (!AI_STATUS_LABELS[q.ai.answerStatus]) out.labelsMissing.push(q.ai.answerStatus)
  out.formatted[q.id] = formatAnswer(q, q.originalAnswer)
}}
const cases = bank.questions.filter((q) => q.type === 'case_study')
out.caseFull = cases.every((c) => gradeQuestion(c, Object.fromEntries(c.questions.map((s) => [s.id, practice(s)]))).correct)
out.aiCount = units.filter((q) => q.ai.practiceSource === 'ai').length
console.log(JSON.stringify(out))
""", encoding="utf-8")
    r = subprocess.run([NODE, str(script), str(bank_file)], capture_output=True, text=True, encoding="utf-8")
    return json.loads(r.stdout) if r.returncode == 0 else {"node_error": r.stderr}


if NODE:
    n = node_check(out_path)
    check("F1 frontend validateBank accepts the AI bank", n.get("errors") == [], n)
    check("F2 practice answers grade as fully correct for every question (incl. case study)",
          n.get("practiceFull") == n.get("units") == 20 and n.get("caseFull") is True, n)
    check("F2 practice answer fields equal ai.answer where AI is used", n.get("aiMatchesField") == n.get("aiCount"), n)
    check("F3 original answer is NOT graded correct where AI differs (scoring follows AI)",
          n.get("differ") > 0 and n.get("originalNotFullWhenDiffer") == n.get("differ"), n)
    check("F4 every answerStatus has a UI label; original answers formatted with option text",
          n.get("labelsMissing") == [] and n["formatted"]["t-differ"] == "A. alpha"
          and n["formatted"]["t-matrix"] == "敘述 1：Yes；敘述 2：No" and n["formatted"]["t-drag"] == "1) a. step a → 2) b. step b → 3) c. step c",
          n.get("formatted"))
else:
    print("NOTE frontend checks skipped: node not found")

# ---------------------------------------------------------------------------
# 4. 真實資料（唯讀）：輸出寫到暫存目錄
# ---------------------------------------------------------------------------
REAL_WS = PROJECT / "output" / "gemini_run_434"
REAL_REVIEW = PROJECT / "output" / "gemini_review_20261029_092437" / "review_list.json"
if REAL_REVIEW.is_file() and (REAL_WS / "gemini_judgments.json").is_file():
    real_before = {"bank": sha(va.DEFAULT_BANK), "results": sha(REAL_WS / "gemini_judgments.json"),
                   "packets": tree(REAL_WS / "gemini_judge"), "review": sha(REAL_REVIEW)}
    real_out = TMP / "real" / "sc200.ai.json"
    rc, log = run_main(["--workspace", str(REAL_WS), "--review", str(REAL_REVIEW), "--out", str(real_out)])
    ro = json.loads(real_out.read_text(encoding="utf-8"))
    RU = {u["id"]: u for u in bab.answerable(ro["questions"])}
    st = {}
    for u in RU.values():
        st[u["ai"]["answerStatus"]] = st.get(u["ai"]["answerStatus"], 0) + 1
    valid = sum(st.get(k, 0) for k in ("agree", "differ", "invalid_spec", "stale"))
    check("R1 real: 434 answerable, 418 valid judgments, 16 without a valid one",
          rc == 0 and len(RU) == 434 and valid == 418 and st.get("deferred", 0) + st.get("fallback", 0) + st.get("community", 0) == 16, st)
    check("R1 real: qd7-qs1-q028 rejected by the generic step-count rule, original b,f,c kept",
          RU["qd7-qs1-q028"]["ai"]["answerStatus"] == "invalid_spec" and RU["qd7-qs1-q028"]["answer"] == ["b", "f", "c"], RU["qd7-qs1-q028"]["ai"])
    check("R1 real: review status 70 待複核", sum(1 for u in RU.values() if u["ai"]["reviewStatus"] == "待複核") == 70)
    check("R1 real: no unknown/unavailable/basis_missing/stale", not {"unavailable", "basis_missing", "stale"} & set(st), st)
    check("R2 real sources unchanged", real_before == {"bank": sha(va.DEFAULT_BANK), "results": sha(REAL_WS / "gemini_judgments.json"),
                                                       "packets": tree(REAL_WS / "gemini_judge"), "review": sha(REAL_REVIEW)})
    if NODE:
        rn = node_check(real_out)
        check("R3 real AI bank: frontend validateBank 0 errors, every practice answer grades correct",
              rn.get("errors") == [] and rn.get("practiceFull") == rn.get("units") == 434 and rn.get("caseFull") is True, rn)
else:
    print("NOTE real-data checks skipped: real workspace or review list not present")

check("zero network attempts", not NET_ATTEMPTS, NET_ATTEMPTS)
print(f"{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
sys.exit(1 if FAILED else 0)
