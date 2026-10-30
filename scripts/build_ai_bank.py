#!/usr/bin/env python3
"""SC-200 AI 判讀版題庫產生程式（唯讀讀取來源，不呼叫任何 API）。

讀取正式題庫（public/data/sc200.json）、Gemini 判讀工作區（gemini_judgments.json 與證據包）及人工複核清單，
產生前端可直接匯入的 public/data/sc200.ai.json（題庫 id：sc200-ai）：

- 練習答案寫入前端原本的答案欄位（answer／blanks[].answer／statements[].answer），評分邏輯不需修改。
- 原題庫答案另存 originalAnswer；判讀資訊存於 ai 物件。原本的 explanation 不變。
- 有效 Gemini 判讀（且答案結構符合題目要求、判讀依據未過期）→ 採用 Gemini 答案；
  其餘（deferred、fallback、社群採用、答案規格不符、判讀依據過期、缺少判讀資料）→ 使用原題庫答案並標示狀態。
- ai.references 只列出含有「可在證據包原文中確認」引文的文件；其他證據包文件列於 ai.unreferencedSources（檢索到、未引用）。
- ai.reviewStatus 只會是「待複核」或「不在複核清單」，不推定人工複核已完成。

輸出檔已存在即停止，不覆寫。寫入前驗證題數、題號、題組結構與所有非答案欄位都與正式題庫相同。

    python scripts/build_ai_bank.py --workspace output/gemini_run_434 \\
        --review output/gemini_review_20261029_092437/review_list.json --out public/data/sc200.ai.json

--workspace／--review 可重複指定（例如舊題庫的工作區加上新題目的工作區）：判讀紀錄依題號合併，
同一題出現在多個工作區即停止；複核清單取聯集。
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import gemini_judge as gj  # noqa: E402

va, eq = gj.va, gj.eq
RULES_VERSION = "ai-bank-v1"
BANK_ID = "sc200-ai"
REVIEW_PENDING, REVIEW_NONE = "待複核", "不在複核清單"
UNREFERENCED_LABEL = "檢索到、未引用"
ANSWER_FIELDS = {"answer", "blanks", "statements"}  # 題目中存放答案的欄位（blanks/statements 只有 answer 子欄位會變）
ADDED_FIELDS = {"originalAnswer", "ai"}
NUMBERS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}
COUNT_PATTERN = re.compile(r"\b(?:which|select|choose|perform|identify)\s+(two|three|four|five|six|seven|eight)\b", re.I)


# ---- 答案格式 -----------------------------------------------------------------------------------

def original_answer(q: dict, qtype: str):
    """題目目前的答案，以前端作答格式表示（與 grading.js 一致）。"""
    if qtype == "single_choice":
        return q["answer"]
    if qtype in ("multiple_choice", "drag_drop_order"):
        return list(q["answer"])
    if qtype == "dropdown_cloze":
        return {b["id"]: b["answer"] for b in q["blanks"]}
    if qtype == "statement_matrix":
        return {s["id"]: s["answer"] for s in q["statements"]}
    raise ValueError(f"不支援的題型 {qtype}")


def required_count(q: dict, qtype: str) -> int | None:
    """多選／排序題要求的答案數：題幹明確寫出（例如 Which three actions）時以題幹為準，否則以原題庫答案長度為準。"""
    if qtype not in ("multiple_choice", "drag_drop_order"):
        return None
    m = COUNT_PATTERN.search(q.get("question") or "")
    return NUMBERS[m.group(1).lower()] if m else len(q["answer"])


def to_frontend(q: dict, qtype: str, natural):
    """Gemini 答案（判讀程式的自然格式）→ 前端作答格式；不符合題目結構或要求時回傳 (None, 原因)。"""
    if qtype == "single_choice":
        ids = [o["id"] for o in q["options"]]
        return (natural, None) if isinstance(natural, str) and natural in ids else (None, "單選答案不在選項中")
    if qtype == "multiple_choice":
        ids = [o["id"] for o in q["options"]]
        if not (isinstance(natural, list) and natural and len(set(natural)) == len(natural) and all(x in ids for x in natural)):
            return None, "多選答案為空、重複或不在選項中"
        need = required_count(q, qtype)
        return (list(natural), None) if len(natural) == need else (None, f"題目要求選 {need} 個，判讀答案為 {len(natural)} 個")
    if qtype == "dropdown_cloze":
        blanks = {b["id"]: b["options"] for b in q["blanks"]}
        if not (isinstance(natural, dict) and set(natural) == set(blanks) and all(natural[k] in blanks[k] for k in blanks)):
            return None, "填空答案的挖空與題目不符，或答案不在該挖空的選項中"
        return {b["id"]: natural[b["id"]] for b in q["blanks"]}, None
    if qtype == "statement_matrix":
        yes, no = q.get("columns") or ["Yes", "No"]
        ids = [s["id"] for s in q["statements"]]
        if not (isinstance(natural, dict) and set(natural) == set(ids) and all(v in (yes, no) for v in natural.values())):
            return None, f"矩陣答案的敘述與題目不符，或值不是 {yes}／{no}"
        return {s: natural[s] == yes for s in ids}, None
    if qtype == "drag_drop_order":
        ids = [i["id"] for i in q["items"]]
        if not (isinstance(natural, list) and natural and len(set(natural)) == len(natural) and all(x in ids for x in natural)):
            return None, "排序答案為空、重複或不是題目的項目"
        need = required_count(q, qtype)
        return (list(natural), None) if len(natural) == need else (None, f"題目要求排列 {need} 個步驟，判讀答案為 {len(natural)} 個")
    return None, f"不支援的題型 {qtype}"


def write_answer(q: dict, qtype: str, ans) -> None:
    """把前端格式的答案寫入題目原本的答案欄位。"""
    if qtype in ("single_choice", "multiple_choice", "drag_drop_order"):
        q["answer"] = copy.deepcopy(ans)
    elif qtype == "dropdown_cloze":
        for b in q["blanks"]:
            b["answer"] = ans[b["id"]]
    elif qtype == "statement_matrix":
        for s in q["statements"]:
            s["answer"] = ans[s["id"]]


# ---- 單題判讀資訊 -------------------------------------------------------------------------------

def sources_of(packet: dict | None, citations: list, applied: bool) -> tuple[list, list]:
    """references：含可確認引文的文件（只在採用 AI 答案時）；unreferencedSources：其他證據包文件。"""
    docs = [d for d in ((packet or {}).get("microsoft_learn") or {}).get("documents") or [] if isinstance(d, dict)]
    quotes: dict = {}
    if applied:
        for c in citations or []:
            if isinstance(c, dict) and c.get("quote_check") == "found":
                quotes.setdefault(str(c.get("passage_id", "")).split("-", 1)[0], []).append(
                    {"target": c.get("target_id"), "quote": c.get("quote"), "relation": c.get("relation_claimed_by_model")})
    refs = [{"title": d.get("title"), "url": d.get("url"), "quotes": quotes[d.get("doc_id")]} for d in docs if d.get("doc_id") in quotes]
    unref = [{"title": d.get("title"), "url": d.get("url"), "label": UNREFERENCED_LABEL} for d in docs if d.get("doc_id") not in quotes]
    return refs, unref


def ai_info(unit, runs, packet_dir: Path, review_ids: set) -> tuple[dict, object]:
    """回傳 (ai 物件, 要寫入的練習答案；None 表示使用原題庫答案)。"""
    q, qtype = unit.question, unit.type
    info = {"answer": None, "explanation": None, "answerStatus": "unavailable", "practiceSource": "original",
            "compare": None, "evidenceLevel": None, "references": [], "unreferencedSources": [], "conflicts": [],
            "reviewStatus": REVIEW_PENDING if unit.question_id in review_ids else REVIEW_NONE,
            "note": None, "rejectedAnswer": None, "community": None, "basis": None}
    if not (isinstance(runs, list) and runs and all(isinstance(r, dict) for r in runs)):
        info["note"] = "沒有可用的判讀紀錄"
        return info, None
    run = runs[gj.effective_run_index(runs)]
    view = gj.judged_view(runs)
    ev = run.get("evaluation") if isinstance(run.get("evaluation"), dict) else {}
    packet, psha = None, None
    if run.get("packet_file"):
        path = packet_dir / run["packet_file"]
        if path.is_file():
            packet, psha, ok = gj.load_packet(path)
            if not ok:
                packet = None
        info["basis"] = {"packetFile": run["packet_file"], "packetSha256": psha if packet else None, "judgedAt": run.get("at"),
                         "bankSha256AtJudging": (packet or {}).get("bank_sha256"),
                         "bankAnswerAtJudging": (packet or {}).get("bank_answer", {}).get("value")}
    info["references"], info["unreferencedSources"] = sources_of(packet, [], False)
    outcome = view["outcome"]
    if outcome == "community":
        fin = run.get("final_exam_recommendation") or {}
        info.update(answerStatus="community", note="已達社群共識門檻，未呼叫 Gemini；以原題庫答案評分")
        comm, _ = to_frontend(q, qtype, fin.get("answer"))  # 無法安全轉換時為 None，不補造
        info["community"] = {"answer": comm, "source": fin.get("source"),
                             "agreesWithOriginal": va.compare_answers(va.answer_of(unit), fin.get("answer")) == "agree"}
        return info, None
    if outcome in ("fallback", "deferred"):
        info.update(answerStatus=outcome, note="沒有有效的 AI 判讀，以原題庫答案評分"
                    + ("（Gemini 輸出格式無效）" if outcome == "fallback" else "（Gemini 服務暫時不可用或逾時）"))
        return info, None
    if outcome != "done" or ev.get("status") != "evaluated":
        info["note"] = f"判讀狀態無法使用（{outcome}／{run.get('status')}），以原題庫答案評分"
        return info, None
    if packet is None:
        info.update(answerStatus="basis_missing", note="找不到或無法驗證判讀時的證據包，無法確認判讀依據；以原題庫答案評分")
        return info, None
    natural = ev.get("suggested_answer")
    if packet.get("bank_answer", {}).get("value") != va.answer_of(unit):
        info.update(answerStatus="stale", rejectedAnswer=natural,
                    note="判讀依據已過期：判讀時的題庫答案與目前原題庫答案不同；以目前原題庫答案評分")
        return info, None
    ans, problem = to_frontend(q, qtype, natural)
    if ans is None:
        info.update(answerStatus="invalid_spec", rejectedAnswer=natural, note=f"答案規格異常，已退回原題庫答案（{problem}）")
        return info, None
    cmp = va.compare_answers(va.answer_of(unit), natural)
    level = (ev.get("official_evidence_assessment") or {}).get("confidence") or ev.get("confidence")
    info.update(answer=ans, explanation=((run.get("model_output") or {}).get("rationale") or None),
                answerStatus="agree" if cmp == "agree" else "differ", practiceSource="ai", compare=cmp,
                evidenceLevel=level if level in ("高", "中", "低") else None,
                conflicts=list(ev.get("official_conflicts") or []))
    info["references"], info["unreferencedSources"] = sources_of(packet, ev.get("citations"), True)
    return info, ans


# ---- 建立與驗證 ---------------------------------------------------------------------------------

def strip(q: dict) -> dict:
    """移除答案與新增欄位後的題目（用於比對非答案欄位是否完全不變）。"""
    out = {k: v for k, v in q.items() if k not in ADDED_FIELDS and k != "answer"}
    if "blanks" in out:
        out["blanks"] = [{k: v for k, v in b.items() if k != "answer"} for b in out["blanks"]]
    if "statements" in out:
        out["statements"] = [{k: v for k, v in s.items() if k != "answer"} for s in out["statements"]]
    if "questions" in out:
        out["questions"] = [strip(s) for s in out["questions"]]
    return out


def answerable(questions: list) -> list[dict]:
    out = []
    for q in questions:
        out += q["questions"] if q.get("type") == "case_study" else [q]
    return out


def validate_output(src: dict, out: dict, n_units: int) -> list[str]:
    p = []
    if out.get("id") != BANK_ID:
        p.append("題庫 id 不是 sc200-ai")
    if len(out["questions"]) != len(src["questions"]):
        p.append("頂層題數不同")
    if [q["id"] for q in out["questions"]] != [q["id"] for q in src["questions"]]:
        p.append("頂層題號或順序不同")
    if [strip(q) for q in out["questions"]] != [strip(q) for q in src["questions"]]:
        p.append("非答案欄位（題目、選項、題組、情境、備註、解析、來源）與正式題庫不同")
    units = answerable(out["questions"])
    if len(units) != n_units or [u["id"] for u in units] != [u["id"] for u in answerable(src["questions"])]:
        p.append(f"可作答題數或題號不同（{len(units)} / {n_units}）")
    for u in units:
        ai = u.get("ai")
        if not isinstance(ai, dict) or "originalAnswer" not in u:
            p.append(f"{u['id']}：缺少 ai 或 originalAnswer")
            continue
        cur = original_answer(u, u["type"])
        if ai["practiceSource"] == "original" and cur != u["originalAnswer"]:
            p.append(f"{u['id']}：使用原題庫答案的題目，練習答案與 originalAnswer 不同")
        if ai["practiceSource"] == "ai" and cur != ai["answer"]:
            p.append(f"{u['id']}：使用 AI 答案的題目，練習答案與 ai.answer 不同")
        if ai["reviewStatus"] not in (REVIEW_PENDING, REVIEW_NONE):
            p.append(f"{u['id']}：reviewStatus 不是允許值")
        _, problem = to_frontend(u, u["type"], va.answer_of(SimpleUnit(u)))
        if problem:
            p.append(f"{u['id']}：練習答案不符題目結構（{problem}）")
    return p


class SimpleUnit:
    """讓 va.answer_of() 讀取輸出題目（只需 question 與 type）。"""
    def __init__(self, q):
        self.question, self.type = q, q["type"]


def build(ctx, results: dict, packet_dirs: dict, review_ids: set) -> tuple[dict, dict]:
    """packet_dirs：題號 → 該題判讀紀錄所在工作區的證據包目錄。"""
    out = copy.deepcopy(ctx.bank)
    out.update(id=BANK_ID, title=f"{ctx.bank.get('title', 'SC-200')}（AI 判讀版）",
               description="練習答案優先採用有效的 Gemini 判讀（AI 判讀，未經獨立驗證）；原題庫答案保存於 originalAnswer。")
    counts: dict = {}
    issues = []
    for q in answerable(out["questions"]):
        unit = ctx.units[q["id"]]
        orig = original_answer(q, unit.type)
        info, practice = ai_info(unit, (results["questions"].get(q["id"]) or {}).get("runs"),
                                 packet_dirs.get(q["id"]), review_ids)
        q["originalAnswer"] = orig
        q["ai"] = info
        if practice is not None:
            write_answer(q, unit.type, practice)
        counts[info["answerStatus"]] = counts.get(info["answerStatus"], 0) + 1
        if info["answerStatus"] in ("unavailable", "basis_missing"):
            issues.append(f"{q['id']}：{info['note']}")
    return out, {"counts": counts, "issues": issues}


def main(argv=None) -> int:
    eq.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", type=Path, default=va.DEFAULT_BANK)
    ap.add_argument("--workspace", type=Path, action="append", required=True,
                    help="判讀工作區；可重複指定，同一題只能出現在一個工作區")
    ap.add_argument("--review", type=Path, action="append", required=True,
                    help="export_review_list.py 產生的 review_list.json；可重複指定，題號取聯集")
    ap.add_argument("--out", type=Path, default=va.DEFAULT_BANK.with_name("sc200.ai.json"))
    args = ap.parse_args(argv)
    out_path = args.out.resolve()
    if out_path.exists():
        print(f"輸出檔已存在，停止，不覆寫：{out_path}")
        return 2
    workspaces = [w.resolve() for w in args.workspace]
    ctx = va.Ctx(args.bank.resolve(), workspaces[0])
    # 合併各工作區的判讀紀錄；同一題出現在多個工作區時無法判斷該採用哪一份，停止
    results, packet_dirs, owner, sources = {"questions": {}}, {}, {}, []
    for ws in workspaces:
        ws_results = gj.load_results(ws / "gemini_judgments.json")
        ws_ids = [i for i, r in ws_results["questions"].items() if (r or {}).get("runs")]
        dup = sorted(i for i in ws_ids if i in owner)
        if dup:
            print(f"同一題出現在多個工作區，停止：{dup[:10]}（{owner[dup[0]]} 與 {ws}）")
            return 2
        for i in ws_ids:
            results["questions"][i] = ws_results["questions"][i]
            packet_dirs[i], owner[i] = ws / "gemini_judge" / "packets", ws
        sources.append({"workspace": ws.name, "resultsSha256": eq.sha256_file(ws / "gemini_judgments.json"),
                        "questions": len(ws_ids)})
    review_ids = set()
    for path in args.review:
        review = json.loads(path.read_text(encoding="utf-8"))
        review_ids |= {q["id"] for q in review.get("questions") or [] if isinstance(q, dict) and q.get("id")}
    unknown_review = sorted(review_ids - set(ctx.units))
    if unknown_review:
        print(f"複核清單中有題庫找不到的題號，停止：{unknown_review}")
        return 2
    out, summary = build(ctx, results, packet_dirs, review_ids)
    out["aiBuild"] = {"rulesVersion": RULES_VERSION, "generatedAt": va.now_iso(), "sourceBankSha256": ctx.bank_sha,
                      "sources": sources, "reviewListSha256s": [eq.sha256_file(p) for p in args.review],
                      "answerStatusCounts": summary["counts"],
                      "note": "AI 判讀，未經獨立驗證；練習答案依 ai.practiceSource，原題庫答案見 originalAnswer"}
    if len(workspaces) == 1:  # 單一工作區時保留原本的欄位
        out["aiBuild"].update(resultsSha256=sources[0]["resultsSha256"], reviewListSha256=eq.sha256_file(args.review[0]))
    problems = validate_output(ctx.bank, out, len(ctx.units))
    if problems:
        print("輸出驗證未通過，未寫入：\n  " + "\n  ".join(problems[:20]))
        return 2
    gj.write_new_file(out_path, json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    units = answerable(out["questions"])
    print(f"已產生：{out_path}")
    print(f"頂層 {len(out['questions'])} 題、可作答 {len(units)} 題；使用 AI 答案 "
          f"{sum(1 for u in units if u['ai']['practiceSource'] == 'ai')} 題，使用原題庫答案 "
          f"{sum(1 for u in units if u['ai']['practiceSource'] == 'original')} 題")
    print(f"answerStatus：{summary['counts']}")
    print(f"reviewStatus：待複核 {sum(1 for u in units if u['ai']['reviewStatus'] == REVIEW_PENDING)}、"
          f"不在複核清單 {sum(1 for u in units if u['ai']['reviewStatus'] == REVIEW_NONE)}")
    if summary["issues"]:
        print("⚠ 未涵蓋的資料異常（已使用原題庫答案）：\n  " + "\n  ".join(summary["issues"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
