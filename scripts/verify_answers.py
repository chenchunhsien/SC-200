#!/usr/bin/env python3
"""SC-200 答案驗證工具：整理題庫答案、PDF 原始答案、Microsoft Learn 證據與 ExamTopics 社群資料。

只在本機運作：不連線、不爬取網站、不呼叫任何生成 API；不修改題庫、解析檔與進度檔。
所有資料寫入 output/verification.json，報告寫入 output/verification_checklist.md；
寫入前一律先把既有檔案備份到 output/backups/（檔名含時間戳記）並驗證備份內容。

原則
----
- 官方文件一致性與社群支持度分開呈現，不合成單一分數。
- ExamTopics 頁面只能由人工確認為同一題（--confirm-same-question）；候選網址一律標為 probable。
- answer_status 預設 pending，只能用 set-status 由人指定。verified 的依據（--basis）二擇一：
  microsoft_docs（預設）：已查證、支持題庫答案且無已查證反證的 Microsoft 證據；
  examtopics_consensus：人工確認同一題、社群最高票答案與題庫答案一致，且比例 ≥ 70%、總票數 ≥ 10。
  依據與當時的官方一致性會記錄在狀態歷程。缺少證據不代表題庫答案錯誤，未判定題目暫定沿用題庫答案。
- 工具永遠不修改題庫答案；候選答案（proposed_answer）只是紀錄。

用法
----
    python scripts/verify_answers.py init --preset trial20          # 建立 20 題驗證項目（已存在者不覆寫）
    python scripts/verify_answers.py checklist                       # 產生 output/verification_checklist.md
    python scripts/verify_answers.py add-ms --id ID --url https://learn.microsoft.com/... --title T \\
        --excerpt "原文短句" --supports bank --verified --retrieved-at 2026-10-22 --checked-by NAME
    python scripts/verify_answers.py examtopics --id ID --candidate URL            # 只記錄候選網址
    python scripts/verify_answers.py examtopics --id ID --match exact --url URL --confirm-same-question \\
        --page-answer '"D"' --vote-answer '"D"' --vote-share 78 --vote-as-of 2026-10-22 --reason "..." --counter "..."
    python scripts/verify_answers.py examtopics --id ID --match not_found --search-note "搜尋了哪些關鍵字"
    python scripts/verify_answers.py propose --id ID --answer '"C"' --basis microsoft_evidence --by NAME
    python scripts/verify_answers.py compare                         # 只計算比對結果，不改狀態
    python scripts/verify_answers.py set-status --id ID --status verified --by NAME --reason "..."
    python scripts/verify_answers.py report

答案（--answer / --page-answer / --vote-answer）以 JSON 表示，格式依題型：
    單選 "B"；複選 ["A","D"]；下拉填空 {"select_0":"join"}；排序 ["d","a","f"]；Yes/No {"s1":"Yes","s2":"No"}
"""

from __future__ import annotations

import argparse
import difflib
import html as htmllib
import importlib.util
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
BASE_DIR = SCRIPTS.parent
DEFAULT_BANK = BASE_DIR / "public" / "data" / "sc200.json"
DEFAULT_OUT = BASE_DIR / "output"
SCHEMA_VERSION = 1

# 第一批試跑的 20 題（題型、章節、題組與答案疑點分布見規劃報告）
PRESETS = {
    "trial20": [
        "qd1-qs4-q002", "qd1-qs4-q008", "qd1-t3-q3", "qd2-t4-q1", "qd2-qs5-q014",
        "qd3-t1-q6", "qd3-t2-q1", "qd3-t3-q1", "qd3-t4-q6", "qd3-qs5-q017",
        "qd3-qs5-q043", "qd4-qs2-q004", "qd4-qs2-q040", "qd5-qs1-q011", "qd6-qs1-q019",
        "qd6-qs1-q034", "qd6-qs1-q035", "qd7-qs1-q032", "qd7-qs1-q039", "qd7-qs1-q047",
    ],
}

MS_URL = re.compile(r"^https://learn\.microsoft\.com/\S+$")
EXAMTOPICS_URL = re.compile(r"^https://(www\.)?examtopics\.com/discussions/\S+$")
CORRECTION_PREFIX = "已依 PDF 作者標註更正"
STATUSES = ("pending", "verified", "disputed")


def _load_enrich():
    """唯讀沿用 enrich_questions.py 的題目展開與題型定義，確保對象 ID 與解析檔一致。"""
    spec = importlib.util.spec_from_file_location("enrich_questions", SCRIPTS / "enrich_questions.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("enrich_questions", mod)
    spec.loader.exec_module(mod)
    return mod


eq = _load_enrich()


# ---------------------------------------------------------------------------
# 答案格式
# ---------------------------------------------------------------------------


def answer_of(unit) -> object:
    """題庫目前答案，以各題型的自然格式表示。"""
    q, t = unit.question, unit.type
    if t == "single_choice":
        return q["answer"]
    if t == "multiple_choice":
        return sorted(q["answer"])
    if t == "dropdown_cloze":
        return {b["id"]: b["answer"] for b in q["blanks"]}
    if t == "drag_drop_order":
        return list(q["answer"])
    if t == "statement_matrix":
        yes, no = q.get("columns", ["Yes", "No"])
        return {s["id"]: (yes if s["answer"] else no) for s in q["statements"]}
    raise ValueError(f"不支援的題型 {t}")


def parse_answer(unit, raw: str) -> object:
    """解析並驗證使用者輸入的答案 JSON；格式或值不符合該題結構時拋出 ValueError。"""
    try:
        val = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"答案必須是 JSON：{e}") from e
    q, t = unit.question, unit.type
    if t == "single_choice":
        ids = {o["id"] for o in q["options"]}
        if not isinstance(val, str) or val not in ids:
            raise ValueError(f"單選答案必須是選項 ID 字串，可用：{sorted(ids)}")
        return val
    if t == "multiple_choice":
        ids = {o["id"] for o in q["options"]}
        if isinstance(val, str):
            val = list(val)
        if not isinstance(val, list) or not val or not set(val) <= ids or len(set(val)) != len(val):
            raise ValueError(f"複選答案必須是不重複的選項 ID 陣列，可用：{sorted(ids)}")
        return sorted(val)
    if t == "dropdown_cloze":
        blanks = {b["id"]: b["options"] for b in q["blanks"]}
        if not isinstance(val, dict) or set(val) != set(blanks):
            raise ValueError(f"填空答案必須是包含所有挖空的物件：{sorted(blanks)}")
        for k, v in val.items():
            if v not in blanks[k]:
                raise ValueError(f"{k} 的答案必須是該挖空的選項之一")
        return val
    if t == "drag_drop_order":
        ids = {i["id"] for i in q["items"]}
        if not isinstance(val, list) or not val or not set(val) <= ids or len(set(val)) != len(val):
            raise ValueError(f"排序答案必須是不重複的項目 ID 陣列，可用：{sorted(ids)}")
        return val
    if t == "statement_matrix":
        yes, no = q.get("columns", ["Yes", "No"])
        ids = {s["id"] for s in q["statements"]}
        if not isinstance(val, dict) or set(val) != ids or any(v not in (yes, no) for v in val.values()):
            raise ValueError(f"矩陣答案必須是 {{陳述句 ID: \"{yes}\"/\"{no}\"}}，陳述句：{sorted(ids)}")
        return val
    raise ValueError(f"不支援的題型 {t}")


def compare_answers(a, b) -> str:
    """agree / disagree / partial（逐項部分相符）/ undetermined（任一方缺值）。"""
    if a is None or b is None:
        return "undetermined"
    if a == b:
        return "agree"
    if isinstance(a, dict) and isinstance(b, dict):
        same = sum(1 for k in a if b.get(k) == a[k])
        return "partial" if same else "disagree"
    if isinstance(a, list) and isinstance(b, list):
        return "partial" if set(a) & set(b) else "disagree"
    return "disagree"


# ---------------------------------------------------------------------------
# 檔案：備份後原子寫入
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def backup_file(path: Path, backup_dir: Path) -> Path | None:
    """既有檔案先複製到 backup_dir（時間戳記檔名、不覆蓋舊備份），並以 SHA256 驗證；失敗即拋出例外。"""
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dst = backup_dir / f"{path.stem}.{stamp}{path.suffix}"
    if dst.exists():
        raise FileExistsError(f"備份檔已存在：{dst}")
    shutil.copy2(path, dst)
    if eq.sha256_file(dst) != eq.sha256_file(path):
        raise OSError(f"備份內容與原檔不一致：{dst}")
    return dst


def safe_write(path: Path, text: str, backup_dir: Path) -> Path | None:
    made = backup_file(path, backup_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return made


class Ctx:
    def __init__(self, bank_path: Path, out_dir: Path):
        self.bank_path = bank_path
        self.out_dir = out_dir
        self.ver_path = out_dir / "verification.json"
        self.checklist_path = out_dir / "verification_checklist.md"
        self.backup_dir = out_dir / "backups"
        self.bank = json.loads(bank_path.read_text(encoding="utf-8"))
        self.bank_sha = eq.sha256_file(bank_path)
        self.units = {u.question_id: u for u in eq.iter_units(self.bank)}
        self.data = (json.loads(self.ver_path.read_text(encoding="utf-8")) if self.ver_path.exists()
                     else {"meta": {"schema_version": SCHEMA_VERSION, "created_at": now_iso()}, "questions": {}})
        self.data.setdefault("questions", {})
        self.locked_ids = self._locked_ids()

    def _locked_ids(self) -> set[str]:
        exp = self.out_dir / "explanations.json"
        if not exp.exists():
            return set()
        recs = json.loads(exp.read_text(encoding="utf-8")).get("explanations", {})
        return {k for k, r in recs.items() if (r.get("manual_revision") or {}).get("locked") is True}

    def entry(self, qid: str) -> dict:
        if qid not in self.data["questions"]:
            raise SystemExit(f"{qid} 尚未建立驗證項目，請先執行 init")
        return self.data["questions"][qid]

    def save(self) -> None:
        self.data["meta"].update(schema_version=SCHEMA_VERSION, updated_at=now_iso(), bank_sha256=self.bank_sha)
        made = safe_write(self.ver_path, json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", self.backup_dir)
        if made:
            print(f"已備份：{made}")
        print(f"已寫入：{self.ver_path}")


# ---------------------------------------------------------------------------
# 驗證項目
# ---------------------------------------------------------------------------


def new_entry(ctx: Ctx, unit) -> dict:
    q = unit.question
    note = q.get("reviewNote") or ""
    if note.startswith(CORRECTION_PREFIX):
        # 原始答案只以文字形式記在備註中；結構化答案需人工確認後再填入
        pdf = {"status": "text_only", "value": None, "text": note,
               "reason": "題庫答案曾依 PDF 作者標註更正；原始答案僅記錄於備註文字"}
    else:
        pdf = {"status": "unavailable", "value": None, "text": None,
               "reason": "題庫未記錄更正，但未逐題對照 PDF 確認原始答案"}
    notes = [{"kind": "agent_doubt", "text": note}] if note and not note.startswith(CORRECTION_PREFIX) else []
    case = unit.case
    return {
        "question_id": unit.question_id,
        "root_id": unit.root_id,
        "case_id": unit.root_id if case else None,
        "case_title": (case.get("caseInfo") or {}).get("title") if case else None,
        "type": unit.type,
        "chapter": (case or q).get("chapter"),
        "source": q.get("source"),
        "bank_answer": {"value": answer_of(unit), "snapshot_at": now_iso(), "bank_sha256": ctx.bank_sha},
        "pdf_original_answer": pdf,
        "prior_notes": notes,
        "proposed_answer": None,
        "microsoft_evidence": [],
        "examtopics_evidence": {
            "match_status": "not_searched", "candidates": [], "url": None, "topic": None,
            "question_number": None, "page_answer": None,
            "community_vote": {"answer": None, "share_percent": None, "vote_count": None, "as_of": None},
            "main_reasons": [], "counterarguments": [], "excerpt_file": None, "search_note": None,
            "captured_at": None, "captured_by": None,
        },
        "answer_comparison": None,
        "answer_status": "pending",
        "status_history": [],
        "uncertainty": None,
    }


def resolve_ids(ctx: Ctx, args) -> list[str]:
    ids = list(PRESETS[args.preset]) if getattr(args, "preset", None) else []
    if getattr(args, "ids", None):
        ids += [x.strip() for x in args.ids.split(",") if x.strip()]
    missing = [i for i in ids if i not in ctx.units]
    if missing:
        raise SystemExit(f"題庫中找不到這些子題 ID：{', '.join(missing)}")
    return list(dict.fromkeys(ids))


def cmd_init(ctx: Ctx, args) -> int:
    ids = resolve_ids(ctx, args)
    if not ids:
        raise SystemExit("請指定 --preset 或 --ids")
    added = [i for i in ids if i not in ctx.data["questions"]]
    kept = [i for i in ids if i in ctx.data["questions"]]
    for i in added:
        ctx.data["questions"][i] = new_entry(ctx, ctx.units[i])
    print(f"新增 {len(added)} 題；已存在、保持不變 {len(kept)} 題")
    if kept:
        print("  既有項目不覆寫（含快照與證據）：" + ", ".join(kept))
    locked = [i for i in ids if i in ctx.locked_ids]
    if locked:
        print("  注意：這些題目的解析為人工修訂鎖定，本工具不會改動解析：" + ", ".join(locked))
    if added:
        ctx.save()
    return 0


def cmd_add_ms(ctx: Ctx, args) -> int:
    e = ctx.entry(args.id)
    if not MS_URL.match(args.url):
        raise SystemExit("Microsoft 證據網址必須是 https://learn.microsoft.com/...")
    if args.verified and not (args.excerpt and args.retrieved_at and args.checked_by):
        raise SystemExit("標記 --verified 需同時提供 --excerpt（實際讀到的原文短句）、--retrieved-at 與 --checked-by")
    e["microsoft_evidence"].append({
        "url": args.url, "title": args.title, "excerpt": args.excerpt, "supports": args.supports,
        "verification": "verified" if args.verified else "unverified",
        "retrieved_at": args.retrieved_at, "checked_by": args.checked_by, "added_at": now_iso(),
    })
    ctx.save()
    return 0


def cmd_examtopics(ctx: Ctx, args) -> int:
    e = ctx.entry(args.id)
    et = e["examtopics_evidence"]
    unit = ctx.units[args.id]
    for url in args.candidate or []:
        if not EXAMTOPICS_URL.match(url):
            raise SystemExit(f"候選網址格式不符（需為 https://www.examtopics.com/discussions/...）：{url}")
        if url not in [c["url"] for c in et["candidates"]]:
            et["candidates"].append({"url": url, "added_at": now_iso(), "note": "候選，尚未人工確認是否為同一題"})
        if et["match_status"] in ("not_searched", "not_found"):
            et["match_status"] = "probable"
    if args.match:
        if args.match == "exact":
            if not args.confirm_same_question:
                raise SystemExit("標記 exact 必須加上 --confirm-same-question，代表你已人工核對題幹、選項與案例背景")
            if not args.url or not EXAMTOPICS_URL.match(args.url):
                raise SystemExit("exact 必須提供 ExamTopics 討論頁網址 --url https://www.examtopics.com/discussions/...")
        if args.match == "probable" and not (args.url or et["candidates"]):
            raise SystemExit("probable 需提供 --url 或先以 --candidate 記錄候選網址")
        if args.match == "not_found" and not args.search_note:
            raise SystemExit("not_found 必須以 --search-note 記錄搜尋過的關鍵字或方式")
        et["match_status"] = args.match
        if args.url:
            et["url"] = args.url
    if args.page_answer is not None or args.vote_answer is not None:
        if et["match_status"] != "exact":
            raise SystemExit("只有人工確認為同一題（exact）後，才能記錄 ExamTopics 的頁面答案與投票")
    try:
        if args.page_answer is not None:
            et["page_answer"] = parse_answer(unit, args.page_answer)
        if args.vote_answer is not None:
            et["community_vote"]["answer"] = parse_answer(unit, args.vote_answer)
    except ValueError as err:
        raise SystemExit(str(err)) from err
    for key, val in (("share_percent", args.vote_share), ("vote_count", args.vote_count), ("as_of", args.vote_as_of)):
        if val is not None:
            et["community_vote"][key] = val
    et["main_reasons"] += args.reason or []
    et["counterarguments"] += args.counter or []
    for key in ("topic", "question_number", "excerpt_file", "search_note"):
        if getattr(args, key) is not None:
            et[key] = getattr(args, key)
    et["captured_at"], et["captured_by"] = now_iso(), args.by or et["captured_by"]
    ctx.save()
    return 0


def cmd_propose(ctx: Ctx, args) -> int:
    e = ctx.entry(args.id)
    try:
        value = parse_answer(ctx.units[args.id], args.answer)
    except ValueError as err:
        raise SystemExit(str(err)) from err
    e["proposed_answer"] = {"value": value, "basis": args.basis, "proposed_by": args.by,
                            "note": args.note, "confirmed": False, "at": now_iso()}
    print("已記錄候選答案（不會修改題庫；如需更改答案請人工編輯 sc200.json）")
    ctx.save()
    return 0


KNOWN_SUPPORTS = {"bank", "proposed", "neither"}
# examtopics_consensus 判定門檻：最高票比例與總票數
CONSENSUS_MIN_SHARE = 70
CONSENSUS_MIN_VOTES = 10
NO_COMMUNITY = "未取得社群答案資訊"


def microsoft_consistency(e: dict) -> str:
    """只依已查證（verification == "verified"）的 Microsoft 證據判斷方向；未查證證據不列入。

    consistent           已查證證據只支持題庫答案（neither 不影響）
    proposed_supported   已查證證據只支持題庫以外的答案；代表需要人工檢視，不代表候選答案已確立
    conflicting          同時有支持題庫與支持其他答案的已查證證據
    undetermined         已查證證據都是 neither（與答案無直接關係）
    invalid_evidence     出現未知的 supports 值；保守處理，不推論方向
    no_verified_evidence 沒有已查證證據
    """
    supports = {x.get("supports") for x in e["microsoft_evidence"] if x.get("verification") == "verified"}
    if not supports:
        return "no_verified_evidence"
    if supports - KNOWN_SUPPORTS:
        return "invalid_evidence"
    directions = supports - {"neither"}
    if directions == {"bank"}:
        return "consistent"
    if directions == {"proposed"}:
        return "proposed_supported"
    if directions == {"bank", "proposed"}:
        return "conflicting"
    return "undetermined"


def compute(ctx: Ctx, e: dict) -> dict:
    unit = ctx.units[e["question_id"]]
    current = answer_of(unit)
    et = e["examtopics_evidence"]
    exact = et["match_status"] == "exact"
    proposed = (e.get("proposed_answer") or {}).get("value")
    return {
        "bank_changed_since_snapshot": current != e["bank_answer"]["value"],
        "microsoft_consistency": microsoft_consistency(e),
        "microsoft_evidence_count": {
            "verified": sum(x["verification"] == "verified" for x in e["microsoft_evidence"]),
            "unverified": sum(x["verification"] != "verified" for x in e["microsoft_evidence"]),
        },
        "examtopics_match": et["match_status"],
        "bank_vs_examtopics_page": compare_answers(current, et["page_answer"]) if exact else "no_confirmed_match",
        "bank_vs_examtopics_vote": (compare_answers(current, et["community_vote"]["answer"]) if exact
                                    else "no_confirmed_match"),
        "community_vote_share_percent": et["community_vote"]["share_percent"] if exact else None,
        "community_vote_count": et["community_vote"].get("vote_count") if exact else None,
        "bank_vs_proposed": compare_answers(current, proposed) if proposed is not None else "no_proposal",
        "exam_guidance": exam_guidance(e),
        "community_threshold": community_threshold(e),
        "recommendation": recommend(ctx, e),
        "computed_at": now_iso(),
    }


def exam_guidance(e: dict) -> str:
    """考試用的建議（每次推導，不另存狀態）：已判定者依依據顯示，未判定者暫定沿用題庫答案。"""
    et = e["examtopics_evidence"]
    if e["answer_status"] == "verified":
        last = next((h for h in reversed(e.get("status_history") or []) if h.get("to") == "verified"), {})
        # 舊紀錄沒有 basis 欄位：當時只能依官方文件判定
        return "採用（社群共識）" if last.get("basis") == "examtopics_consensus" else "採用（官方文件）"
    if e["answer_status"] == "disputed":
        return "有爭議，見理由"
    if et["match_status"] == "not_found":
        return "暫定：題庫答案（ExamTopics 無相同題）"
    if et["match_status"] == "exact" and et["community_vote"].get("answer") is not None:
        return "待判定：已有社群資料"
    return "暫定：題庫答案（尚未查證）"


def vote_text(et: dict) -> str:
    """社群投票的顯示文字；沒有資料時不印出 None。"""
    vote = et["community_vote"]
    if et["match_status"] != "exact" or vote.get("answer") is None:
        return NO_COMMUNITY
    extra = [f"{vote['share_percent']:g}%" if vote.get("share_percent") is not None else None,
             f"{vote['vote_count']} 票" if vote.get("vote_count") is not None else None,
             f"截至 {vote['as_of']}" if vote.get("as_of") else None]
    extra = [x for x in extra if x]
    return f"`{fmt(vote['answer'])}`" + (f"（{'，'.join(extra)}）" if extra else "")


def cmd_compare(ctx: Ctx, args) -> int:
    for qid, e in ctx.data["questions"].items():
        e["answer_comparison"] = compute(ctx, e)
    print(f"已計算 {len(ctx.data['questions'])} 題的比對結果（answer_status 未變更）")
    ctx.save()
    return 0


def consensus_problems(ctx: Ctx, e: dict) -> list[str]:
    """examtopics_consensus 依據的門檻：人工確認同一題、最高票答案與題庫答案完全一致、比例與票數達標。"""
    et = e["examtopics_evidence"]
    vote = et["community_vote"]
    if et["match_status"] != "exact":
        return [f"ExamTopics 尚未人工確認為同一題（目前：{et['match_status']}）"]
    if vote.get("answer") is None:
        return ["未取得社群答案資訊（最高票答案未記錄）"]
    problems = []
    if compare_answers(answer_of(ctx.units[e["question_id"]]), vote["answer"]) != "agree":
        problems.append(f"社群最高票答案 {fmt(vote['answer'])} 與題庫答案不同；請改標 disputed 並由人決定是否修改題庫")
    share, count = vote.get("share_percent"), vote.get("vote_count")
    if share is None:
        problems.append("未記錄最高票比例（--vote-share）")
    elif share < CONSENSUS_MIN_SHARE:
        problems.append(f"最高票比例 {share:g}% 未達 {CONSENSUS_MIN_SHARE}%")
    if count is None:
        problems.append("未記錄總票數（--vote-count）")
    elif count < CONSENSUS_MIN_VOTES:
        problems.append(f"總票數 {count} 未達 {CONSENSUS_MIN_VOTES} 票")
    return problems


def cmd_set_status(ctx: Ctx, args) -> int:
    e = ctx.entry(args.id)
    if not args.by or args.by.strip().lower() in ("auto", "tool", "gemini", "model"):
        raise SystemExit("--by 必須是做判定的人；狀態不可由工具或模型自動設定")
    ms = microsoft_consistency(e)
    if args.status == "verified":
        if args.basis == "microsoft_docs" and ms != "consistent":
            raise SystemExit(f"不能以 microsoft_docs 標記 verified：需要已查證且支持題庫答案的 Microsoft 證據，"
                             f"且無已查證反證（目前：{ms}）。社群共識請改用 --basis examtopics_consensus。")
        if args.basis == "examtopics_consensus":
            problems = consensus_problems(ctx, e)
            if problems:
                raise SystemExit("不能以 examtopics_consensus 標記 verified：" + "；".join(problems))
            if ms not in ("consistent", "no_verified_evidence"):
                print(f"注意：Microsoft 官方一致性為 {ms}，已記錄於狀態歷程供追蹤。")
        if compute(ctx, e)["bank_changed_since_snapshot"]:
            raise SystemExit("題庫答案已在建立快照後變更，請先重新核對證據")
    if args.status == "disputed" and not args.reason:
        raise SystemExit("標記 disputed 必須以 --reason 說明爭議")
    e["status_history"].append({"from": e["answer_status"], "to": args.status, "by": args.by,
                                "reason": args.reason, "at": now_iso(),
                                "basis": args.basis if args.status == "verified" else None,
                                "microsoft_consistency_at_decision": ms})
    e["answer_status"] = args.status
    if args.uncertainty is not None:
        e["uncertainty"] = args.uncertainty
    ctx.save()
    return 0


# ---------------------------------------------------------------------------
# 社群門檻與建議答案（每次即時推導，不修改題庫與 answer_status）
# ---------------------------------------------------------------------------


def community_threshold(e: dict) -> dict:
    """社群共識門檻：同一題（exact）、總票數 ≥ 10、最高票比例 ≥ 70%。與題庫答案是否一致分開判斷。"""
    et = e["examtopics_evidence"]
    vote = et["community_vote"]
    exact = et["match_status"] == "exact"
    share, count = vote.get("share_percent"), vote.get("vote_count")
    reasons = []
    if not exact:
        reasons.append(f"題目未確認為同一題（{et['match_status']}）")
    else:
        if vote.get("answer") is None:
            reasons.append("未取得最高票答案")
        if share is None:
            reasons.append("未取得最高票比例")
        elif share < CONSENSUS_MIN_SHARE:
            reasons.append(f"最高票比例 {share:g}% 未達 {CONSENSUS_MIN_SHARE}%")
        if count is None:
            reasons.append("未取得總票數")
        elif count < CONSENSUS_MIN_VOTES:
            reasons.append(f"總票數 {count} 未達 {CONSENSUS_MIN_VOTES}")
    return {"met": not reasons, "reasons": reasons, "top_answer": vote.get("answer") if exact else None,
            "share_percent": share if exact else None, "total_votes": count if exact else None}


def recommend(ctx: "Ctx", e: dict) -> dict:
    """依「ExamTopics → Microsoft 官方文件 → 題庫答案」產生建議答案與可信程度。

    高：社群共識達標且有已查證官方文件支持同一答案；社群共識達標但無官方佐證最多為中；
    官方證據只在已查證且明確支持時才列入；沒有找到反證不視為官方支持。
    """
    unit = ctx.units[e["question_id"]]
    bank = answer_of(unit)
    et = e["examtopics_evidence"]
    th = community_threshold(e)
    ms = microsoft_consistency(e)
    proposed = (e.get("proposed_answer") or {}).get("value")
    exact = et["match_status"] == "exact"
    candidate = None
    if exact:
        candidate = et["page_answer"] if et["page_answer"] is not None else et["community_vote"].get("answer")
    conflicts, uncertain = [], []

    def ms_supports(ans) -> bool:
        return (ms == "consistent" and ans == bank) or (ms == "proposed_supported" and proposed is not None and ans == proposed)

    def ms_against(ans) -> bool:
        if ms in ("conflicting", "invalid_evidence"):
            return True
        if ms == "consistent":
            return ans != bank
        if ms == "proposed_supported":
            return not (proposed is not None and ans == proposed)
        return False

    if th["met"]:
        answer = th["top_answer"]
        if ms_supports(answer):
            confidence, source, nxt = "高", "ExamTopics 社群共識＋Microsoft 官方文件", "可請你確認後標記 verified"
        elif ms_against(answer):
            confidence, source = "低", "ExamTopics 社群共識（與官方證據衝突）"
            conflicts.append(f"官方文件一致性為 {ms}，與社群共識不一致")
            nxt = "檢查題目版本、適用條件與產品行為"
        else:
            confidence, source = "中", "ExamTopics 社群共識"
            uncertain.append("尚無已查證的官方文件佐證")
            nxt = "判讀 Microsoft Learn 候選文件以提升可信程度"
        if answer != bank:
            conflicts.append(f"社群共識 {fmt(answer)} 與題庫答案 {fmt(bank)} 不同")
            nxt = "由你決定是否修改題庫答案；" + nxt
    else:
        if exact and th["reasons"] and candidate is not None:
            uncertain.append("社群資料未達共識門檻：" + "、".join(th["reasons"]))
        if ms == "consistent":
            answer, confidence, source, nxt = bank, "中", "Microsoft 官方文件", "可請你確認後標記 verified"
            if candidate is not None and candidate != bank:
                conflicts.append(f"社群候選 {fmt(candidate)} 與官方文件支持的題庫答案不同")
        elif ms == "proposed_supported" and proposed is not None:
            answer, confidence, source = proposed, "中", "Microsoft 官方文件（候選答案）"
            conflicts.append(f"官方文件支持的候選答案 {fmt(proposed)} 與題庫答案 {fmt(bank)} 不同")
            nxt = "由你決定是否修改題庫答案"
        elif ms in ("proposed_supported", "conflicting", "invalid_evidence"):
            answer, confidence, source = bank, "低", "題庫答案（暫定）"
            conflicts.append(f"官方文件一致性為 {ms}")
            nxt = "人工檢視官方證據並決定候選答案"
        elif candidate is not None:
            answer, confidence = bank, "低"
            if candidate == bank:
                source = "題庫答案（與 ExamTopics 社群候選一致）"
            else:
                source = "題庫答案（暫定）"
                conflicts.append(f"ExamTopics 社群候選 {fmt(candidate)} 與題庫答案 {fmt(bank)} 不同")
            nxt = "判讀 Microsoft Learn 候選文件比對社群候選"
        else:
            answer, confidence, source = bank, "未驗證", "題庫答案（暫定）"
            nxt = {
                "not_searched": "搜尋 ExamTopics",
                "probable": "檢視自動比對報告或查 Microsoft Learn",
                "not_found": "判讀 Microsoft Learn 候選文件",
            }.get(et["match_status"], "社群資料不可得，判讀 Microsoft Learn 候選文件")
            uncertain.append("無可用的社群資料與已查證官方證據")
    if e["answer_status"] == "disputed":
        conflicts.append("已人工標記 disputed")
    if e.get("uncertainty"):
        uncertain.append(e["uncertainty"])
    cands = [c for c in e.get("microsoft_candidates") or [] if c.get("status") == "unverified_candidate"]
    if cands and confidence in ("低", "未驗證", "中") and "Microsoft Learn" in nxt:
        nxt += f"（已有 {len(cands)} 篇未查證候選）"
    return {"answer": answer, "source": source, "confidence": confidence, "community_candidate": candidate,
            "microsoft_consistency": ms, "conflicts": conflicts, "uncertainty": uncertain, "next_step": nxt}


def consensus_eligible(ctx: "Ctx", e: dict) -> bool:
    """可供你檢視的社群共識候選：達門檻、與題庫答案一致、無官方反證、尚未判定。不會自動設定狀態。"""
    rec = recommend(ctx, e)
    return (community_threshold(e)["met"] and rec["answer"] == answer_of(ctx.units[e["question_id"]])
            and rec["confidence"] in ("中", "高") and e["answer_status"] == "pending")


def cmd_consensus_candidates(ctx: "Ctx", args) -> int:
    rows = [(k, e) for k, e in ctx.data["questions"].items() if consensus_eligible(ctx, e)]
    print(f"社群共識候選（達門檻、與題庫一致、無官方反證、pending）：{len(rows)} 題；本指令只列出，不變更狀態")
    for k, e in rows:
        th, rec = community_threshold(e), recommend(ctx, e)
        print(f"  {k:<15} 答案 {fmt(th['top_answer']):<10} {th['share_percent']:g}% / {th['total_votes']} 票  "
              f"官方：{rec['microsoft_consistency']:<20} 可信程度：{rec['confidence']}")
    return 0


# ---------------------------------------------------------------------------
# ExamTopics 題目列表頁（只抓取免費公開的第 1 頁；不登入、不繞過任何限制）
# ---------------------------------------------------------------------------

EXAMTOPICS_LIST_URL = "https://www.examtopics.com/exams/microsoft/sc-200/view/{page}/"
FREE_LIST_PAGES = {1}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SC200-personal-study"
DISCUSSION_ID = re.compile(r"/discussions/microsoft/view/(\d+)-")
RENAMES = [  # 比對題目時視為相同的產品更名
    (r"microsoft 365 defender", "microsoft defender xdr"), (r"azure security center", "microsoft defender for cloud"),
    (r"microsoft cloud app security|cloud app security", "microsoft defender for cloud apps"),
    (r"azure sentinel", "microsoft sentinel"), (r"azure active directory|azure ad\b", "microsoft entra id"),
]


class FetchStop(Exception):
    """抓取或解析必須停止的情況：需要登入、驗證頁、網站結構改變或資料不完整。"""


def _text(fragment: str) -> str:
    fragment = re.sub(r"(?s)<span[^>]*most-voted-answer-badge[^>]*>.*?</span>", " ", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>", " ", fragment)
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def fetch_list_page(page: int) -> tuple[str, str]:
    if page not in FREE_LIST_PAGES:
        raise FetchStop(f"只允許抓取免費公開的第 1 頁（要求第 {page} 頁）")
    url = EXAMTOPICS_LIST_URL.format(page=page)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # 轉址（通常是登入頁）一律視為停止條件
            return None

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as resp:
            return url, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        loc = err.headers.get("Location") if err.headers else None
        raise FetchStop(f"HTTP {err.code}" + (f"，轉址至 {loc}" if loc else "")) from err
    except urllib.error.URLError as err:
        raise FetchStop(f"連線失敗：{err.reason}") from err


def parse_list_page(page_html: str) -> list[dict]:
    if re.search(r"(?i)create a free account|log in to continue|cf-challenge|captcha", page_html):
        raise FetchStop("頁面要求登入或驗證")
    parts = page_html.split("exam-question-card")[1:]
    if not parts:
        raise FetchStop("找不到題卡（網站結構可能已改變）")
    cards = []
    for raw in parts:
        label = re.search(r"Exam SC-200 topic (\d+) question (\d+) discussion", raw)
        disc = re.search(r'fa fa-comment" title="(\d+)"', raw)
        stem = re.search(r'(?s)<p class="card-text">(.*?)</p>', raw)
        if not (label and disc and stem):
            raise FetchStop("題卡缺少題號、討論頁 ID 或題幹（網站結構可能已改變）")
        options = [(m.group(1), _text(m.group(2))) for m in
                   re.finditer(r'(?s)data-choice-letter="([A-Z])">\s*[A-Z]\.\s*</span>(.*?)</li>', raw)]
        ans = re.search(r"<strong>Correct Answer:</strong>\s*<strong[^>]*>([^<]*)</strong>", raw)
        chart = re.search(r'data-chart="([^"]*)"', raw)
        dist, chart_status = None, "missing"
        if chart:
            try:
                dist = json.loads(htmllib.unescape(chart.group(1)))
                if not dist or not all(isinstance(d.get("answer"), str) and isinstance(d.get("percent"), (int, float))
                                       and isinstance(d.get("votes"), int) for d in dist):
                    raise ValueError
                chart_status = "ok"
            except (ValueError, AttributeError):
                dist, chart_status = None, "invalid"
        cards.append({"discussion_id": disc.group(1), "topic": label.group(1), "question_number": label.group(2),
                      "stem": _text(stem.group(1)), "options": options,
                      "page_answer_raw": ans.group(1).strip() or None if ans else None,
                      "distribution": dist, "chart_status": chart_status})
    return cards


def _norm(s: str) -> str:
    s = s.lower()
    for pat, rep in RENAMES:
        s = re.sub(pat, rep, s)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def auto_match(unit, card: dict) -> dict:
    """比對題幹相似度、選項文字與順序；只產生報告，不改變 match_status。"""
    q = unit.question
    stem_ratio = round(difflib.SequenceMatcher(None, _norm(q["question"]), _norm(card["stem"])).ratio(), 3)
    report = {"discussion_id": card["discussion_id"], "examtopics_topic": card["topic"],
              "examtopics_question": card["question_number"], "stem_similarity": stem_ratio, "checked_at": now_iso()}
    if unit.type not in ("single_choice", "multiple_choice") or not card["options"]:
        report.update(options_match=None, order_match=None, verdict="incomplete",
                      note="非選擇題或頁面選項非文字，無法逐項比對選項")
        return report
    bank_opts = [(o["id"], _norm(o["text"])) for o in q["options"]]
    page_opts = [(k, _norm(v)) for k, v in card["options"]]
    same_set = sorted(t for _, t in bank_opts) == sorted(t for _, t in page_opts)
    same_order = bank_opts == page_opts
    verdict = "all_match" if same_order and stem_ratio >= 0.85 else "mismatch"
    report.update(options_match=same_set, order_match=same_order, verdict=verdict,
                  note=None if verdict == "all_match" else "選項文字、順序或題幹相似度未完全相符")
    return report


def card_capture(unit, card: dict) -> tuple[dict, list[str]]:
    """把題卡轉成社群資料；只使用頁面顯示的數字，總票數為各答案顯示票數加總。"""
    notes, values = [], {"page_answer": None, "vote_answer": None, "share_percent": None, "vote_count": None}
    choice = unit.type in ("single_choice", "multiple_choice")

    def as_answer(raw):
        if raw is None or not choice:
            return None
        try:
            return parse_answer(unit, json.dumps(raw if unit.type == "single_choice" else list(raw)))
        except ValueError:
            notes.append(f"頁面答案 {raw} 無法對應本題選項")
            return None

    values["page_answer"] = as_answer(card["page_answer_raw"])
    if card["page_answer_raw"] is None:
        notes.append("頁面未以文字提供答案（可能為圖片）")
    elif not choice:
        notes.append("非選擇題，頁面答案無法對應本題結構，僅保留原文")
    dist = card["distribution"]
    if dist is None:
        notes.append("頁面沒有投票分布" if card["chart_status"] == "missing" else "投票分布格式無法解析")
    else:
        values["vote_count"] = sum(d["votes"] for d in dist)
        top = max(dist, key=lambda d: d["votes"])
        if sum(1 for d in dist if d["votes"] == top["votes"]) > 1:
            notes.append("最高票同票，不判定最高票答案")
        else:
            values["vote_answer"] = as_answer(top["answer"])
            if values["vote_answer"] is None:  # 無法對應本題答案時，比例也不單獨保留（分布仍完整保存）
                if not choice:
                    notes.append("非選擇題，投票答案無法對應本題結構，僅保留投票分布與總票數")
            else:
                values["share_percent"] = top["percent"]
    return values, notes


def discussion_ids(et: dict) -> list[str]:
    urls = [et.get("url")] + [c["url"] for c in et.get("candidates") or []]
    return [m.group(1) for u in urls if u for m in [DISCUSSION_ID.search(u)] if m]


def cmd_import_examtopics(ctx: "Ctx", args) -> int:
    if args.page not in FREE_LIST_PAGES:
        raise SystemExit("只允許免費公開的第 1 頁")
    log = ctx.data["meta"].setdefault("examtopics_fetch_log", [])
    try:
        if args.from_file:  # 測試或離線使用
            source, page_html = f"file:{Path(args.from_file).name}", Path(args.from_file).read_text(encoding="utf-8")
        else:
            source, page_html = fetch_list_page(args.page)
        cards = parse_list_page(page_html)
    except FetchStop as err:
        log.append({"at": now_iso(), "page": args.page, "status": "stopped", "reason": str(err)})
        print(f"已停止本次抓取：{err}")
        if not args.dry_run:
            ctx.save()
        return 2
    by_id = {c["discussion_id"]: c for c in cards}
    captured_at = now_iso()
    summary = {"cards": len(cards), "matched": [], "written": [], "unchanged": [], "kept_existing": [], "report_only": []}
    for qid, e in ctx.data["questions"].items():
        et = e["examtopics_evidence"]
        hit = next((by_id[i] for i in discussion_ids(et) if i in by_id), None)
        if not hit:
            continue
        unit = ctx.units[qid]
        summary["matched"].append(qid)
        et["auto_match"] = auto_match(unit, hit)
        if et["match_status"] != "exact":
            summary["report_only"].append(qid)
            continue
        values, notes = card_capture(unit, hit)
        vote = et["community_vote"]
        current = {"page_answer": et["page_answer"], "vote_answer": vote.get("answer"),
                   "share_percent": vote.get("share_percent"), "vote_count": vote.get("vote_count")}
        differs = [k for k in values if current[k] is not None and values[k] != current[k]]
        if differs and not args.refresh:
            summary["kept_existing"].append(f"{qid}（{', '.join(differs)} 與既有資料不同，未覆寫；可加 --refresh）")
            notes.append("新擷取資料與既有資料不同，保留既有值")
        else:
            if differs:  # --refresh：先保存舊值再更新
                et.setdefault("capture_history", []).append({**current, "distribution": vote.get("distribution"),
                                                             "replaced_at": captured_at})
            changed = values != current or vote.get("distribution") != hit["distribution"]
            et["page_answer"] = values["page_answer"]
            vote.update(answer=values["vote_answer"], share_percent=values["share_percent"],
                        vote_count=values["vote_count"], as_of=captured_at[:10] if hit["distribution"] else vote.get("as_of"),
                        distribution=hit["distribution"])
            (summary["written"] if changed else summary["unchanged"]).append(qid)
        et["community_capture"] = {
            "source": source, "page": args.page, "discussion_id": hit["discussion_id"],
            "examtopics_topic": hit["topic"], "examtopics_question": hit["question_number"],
            "page_answer_raw": hit["page_answer_raw"], "captured_at": captured_at,
            "status": "complete" if values["page_answer"] is not None and values["vote_count"] is not None else "partial",
            "notes": notes,
        }
    log.append({"at": captured_at, "page": args.page, "status": "ok", "source": source, "cards": len(cards),
                "matched": summary["matched"]})
    print(f"題卡 {summary['cards']} 張；對應到 {len(summary['matched'])} 題：{', '.join(summary['matched']) or '無'}")
    for key, title in (("written", "寫入社群資料"), ("unchanged", "資料相同未變更"), ("kept_existing", "保留既有資料"),
                       ("report_only", "probable：只產生比對報告")):
        if summary[key]:
            print(f"  {title}：{', '.join(summary[key])}")
    if args.dry_run:
        print("（dry run，未寫入）")
        return 0
    ctx.save()
    return 0


def cmd_accept_match(ctx: "Ctx", args) -> int:
    """由你明確接受自動比對結果，將 probable 改為 exact。"""
    e = ctx.entry(args.id)
    et = e["examtopics_evidence"]
    am = et.get("auto_match") or {}
    if not args.by or args.by.strip().lower() in ("auto", "tool", "gemini", "model"):
        raise SystemExit("--by 必須是做判定的人")
    if et["match_status"] != "probable":
        raise SystemExit(f"只有 probable 可接受自動比對（目前：{et['match_status']}）")
    if am.get("verdict") != "all_match":
        raise SystemExit(f"自動比對結果不是 all_match（目前：{am.get('verdict')}），不能接受")
    cand = next((c["url"] for c in et["candidates"] if DISCUSSION_ID.search(c["url"])
                 and DISCUSSION_ID.search(c["url"]).group(1) == am.get("discussion_id")), None)
    if not cand:
        raise SystemExit("自動比對的討論頁 ID 不在候選網址中")
    et.setdefault("match_history", []).append({"from": "probable", "to": "exact", "by": args.by, "at": now_iso(),
                                               "basis": "auto_match_accepted", "auto_match": am})
    et["match_status"], et["url"] = "exact", cand
    ctx.save()
    return 0


# ---------------------------------------------------------------------------
# Microsoft Learn 候選文件（只存為未查證候選，不影響官方一致性判定）
# ---------------------------------------------------------------------------

LEARN_SEARCH = "https://learn.microsoft.com/api/search"
STOPWORDS = set("""a an the and or of to in on for from by with that this these those is are be been was were use
using you your should which what when then than into as at it its can will must each all any only not no more
most other same per via based new add set create configure enable select microsoft azure""".split())


def learn_search(query: str, top: int = 5) -> list[dict]:
    url = f"{LEARN_SEARCH}?{urllib.parse.urlencode({'search': query, 'locale': 'en-us', '$top': top})}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [{"title": r.get("title", ""), "url": r.get("url", "")} for r in data.get("results", [])
            if str(r.get("url", "")).startswith("https://learn.microsoft.com/")]


def fetch_doc_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        page = resp.read().decode("utf-8", errors="replace")
    main = re.search(r"(?s)<main.*?</main>", page)
    page = main.group(0) if main else page
    page = re.sub(r"(?s)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", page)
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def answer_terms(unit) -> list[str]:
    """題目選項 / 挖空選項 / 排序項目 / 陳述句中的關鍵字，用來找出文件中相關的段落。"""
    q = unit.question
    texts = [o["text"] for o in q.get("options", [])] + [i["text"] for i in q.get("items", [])]
    texts += [x for b in q.get("blanks", []) for x in b["options"]] + [s["text"] for s in q.get("statements", [])]
    words = {w for t in texts for w in re.findall(r"[A-Za-z_][A-Za-z0-9_\-]{3,}", t) if w.lower() not in STOPWORDS}
    return sorted(words, key=str.lower)


def doc_excerpts(text: str, terms: list[str], limit: int = 3) -> list[dict]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    scored = []
    for s in sentences:
        hit = sorted({t for t in terms if re.search(rf"\b{re.escape(t)}\b", s, re.I)})
        if hit and 30 <= len(s) <= 600:
            scored.append((len(hit), s, hit))
    scored.sort(key=lambda x: -x[0])
    return [{"text": s[:400], "matched_terms": hit} for _, s, hit in scored[:limit]]


def needs_docs(ctx: "Ctx", e: dict) -> bool:
    """無可用社群證據、未達門檻、社群與題庫不一致、匹配不確定或官方證據可能衝突時，需要查官方文件。"""
    th, rec = community_threshold(e), recommend(ctx, e)
    agree = th["met"] and th["top_answer"] == answer_of(ctx.units[e["question_id"]])
    return not agree or rec["microsoft_consistency"] in ("proposed_supported", "conflicting", "invalid_evidence")


def cmd_ms_search(ctx: "Ctx", args) -> int:
    ids = [i.strip() for i in (args.ids or "").split(",") if i.strip()] or list(ctx.data["questions"])
    targets = [i for i in ids if i in ctx.data["questions"] and needs_docs(ctx, ctx.data["questions"][i])]
    if not args.refresh:
        targets = [i for i in targets if not ctx.data["questions"][i].get("microsoft_candidates")]
    if args.limit:
        targets = targets[: args.limit]
    print(f"需要查 Microsoft Learn 的題目：{len(targets)} 題" + ("（dry run）" if args.dry_run else ""))
    if args.dry_run:
        print("  " + ", ".join(targets))
        return 0
    for n, qid in enumerate(targets, start=1):
        e, unit = ctx.data["questions"][qid], ctx.units[qid]
        query = search_phrase(unit.question["question"])
        terms = answer_terms(unit)
        found, error = [], None
        try:
            results = learn_search(query)
            docs = [r for r in results if "/training/" not in r["url"]][: args.docs] or results[: args.docs]
            for r in docs:
                time.sleep(args.delay)
                found.append({"url": r["url"], "title": r["title"], "query": query, "retrieved_at": now_iso(),
                              "excerpts": doc_excerpts(fetch_doc_text(r["url"]), terms),
                              "status": "unverified_candidate"})
        except (urllib.error.URLError, OSError, ValueError) as err:
            error = f"{type(err).__name__}: {err}"
        prev = e.get("microsoft_candidates") or []
        e["microsoft_candidates"] = found if found else prev
        e.setdefault("microsoft_search_log", []).append(
            {"at": now_iso(), "query": query, "found": len(found), "error": error})
        print(f"  [{n}/{len(targets)}] {qid}：候選 {len(found)} 篇" + (f"（錯誤：{error}）" if error else ""))
        time.sleep(args.delay)
    ctx.save()
    return 0


# ---------------------------------------------------------------------------
# 清單與報告
# ---------------------------------------------------------------------------


GENERIC_SENTENCE = re.compile(
    r"^(For each|How should|To answer|Which|What|Each correct|NOTE|Otherwise|The solution must"
    r"|You have an? (Microsoft 365( E5)?|Azure) (subscription|tenant)( that uses [^.]*)?\.$)",
    re.I,
)


def search_phrase(stem: str) -> str:
    """取題幹中最長、非通用的一句（排除 'The solution must…'、'For each…' 等常見句），作為人工搜尋的關鍵字建議。"""
    sents = [s.strip() for s in re.split(r"(?<=[.?!])\s+", " ".join(stem.split())) if s.strip()]
    useful = [s for s in sents if not GENERIC_SENTENCE.match(s)] or sents
    best = max(useful, key=len) if useful else ""
    return " ".join(best.split()[:14])


def fmt(v) -> str:
    if v is None:
        return "—"
    return json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v


def render_checklist(ctx: Ctx) -> str:
    lines = [
        "# SC-200 答案驗證核對清單",
        "",
        f"- 產生時間：{now_iso()}",
        f"- 題庫：`{ctx.bank_path.name}`（SHA256 `{ctx.bank_sha[:16]}…`）",
        f"- 題數：{len(ctx.data['questions'])}；資料檔：`output/verification.json`",
        "- 本清單由 `scripts/verify_answers.py checklist` 產生，含真實題目內容，僅供本機使用、請勿提交或公開。",
        "- 判定優先順序：① ExamTopics 社群共識（人工確認同一題、最高票與題庫答案一致、"
        f"≥ {CONSENSUS_MIN_SHARE}% 且 ≥ {CONSENSUS_MIN_VOTES} 票）→ ② 社群分歧時參考 Microsoft 官方文件 "
        "→ ③ 皆無法判斷時暫定沿用題庫答案。缺少證據不代表題庫答案錯誤。",
        "- ExamTopics：請以瀏覽器人工搜尋與核對；工具不爬取網站。",
        "- 官方一致性只計入已查證（verified）的 Microsoft Learn 證據：consistent＝只支持題庫答案；"
        "proposed_supported＝只指向其他答案（候選答案未確立，需人工檢視）；conflicting＝兩方向並存；"
        "undetermined＝僅與答案無直接關係；invalid_evidence＝含未知 supports 值；no_verified_evidence＝無已查證證據。",
        "",
        f"- 可信程度：高＝社群共識達標且已查證官方文件支持同一答案；中＝僅社群共識達標或僅已查證官方文件支持；"
        "低＝僅社群候選或來源衝突；未驗證＝暫用題庫答案。建議答案為即時推導，不會寫入題庫。",
        f"- 社群共識候選（待你檢視，不會自動設定狀態）："
        + (", ".join(f"`{k}`" for k, e in ctx.data["questions"].items() if consensus_eligible(ctx, e)) or "無"),
        "",
        "| 題目 | 題型 | 題庫答案 | 建議答案 | 可信程度 | 官方一致性 | ExamTopics | 社群支持 | 狀態 | 考試建議 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for qid, e in ctx.data["questions"].items():
        c = compute(ctx, e)
        et = e["examtopics_evidence"]
        rec = c["recommendation"]
        lock = "🔒 " if qid in ctx.locked_ids else ""
        lines.append(f"| {lock}`{qid}` | {e['type']} | `{fmt(answer_of(ctx.units[qid]))}` | `{fmt(rec['answer'])}` "
                     f"| {rec['confidence']} | {c['microsoft_consistency']} | {et['match_status']} | {vote_text(et)} "
                     f"| **{e['answer_status']}** | {c['exam_guidance']} |")
    lines.append("")
    for qid, e in ctx.data["questions"].items():
        unit = ctx.units[qid]
        q, s, et = unit.question, e.get("source") or {}, e["examtopics_evidence"]
        c = compute(ctx, e)
        lines += [
            f"## {qid}（{e['type']}）",
            "",
            f"- PDF 位置：{s.get('section', '—')} · QUESTION {s.get('number', '—')} · 第 {s.get('pages', '—')} 頁",
            f"- 題組：{e['case_title'] or '—'}",
            f"- 題幹開頭：{' '.join(q['question'].split())[:160]}…",
            f"- 建議搜尋關鍵字：`SC-200 \"{search_phrase(q['question'])}\"`",
            f"- 題庫答案：`{fmt(answer_of(unit))}`"
            + ("（⚠ 建立快照後已變更）" if c["bank_changed_since_snapshot"] else ""),
            f"- PDF 原始答案：{e['pdf_original_answer']['status']} — {e['pdf_original_answer']['text'] or e['pdf_original_answer']['reason']}",
        ]
        for n in e["prior_notes"]:
            lines.append(f"- 既有疑點備註（未查證）：{n['text']}")
        lines.append(f"- 候選答案：{fmt((e.get('proposed_answer') or {}).get('value'))}"
                     + (f"（{e['proposed_answer']['basis']}，{e['proposed_answer']['proposed_by']}，未確認）"
                        if e.get("proposed_answer") else ""))
        lines.append(f"- Microsoft 證據：{c['microsoft_consistency']}（已查證 {c['microsoft_evidence_count']['verified']}、"
                     f"未查證 {c['microsoft_evidence_count']['unverified']}）")
        for x in e["microsoft_evidence"]:
            lines.append(f"  - [{x['verification']}] {x['title']} — {x['url']}（支持：{x['supports']}）"
                         + (f"：「{x['excerpt']}」" if x.get("excerpt") else ""))
        lines.append(f"- ExamTopics：{et['match_status']}" + (f" — {et['url']}" if et["url"] else ""))
        for cand in et["candidates"]:
            lines.append(f"  - 候選（未確認）：{cand['url']}")
        if et["match_status"] == "exact":
            page = f"`{fmt(et['page_answer'])}`" if et["page_answer"] is not None else "未記錄"
            lines.append(f"  - 頁面答案：{page}；社群投票：{vote_text(et)}")
            lines += [f"  - 理由：{r}" for r in et["main_reasons"]]
            lines += [f"  - 反方：{r}" for r in et["counterarguments"]]
        if et["search_note"]:
            lines.append(f"  - 搜尋紀錄：{et['search_note']}")
        cap = et.get("community_capture")
        if cap:
            dist = et["community_vote"].get("distribution")
            dist_text = "、".join(f"{d['answer']} {d['percent']:g}%（{d['votes']} 票）" for d in dist) if dist else "頁面沒有投票分布"
            lines.append(f"  - 自動擷取：{cap['source']}（Topic {cap['examtopics_topic']} Q{cap['examtopics_question']}，"
                         f"{cap['captured_at'][:10]}，{cap['status']}）；投票分布：{dist_text}"
                         + (f"；備註：{'；'.join(cap['notes'])}" if cap["notes"] else ""))
        am = et.get("auto_match")
        if am:
            yn = {True: "是", False: "否", None: "不適用"}
            lines.append(f"  - 自動比對：{am['verdict']}（題幹相似度 {am['stem_similarity']}，選項相符 {yn[am['options_match']]}，"
                         f"順序相符 {yn[am['order_match']]}）" + ("；可由你執行 accept-match 接受" if am["verdict"] == "all_match"
                                                             and et["match_status"] == "probable" else ""))
        th, rec = c["community_threshold"], c["recommendation"]
        lines.append(f"- 社群共識門檻：{'達標' if th['met'] else '未達：' + '、'.join(th['reasons'])}")
        lines.append(f"- 建議答案：`{fmt(rec['answer'])}`（{rec['source']}；可信程度：{rec['confidence']}）")
        if rec["conflicts"]:
            lines.append(f"- 衝突：{'；'.join(rec['conflicts'])}")
        if rec["uncertainty"]:
            lines.append(f"- 不確定原因：{'；'.join(rec['uncertainty'])}")
        lines.append(f"- 下一步：{rec['next_step']}")
        for d in e.get("microsoft_candidates") or []:
            lines.append(f"  - [未查證候選] {d['title']} — {d['url']}")
            lines += [f"    - 「{x['text']}」（關鍵字：{', '.join(x['matched_terms'][:6])}）" for x in d["excerpts"]]
        lines += [f"- 狀態：**{e['answer_status']}**；考試建議：{c['exam_guidance']}"
                  + (f"；不確定性：{e['uncertainty']}" if e["uncertainty"] else ""),
                  "- [ ] 已搜尋 ExamTopics　- [ ] 已人工確認同一題／未找到　- [ ] 已收集 Microsoft 證據　- [ ] 已判定狀態",
                  ""]
    return "\n".join(lines) + "\n"


def cmd_checklist(ctx: Ctx, args) -> int:
    if not ctx.data["questions"]:
        raise SystemExit("尚無驗證項目，請先執行 init")
    made = safe_write(ctx.checklist_path, render_checklist(ctx), ctx.backup_dir)
    if made:
        print(f"已備份：{made}")
    print(f"已寫入：{ctx.checklist_path}")
    return 0


def cmd_report(ctx: Ctx, args) -> int:
    print(f"{'題目':<15}{'題型':<18}{'官方一致性':<22}{'ExamTopics':<14}{'投票比對':<20}{'狀態':<10}{'考試建議'}")
    for qid, e in ctx.data["questions"].items():
        c = compute(ctx, e)
        et = e["examtopics_evidence"]
        vote_cmp = (c["bank_vs_examtopics_vote"] if et["community_vote"].get("answer") is not None or et["match_status"] != "exact"
                    else NO_COMMUNITY)
        rec = c["recommendation"]
        print(f"{qid:<15}{e['type']:<18}{c['microsoft_consistency']:<22}{c['examtopics_match']:<14}"
              f"{vote_cmp:<20}{e['answer_status']:<10}{c['exam_guidance']}"
              f"｜建議 {fmt(rec['answer'])}（{rec['confidence']}）"
              + ("  ⚠題庫答案已變更" if c["bank_changed_since_snapshot"] else ""))
    return 0


def main() -> int:
    eq.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", type=Path, default=DEFAULT_BANK)
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="建立驗證項目（已存在者不覆寫）")
    p.add_argument("--preset", choices=sorted(PRESETS))
    p.add_argument("--ids")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("checklist", help="產生 verification_checklist.md")
    p.set_defaults(func=cmd_checklist)

    p = sub.add_parser("add-ms", help="新增 Microsoft Learn 證據")
    p.add_argument("--id", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--excerpt")
    p.add_argument("--supports", choices=["bank", "proposed", "neither"], required=True)
    p.add_argument("--verified", action="store_true", help="你已實際讀過該頁並確認原文")
    p.add_argument("--retrieved-at")
    p.add_argument("--checked-by")
    p.set_defaults(func=cmd_add_ms)

    p = sub.add_parser("examtopics", help="記錄 ExamTopics 候選網址或人工核對結果")
    p.add_argument("--id", required=True)
    p.add_argument("--candidate", action="append", help="候選網址（可重複；只會標為 probable）")
    p.add_argument("--match", choices=["exact", "probable", "not_found"])
    p.add_argument("--url")
    p.add_argument("--confirm-same-question", action="store_true", help="你已人工核對為同一題")
    p.add_argument("--topic")
    p.add_argument("--question-number")
    p.add_argument("--page-answer", help="頁面公布的答案（JSON）")
    p.add_argument("--vote-answer", help="社群最多票的答案（JSON）")
    p.add_argument("--vote-share", type=float)
    p.add_argument("--vote-count", type=int)
    p.add_argument("--vote-as-of")
    p.add_argument("--reason", action="append", help="支持方主要理由（用自己的話摘要，可重複）")
    p.add_argument("--counter", action="append", help="反方主要理由（可重複）")
    p.add_argument("--excerpt-file", help="本機摘錄文字檔路徑（人工貼上）")
    p.add_argument("--search-note")
    p.add_argument("--by")
    p.set_defaults(func=cmd_examtopics)

    p = sub.add_parser("propose", help="記錄候選答案（不修改題庫）")
    p.add_argument("--id", required=True)
    p.add_argument("--answer", required=True)
    p.add_argument("--basis", choices=["microsoft_evidence", "examtopics", "model", "human_judgement"], required=True)
    p.add_argument("--by", required=True)
    p.add_argument("--note")
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("compare", help="計算比對結果（不變更狀態）")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("set-status", help="由人設定 answer_status")
    p.add_argument("--id", required=True)
    p.add_argument("--status", choices=STATUSES, required=True)
    p.add_argument("--basis", choices=["microsoft_docs", "examtopics_consensus"], default="microsoft_docs",
                   help=f"verified 的判定依據（預設 microsoft_docs）；examtopics_consensus 需同一題、"
                        f"最高票答案與題庫一致、比例 ≥ {CONSENSUS_MIN_SHARE}%% 且總票數 ≥ {CONSENSUS_MIN_VOTES}")
    p.add_argument("--by", required=True)
    p.add_argument("--reason")
    p.add_argument("--uncertainty")
    p.set_defaults(func=cmd_set_status)

    p = sub.add_parser("report", help="在終端機列出比對摘要")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("import-examtopics", help="匯入 ExamTopics 免費公開題目列表第 1 頁的頁面答案與投票分布")
    p.add_argument("--page", type=int, default=1, help="只允許 1")
    p.add_argument("--from-file", help="改讀本機 HTML（測試或離線用）")
    p.add_argument("--refresh", action="store_true", help="與既有資料不同時，保存舊值於 capture_history 後更新")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_import_examtopics)

    p = sub.add_parser("accept-match", help="由你接受自動比對結果，將 probable 改為 exact")
    p.add_argument("--id", required=True)
    p.add_argument("--by", required=True)
    p.set_defaults(func=cmd_accept_match)

    p = sub.add_parser("consensus-candidates", help="列出社群共識候選（不變更狀態）")
    p.set_defaults(func=cmd_consensus_candidates)

    p = sub.add_parser("ms-search", help="搜尋 Microsoft Learn 並擷取候選文件段落（標記為未查證候選）")
    p.add_argument("--ids")
    p.add_argument("--limit", type=int)
    p.add_argument("--docs", type=int, default=2, help="每題擷取的文件數（預設 2）")
    p.add_argument("--delay", type=float, default=1.0, help="請求間隔秒數（預設 1）")
    p.add_argument("--refresh", action="store_true", help="已有候選者也重新搜尋")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_ms_search)

    args = ap.parse_args()
    ctx = Ctx(args.bank.resolve(), args.output_dir.resolve())
    try:
        return args.func(ctx, args)
    except (OSError, FileExistsError) as err:
        print(f"備份或寫入失敗，未變更任何檔案：{err}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
