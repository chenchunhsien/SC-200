"""ExamTopics 第 1 頁匯入、社群門檻、建議答案與 Microsoft Learn 候選流程測試。
只使用自行編寫的模擬 HTML（假的討論頁 ID），所有網路呼叫都被替換；只寫入暫存目錄。"""
import contextlib
import hashlib
import html
import importlib.util
import io
import json
import shutil
import sys
import urllib.error
from pathlib import Path

ROOT = Path(r"D:\SC-200-platform")
OUT = Path(__file__).parent / "out_autocollect"
BANK = ROOT / "public/data/sc200.json"
PROTECTED = ["public/data/sc200.json", "output/explanations.json", "output/progress.json", "output/verification.json"]
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()  # noqa: E731
REAL = {p: sha(ROOT / p) for p in PROTECTED}

spec = importlib.util.spec_from_file_location("verify_answers", ROOT / "scripts" / "verify_answers.py")
va = importlib.util.module_from_spec(spec)
sys.modules["verify_answers"] = va
spec.loader.exec_module(va)
va.time.sleep = lambda s: None
UNITS = {u.question_id: u for u in va.eq.iter_units(json.loads(BANK.read_text(encoding="utf-8")))}


def url(i):
    return f"https://www.examtopics.com/discussions/microsoft/view/{i}-exam-sc-200-topic-1-question-{i % 100}-discussion/"


def card(disc, qn, unit=None, stem=None, options=None, answer=None, chart=None, topic=1):
    q = unit.question if unit else {}
    stem = stem if stem is not None else q.get("question", "Fake stem text for a mock card.")
    if options is None:
        options = [(o["id"], o["text"]) for o in q.get("options", [])]
    opts = "".join(f'<li class="multi-choice-item"><span class="multi-choice-letter" data-choice-letter="{k}"> {k}. </span> '
                   f'{html.escape(t)} <span class="badge badge-success most-voted-answer-badge d-none">Most Voted</span></li>'
                   for k, t in options)
    ans = (f'<span><strong>Correct Answer:</strong> <strong class="ml-1">{answer}</strong> </span>' if answer
           else '<img src="answer.png">')
    ch = f'<div class="answer-chart" data-chart="{html.escape(json.dumps(chart))}"></div>' if chart is not None else ""
    return (f'<div class="card exam-question-card"><div class="card-body question-body"><p class="card-text">{html.escape(stem)}</p>'
            f'<ul class="choice-listAr">{opts}</ul><a class="btn"><i class="fa fa-comment" title="{disc}"></i></a>'
            f'<span style="display:none;">Exam SC-200 topic {topic} question {qn} discussion</span>'
            f'<div class="card-text question-answer bg-light">{ans}{ch}</div></div></div>')


def page(*cards):
    p = OUT / "mock_page.html"
    p.write_text("<html><body>Last updated on Oct 03, 2026." + "".join(cards) + "</body></html>", encoding="utf-8")
    return p


def run(*argv):
    buf = io.StringIO()
    sys.argv = ["verify_answers.py", "--bank", str(BANK), "--output-dir", str(OUT), *argv]
    with contextlib.redirect_stdout(buf):
        try:
            code = va.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 2
            print(e.code)
    return code, buf.getvalue()


def seed():
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    for f in ("explanations.json", "progress.json"):
        shutil.copy2(ROOT / "output" / f, OUT / f)
    run("init", "--ids", "qd1-qs4-q002,qd1-qs4-q008,qd7-qs1-q047,qd3-qs5-q043")
    for qid, i in (("qd1-qs4-q002", 9001), ("qd1-qs4-q008", 9002), ("qd3-qs5-q043", 9004)):
        run("examtopics", "--id", qid, "--match", "exact", "--url", url(i), "--confirm-same-question", "--by", "tester")
    run("examtopics", "--id", "qd7-qs1-q047", "--candidate", url(9003))


data = lambda: json.loads((OUT / "verification.json").read_text(encoding="utf-8"))  # noqa: E731
Q = lambda k: data()["questions"][k]  # noqa: E731
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" — {str(detail)[:500]}"))


U2, U8, U47, U43 = (UNITS[k] for k in ("qd1-qs4-q002", "qd1-qs4-q008", "qd7-qs1-q047", "qd3-qs5-q043"))
STD = [card(9001, 2, U2, answer="C", chart=[{"answer": "C", "percent": 100, "votes": 21}]),
       card(9002, 8, U8, answer="AE", chart=[{"answer": "AD", "percent": 12, "votes": 3}, {"answer": "AE", "percent": 88, "votes": 23}]),
       card(9003, 47, U47, answer="B", chart=[{"answer": "B", "percent": 60, "votes": 6}, {"answer": "D", "percent": 40, "votes": 4}]),
       card(9004, 43, stem=U43.question["question"], options=[], answer=None,
            chart=[{"answer": "YNN", "percent": 80, "votes": 16}, {"answer": "NNN", "percent": 20, "votes": 4}]),
       card(5555, 99, stem="Unrelated question", options=[("A", "x"), ("B", "y")], answer="A",
            chart=[{"answer": "A", "percent": 100, "votes": 50}])]

# ---- 解析 ----
OUT.mkdir(parents=True, exist_ok=True)
cards = va.parse_list_page(page(*STD).read_text(encoding="utf-8"))
c1 = cards[0]
check("P1 解析題卡：討論 ID、題號、題幹、選項（去除 Most Voted）、頁面答案、投票分布",
      c1["discussion_id"] == "9001" and c1["question_number"] == "2" and c1["page_answer_raw"] == "C"
      and c1["options"][2] == ("C", U2.question["options"][2]["text"]) and c1["chart_status"] == "ok"
      and c1["distribution"][0]["votes"] == 21, c1)
check("P2 頁面答案為圖片時為 None", cards[3]["page_answer_raw"] is None)
nochart = va.parse_list_page(card(1, 1, U2, answer="C"))
check("P3 沒有投票圖表時 chart_status=missing", nochart[0]["chart_status"] == "missing" and nochart[0]["distribution"] is None)
bad = va.parse_list_page(card(1, 1, U2, answer="C").replace("</div></div></div>", '<div data-chart="[{&quot;answer&quot;:1}]"></div></div></div>'))
check("P4 投票圖表格式錯誤時 chart_status=invalid", bad[0]["chart_status"] == "invalid" and bad[0]["distribution"] is None)
for name, text in (("P5 登入頁停止", "<html>Create a free account or log in to continue</html>"),
                   ("P6 找不到題卡停止", "<html><body>no cards here</body></html>"),
                   ("P7 題卡缺少討論 ID 停止", card(1, 1, U2, answer="C").replace('title="1"', ""))):
    try:
        va.parse_list_page(text)
        check(name, False)
    except va.FetchStop:
        check(name, True)

# ---- 匯入 ----
seed()
code, out = run("import-examtopics", "--from-file", str(page(*STD)))
q2, q8, q47, q43 = Q("qd1-qs4-q002"), Q("qd1-qs4-q008"), Q("qd7-qs1-q047"), Q("qd3-qs5-q043")
e2 = q2["examtopics_evidence"]
check("I1 單選：頁面答案、最高票答案、比例、總票數、分布、擷取狀態",
      code == 0 and e2["page_answer"] == "C" and e2["community_vote"]["answer"] == "C" and e2["community_vote"]["share_percent"] == 100
      and e2["community_vote"]["vote_count"] == 21 and e2["community_vote"]["distribution"][0]["votes"] == 21
      and e2["community_capture"]["status"] == "complete", out)
e8 = q8["examtopics_evidence"]
check("I2 複選：最高票 AE 88%、總票數為各答案加總 26，完整保留分布",
      e8["page_answer"] == ["A", "E"] and e8["community_vote"]["answer"] == ["A", "E"] and e8["community_vote"]["share_percent"] == 88
      and e8["community_vote"]["vote_count"] == 26 and len(e8["community_vote"]["distribution"]) == 2)
e47 = q47["examtopics_evidence"]
check("I3 probable 只產生比對報告，不寫入社群資料、不改匹配狀態",
      e47["match_status"] == "probable" and e47["auto_match"]["verdict"] == "all_match" and e47["page_answer"] is None
      and e47["community_vote"]["answer"] is None and "community_capture" not in e47)
e43 = q43["examtopics_evidence"]
check("I6 非選擇題：保留分布與總票數，但不把字母答案寫成本題答案",
      e43["page_answer"] is None and e43["community_vote"]["answer"] is None and e43["community_vote"]["share_percent"] is None
      and e43["community_vote"]["vote_count"] == 20 and e43["community_capture"]["status"] == "partial"
      and e43["community_capture"]["notes"])
check("I12 只依完全相同的討論頁 ID 對應（不相關題卡被忽略）", "5555" not in json.dumps([e["examtopics_evidence"].get("community_capture") for e in data()["questions"].values()]))
check("I13 匯入不改 answer_status / proposed_answer / 題庫答案快照",
      all(e["answer_status"] == "pending" and e["proposed_answer"] is None for e in data()["questions"].values()))

# 接受自動比對
code, out = run("accept-match", "--id", "qd7-qs1-q047", "--by", "gemini")
check("I4a 模型 / 工具不得接受比對", code == 2 and Q("qd7-qs1-q047")["examtopics_evidence"]["match_status"] == "probable")
code, out = run("accept-match", "--id", "qd7-qs1-q047", "--by", "tester")
e47 = Q("qd7-qs1-q047")["examtopics_evidence"]
check("I4b 由人接受後才變 exact，並留下 match_history",
      code == 0 and e47["match_status"] == "exact" and e47["url"] == url(9003) and e47["match_history"][-1]["by"] == "tester", out)
run("import-examtopics", "--from-file", str(page(*STD)))
check("I4c 接受後重新匯入才寫入社群資料", Q("qd7-qs1-q047")["examtopics_evidence"]["community_vote"]["vote_count"] == 10)

seed()
altered = card(9003, 47, U47, options=[(k, t + " (changed)") for k, t in [(o["id"], o["text"]) for o in U47.question["options"]]])
run("import-examtopics", "--from-file", str(page(altered)))
code, out = run("accept-match", "--id", "qd7-qs1-q047", "--by", "tester")
check("I5 選項不符（mismatch）時不能接受", code == 2 and Q("qd7-qs1-q047")["examtopics_evidence"]["auto_match"]["verdict"] == "mismatch")

# 同票、既有資料保護、--refresh
seed()
run("import-examtopics", "--from-file", str(page(card(9001, 2, U2, answer="C", chart=[{"answer": "B", "percent": 50, "votes": 5},
                                                                                    {"answer": "C", "percent": 50, "votes": 5}]))))
e2 = Q("qd1-qs4-q002")["examtopics_evidence"]
check("I7 最高票同票時不判定最高票答案（比例也不填）",
      e2["community_vote"]["answer"] is None and e2["community_vote"]["share_percent"] is None and e2["community_vote"]["vote_count"] == 10)
seed()
run("import-examtopics", "--from-file", str(page(*STD)))
newer = page(card(9001, 2, U2, answer="C", chart=[{"answer": "C", "percent": 96, "votes": 24}, {"answer": "A", "percent": 4, "votes": 1}]))
code, out = run("import-examtopics", "--from-file", str(newer))
check("I8a 新資料與既有不同時預設不覆寫", Q("qd1-qs4-q002")["examtopics_evidence"]["community_vote"]["vote_count"] == 21 and "未覆寫" in out, out)
code, out = run("import-examtopics", "--from-file", str(newer), "--refresh")
e2 = Q("qd1-qs4-q002")["examtopics_evidence"]
check("I8b --refresh 先保存舊值再更新", e2["community_vote"]["vote_count"] == 25 and e2["capture_history"][-1]["vote_count"] == 21)

# 停止條件、只允許第 1 頁、dry run
seed()
before = Q("qd1-qs4-q002")
login = OUT / "login.html"
login.write_text("<html>Create a free account or log in to continue</html>", encoding="utf-8")
code, out = run("import-examtopics", "--from-file", str(login))
log = data()["meta"]["examtopics_fetch_log"][-1]
check("I9 遇到登入頁停止並記錄原因，不改任何題目", code == 2 and log["status"] == "stopped" and "登入" in log["reason"]
      and Q("qd1-qs4-q002") == before, out)
code, out = run("import-examtopics", "--page", "2")
check("I10 不允許第 2 頁以後", code == 2)
pre = sha(OUT / "verification.json")
code, out = run("import-examtopics", "--from-file", str(page(*STD)), "--dry-run")
check("I11 dry run 不寫入", code == 0 and sha(OUT / "verification.json") == pre and "dry run" in out)

# 網路層：只允許免費頁、轉址即停止
try:
    va.fetch_list_page(2)
    check("N1 fetch_list_page 拒絕第 2 頁", False)
except va.FetchStop:
    check("N1 fetch_list_page 拒絕第 2 頁", True)


class RedirectOpener:
    def open(self, req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": "https://www.examtopics.com/login/"}, None)


real_build = va.urllib.request.build_opener
va.urllib.request.build_opener = lambda *a: RedirectOpener()
try:
    va.fetch_list_page(1)
    check("N2 轉址（登入頁）時停止", False)
except va.FetchStop as err:
    check("N2 轉址（登入頁）時停止", "302" in str(err) and "login" in str(err))
va.urllib.request.build_opener = real_build

# ---- 門檻邊界 ----
seed()
for name, chart, met in (("T1 9 票未達", [{"answer": "C", "percent": 100, "votes": 9}], False),
                         ("T2 10 票且 70% 達標", [{"answer": "C", "percent": 70, "votes": 7}, {"answer": "A", "percent": 30, "votes": 3}], True),
                         ("T3 69% 未達", [{"answer": "C", "percent": 69, "votes": 69}, {"answer": "A", "percent": 31, "votes": 31}], False)):
    seed()
    run("import-examtopics", "--from-file", str(page(card(9001, 2, U2, answer="C", chart=chart))))
    th = va.community_threshold(Q("qd1-qs4-q002"))
    check(name, th["met"] is met, th)

# ---- 建議答案與可信程度 ----
seed()
run("import-examtopics", "--from-file", str(page(*STD)))
ctx = va.Ctx(BANK, OUT)
r2 = va.recommend(ctx, ctx.data["questions"]["qd1-qs4-q002"])
check("R1 社群共識達標、無官方佐證：最多中", r2["confidence"] == "中" and r2["answer"] == "C" and "Microsoft Learn" in r2["next_step"], r2)
run("add-ms", "--id", "qd1-qs4-q002", "--url", "https://learn.microsoft.com/en-us/x", "--title", "x", "--supports", "bank",
    "--verified", "--excerpt", "原文", "--retrieved-at", "2026-10-27", "--checked-by", "tester")
ctx = va.Ctx(BANK, OUT)
check("R2 社群共識＋已查證官方支持同一答案：高", va.recommend(ctx, ctx.data["questions"]["qd1-qs4-q002"])["confidence"] == "高")
run("add-ms", "--id", "qd1-qs4-q008", "--url", "https://learn.microsoft.com/en-us/y", "--title", "y", "--supports", "proposed",
    "--verified", "--excerpt", "原文", "--retrieved-at", "2026-10-27", "--checked-by", "tester")
ctx = va.Ctx(BANK, OUT)
r8 = va.recommend(ctx, ctx.data["questions"]["qd1-qs4-q008"])
check("R3 社群共識與官方反證衝突：低並列出衝突", r8["confidence"] == "低" and r8["conflicts"] and "版本" in r8["next_step"], r8)
r47 = va.recommend(ctx, ctx.data["questions"]["qd7-qs1-q047"])
check("R4 probable（社群資料不採用）：未驗證、暫用題庫答案", r47["confidence"] == "未驗證" and r47["answer"] == "D", r47)
seed()
run("examtopics", "--id", "qd7-qs1-q047", "--match", "exact", "--url", url(9003), "--confirm-same-question", "--by", "tester")
run("import-examtopics", "--from-file", str(page(*STD)))
ctx = va.Ctx(BANK, OUT)
r47 = va.recommend(ctx, ctx.data["questions"]["qd7-qs1-q047"])
check("R5 未達門檻的社群候選與題庫不同：低、保留題庫答案、列出衝突與未達原因",
      r47["confidence"] == "低" and r47["answer"] == "D" and r47["community_candidate"] == "B"
      and any("不同" in c for c in r47["conflicts"]) and any("未達共識門檻" in u for u in r47["uncertainty"]), r47)
seed()
run("import-examtopics", "--from-file", str(page(card(9001, 2, U2, answer="A", chart=[{"answer": "A", "percent": 90, "votes": 30},
                                                                                    {"answer": "C", "percent": 10, "votes": 3}]))))
ctx = va.Ctx(BANK, OUT)
r2 = va.recommend(ctx, ctx.data["questions"]["qd1-qs4-q002"])
check("R6 社群共識與題庫不同：建議社群答案、最多中、列出衝突並交由你決定",
      r2["answer"] == "A" and r2["confidence"] == "中" and any("題庫答案" in c for c in r2["conflicts"]) and "由你決定" in r2["next_step"], r2)
check("R6b 題庫答案未被修改", va.answer_of(UNITS["qd1-qs4-q002"]) == "C" and sha(BANK) == REAL["public/data/sc200.json"])
seed()
r_none = va.recommend(va.Ctx(BANK, OUT), va.Ctx(BANK, OUT).data["questions"]["qd1-qs4-q002"])
check("R7 無任何資料：未驗證", r_none["confidence"] == "未驗證")

# 共識候選清單
seed()
run("import-examtopics", "--from-file", str(page(*STD)))
before_status = {k: e["answer_status"] for k, e in data()["questions"].items()}
code, out = run("consensus-candidates")
check("C1 只列出達門檻且與題庫一致者，不變更狀態",
      code == 0 and "qd1-qs4-q002" in out and "qd1-qs4-q008" in out and "qd3-qs5-q043" not in out
      and {k: e["answer_status"] for k, e in data()["questions"].items()} == before_status, out)
code, out = run("checklist")
text = (OUT / "verification_checklist.md").read_text(encoding="utf-8")
check("C2 清單含建議答案、可信程度、門檻與擷取資訊，且沒有 None",
      "建議答案" in text and "可信程度" in text and "社群共識門檻：達標" in text and "自動擷取" in text and "None" not in text)

# ---- Microsoft Learn 候選 ----
seed()
run("import-examtopics", "--from-file", str(page(*STD)))
calls = []
va.learn_search = lambda q, top=5: (calls.append(q) or [{"title": "Doc A", "url": "https://learn.microsoft.com/en-us/a"},
                                                       {"title": "Training", "url": "https://learn.microsoft.com/en-us/training/x"},
                                                       {"title": "Doc B", "url": "https://learn.microsoft.com/en-us/b"}])
va.fetch_doc_text = lambda u: ("Isolating a device stops network traffic. Collect an investigation package to gather logs "
                               "from the device. Unrelated sentence about something else entirely here.")
code, out = run("ms-search", "--dry-run")
check("M1 dry run 不連網、不寫入", not calls and "dry run" in out)
ms_before = {k: (e["microsoft_evidence"], va.microsoft_consistency(e)) for k, e in data()["questions"].items()}
code, out = run("ms-search")
d = data()["questions"]
cands = d["qd7-qs1-q047"].get("microsoft_candidates") or []
check("M2 需要查文件的題目擷取候選文件（排除 training），一律標記未查證候選",
      code == 0 and len(cands) == 2 and all(c["status"] == "unverified_candidate" for c in cands)
      and all("/training/" not in c["url"] for c in cands) and cands[0]["excerpts"], cands)
check("M3 達共識且與題庫一致的題目不搜尋", "microsoft_candidates" not in d["qd1-qs4-q002"])
check("M4 候選文件不影響官方證據與一致性判定",
      {k: (e["microsoft_evidence"], va.microsoft_consistency(e)) for k, e in d.items()} == ms_before)
n = len(calls)
code, out = run("ms-search")
check("M5 已有候選者預設不重複搜尋", len(calls) == n)


def boom(q, top=5):
    raise urllib.error.URLError("simulated outage")


va.learn_search = boom
code, out = run("ms-search", "--refresh", "--ids", "qd7-qs1-q047")
d = data()["questions"]["qd7-qs1-q047"]
check("M6 搜尋失敗時記錄錯誤並保留既有候選", d["microsoft_search_log"][-1]["error"] and len(d["microsoft_candidates"]) == 2, d["microsoft_search_log"][-1])

check("S1 真實題庫、解析檔、進度檔、驗證檔未被修改", REAL == {p: sha(ROOT / p) for p in PROTECTED})
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
