"""verify_answers.py 測試：只寫入暫存目錄，不連網、不呼叫任何 API。"""
import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(r"D:\SC-200-platform")
OUT = Path(__file__).parent / "out_verify"
BANK = ROOT / "public/data/sc200.json"
REAL = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
        for p in ("public/data/sc200.json", "output/explanations.json", "output/progress.json")}
REAL_VER = {p: (hashlib.sha256((ROOT / p).read_bytes()).hexdigest() if (ROOT / p).exists() else None)
            for p in ("output/verification.json", "output/verification_checklist.md")}
LOCKED = ["qd1-t1-q1", "qd1-qs4-q004", "qd1-qs4-q007", "qd1-qs4-q006", "qd1-qs4-q044"]
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()  # noqa: E731

spec = importlib.util.spec_from_file_location("verify_answers", ROOT / "scripts" / "verify_answers.py")
va = importlib.util.module_from_spec(spec)
sys.modules["verify_answers"] = va
spec.loader.exec_module(va)
REAL_COPY2 = va.shutil.copy2


def seed():
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    for f in ("explanations.json", "progress.json"):
        shutil.copy2(ROOT / "output" / f, OUT / f)


def run(*argv, bank=BANK):
    buf = io.StringIO()
    sys.argv = ["verify_answers.py", "--bank", str(bank), "--output-dir", str(OUT), *argv]
    with contextlib.redirect_stdout(buf):
        try:
            code = va.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 2
            print(e.code)
    return code, buf.getvalue()


data = lambda: json.loads((OUT / "verification.json").read_text(encoding="utf-8"))["questions"]  # noqa: E731
backups = lambda: sorted((OUT / "backups").glob("*")) if (OUT / "backups").exists() else []  # noqa: E731
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" — {str(detail)[:500]}"))


# 1. init：20 題、全部 pending、來源預設未搜尋、PDF 原始答案不猜測
seed()
code, out = run("init", "--preset", "trial20")
d = data()
check("1. init 建立 20 題且全部 pending", code == 0 and len(d) == 20 and all(e["answer_status"] == "pending" for e in d.values()), out)
check("1b. ExamTopics 預設 not_searched、無答案無投票",
      all(e["examtopics_evidence"]["match_status"] == "not_searched" and e["examtopics_evidence"]["page_answer"] is None
          and e["examtopics_evidence"]["community_vote"]["answer"] is None for e in d.values()))
corr = [k for k, e in d.items() if e["pdf_original_answer"]["status"] == "text_only"]
check("1c. 有更正紀錄者為 text_only（值不猜測），其餘 unavailable",
      sorted(corr) == ["qd3-qs5-q043", "qd3-t1-q6", "qd4-qs2-q004"]
      and all(e["pdf_original_answer"]["value"] is None for e in d.values())
      and all(e["pdf_original_answer"]["status"] == "unavailable" for k, e in d.items() if k not in corr), corr)
check("1d. 代理疑點備註另列 prior_notes，不當成 PDF 原始答案",
      sorted(k for k, e in d.items() if e["prior_notes"]) == ["qd6-qs1-q019", "qd6-qs1-q034", "qd7-qs1-q039"])
check("1e. 題組子題保留題組資訊", d["qd3-t2-q1"]["case_id"] == "qd3-t2" and d["qd3-t2-q1"]["case_title"])
check("1f. 題庫答案快照格式正確",
      d["qd1-qs4-q002"]["bank_answer"]["value"] == "C" and isinstance(d["qd4-qs2-q040"]["bank_answer"]["value"], list)
      and isinstance(d["qd3-t1-q6"]["bank_answer"]["value"], dict))

# 2. 重跑 init 不覆寫既有項目；寫入前有備份
run("examtopics", "--id", "qd1-qs4-q002", "--match", "not_found", "--search-note", "測試搜尋")
before = data()["qd1-qs4-q002"]
n_bk = len(backups())
code, out = run("init", "--preset", "trial20")
check("2. 重跑 init 不覆寫既有項目", data()["qd1-qs4-q002"] == before and "保持不變 20" in out, out)
code, out = run("init", "--ids", "qd1-qs4-q005")
check("2b. 新增題目時會先備份既有 verification.json",
      code == 0 and len(backups()) == n_bk + 1 and "qd1-qs4-q005" in data() and "已備份" in out, out)
bk = backups()[-1]
check("2c. 備份內容即寫入前的檔案", "qd1-qs4-q005" not in json.loads(bk.read_text(encoding="utf-8"))["questions"])

# 3. 備份失敗時不寫入
pre = sha(OUT / "verification.json")
def broken(*a, **k):
    raise OSError("模擬磁碟錯誤")
va.shutil.copy2 = broken
code, out = run("init", "--ids", "qd1-qs4-q009")
va.shutil.copy2 = REAL_COPY2
check("3. 備份失敗時不寫入", code == 2 and sha(OUT / "verification.json") == pre and "未變更" in out, out)

# 4. ExamTopics：候選只會是 probable；exact 需人工確認；未確認不得記錄答案
U = "https://www.examtopics.com/discussions/microsoft/view/1-exam-sc-200-topic-1-question-1-discussion/"
code, out = run("examtopics", "--id", "qd1-qs4-q008", "--candidate", U)
check("4. 候選網址只標 probable", code == 0 and data()["qd1-qs4-q008"]["examtopics_evidence"]["match_status"] == "probable")
code, out = run("examtopics", "--id", "qd1-qs4-q008", "--match", "exact", "--url", U)
check("4b. exact 未加 --confirm-same-question 被拒", code == 2 and data()["qd1-qs4-q008"]["examtopics_evidence"]["match_status"] == "probable", out)
code, out = run("examtopics", "--id", "qd1-qs4-q008", "--page-answer", '["A","E"]')
check("4c. 未確認同一題時不可記錄頁面答案", code == 2 and data()["qd1-qs4-q008"]["examtopics_evidence"]["page_answer"] is None, out)
code, out = run("examtopics", "--id", "qd1-qs4-q008", "--candidate", "https://example.com/x")
check("4d. 非 ExamTopics 討論頁網址被拒", code == 2)
code, out = run("examtopics", "--id", "qd1-qs4-q008", "--match", "exact", "--url", U, "--confirm-same-question",
                "--page-answer", '["A","E"]', "--vote-answer", '["A","E"]', "--vote-share", "91", "--vote-as-of", "2026-10-22",
                "--reason", "支持理由摘要", "--counter", "反方摘要", "--by", "tester")
et = data()["qd1-qs4-q008"]["examtopics_evidence"]
check("4e. 人工確認後可記錄頁面答案、投票、正反理由",
      code == 0 and et["match_status"] == "exact" and et["community_vote"]["share_percent"] == 91 and et["counterarguments"], out)

# 5. not_found 必須記錄搜尋方式
code, out = run("examtopics", "--id", "qd1-qs4-q008", "--match", "not_found")
check("5. not_found 未附搜尋紀錄被拒", code == 2)

# 6. 社群高票不能讓狀態變 verified
code, out = run("set-status", "--id", "qd1-qs4-q008", "--status", "verified", "--by", "tester")
check("6. 只有 ExamTopics 高票時拒絕 verified", code == 2 and data()["qd1-qs4-q008"]["answer_status"] == "pending", out)
code, out = run("set-status", "--id", "qd1-qs4-q008", "--status", "verified", "--by", "gemini")
check("6b. 模型 / 工具不得設定狀態", code == 2)

# 7. Microsoft 證據：網址限 learn.microsoft.com；verified 需原文短句等；未查證證據不計入
code, out = run("add-ms", "--id", "qd1-qs4-q008", "--url", "https://example.com/doc", "--title", "x", "--supports", "bank")
check("7. 非 Microsoft Learn 網址被拒", code == 2)
code, out = run("add-ms", "--id", "qd1-qs4-q008", "--url", "https://learn.microsoft.com/en-us/x", "--title", "x",
                "--supports", "bank", "--verified")
check("7b. 標 verified 但缺原文短句被拒", code == 2)
run("add-ms", "--id", "qd1-qs4-q008", "--url", "https://learn.microsoft.com/en-us/x", "--title", "x", "--supports", "bank")
code, out = run("set-status", "--id", "qd1-qs4-q008", "--status", "verified", "--by", "tester")
check("7c. 只有未查證證據時拒絕 verified", code == 2, out)
run("add-ms", "--id", "qd1-qs4-q008", "--url", "https://learn.microsoft.com/en-us/y", "--title", "y", "--supports", "bank",
    "--verified", "--excerpt", "原文", "--retrieved-at", "2026-10-22", "--checked-by", "tester")
code, out = run("set-status", "--id", "qd1-qs4-q008", "--status", "verified", "--by", "tester", "--reason", "官方文件支持")
check("7d. 有已查證且支持題庫答案的證據時，可由人設定 verified",
      code == 0 and data()["qd1-qs4-q008"]["answer_status"] == "verified"
      and data()["qd1-qs4-q008"]["status_history"][-1]["by"] == "tester", out)
run("add-ms", "--id", "qd1-qs4-q002", "--url", "https://learn.microsoft.com/en-us/z", "--title", "z", "--supports", "proposed",
    "--verified", "--excerpt", "原文", "--retrieved-at", "2026-10-22", "--checked-by", "tester")
code, out = run("set-status", "--id", "qd1-qs4-q002", "--status", "verified", "--by", "tester")
check("7e. 官方證據只支持其他答案（proposed_supported）時拒絕 verified",
      code == 2 and "proposed_supported" in out and data()["qd1-qs4-q002"]["answer_status"] == "pending", out)
code, out = run("set-status", "--id", "qd1-qs4-q002", "--status", "disputed", "--by", "tester")
check("7f. disputed 必須附理由", code == 2)

# 8. compare：官方一致性與社群支持分開計算，不改狀態
status_before = {k: e["answer_status"] for k, e in data().items()}
code, out = run("compare")
d = data()
c8, c2 = d["qd1-qs4-q008"]["answer_comparison"], d["qd1-qs4-q002"]["answer_comparison"]
check("8. compare 分開列出官方一致性與社群支持",
      c8["microsoft_consistency"] == "consistent" and c8["bank_vs_examtopics_page"] == "agree"
      and c8["community_vote_share_percent"] == 91 and c2["microsoft_consistency"] == "proposed_supported"
      and "score" not in json.dumps(c8))
check("8b. compare 不改 answer_status", {k: e["answer_status"] for k, e in d.items()} == status_before)
check("8c. 未確認同一題時不比對社群答案", d["qd1-t3-q3"]["answer_comparison"]["bank_vs_examtopics_vote"] == "no_confirmed_match")

# 9. propose：記錄但不改題庫；答案格式依題型驗證
code, out = run("propose", "--id", "qd3-t1-q6", "--answer", '{"select_0":"x"}', "--basis", "model", "--by", "t")
check("9. 不符合題目結構的答案被拒", code == 2)
u = va.eq.iter_units(json.loads(BANK.read_text(encoding="utf-8")))
blank_opts = next(x for x in u if x.question_id == "qd3-t1-q6").question["blanks"]
ans = json.dumps({b["id"]: b["options"][0] for b in blank_opts})
code, out = run("propose", "--id", "qd3-t1-q6", "--answer", ans, "--basis", "human_judgement", "--by", "tester")
check("9b. 候選答案只記錄、confirmed 為 false", code == 0 and data()["qd3-t1-q6"]["proposed_answer"]["confirmed"] is False)

# 10. 題庫答案在快照後變更：偵測並阻止 verified
mod_bank = OUT / "bank_modified.json"
b = json.loads(BANK.read_text(encoding="utf-8"))
next(q for q in b["questions"] if q["id"] == "qd1-qs4-q008")["answer"] = ["A", "B"]
mod_bank.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
code, out = run("report", bank=mod_bank)
check("10. 題庫答案變更會被標示", "qd1-qs4-q008" in out and "題庫答案已變更" in out, out)
run("set-status", "--id", "qd1-qs4-q008", "--status", "pending", "--by", "tester", bank=mod_bank)
code, out = run("set-status", "--id", "qd1-qs4-q008", "--status", "verified", "--by", "tester", bank=mod_bank)
check("10b. 題庫答案變更後拒絕 verified", code == 2, out)

# 11. 清單：產生、含 20+ 題、重新產生前先備份
code, out = run("checklist")
text = (OUT / "verification_checklist.md").read_text(encoding="utf-8")
check("11. 產生核對清單", code == 0 and "## qd7-qs1-q047" in text and "判定優先順序" in text, out)
n = len(backups())
code, out = run("checklist")
check("11b. 重新產生清單前先備份舊清單", len(backups()) == n + 1 and any(p.suffix == ".md" for p in backups()))

# 12. 鎖定題目：顯示為鎖定，解析檔不受影響
run("init", "--ids", "qd1-qs4-q044")
code, out = run("checklist")
text = (OUT / "verification_checklist.md").read_text(encoding="utf-8")
exp_copy = json.loads((OUT / "explanations.json").read_text(encoding="utf-8"))["explanations"]
check("12. 鎖定題目在清單中標示，解析檔副本未被修改",
      "🔒 `qd1-qs4-q044`" in text and all(exp_copy[q]["manual_revision"]["locked"] for q in LOCKED)
      and sha(OUT / "explanations.json") == sha(ROOT / "output/explanations.json"))

# 13. ExamTopics 內容不會進入 Gemini 提示詞
eq = va.eq
unit = next(x for x in eq.iter_units(json.loads(BANK.read_text(encoding="utf-8"))) if x.question_id == "qd1-qs4-q008")
prompt = eq.build_prompt(unit)
src = (ROOT / "scripts/enrich_questions.py").read_text(encoding="utf-8")
check("13. 解析生成程式不讀取驗證檔，提示詞不含社群內容",
      "verification.json" not in src and "verify_answers" not in src and "examtopics" not in src.lower()
      and "支持理由摘要" not in prompt)

# 14. microsoft_consistency 判定規則（直接呼叫函式）
def ev(*items):
    return {"microsoft_evidence": [{"supports": s, "verification": v} for s, v in items]}
V, U = "verified", "unverified"
cases = [
    ("14a 只有已查證 bank", ev(("bank", V)), "consistent"),
    ("14b 只有已查證 proposed", ev(("proposed", V), ("proposed", V)), "proposed_supported"),
    ("14c 已查證 bank + proposed", ev(("bank", V), ("proposed", V)), "conflicting"),
    ("14d 只有已查證 neither", ev(("neither", V)), "undetermined"),
    ("14e 沒有任何證據", ev(), "no_verified_evidence"),
    ("14f 只有未查證證據", ev(("bank", U), ("proposed", U)), "no_verified_evidence"),
    ("14g 未查證 proposed 不影響已查證 bank", ev(("bank", V), ("proposed", U)), "consistent"),
    ("14h 未查證 bank 不影響已查證 proposed", ev(("proposed", V), ("bank", U)), "proposed_supported"),
    ("14i neither + bank", ev(("neither", V), ("bank", V)), "consistent"),
    ("14j neither + proposed", ev(("neither", V), ("proposed", V)), "proposed_supported"),
    ("14k neither + bank + proposed", ev(("neither", V), ("bank", V), ("proposed", V)), "conflicting"),
    ("14l 未知 supports 值", ev(("maybe", V)), "invalid_evidence"),
    ("14m 未知 supports 與 bank 並存仍保守處理", ev(("bank", V), ("maybe", V)), "invalid_evidence"),
    ("14n 缺 supports 欄位", {"microsoft_evidence": [{"verification": V}]}, "invalid_evidence"),
    ("14o 未知值但未查證則不列入", ev(("bank", V), ("maybe", U)), "consistent"),
    ("14p 非 verified 的 verification 值不列入", ev(("bank", "pending-check")), "no_verified_evidence"),
]
for name, entry, want in cases:
    got = va.microsoft_consistency(entry)
    check(f"{name} → {want}", got == want, got)

# 15. 未知 supports 值（手動編輯造成）：報告顯示 invalid_evidence，且不可標 verified
seed()
run("init", "--ids", "qd1-qs4-q002")
raw = json.loads((OUT / "verification.json").read_text(encoding="utf-8"))
raw["questions"]["qd1-qs4-q002"]["microsoft_evidence"] = [
    {"url": "https://learn.microsoft.com/en-us/a", "title": "a", "excerpt": "x", "supports": "bank", "verification": "verified"},
    {"url": "https://learn.microsoft.com/en-us/b", "title": "b", "excerpt": "x", "supports": "maybe", "verification": "verified"},
]
(OUT / "verification.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
code, out = run("report")
check("15. 報告顯示 invalid_evidence", "invalid_evidence" in out, out)
code, out = run("set-status", "--id", "qd1-qs4-q002", "--status", "verified", "--by", "tester")
check("15b. invalid_evidence 時拒絕 verified", code == 2 and data()["qd1-qs4-q002"]["answer_status"] == "pending", out)

# 16. proposed_supported 不放行 verified，也不自動改狀態；compare 不填 proposed_answer
seed()
run("init", "--ids", "qd1-qs4-q002")
run("add-ms", "--id", "qd1-qs4-q002", "--url", "https://learn.microsoft.com/en-us/z", "--title", "z", "--supports", "proposed",
    "--verified", "--excerpt", "原文", "--retrieved-at", "2026-10-22", "--checked-by", "tester")
run("set-status", "--id", "qd1-qs4-q002", "--status", "disputed", "--by", "tester", "--reason", "官方證據指向其他答案")
code, out = run("compare")
e16 = data()["qd1-qs4-q002"]
check("16. compare 後為 proposed_supported、狀態仍 disputed、不自動填候選答案",
      code == 0 and e16["answer_comparison"]["microsoft_consistency"] == "proposed_supported"
      and e16["answer_status"] == "disputed" and e16["proposed_answer"] is None
      and e16["answer_comparison"]["bank_vs_proposed"] == "no_proposal", out)
code, out = run("set-status", "--id", "qd1-qs4-q002", "--status", "verified", "--by", "tester")
check("16b. proposed_supported 時 set-status verified 被拒", code == 2 and data()["qd1-qs4-q002"]["answer_status"] == "disputed", out)

# 17. examtopics_consensus 依據（題庫答案 qd1-qs4-q002 = "C"）
UQ = "https://www.examtopics.com/discussions/microsoft/view/2-exam-sc-200-topic-1-question-2-discussion/"


def consensus_setup(vote='"C"', share="75", count="12", match="exact"):
    seed()
    run("init", "--ids", "qd1-qs4-q002")
    args = ["examtopics", "--id", "qd1-qs4-q002", "--match", "exact", "--url", UQ, "--confirm-same-question", "--by", "tester"]
    if vote is not None:
        args += ["--vote-answer", vote]
    if share is not None:
        args += ["--vote-share", share]
    if count is not None:
        args += ["--vote-count", count]
    run(*args)
    if match != "exact":
        run("examtopics", "--id", "qd1-qs4-q002", "--match", match, "--url", UQ)


def try_consensus():
    return run("set-status", "--id", "qd1-qs4-q002", "--status", "verified", "--basis", "examtopics_consensus", "--by", "tester")


consensus_setup()
code, out = run("set-status", "--id", "qd1-qs4-q002", "--status", "verified", "--by", "tester")
check("17a. 預設 microsoft_docs 行為不變（社群高票仍不能用預設依據成立）",
      code == 2 and "microsoft_docs" in out and data()["qd1-qs4-q002"]["answer_status"] == "pending", out)
code, out = try_consensus()
h = data()["qd1-qs4-q002"]["status_history"][-1]
check("17b. ≥70% 且 ≥10 票、答案一致時允許 examtopics_consensus",
      code == 0 and data()["qd1-qs4-q002"]["answer_status"] == "verified" and h["basis"] == "examtopics_consensus"
      and h["microsoft_consistency_at_decision"] == "no_verified_evidence", out)
code, out = run("checklist")
text = (OUT / "verification_checklist.md").read_text(encoding="utf-8")
check("17b2. 清單顯示依社群共識採用", "採用（社群共識）" in text and "（75%，12 票）" in text)

for name, kw, expect in [
    ("17c. 比例 65% 低於門檻時拒絕", {"share": "65"}, "未達 70%"),
    ("17c2. 比例剛好 70% 時允許", {"share": "70"}, None),
    ("17d. 總票數 9 少於 10 時拒絕", {"count": "9"}, "未達 10 票"),
    ("17d2. 未記錄總票數時拒絕", {"count": None}, "未記錄總票數"),
    ("17d3. 未記錄比例時拒絕", {"share": None}, "未記錄最高票比例"),
    ("17e. 匹配狀態不是 exact 時拒絕", {"match": "probable"}, "尚未人工確認為同一題"),
    ("17f. 社群答案與題庫答案不同時拒絕", {"vote": '"A"'}, "與題庫答案不同"),
    ("17g. 投票資料缺失時拒絕", {"vote": None, "share": None, "count": None}, "未取得社群答案資訊"),
]:
    consensus_setup(**kw)
    code, out = try_consensus()
    st = data()["qd1-qs4-q002"]["answer_status"]
    if expect is None:
        check(name, code == 0 and st == "verified", out)
    else:
        check(name, code == 2 and st == "pending" and expect in out, out)

# 17h. 社群答案不同時，既有 disputed 狀態與答案保留
consensus_setup(vote='"A"')
run("set-status", "--id", "qd1-qs4-q002", "--status", "disputed", "--by", "tester", "--reason", "社群指向 A")
before17h = data()["qd1-qs4-q002"]
code, out = try_consensus()
check("17h. 拒絕時保留既有 disputed 狀態與所有資料、不填候選答案",
      code == 2 and data()["qd1-qs4-q002"] == before17h and before17h["proposed_answer"] is None)

# 17i. Microsoft 一致性不同時，仍可依社群共識判定並記錄當時的一致性
consensus_setup()
run("add-ms", "--id", "qd1-qs4-q002", "--url", "https://learn.microsoft.com/en-us/z", "--title", "z", "--supports", "proposed",
    "--verified", "--excerpt", "原文", "--retrieved-at", "2026-10-22", "--checked-by", "tester")
code, out = try_consensus()
h = data()["qd1-qs4-q002"]["status_history"][-1]
check("17i. 官方 proposed_supported 時仍可依社群共識判定，並記錄當時一致性",
      code == 0 and h["microsoft_consistency_at_decision"] == "proposed_supported" and "proposed_supported" in out, out)

# 17j. 缺值顯示：清單與報告不出現 None
consensus_setup(vote=None, share=None, count=None)
run("init", "--ids", "qd1-qs4-q008")
run("checklist")
text = (OUT / "verification_checklist.md").read_text(encoding="utf-8")
code, out = run("report")
check("17j. 清單缺值顯示「未取得社群答案資訊」且無 None",
      "未取得社群答案資訊" in text and "None" not in text and "頁面答案：未記錄" in text)
check("17j2. 報告缺值顯示「未取得社群答案資訊」且無 None", "未取得社群答案資訊" in out and "None" not in out, out)
consensus_setup(share=None, count=None)
run("checklist")
text = (OUT / "verification_checklist.md").read_text(encoding="utf-8")
check("17j3. 只有答案、缺比例與票數時不印出 None", "None" not in text and "社群投票：`C`" in text)

# 17k. 舊狀態歷程（沒有 basis / microsoft_consistency_at_decision）可正常讀取
seed()
run("init", "--ids", "qd1-qs4-q002")
raw = json.loads((OUT / "verification.json").read_text(encoding="utf-8"))
raw["questions"]["qd1-qs4-q002"]["answer_status"] = "verified"
raw["questions"]["qd1-qs4-q002"]["status_history"] = [{"from": "pending", "to": "verified", "by": "old", "reason": "舊紀錄", "at": "2026-10-22"}]
(OUT / "verification.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
c1, out1 = run("report")
c2, out2 = run("checklist")
c3, out3 = run("compare")
check("17k. 舊歷程可讀取，report / checklist / compare 正常並視為官方文件依據",
      c1 == c2 == c3 == 0 and "採用（官方文件）" in out1
      and data()["qd1-qs4-q002"]["answer_comparison"]["exam_guidance"] == "採用（官方文件）", out1 + out2 + out3)

# 17l. 考試建議：not_found 時暫定沿用題庫答案
seed()
run("init", "--ids", "qd1-qs4-q002")
run("examtopics", "--id", "qd1-qs4-q002", "--match", "not_found", "--search-note", "搜尋過題幹")
code, out = run("report")
check("17l. ExamTopics 無相同題時暫定沿用題庫答案", "暫定：題庫答案（ExamTopics 無相同題）" in out, out)

# 18. 所有子指令的 --help 都能顯示（說明文字中的 % 不可造成例外）
helps_ok = True
for sub in ("init", "checklist", "add-ms", "examtopics", "propose", "compare", "set-status", "report"):
    code, out = run(sub, "--help")
    helps_ok &= code == 0
check("18. 所有子指令 --help 正常（含 --basis 說明的 70%）", helps_ok and "70%" in run("set-status", "--help")[1])

# 真實檔案不變
check("真實題庫、解析檔、進度檔 SHA256 不變", REAL == {p: sha(ROOT / p) for p in REAL})
check("測試未建立或改動真實 output/ 的驗證檔與清單",
      REAL_VER == {p: (sha(ROOT / p) if (ROOT / p).exists() else None) for p in REAL_VER})
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
