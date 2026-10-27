#!/usr/bin/env python3
"""SC-200 題庫解析工具：本機結構診斷、dry-run 預覽，以及 Gemini 批次解析。

用法
----
本機（不連線、不呼叫任何 API）：
    python scripts/enrich_questions.py                       # 統計 + 結構驗證
    python scripts/enrich_questions.py --dry-run --limit 5   # 預覽前 5 道子題將送出的欄位

Gemini（需 .env 的 GEMINI_API_KEY 與 scripts/requirements-gemini.txt）：
    python scripts/enrich_questions.py --estimate --limit 5  # 只用 count_tokens 估算，不生成
    python scripts/enrich_questions.py --run --tier free --ids a,b,c   # 估算 → 確認 → 逐題生成
    python scripts/enrich_questions.py --run --tier paid --resume     # 續跑：跳過已有有效解析的子題

output/explanations.json 中 manual_revision.locked 為 true 的人工修訂題目一律跳過（不論模型、提示詞或題目是否改變）；
只有加上 --overwrite-manual 並在 --ids 中逐一列出子題 ID、且完成確認後才會覆寫。每次生成寫入前都會先把
explanations.json 與 progress.json 備份到 output/backups/（檔名含時間戳記）；備份失敗即中止。

--tier 為必填，代表你確認了帳戶的計費層級：free 不計費但內容可能被用於改善 Google 產品，
且有依專案而定的速率限制；paid 依用量計費、內容不用於改善產品。不使用 Google Search grounding。

「題」一律以子題計：獨立題目 1 題、情境題組依子題數計；--limit 亦以子題計。
解析與進度分別寫入 output/explanations.json、output/progress.json，絕不修改題庫。
注意：--estimate 與 --run 會把題目內容送到 Google；免費層的內容可能被用於改善 Google 產品。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_BANK = BASE_DIR / "public" / "data" / "sc200.json"
OUTPUT_DIR = BASE_DIR / "output"
PRICING_FILE = Path(__file__).resolve().parent / "gemini_pricing.json"
DEFAULT_MODEL = "gemini-3.1-flash-lite"

QUESTION_TYPES = {"single_choice", "multiple_choice", "dropdown_cloze", "drag_drop_order", "statement_matrix"}
ROOT_TYPES = QUESTION_TYPES | {"case_study"}
BLANK_PATTERN = re.compile(r"\{\{(\w+)\}\}")  # 與前端 src/lib/grading.js 相同
LETTER = re.compile(r"^[A-Z]$")
MAX_LISTED = 20  # 每類異常最多列出的筆數，避免輸出大量內容


def setup_console() -> None:
    """Windows 主控台預設可能是 cp950，統一改為 UTF-8 以免中文 / 符號輸出失敗。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# 子題（解析的最小單位）
# ---------------------------------------------------------------------------


@dataclass
class Unit:
    """一道需要產生解析的子題。question_id 即題庫中該題（或題組子題）的 id。"""

    question_id: str
    root_id: str
    type: str
    question: dict
    case: dict | None = None  # 所屬情境題組（僅子題）


def iter_units(bank: dict):
    for q in bank["questions"]:
        if q.get("type") == "case_study":
            for sub in q.get("questions") or []:
                yield Unit(sub.get("id"), q.get("id"), sub.get("type"), sub, q)
        else:
            yield Unit(q.get("id"), q.get("id"), q.get("type"), q)


def build_payload(unit: Unit) -> dict:
    """依題型組出送給模型的題目內容（第二階段沿用）。只取實際存在的欄位，不含 reviewNote。"""
    q = unit.question
    payload: dict = {"question_id": unit.question_id, "type": unit.type, "question": q.get("question", "")}
    if q.get("code"):
        payload["code"] = q["code"]
    t = unit.type
    if t in ("single_choice", "multiple_choice"):
        payload["options"] = q.get("options", [])
        payload["answer"] = q.get("answer")
    elif t == "dropdown_cloze":
        payload["templateType"] = q.get("templateType", "code")
        payload["template"] = q.get("template", "")
        payload["blanks"] = q.get("blanks", [])
    elif t == "drag_drop_order":
        payload["items"] = q.get("items", [])
        payload["answer"] = q.get("answer")
    elif t == "statement_matrix":
        payload["columns"] = q.get("columns", ["Yes", "No"])
        payload["statements"] = q.get("statements", [])
    if unit.case is not None:
        info = unit.case.get("caseInfo") or {}
        payload["case_study"] = {
            "case_id": unit.root_id,
            "title": info.get("title", ""),
            "sections": info.get("sections", []),
        }
    return payload


def answerable_targets(unit: Unit) -> list[str]:
    """解析需逐一說明的對象：選項 / 排序項目 / 挖空 / 陳述句的 id（第二階段用於核對 option_explanations）。"""
    q = unit.question
    key = {"single_choice": "options", "multiple_choice": "options", "drag_drop_order": "items",
           "dropdown_cloze": "blanks", "statement_matrix": "statements"}.get(unit.type)
    return [x.get("id") for x in q.get(key) or [] if isinstance(x, dict)]


# ---------------------------------------------------------------------------
# 驗證
# ---------------------------------------------------------------------------


@dataclass
class Report:
    errors: dict[str, list[str]] = field(default_factory=dict)
    warnings: dict[str, list[str]] = field(default_factory=dict)

    def error(self, kind: str, msg: str) -> None:
        self.errors.setdefault(kind, []).append(msg)

    def warn(self, kind: str, msg: str) -> None:
        self.warnings.setdefault(kind, []).append(msg)


def unique_ids(items, label: str, where: str, rep: Report, pattern: re.Pattern | None = None) -> list[str]:
    ids = [x.get("id") if isinstance(x, dict) else None for x in items]
    if any(not isinstance(i, str) or not i for i in ids):
        rep.error("欄位缺漏", f"{where}：{label} 有項目缺少 id")
    elif len(set(ids)) != len(ids):
        rep.error("ID 重複", f"{where}：{label} 的 id 重複")
    elif pattern and not all(pattern.match(i) for i in ids):
        rep.error("欄位格式", f"{where}：{label} 的 id 應為單一大寫字母")
    return ids


def check_question(q: dict, where: str, rep: Report) -> None:
    t = q.get("type")
    if not isinstance(q.get("question"), str) or not q["question"].strip():
        rep.error("欄位缺漏", f"{where}：question 為空")
    if t in ("single_choice", "multiple_choice"):
        opts = q.get("options")
        if not isinstance(opts, list) or len(opts) < 2:
            rep.error("欄位缺漏", f"{where}：options 少於 2 個")
            return
        ids = unique_ids(opts, "options", where, rep, LETTER)
        if any(not (o.get("text") or "").strip() for o in opts):
            rep.error("欄位缺漏", f"{where}：有選項文字為空")
        ans = q.get("answer")
        if t == "single_choice":
            if not isinstance(ans, str):
                rep.error("答案異常", f"{where}：single_choice 的 answer 應為字串")
            elif ans not in ids:
                rep.error("答案異常", f"{where}：answer {ans} 不在選項中")
        else:
            if not isinstance(ans, list) or not ans:
                rep.error("答案異常", f"{where}：multiple_choice 的 answer 應為非空陣列")
            elif len(set(ans)) != len(ans) or not set(ans) <= set(ids):
                rep.error("答案異常", f"{where}：answer {ans} 重複或不在選項中")
            elif len(ans) == 1:
                rep.warn("答案提醒", f"{where}：multiple_choice 只有 1 個答案")
    elif t == "dropdown_cloze":
        blanks = q.get("blanks")
        template = q.get("template")
        if not isinstance(template, str) or not isinstance(blanks, list) or not blanks:
            rep.error("欄位缺漏", f"{where}：缺少 template 或 blanks")
            return
        if q.get("templateType", "code") not in ("code", "text"):
            rep.error("欄位格式", f"{where}：templateType 應為 code / text")
        ids = unique_ids(blanks, "blanks", where, rep)
        holes = BLANK_PATTERN.findall(template)
        if sorted(holes) != sorted(ids):
            rep.error("答案異常", f"{where}：template 挖空 {holes} 與 blanks {ids} 不一致")
        for b in blanks:
            opts = b.get("options")
            if not isinstance(opts, list) or len(opts) < 2:
                rep.error("欄位缺漏", f"{where}：blank {b.get('id')} 的 options 少於 2 個")
            elif b.get("answer") not in opts:
                rep.error("答案異常", f"{where}：blank {b.get('id')} 的 answer 不在 options 中")
            elif len(set(opts)) != len(opts):
                rep.warn("選項重複", f"{where}：blank {b.get('id')} 的 options 有重複值")
    elif t == "drag_drop_order":
        items = q.get("items")
        if not isinstance(items, list) or len(items) < 2:
            rep.error("欄位缺漏", f"{where}：items 少於 2 個")
            return
        ids = unique_ids(items, "items", where, rep)
        ans = q.get("answer")
        if not isinstance(ans, list) or not ans:
            rep.error("答案異常", f"{where}：answer 應為非空陣列")
        elif len(set(ans)) != len(ans) or not set(ans) <= set(ids):
            rep.error("答案異常", f"{where}：answer 有重複或不存在的 items id")
    elif t == "statement_matrix":
        sts = q.get("statements")
        if not isinstance(sts, list) or not sts:
            rep.error("欄位缺漏", f"{where}：缺少 statements")
            return
        unique_ids(sts, "statements", where, rep)
        if any(not isinstance(s.get("answer"), bool) for s in sts):
            rep.error("答案異常", f"{where}：statements 的 answer 應為 true / false")
        cols = q.get("columns")
        if cols is not None and (not isinstance(cols, list) or len(cols) != 2):
            rep.error("欄位格式", f"{where}：columns 應為兩個字串")


def validate(bank: dict, rep: Report) -> None:
    if not isinstance(bank, dict) or not isinstance(bank.get("questions"), list):
        rep.error("結構", "題庫根層級應為物件且包含 questions 陣列")
        return
    chapter_ids = {c.get("id") for c in bank.get("chapters") or [] if isinstance(c, dict)}
    seen: Counter = Counter()
    last_index = 0
    for i, q in enumerate(bank["questions"], start=1):
        qid = q.get("id") if isinstance(q, dict) else None
        where = f"#{i} {qid or '(無 id)'}"
        if not isinstance(q, dict):
            rep.error("結構", f"#{i}：不是物件")
            continue
        if not isinstance(qid, str) or not qid:
            rep.error("ID 缺失", f"#{i}：缺少 id")
        seen[qid] += 1
        t = q.get("type")
        if t not in ROOT_TYPES:
            rep.error("題型", f"{where}：不支援的題型 {t}")
            continue
        if q.get("chapter") not in chapter_ids:
            rep.error("欄位缺漏", f"{where}：chapter {q.get('chapter')} 不在 chapters 中")
        idx = (q.get("source") or {}).get("index")
        if isinstance(idx, int):
            if idx <= last_index:
                rep.warn("排序", f"{where}：source.index 未遞增（與 PDF 順序不一致）")
            last_index = idx

        if t == "case_study":
            info = q.get("caseInfo") or {}
            sections = info.get("sections")
            if not isinstance(sections, list) or not sections:
                rep.error("案例結構", f"{where}：caseInfo.sections 為空")
            elif any(not (s.get("title") or "").strip() or not (s.get("content") or "").strip() for s in sections):
                rep.error("案例結構", f"{where}：caseInfo 有分頁標題或內容為空")
            subs = q.get("questions")
            if not isinstance(subs, list) or not subs:
                rep.error("案例結構", f"{where}：沒有子題")
                continue
            for j, s in enumerate(subs, start=1):
                sid = s.get("id")
                swhere = f"{where} 子題{j} {sid or '(無 id)'}"
                if not isinstance(sid, str) or not sid:
                    rep.error("ID 缺失", f"{swhere}：子題缺少 id")
                seen[sid] += 1
                if s.get("type") not in QUESTION_TYPES:
                    rep.error("案例結構", f"{swhere}：子題題型 {s.get('type')} 不支援")
                    continue
                if isinstance(sid, str) and isinstance(qid, str) and not sid.startswith(qid):
                    rep.warn("ID 提醒", f"{swhere}：子題 id 未以題組 id 為前綴")
                check_question(s, swhere, rep)
        else:
            check_question(q, where, rep)

        if "TODO" in json.dumps(q, ensure_ascii=False):
            rep.error("未完成內容", f"{where}：仍含 TODO 佔位文字")

    for qid, n in seen.items():
        if qid and n > 1:
            rep.error("ID 重複", f"id {qid} 出現 {n} 次")


# ---------------------------------------------------------------------------
# 報告
# ---------------------------------------------------------------------------


def has_text(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def print_stats(bank: dict, units: list[Unit], path: Path) -> None:
    roots = bank["questions"]
    root_types = Counter(q.get("type", "unknown") for q in roots)
    unit_types = Counter(u.type for u in units)
    cases = [q for q in roots if q.get("type") == "case_study"]

    print("=" * 56)
    print("SC-200 本機題庫結構診斷報告")
    print("=" * 56)
    print(f"題庫檔案                 : {path}")
    print(f"根層級物件               : {len(roots)} 筆")
    print(f"總子題數（解析單位）     : {len(units)} 題")
    print(f"情境題組                 : {len(cases)} 組，共 {sum(len(q.get('questions') or []) for q in cases)} 道子題")
    print(f"已有解析 / 缺少解析      : {sum(has_text(u.question.get('explanation')) for u in units)} / "
          f"{sum(not has_text(u.question.get('explanation')) for u in units)} 題")
    print(f"含 code（圖表/查詢轉文字）: {sum(bool(u.question.get('code')) for u in units)} 題")
    print(f"含 reviewNote（待確認備註）: {sum(has_text(u.question.get('reviewNote')) for u in units)} 題")
    print("-" * 56)
    print(f"{'題型':<20}{'根層級':>8}{'子題數':>8}")
    for t in sorted(ROOT_TYPES, key=lambda x: (-unit_types[x], x)):
        if root_types[t] or unit_types[t]:
            print(f"{t:<20}{root_types[t]:>8}{unit_types[t]:>8}")
    print(f"{'合計':<20}{len(roots):>8}{len(units):>8}")


def print_report(rep: Report) -> None:
    print("-" * 56)
    for title, groups in (("錯誤", rep.errors), ("提醒", rep.warnings)):
        total = sum(map(len, groups.values()))
        print(f"{title}：{total} 筆" + ("" if total else "（無）"))
        for kind, msgs in sorted(groups.items()):
            print(f"  [{kind}] {len(msgs)} 筆")
            for m in msgs[:MAX_LISTED]:
                print(f"    - {m}")
            if len(msgs) > MAX_LISTED:
                print(f"    … 其餘 {len(msgs) - MAX_LISTED} 筆省略")


def print_dry_run(units: list[Unit], limit: int | None) -> None:
    chosen = units[:limit] if limit else units
    sizes = [len(json.dumps(build_payload(u), ensure_ascii=False)) for u in units]
    print("-" * 56)
    print(f"DRY RUN：不呼叫任何 API。預覽 {len(chosen)} / {len(units)} 道子題將送出的欄位")
    for u in chosen:
        p = build_payload(u)
        fields = ", ".join(k for k in p if k not in ("question_id", "type"))
        targets = answerable_targets(u)
        extra = f"｜題組 {u.root_id}" if u.case else ""
        print(f"  {u.question_id:<16} {u.type:<17} {len(json.dumps(p, ensure_ascii=False)):>6} 字元"
              f"｜欄位：{fields}｜需解析對象 {len(targets)} 個{extra}")
    # 只提供題目內容的字元量；token 數與費用需在第二階段以官方 count_tokens 及當時價目表計算
    sel = sizes[: len(chosen)]
    print(f"  題目內容字元數：本次 {sum(sel):,}；全部 {sum(sizes):,}（平均 {sum(sizes) // max(len(sizes), 1):,}，"
          f"最大 {max(sizes, default=0):,}）")
    print("  ※ 尚未計入系統提示與輸出；token 與費用請用 --estimate（count_tokens，會把題目送至 Google）")


# ---------------------------------------------------------------------------
# Gemini：提示詞、輸出格式與結果驗證
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTION = """你是 Microsoft SC-200（Security Operations Analyst）認證的資深講師，負責為練習題撰寫解析。

規則：
1. 一律使用繁體中文撰寫；Microsoft 資安產品名稱、功能名稱、技術術語、KQL / PowerShell 指令與資料表名稱保留英文原文。
2. 題目附帶的「目前正式答案」是解析的依據：說明為何正式答案正確，以及其他選項 / 項目為何不適合。絕對不可更改或改寫正式答案。
3. 若你認為正式答案可能有誤，仍以正式答案撰寫解析，並在 answer_concern 設 has_concern=true，具體說明疑點與你認為較可能的答案；沒有疑慮時 has_concern=false、detail=null。
4. 題目資訊不足、需要截圖才能判斷、或你無法確定時，在 uncertainty 中明確說明；不可自行補造題目沒有提供的事實。
5. option_explanations 必須剛好涵蓋題目列出的每一個 target_id，各一筆，official_answer 照抄提供的值，不可增減。
6. key_points 列出 2～5 項與此題相關、考試常考的重點。
7. references 只列你有把握確實存在的 Microsoft Learn 官方文件標題；不確定網址時 url 填 null，絕不可編造網址或文件內容。這些參考資料之後會由人工核對。
8. 若題目含情境題組背景（case_study），解析必須依據該背景中的需求與環境。
9. 解析精簡、具體，避免空泛敘述。"""

TYPE_GUIDES = {
    "single_choice": "單選題：說明正確選項為何符合題目需求，再逐一說明其他選項不適合的原因。",
    "multiple_choice": "複選題：每個正確選項都要說明其作用；錯誤選項說明為何不符。",
    "dropdown_cloze": "下拉填空題：template 中的 {{id}} 為挖空處，blanks 為各挖空的選項與正式答案。逐一說明每個挖空選擇該答案的理由，並簡述其他選項為何不適合。",
    "drag_drop_order": "排序題：answer 為正確步驟順序（items 的 id）。說明每個步驟在流程中的位置與理由；未使用的項目說明為何不屬於此流程。",
    "statement_matrix": "Yes / No 判斷題：逐一說明每個陳述句為何是 Yes 或 No。",
}


def answer_targets(unit: Unit) -> list[tuple[str, str]]:
    """(target_id, official_answer)：模型的 option_explanations 必須剛好對應這些對象。"""
    q, t = unit.question, unit.type
    if t in ("single_choice", "multiple_choice"):
        ans = q["answer"] if isinstance(q["answer"], list) else [q["answer"]]
        return [(o["id"], "correct" if o["id"] in ans else "incorrect") for o in q["options"]]
    if t == "dropdown_cloze":
        return [(b["id"], b["answer"]) for b in q["blanks"]]
    if t == "drag_drop_order":
        pos = {iid: k for k, iid in enumerate(q["answer"], start=1)}
        return [(i["id"], f"step {pos[i['id']]}" if i["id"] in pos else "not used") for i in q["items"]]
    if t == "statement_matrix":
        yes, no = q.get("columns", ["Yes", "No"])
        return [(s["id"], yes if s["answer"] else no) for s in q["statements"]]
    return []


def build_prompt(unit: Unit) -> str:
    payload = build_payload(unit)
    targets = "\n".join(f"- target_id={tid}　official_answer={ans}" for tid, ans in answer_targets(unit))
    return (
        f"請為以下 SC-200 題目撰寫解析。question_id 必須填「{unit.question_id}」。\n"
        f"{TYPE_GUIDES[unit.type]}\n\n"
        f"題目（JSON）：\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"option_explanations 必須剛好包含以下對象（各一筆，official_answer 照抄）：\n{targets}\n"
    )


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "question_id": {"type": "string"},
        "explanation": {"type": "string", "description": "說明正式答案為何正確的整體解析"},
        "option_explanations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "official_answer": {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": ["target_id", "official_answer", "explanation"],
            },
        },
        "key_points": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 6},
        "references": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": ["string", "null"]},
                    "relevance": {"type": "string"},
                },
                "required": ["title", "url", "relevance"],
            },
        },
        "answer_concern": {
            "type": "object",
            "properties": {"has_concern": {"type": "boolean"}, "detail": {"type": ["string", "null"]}},
            "required": ["has_concern", "detail"],
        },
        "uncertainty": {"type": ["string", "null"], "description": "資訊不足或無法確定之處；沒有則為 null"},
    },
    "required": ["question_id", "explanation", "option_explanations", "key_points", "references",
                 "answer_concern", "uncertainty"],
}


def check_result(unit: Unit, r) -> list[str]:
    """除了 JSON Schema 外的額外驗證：題目 ID、對象對應、必要欄位與完整性。"""
    if not isinstance(r, dict):
        return ["輸出不是 JSON 物件"]
    errs = []
    if r.get("question_id") != unit.question_id:
        errs.append(f"question_id 不符（{r.get('question_id')!r}）")
    if not has_text(r.get("explanation")):
        errs.append("explanation 為空")
    expected = dict(answer_targets(unit))
    got = r.get("option_explanations")
    if not isinstance(got, list):
        errs.append("option_explanations 不是陣列")
    else:
        ids = [x.get("target_id") if isinstance(x, dict) else None for x in got]
        if sorted(map(str, ids)) != sorted(expected):
            missing = sorted(set(expected) - set(ids))
            extra = sorted({str(i) for i in ids} - set(expected))
            dup = sorted({str(i) for i in ids if ids.count(i) > 1})
            errs.append(f"option_explanations 對象不符（缺 {missing}、多 {extra}、重複 {dup}）")
        for x in got:
            if isinstance(x, dict) and x.get("target_id") in expected:
                if x.get("official_answer") != expected[x["target_id"]]:
                    errs.append(f"{x['target_id']} 的 official_answer 與正式答案不符")
                if not has_text(x.get("explanation")):
                    errs.append(f"{x['target_id']} 的解析為空")
    kp = r.get("key_points")
    if not isinstance(kp, list) or not any(has_text(k) for k in kp):
        errs.append("key_points 為空")
    refs = r.get("references")
    if not isinstance(refs, list):
        errs.append("references 不是陣列")
    else:
        for ref in refs:
            url = ref.get("url") if isinstance(ref, dict) else None
            if not isinstance(ref, dict) or not has_text(ref.get("title")):
                errs.append("references 有項目缺少 title")
            elif url is not None and not re.match(r"^https://\S+$", str(url)):
                errs.append(f"references 的 url 格式不正確：{str(url)[:80]}")
    ac = r.get("answer_concern")
    if not isinstance(ac, dict) or not isinstance(ac.get("has_concern"), bool):
        errs.append("answer_concern 格式不正確")
    elif ac["has_concern"] and not has_text(ac.get("detail")):
        errs.append("answer_concern 標示有疑慮但未說明")
    return errs


def to_record(unit: Unit, r: dict, model: str, input_hash: str) -> dict:
    """保存用格式：review_status 與參考資料核對狀態一律由程式設定，不由模型決定。"""
    return {
        "question_id": unit.question_id,
        "root_id": unit.root_id,
        "case_id": unit.root_id if unit.case else None,
        "type": unit.type,
        "explanation": r["explanation"].strip(),
        "option_explanations": [
            {"target_id": x["target_id"], "official_answer": x["official_answer"], "explanation": x["explanation"].strip()}
            for x in r["option_explanations"]
        ],
        "key_points": [k.strip() for k in r["key_points"] if has_text(k)],
        "references": [
            {"title": x["title"].strip(), "url": x.get("url"), "relevance": x.get("relevance", ""), "verification": "待核對"}
            for x in r["references"]
        ],
        "references_status": "待核對：由模型產生，尚未經人工確認",
        "answer_concern": {"has_concern": r["answer_concern"]["has_concern"], "detail": r["answer_concern"].get("detail")},
        "uncertainty": r.get("uncertainty"),
        "review_status": "pending",
        "model": model,
        "input_hash": input_hash,
        "generated_at": now_iso(),
    }


# ---------------------------------------------------------------------------
# 輸出檔（每題寫入一次，以暫存檔 + 取代避免中斷時毀損）
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def input_hash(unit: Unit, model: str) -> str:
    """題目內容或模型改變時，舊解析即視為過期。"""
    blob = json.dumps({"model": model, "system": SYSTEM_INSTRUCTION, "prompt": build_prompt(unit)}, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class Store:
    """output/ 的讀寫。所有寫入都經過 save()：必須先完成備份，且不可覆寫未授權的人工修訂鎖定題目。"""

    def __init__(self, out_dir: Path, bank_path: Path):
        self.exp_path = out_dir / "explanations.json"
        self.prog_path = out_dir / "progress.json"
        self.backup_dir = out_dir / "backups"
        self.exp = load_json(self.exp_path, {"explanations": {}})
        self.prog = load_json(self.prog_path, {"items": {}})
        self.exp.setdefault("explanations", {})
        self.prog.setdefault("items", {})
        self.bank_path = bank_path
        self.backed_up = False
        self.overwritable: set[str] = set()  # 本次經 --overwrite-manual 明確授權覆寫的鎖定題目

    def valid(self, unit: Unit, model: str) -> bool:
        rec = self.exp["explanations"].get(unit.question_id)
        return bool(rec) and rec.get("input_hash") == input_hash(unit, model)

    def locked(self, unit: Unit) -> bool:
        """人工修訂的題目（manual_revision.locked == true）；舊紀錄沒有此欄位即視為未鎖定。"""
        mr = (self.exp["explanations"].get(unit.question_id) or {}).get("manual_revision")
        return isinstance(mr, dict) and mr.get("locked") is True

    def backup(self) -> list[Path]:
        """寫入前把既有輸出檔複製到 output/backups/（檔名含時間戳記，不覆蓋舊備份）；失敗時拋出例外。"""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        made = []
        for src in (self.exp_path, self.prog_path):
            if not src.exists():
                continue
            dst = self.backup_dir / f"{src.stem}.{stamp}{src.suffix}"
            if dst.exists():
                raise FileExistsError(f"備份檔已存在：{dst}")
            shutil.copy2(src, dst)
            if sha256_file(dst) != sha256_file(src):
                raise OSError(f"備份內容與原檔不一致：{dst}")
            made.append(dst)
        self.backed_up = True
        return made

    def measured_output_tokens(self) -> int | None:
        outs = [i["usage"]["output_tokens"] + i["usage"]["thoughts_tokens"]
                for i in self.prog["items"].values() if i.get("status") == "done" and i.get("usage")]
        return round(sum(outs) / len(outs)) if outs else None

    def save(self, unit: Unit, item: dict, record: dict | None) -> None:
        if not self.backed_up:
            raise RuntimeError("尚未完成備份，拒絕寫入 output/")
        qid = unit.question_id
        if record is not None and self.locked(unit) and qid not in self.overwritable:
            raise RuntimeError(f"{qid} 為人工修訂鎖定題目，未經 --overwrite-manual 明確授權，拒絕覆寫")
        if record is None and qid in self.exp["explanations"]:
            # 本次生成失敗：保留既有成功解析與其進度狀態，只附註這次的失敗紀錄
            prev = dict(self.prog["items"].get(qid) or {"status": "done"})
            prev["last_failed_attempt"] = item
            item = prev
        meta = {"bank": str(self.bank_path), "bank_sha256": sha256_file(self.bank_path), "updated_at": now_iso()}
        if record is not None:
            self.exp["explanations"][unit.question_id] = record
            self.exp["meta"] = meta
            write_json_atomic(self.exp_path, self.exp)
        self.prog["items"][unit.question_id] = item
        self.prog["meta"] = meta
        write_json_atomic(self.prog_path, self.prog)


# ---------------------------------------------------------------------------
# Gemini 呼叫
# ---------------------------------------------------------------------------


def load_api_key() -> str:
    """只從 .env / 環境變數讀取 GEMINI_API_KEY；金鑰不會被列印或寫入任何檔案。"""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    env_file = BASE_DIR / ".env"
    if not key and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*GEMINI_API_KEY\s*=\s*(.*?)\s*$", line)
            if m:
                key = m.group(1).strip().strip('"').strip("'")
    return key


def make_client(api_key: str, timeout_s: int):
    from google import genai
    from google.genai import types

    # 重試由本程式控制（有上限、會記錄），關閉 SDK 內建重試以免重複計費
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(
        timeout=timeout_s * 1000, retry_options=types.HttpRetryOptions(attempts=1)))


def gen_config(args):
    from google.genai import types

    cfg = {
        "system_instruction": SYSTEM_INSTRUCTION,
        "response_mime_type": "application/json",
        "response_json_schema": RESPONSE_SCHEMA,
        "max_output_tokens": args.max_output_tokens,
    }
    if args.thinking_level and args.thinking_level.lower() != "none":
        cfg["thinking_config"] = types.ThinkingConfig(thinking_level=args.thinking_level)
    return types.GenerateContentConfig(**cfg)


def count_input_tokens(client, model: str, unit: Unit) -> int:
    from google.genai import types

    res = client.models.count_tokens(
        model=model, contents=build_prompt(unit),
        config=types.CountTokensConfig(system_instruction=SYSTEM_INSTRUCTION))
    return res.total_tokens


def redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def error_status(e: Exception) -> int | None:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    return code if isinstance(code, int) else None


def usage_of(resp) -> dict:
    u = getattr(resp, "usage_metadata", None)
    get = lambda k: (getattr(u, k, None) or 0) if u else 0  # noqa: E731
    return {"input_tokens": get("prompt_token_count"), "output_tokens": get("candidates_token_count"),
            "thoughts_tokens": get("thoughts_token_count"), "total_tokens": get("total_token_count")}


class FatalApiError(Exception):
    """設定或權限錯誤（400/401/403/404），或重試後仍遇到速率 / 配額限制（429）：立即停止整批。"""


def generate_one(client, args, unit: Unit, api_key: str) -> tuple[dict | None, dict]:
    """回傳 (通過驗證的模型輸出或 None, 進度紀錄)。每次嘗試的 token 用量都會累計。"""
    usage = {"input_tokens": 0, "output_tokens": 0, "thoughts_tokens": 0, "total_tokens": 0}
    attempts, last_error = 0, None
    config = gen_config(args)
    while attempts <= args.retries:
        attempts += 1
        try:
            resp = client.models.generate_content(model=args.model, contents=build_prompt(unit), config=config)
        except Exception as e:  # noqa: BLE001 — SDK 例外型別依版本而異
            status = error_status(e)
            last_error = redact(f"{type(e).__name__}({status}): {e}", api_key)[:500]
            if status in (400, 401, 403, 404):
                raise FatalApiError(last_error) from e
            if attempts <= args.retries:
                time.sleep(min(5 * 3 ** (attempts - 1), 45))  # 429 / 5xx / 逾時：有限次退避
                continue
            if status == 429:  # 重試後仍超出速率或每日配額：後面的題目也會失敗，停止整批
                raise FatalApiError(f"{last_error}（速率或配額限制；免費層 RPD 於太平洋時間午夜重置）") from e
            continue
        for k, v in usage_of(resp).items():
            usage[k] += v
        finish = str(getattr((resp.candidates or [None])[0], "finish_reason", "") or "")
        try:
            result = json.loads(resp.text or "")
        except (json.JSONDecodeError, ValueError):
            last_error = f"輸出不是有效 JSON（finish_reason={finish}）"
            continue
        errs = check_result(unit, result)
        if not errs:
            return result, {"status": "done", "attempts": attempts, "last_error": None, "usage": usage}
        last_error = "；".join(errs)[:500]
    return None, {"status": "failed", "attempts": attempts, "last_error": last_error, "usage": usage}


def load_price(model: str) -> tuple[dict | None, str]:
    """回傳該模型的價格設定（含 paid / free_tier）與來源說明。"""
    try:
        cfg = json.loads(PRICING_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "找不到價格設定檔"
    p = cfg.get("models", {}).get(model)
    if not p:
        return None, f"{PRICING_FILE.name} 沒有 {model} 的價格，只顯示 token 數"
    note = f"價格來源 {cfg.get('source')}（{cfg.get('checked_on')} 查核）"
    if p.get("valid_until") and date.today().isoformat() > p["valid_until"]:
        note += f"；⚠ 已超過 {p['valid_until']}，價格可能已變動：{p.get('note', '')}"
    return p, note


def cost_usd(price: dict | None, in_tok: int, out_tok: int) -> float | None:
    """以付費層（Standard）單價換算；免費層不計費，但仍回傳付費層換算值供參考。"""
    paid = (price or {}).get("paid")
    if not paid:
        return None
    return in_tok / 1e6 * paid["input"] + out_tok / 1e6 * paid["output_including_thinking"]


def rough_input_tokens(unit: Unit) -> int:
    """count_tokens 不可用時的粗估：英文約 4 字元 / token、中文約 1 字元 / token。"""
    text = SYSTEM_INSTRUCTION + build_prompt(unit)
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + (len(text) - cjk) // 4


def gemini_main(args, bank_path: Path, units: list[Unit]) -> int:
    store = Store(args.output_dir, bank_path)
    # 先處理人工修訂鎖定：一律跳過，與 input_hash、模型或提示詞是否改變無關；
    # 只有 --overwrite-manual 且在 --ids 中逐一列出的子題 ID 才可覆寫（題組 ID 展開的不算）
    explicit = {x.strip() for x in (args.ids or "").split(",") if x.strip()}
    locked_ids = [u.question_id for u in units if store.locked(u)]
    overwrite_ids = [q for q in locked_ids if args.overwrite_manual and q in explicit]
    protected_ids = [q for q in locked_ids if q not in overwrite_ids]
    store.overwritable = set(overwrite_ids)
    free_units = [u for u in units if u.question_id not in locked_ids]
    done = [u for u in free_units if store.valid(u, args.model)]
    if done and not args.resume:
        print(f"有 {len(done)} 道選定子題已有有效解析。為避免重複付費，請加上 --resume 跳過它們。")
        return 2
    pending_ids = set(overwrite_ids) | {u.question_id for u in free_units if not store.valid(u, args.model)}
    todo = [u for u in units if u.question_id in pending_ids]
    if args.limit:
        todo = todo[: args.limit]
    print("=" * 56)
    print(f"Gemini {'生成' if args.run else '估算'}計畫")
    print(f"  模型          : {args.model}（thinking_level={args.thinking_level}，max_output_tokens={args.max_output_tokens}）")
    print(f"  待處理子題    : {len(todo)} 題（已有有效解析 {len(done)} 題{'，已跳過' if done else ''}）")
    if protected_ids:
        print(f"  人工修訂鎖定  : {len(protected_ids)} 題，已跳過：{', '.join(protected_ids)}")
        if args.overwrite_manual:
            print("                  （--overwrite-manual 只適用於在 --ids 中逐一列出的子題 ID）")
    if overwrite_ids:
        print(f"  ⚠ 將覆寫人工修訂：{', '.join(q for q in overwrite_ids if q in {u.question_id for u in todo})}"
              "（原內容會先備份到 output/backups/）")
    for u in todo[:MAX_LISTED]:
        print(f"    - {u.question_id:<16} {u.type}{'（題組 ' + u.root_id + '）' if u.case else ''}")
    if len(todo) > MAX_LISTED:
        print(f"    … 其餘 {len(todo) - MAX_LISTED} 題省略")
    print(f"  輸出          : {store.exp_path}")
    print(f"                  {store.prog_path}")
    if not todo:
        print("沒有需要處理的子題。")
        return 0

    api_key = load_api_key()
    if not api_key:
        print("找不到 GEMINI_API_KEY。請在專案根目錄的 .env 設定（可參考 .env.example）。")
        return 2
    try:
        client = make_client(api_key, args.timeout)
    except ImportError:
        print("缺少 google-genai，請執行：pip install -r scripts/requirements-gemini.txt")
        return 2
    print("  正在以 count_tokens 計算輸入 token（會將題目內容送至 Google，但不生成）…")
    try:
        in_tok = sum(count_input_tokens(client, args.model, u) for u in todo)
        in_label = "count_tokens 實算"
    except Exception as e:  # noqa: BLE001 — 官方模型頁未列出此模型支援 count_tokens，失敗時改用粗估
        print(f"  count_tokens 無法使用（{redact(str(e), api_key)[:200]}），改用字元數粗估。")
        in_tok = sum(rough_input_tokens(u) for u in todo)
        in_label = "字元數粗估，誤差可能很大"

    measured = store.measured_output_tokens()
    per_out = measured or args.assume_output_tokens
    out_tok = per_out * len(todo)
    price, price_note = load_price(args.model)
    cost = cost_usd(price, in_tok, out_tok)
    free = (price or {}).get("free_tier") or {}
    print("-" * 56)
    print(f"  輸入 token（{in_label}）: {in_tok:,}")
    print(f"  輸出 token（估計，含 thinking）: {out_tok:,}（每題 {per_out:,}，"
          f"{'依已完成題目的實測平均' if measured else '假設值，可用 --assume-output-tokens 調整'}）")
    print(f"  付費層（Standard）估計費用    : {'—' if cost is None else f'約 US${cost:.4f}'}；{price_note}")
    print(f"  免費層                        : {free.get('price', '請查官方定價頁')}；"
          f"{free.get('rate_limits', '速率限制請於 AI Studio 查看')}")
    print("  ※ 以上為估算，非實際帳單；重試會產生額外費用（付費層），實際以 Google 帳單為準。")
    print("  ※ 資料使用：免費層送出的內容可能被用於改善 Google 產品；付費層則不會。")
    print("  ※ 不使用 Google Search grounding；references 一律標示待核對。")
    if not args.run:
        return 0
    tier_text = ("免費層：不計費，但題目內容可能被 Google 用於改善產品，並受專案速率限制" if args.tier == "free"
                 else f"付費層：依用量計費（估計約 US${cost:.4f}）" if cost is not None else "付費層：依用量計費")
    print(f"  你指定的帳戶層級：{tier_text}")

    if not args.yes:
        if not sys.stdin.isatty():
            print("非互動模式：未加 --yes，不開始生成。")
            return 2
        try:
            answer = input("確認以上計畫並開始生成？輸入 yes 繼續：")
        except EOFError:  # Windows 的 NUL 也會被視為 tty；讀不到輸入一律視為取消
            answer = ""
        if answer.strip().lower() != "yes":
            print("\n已取消，未呼叫生成 API。")
            return 2

    try:  # 任何寫入前先備份；備份失敗就不寫入、也不呼叫生成 API
        for p in store.backup():
            print(f"  已備份：{p}")
    except Exception as e:  # noqa: BLE001
        print(f"備份失敗，未寫入任何檔案、未呼叫生成 API：{e}")
        return 2

    totals = Counter()
    failed = []
    for n, u in enumerate(todo, start=1):
        if n > 1 and args.min_interval > 0:
            time.sleep(args.min_interval)  # 免費層可用來避開每分鐘請求數限制
        try:
            result, item = generate_one(client, args, u, api_key)
        except FatalApiError as e:
            print(f"  [{n}/{len(todo)}] {u.question_id} 發生不可重試的錯誤，停止整批：{e}")
            store.save(u, {"status": "failed", "attempts": 1, "last_error": str(e), "usage": None,
                           "model": args.model, "updated_at": now_iso()}, None)
            failed.append(u.question_id)
            break
        item.update(model=args.model, updated_at=now_iso(),
                    cost_usd_estimate=cost_usd(price, item["usage"]["input_tokens"],
                                               item["usage"]["output_tokens"] + item["usage"]["thoughts_tokens"]))
        record = to_record(u, result, args.model, input_hash(u, args.model)) if result else None
        store.save(u, item, record)
        totals.update(item["usage"])
        mark = "✓" if result else "✗"
        concern = "（答案疑點）" if result and result["answer_concern"]["has_concern"] else ""
        print(f"  [{n}/{len(todo)}] {mark} {u.question_id:<16} 嘗試 {item['attempts']} 次，"
              f"in {item['usage']['input_tokens']:,} / out {item['usage']['output_tokens']:,} / "
              f"think {item['usage']['thoughts_tokens']:,}{concern}"
              + ("" if result else f"\n        錯誤：{item['last_error']}"))
        if not result:
            failed.append(u.question_id)

    spent = cost_usd(price, totals["input_tokens"], totals["output_tokens"] + totals["thoughts_tokens"])
    print("-" * 56)
    print(f"完成：成功 {len(todo) - len(failed)}，失敗 {len(failed)}")
    print(f"實際 token：input {totals['input_tokens']:,}、output {totals['output_tokens']:,}、"
          f"thinking {totals['thoughts_tokens']:,}、total {totals['total_tokens']:,}")
    print(f"依價目表換算：{'—' if spent is None else f'約 US${spent:.4f}'}（估算，非帳單）")
    return 1 if failed else 0


def select_ids(units: list[Unit], ids_arg: str) -> tuple[list[Unit], list[str]]:
    wanted = [x.strip() for x in ids_arg.split(",") if x.strip()]
    chosen, missing = [], []
    for w in wanted:
        hit = [u for u in units if u.question_id == w or (u.case and u.root_id == w)]
        if not hit:
            missing.append(w)
        chosen += [u for u in hit if u not in chosen]
    return chosen, missing


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", type=Path, default=DEFAULT_BANK, help="題庫 JSON（預設 public/data/sc200.json）")
    ap.add_argument("--dry-run", action="store_true", help="預覽將送出的題目欄位；不呼叫任何 API")
    ap.add_argument("--limit", type=int, help="最多處理 / 預覽的子題數")
    ap.add_argument("--ids", help="只處理指定 id（逗號分隔；題組 id 會展開為其所有子題）")
    ap.add_argument("--estimate", action="store_true", help="以 count_tokens 估算 token 與費用；不生成")
    ap.add_argument("--run", action="store_true", help="估算後經確認，逐題呼叫 Gemini 生成解析")
    ap.add_argument("--resume", action="store_true", help="跳過 output/ 中已有有效解析的子題")
    ap.add_argument("--yes", action="store_true", help="略過互動確認（僅在已確認計畫後使用）")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini 模型 ID（預設 {DEFAULT_MODEL}）")
    ap.add_argument("--thinking-level", default="low", help="thinking_level（預設 low；設為 none 則不指定）")
    ap.add_argument("--max-output-tokens", type=int, default=8192, help="每次生成的輸出上限（含 thinking，預設 8192）")
    ap.add_argument("--timeout", type=int, default=120, help="單次請求逾時秒數（預設 120）")
    ap.add_argument("--retries", type=int, default=2, help="每題失敗後最多再試幾次（預設 2，上限 3）")
    ap.add_argument("--assume-output-tokens", type=int, default=2500,
                    help="尚無實測資料時，估算用的每題輸出 token（含 thinking，預設 2500）")
    ap.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help="輸出目錄（預設 output/）")
    ap.add_argument("--tier", choices=["free", "paid"],
                    help="帳戶計費層級（--run 必填）：free 不計費但內容可能用於改善 Google 產品；paid 依用量計費")
    ap.add_argument("--min-interval", type=float, default=0, help="每題之間至少間隔秒數（免費層建議 5 以上）")
    ap.add_argument("--overwrite-manual", action="store_true",
                    help="明確授權覆寫人工修訂鎖定題目；只適用於 --ids 中逐一列出的子題 ID，且仍需確認")
    args = ap.parse_args()
    if args.overwrite_manual and not args.ids:
        ap.error("--overwrite-manual 必須搭配 --ids，逐一列出要覆寫的子題 ID")
    if args.run and not args.tier:
        ap.error("--run 需指定 --tier free 或 --tier paid，代表你已確認帳戶層級與資料使用政策")
    if args.limit is not None and args.limit < 1:
        ap.error("--limit 必須是正整數")
    if not 0 <= args.retries <= 3:
        ap.error("--retries 必須介於 0 到 3")
    if args.dry_run and (args.estimate or args.run):
        ap.error("--dry-run 不可與 --estimate / --run 同時使用")

    path = args.bank.resolve()
    if not path.exists():
        print(f"找不到題庫檔案：{path}")
        print("請確認真實題庫位於 public/data/sc200.json，或以 --bank 指定。")
        return 2
    try:
        bank = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"題庫不是有效的 JSON：{e}")
        return 2

    rep = Report()
    validate(bank, rep)
    if "結構" in rep.errors:
        print_report(rep)
        return 1
    units = list(iter_units(bank))
    print_stats(bank, units, path)
    print_report(rep)

    if args.ids:
        units, missing = select_ids(units, args.ids)
        if missing:
            print(f"找不到這些 id：{', '.join(missing)}")
            return 2
    if args.dry_run:
        print_dry_run(units, args.limit)
        return 1 if rep.errors else 0
    if not (args.estimate or args.run):
        return 1 if rep.errors else 0
    if rep.errors:
        print("題庫結構有錯誤，請先修正後再呼叫 Gemini。")
        return 1
    return gemini_main(args, path, units)


if __name__ == "__main__":
    sys.exit(main())
