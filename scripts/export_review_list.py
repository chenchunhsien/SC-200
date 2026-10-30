#!/usr/bin/env python3
"""SC-200 Gemini 判讀：人工複核工作清單匯出（唯讀，不呼叫任何 API）。

讀取正式題庫、判讀工作區（gemini_judgments.json、證據包）與一份 pipeline 統計報告，
1. 以 gemini_judge 的同一套規則（effective_run_index／judged_view／review_reasons）重新計算待人工複核清單，
   逐題與報告列出的題號、原因及累積統計比對，列出任何差異；
2. 每題匯出題目原文與選項、題庫答案、Gemini 建議與理由、Microsoft Learn 文件與引文、判讀狀態及複核原因；
3. 依「答案不一致／部分一致、官方文件衝突、fallback、deferred」分類；缺少資料或無法對應者明確標記，不補造。

輸出到一個新的目錄（已存在即拒絕），內含 review_list.md、review_list.csv（UTF-8 BOM，可用 Excel 開啟）、
review_list.json。不修改題庫、判讀紀錄、證據包或報告。

    python scripts/export_review_list.py --workspace output/gemini_run_434 \\
        --report output/gemini_run_434/reports/pipeline_summary_20261029_091759.md
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import gemini_judge as gj  # noqa: E402

va, fmt = gj.va, gj.fmt
CATEGORIES = {  # 分類順序即報表排序
    "disagree": "答案不一致／部分一致",
    "conflict": "官方文件與 Gemini 建議衝突",
    "fallback": "fallback（Gemini 建議無效，保留題庫答案）",
    "deferred": "deferred（服務暫時不可用或逾時，保留題庫答案）",
    "other": "其他（錯誤停止、未知或請求未完成）",
}
MISSING = "（缺少資料）"


# ---- 報告解析 ------------------------------------------------------------------------------------

def parse_report(text: str) -> dict:
    """報告「二、工作區累積統計」中的待人工複核清單（題號 → 原因）與結果統計表。"""
    part = text.split("## 二、", 1)[-1]
    m = re.search(r"\*\*待人工複核：(\d+) 題\*\*", part)
    review = {}
    for line in part[m.end():].splitlines() if m else []:
        hit = re.match(r"^- ([^：\s]+)：(.+)$", line.strip())
        if hit:
            review[hit.group(1)] = hit.group(2).split("；")
    outcomes = {}
    for label, n in re.findall(r"^\| ([^|]+?) \| (\d+) \|$", part, re.M):
        outcomes[label] = int(n)
    return {"declared": int(m.group(1)) if m else None, "review": review, "outcomes": outcomes}


# ---- 題目呈現 ------------------------------------------------------------------------------------

def question_lines(unit) -> list[str]:
    """題目原文與選項（依題型）；題組子題附上情境全文（折疊）。"""
    q = unit.question
    out = []
    if isinstance(unit.case, dict):
        info = unit.case.get("caseInfo") or {}
        out += [f"<details><summary>題組情境：{info.get('title') or unit.case.get('id')}（點開閱讀全文）</summary>", ""]
        for s in info.get("sections") or []:
            out += [f"**{s.get('title', '')}**", "", str(s.get("content", "")).strip(), ""]
        out += ["</details>", ""]
    out += [str(q.get("question", MISSING)).strip(), ""]
    if q.get("code"):
        out += ["```", str(q["code"]).rstrip(), "```", ""]
    if q.get("template"):
        out += ["```", str(q["template"]).rstrip(), "```", ""]
    for o in q.get("options") or []:
        out.append(f"- **{o.get('id')}**. {o.get('text')}")
    for o in q.get("items") or []:
        out.append(f"- **{o.get('id')}**. {o.get('text')}")
    for s in q.get("statements") or []:
        out.append(f"- **{s.get('id')}**. {s.get('text')}")
    for b in q.get("blanks") or []:
        out.append(f"- **{b.get('id')}**：{' ／ '.join(str(x) for x in b.get('options') or [])}")
    if q.get("columns"):
        out.append(f"- 欄位：{' ／ '.join(q['columns'])}")
    return out + [""]


def choices_text(q: dict) -> str:
    parts = [f"{o.get('id')}. {o.get('text')}" for o in (q.get("options") or []) + (q.get("items") or [])]
    parts += [f"{s.get('id')}. {s.get('text')}" for s in q.get("statements") or []]
    parts += [f"{b.get('id')}: {' / '.join(str(x) for x in b.get('options') or [])}" for b in q.get("blanks") or []]
    return "\n".join(parts)


# ---- 單題資料 ------------------------------------------------------------------------------------

def build_record(qid: str, runs, ctx, packet_dir: Path, reasons: list[str]) -> dict:
    view = gj.judged_view(runs)
    rec = {"id": qid, "outcome": view["outcome"], "reasons": reasons, "flags": [], "categories": []}
    unit = ctx.units.get(qid)
    if unit is None:
        rec["flags"].append("題號不在目前正式題庫中，無法對應題目原文與題庫答案")
    rec["unit"] = unit
    rec["type"] = unit.type if unit else MISSING
    rec["bank_answer"] = va.answer_of(unit) if unit else None
    rec["review_note"] = (unit.question.get("reviewNote") if unit else None) or ""
    run = runs[gj.effective_run_index(runs)] if isinstance(runs, list) and runs else {}
    run = run if isinstance(run, dict) else {}
    ev = run.get("evaluation") if isinstance(run.get("evaluation"), dict) else {}
    out = run.get("model_output") if isinstance(run.get("model_output"), dict) else {}
    fin = run.get("final_exam_recommendation") if isinstance(run.get("final_exam_recommendation"), dict) else {}
    rec.update(status=run.get("status") or MISSING, run_at=run.get("at") or MISSING, http_status=run.get("http_status"),
               error=run.get("error"), evidence_level=view["evidence_level"], bank_cmp=view["bank_cmp"],
               final_answer=fin.get("answer"), final_source=fin.get("source") or MISSING,
               rationale=out.get("rationale") or "", open_questions=out.get("open_questions") or [],
               model_conflicts=out.get("conflicts") or [], official_conflicts=ev.get("official_conflicts") or [],
               evidence_gaps=ev.get("evidence_gaps") or [], incomplete=ev.get("incomplete_reasons") or [],
               citations=[], documents=[], learn_status=MISSING, learn_note="")
    if ev.get("status") == "evaluated":
        rec["gemini_answer"], rec["gemini_note"] = ev.get("suggested_answer"), ""
    elif (ev.get("answer_parse") or {}).get("raw") is not None:
        rec["gemini_answer"] = None
        rec["gemini_note"] = f"無效輸出（原始：{ev['answer_parse']['raw']}；{ev.get('error') or ev.get('status')}）"
    else:
        rec["gemini_answer"], rec["gemini_note"] = None, "沒有模型輸出"
    if unit is not None and run.get("packet_file"):
        path = packet_dir / run["packet_file"]
        if not path.is_file():
            rec["flags"].append(f"找不到判讀時使用的證據包 {run['packet_file']}")
        else:
            packet, _, ok = gj.load_packet(path)
            if not ok:
                rec["flags"].append(f"證據包雜湊核對失敗（{path.name}），引文對應不可信")
            if packet:
                if packet.get("bank_answer", {}).get("value") != rec["bank_answer"]:
                    rec["flags"].append(f"判讀時的題庫答案 {fmt(packet.get('bank_answer', {}).get('value'))} 與目前題庫 "
                                        f"{fmt(rec['bank_answer'])} 不同（判讀後題庫已修改）")
                ml = packet.get("microsoft_learn") or {}
                rec["learn_status"], rec["learn_note"] = ml.get("status") or MISSING, ml.get("note") or ""
                docs = {d.get("doc_id"): d for d in ml.get("documents") or []}
                rec["documents"] = [{"doc_id": d.get("doc_id"), "title": d.get("title"), "url": d.get("url")} for d in docs.values()]
                for c in ev.get("citations") or []:
                    doc = docs.get(str(c.get("passage_id", "")).split("-", 1)[0])
                    rec["citations"].append({"target": c.get("target_id"), "passage_id": c.get("passage_id"),
                                             "url": doc.get("url") if doc else None, "quote": c.get("quote"),
                                             "quote_check": c.get("quote_check"),
                                             "relation": c.get("relation_claimed_by_model"), "alignment": c.get("alignment")})
    elif unit is not None:
        rec["flags"].append("判讀紀錄沒有記錄證據包檔名")
    if rec["learn_status"] != MISSING and not rec["documents"]:
        rec["flags"].append("證據包中沒有 Microsoft Learn 官方文件（官方證據不足）")
    if rec["documents"] and not rec["citations"] and ev.get("status") == "evaluated":
        rec["flags"].append("有官方文件，但 Gemini 沒有引用任何段落")
    bad_quotes = [c for c in rec["citations"] if c["quote_check"] != "found"]
    if bad_quotes:
        rec["flags"].append(f"{len(bad_quotes)} 則引文在證據包中找不到原文（不可視為官方依據）")
    if rec["outcome"] in ("fallback", "deferred"):
        rec["flags"].append("沒有有效的 Gemini 判讀，最終推薦為題庫原始答案")
    cats = []
    if view["bank_cmp"] in ("不一致", "部分一致"):
        cats.append("disagree")
    if view["conflicts"]:
        cats.append("conflict")
    if view["outcome"] in ("fallback", "deferred"):
        cats.append(view["outcome"])
    rec["categories"] = cats or ["other"]
    return rec


# ---- 輸出 ----------------------------------------------------------------------------------------

def render_md(recs: list[dict], meta: dict) -> str:
    L = ["# SC-200 Gemini 判讀：人工複核工作清單", "",
         f"> 產生時間 {meta['generated_at']}｜工作區 `{meta['workspace']}`｜依據報告 " + (f"`{meta['report']}`" if meta["report"] else "（無，未與 pipeline 報告比對）"),
         "> 唯讀匯出，未呼叫任何 API、未修改題庫或判讀紀錄。**Gemini 建議不是經過獨立驗證的正確答案**；"
         "官方證據等級描述證據品質（程式計算），不是正確率。有效引句只代表引文確實存在於證據包段落中。", "",
         "## 對應檢查", ""]
    L += [f"- {x}" for x in meta["checks"]] + [""]
    L += ["## 分類統計", "", "| 分類 | 題數（可重複歸類） | 主要分類題數 |", "|---|---|---|"]
    for k, label in CATEGORIES.items():
        L.append(f"| {label} | {meta['category_any'].get(k, 0)} | {meta['category_primary'].get(k, 0)} |")
    L += ["", f"- 有資料缺漏或證據不足標記的題目：{meta['flagged']} 題", "",
          "## 題目索引", "", "| # | 題號 | 主要分類 | 題庫答案 | Gemini 建議 | 官方證據 | 標記 |", "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(recs, 1):
        g = fmt(r["gemini_answer"]) if r["gemini_answer"] is not None else "—"
        L.append(f"| {i} | [{r['id']}](#{r['id'].lower()}) | {CATEGORIES[r['categories'][0]]} | {fmt(r['bank_answer'])} | {g} "
                 f"| {r['evidence_level']} | {'⚠' * min(len(r['flags']), 3) or '—'} |")
    L.append("")
    current = None
    for i, r in enumerate(recs, 1):
        if r["categories"][0] != current:
            current = r["categories"][0]
            L += [f"## {CATEGORIES[current]}", ""]
        L += [f"### {r['id']}", "",
              f"**#{i}｜{r['type']}｜分類：{'、'.join(CATEGORIES[c] for c in r['categories'])}**", "",
              f"- 複核原因：{'；'.join(r['reasons'])}",
              f"- 題庫原始答案：**{fmt(r['bank_answer'])}**",
              f"- Gemini 建議：**{fmt(r['gemini_answer']) if r['gemini_answer'] is not None else '—'}**"
              + (f"（{r['gemini_note']}）" if r["gemini_note"] else "") + f"｜與題庫：{r['bank_cmp']}",
              f"- 最終推薦（程式依規則產生）：{fmt(r['final_answer'])}（{r['final_source']}）",
              f"- 判讀狀態：{r['status']}" + (f"｜HTTP {r['http_status']}" if r["http_status"] not in (None, 200) else "")
              + f"｜判讀時間 {r['run_at']}｜官方證據等級 {r['evidence_level']}｜Learn 檢索 {r['learn_status']}"]
        if r["error"]:
            L.append(f"- 錯誤：{r['error']}")
        if r["review_note"]:
            L.append(f"- 題庫既有備註：{r['review_note']}")
        if r["flags"]:
            L += ["- ⚠ 資料標記："] + [f"  - {f}" for f in r["flags"]]
        L += ["", "**題目**", ""]
        L += question_lines(r["unit"]) if r["unit"] else [MISSING, ""]
        if r["rationale"]:
            L += ["**Gemini 理由**（模型文字）", "", r["rationale"].strip(), ""]
        for title, items in (("官方文件與 Gemini 建議的衝突", r["official_conflicts"]), ("模型列出的衝突", r["model_conflicts"]),
                             ("證據缺口", r["evidence_gaps"]), ("判讀不完整", r["incomplete"]),
                             ("模型列出的未確認事項", r["open_questions"])):
            if items:
                L += [f"**{title}**", ""] + [f"- {x}" for x in items] + [""]
        if r["documents"]:
            L += ["**Microsoft Learn 官方文件**（證據包）", ""]
            L += [f"- {d['doc_id']} [{d['title']}]({d['url']})" for d in r["documents"]] + [""]
        else:
            L += [f"**Microsoft Learn 官方文件**：無" + (f"（{r['learn_note']}）" if r["learn_note"] else ""), ""]
        if r["citations"]:
            L += ["| 對象 | 段落 | 引文存在 | 模型主張 | 對應 | 引文 |", "|---|---|---|---|---|---|"]
            for c in r["citations"]:
                q = str(c["quote"] or "").replace("|", "\\|").replace("\n", " ")
                link = f"[{c['passage_id']}]({c['url']})" if c["url"] else str(c["passage_id"])
                L.append(f"| {c['target']} | {link} | {c['quote_check']} | {c['relation']} | {c['alignment'] or '—'} | {q} |")
            L.append("")
        L += ["---", ""]
    return "\n".join(L)


CSV_COLUMNS = ["no", "id", "type", "primary_category", "categories", "review_reasons", "bank_answer", "gemini_answer",
               "gemini_note", "bank_cmp", "final_answer", "final_source", "status", "http_status", "evidence_level",
               "learn_status", "official_conflicts", "doc_urls", "citations", "data_flags", "existing_review_note",
               "rationale", "question", "choices"]


def render_csv(recs: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    w.writeheader()
    for i, r in enumerate(recs, 1):
        q = r["unit"].question if r["unit"] else {}
        w.writerow({"no": i, "id": r["id"], "type": r["type"], "primary_category": CATEGORIES[r["categories"][0]],
                    "categories": "；".join(CATEGORIES[c] for c in r["categories"]), "review_reasons": "；".join(r["reasons"]),
                    "bank_answer": fmt(r["bank_answer"]), "gemini_answer": fmt(r["gemini_answer"]) if r["gemini_answer"] is not None else "",
                    "gemini_note": r["gemini_note"], "bank_cmp": r["bank_cmp"], "final_answer": fmt(r["final_answer"]),
                    "final_source": r["final_source"], "status": r["status"], "http_status": r["http_status"] or "",
                    "evidence_level": r["evidence_level"], "learn_status": r["learn_status"],
                    "official_conflicts": "\n".join(r["official_conflicts"]),
                    "doc_urls": "\n".join(d["url"] or "" for d in r["documents"]),
                    "citations": "\n".join(f"[{c['target']}|{c['passage_id']}|{c['quote_check']}|{c['relation']}] {c['quote']}"
                                           for c in r["citations"]),
                    "data_flags": "\n".join(r["flags"]), "existing_review_note": r["review_note"],
                    "rationale": r["rationale"], "question": q.get("question", MISSING), "choices": choices_text(q)})
    return "\ufeff" + buf.getvalue()


def main(argv=None) -> int:
    gj.eq.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", type=Path, default=va.DEFAULT_BANK)
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--report", type=Path, default=None,
                    help="pipeline_summary_*.md（比對其待人工複核清單）；以 --ids 分批判讀、沒有 pipeline 報告時可省略")
    ap.add_argument("--out", type=Path, default=None, help="新的輸出目錄（預設 output/gemini_review_<時間>；已存在即拒絕）")
    args = ap.parse_args(argv)
    ws = args.workspace.resolve()
    out = (args.out or va.DEFAULT_OUT / f"gemini_review_{datetime.now().strftime('%Y%m%d_%H%M%S')}").resolve()
    if out.exists():
        print(f"輸出目錄已存在，不覆寫：{out}")
        return 2
    ctx = va.Ctx(args.bank.resolve(), ws)
    results = gj.load_results(ws / "gemini_judgments.json")
    rep = parse_report(args.report.read_text(encoding="utf-8")) if args.report else None
    order = {q: i for i, q in enumerate(ctx.units)}
    runs_all = {q: v.get("runs") for q, v in results["questions"].items() if isinstance(v, dict) and v.get("runs")}
    unfinished = {u["question_id"] for u in gj.unfinished_attempts(ws, results) if u.get("question_id")}
    stats = gj.summarize_judgments(runs_all, unfinished, order)

    # 對應檢查：報告宣告數、報告列出的題號與原因、目前資料重新計算的結果、正式題庫
    checks = []
    cur_ids = list(stats["review"])
    out_now = {gj.OUTCOMES[k]: stats["outcomes"].get(k, 0) for k in gj.OUTCOMES}
    if rep is None:
        rep = {"review": {}}
        rep_ids, only_rep, only_cur = [], [], []
        checks.append(f"未提供 pipeline 報告，未與報告比對；依目前判讀紀錄計算待人工複核 {len(cur_ids)} 題")
        checks.append(f"累積結果統計：{out_now}")
    else:
        rep_ids = list(rep["review"])
        checks.append(f"報告宣告待人工複核 {rep['declared']} 題；報告實際列出 {len(rep_ids)} 題；依目前判讀紀錄重新計算 {len(cur_ids)} 題")
        only_rep, only_cur = sorted(set(rep_ids) - set(cur_ids)), sorted(set(cur_ids) - set(rep_ids))
        checks.append("報告與目前資料的題號：" + ("完全一致" if not only_rep and not only_cur else
                      f"僅報告有 {only_rep or '無'}；僅目前資料有 {only_cur or '無'}"))
        reason_diff = [q for q in rep_ids if q in stats["review"] and rep["review"][q] != stats["review"][q]]
        checks.append("各題複核原因：" + ("與報告一致" if not reason_diff else f"與報告不同 {len(reason_diff)} 題：{', '.join(reason_diff)}"))
        out_diff = {k: (rep["outcomes"].get(k), v) for k, v in out_now.items() if rep["outcomes"].get(k) != v}
        checks.append("累積結果統計：" + ("與報告一致" if not out_diff else f"與報告不同（報告值, 目前值）：{out_diff}"))
    not_in_bank = [q for q in set(rep_ids) | set(cur_ids) if q not in ctx.units]
    checks.append("對應正式題庫：" + ("全部題號都在 public/data/sc200.json 中" if not not_in_bank else f"題庫中找不到 {not_in_bank}"))
    checks.append(f"判讀紀錄共 {len(runs_all)} 題、正式題庫共 {len(ctx.units)} 個判讀單位；未完成請求 {len(unfinished)} 題")

    ids = sorted(set(rep_ids) | set(cur_ids), key=lambda q: (order.get(q, len(order)), q))
    recs = [build_record(q, runs_all.get(q), ctx, ws / "gemini_judge" / "packets",
                         stats["review"].get(q) or rep["review"].get(q) or []) for q in ids]
    for r in recs:
        if r["id"] in only_rep:
            r["flags"].append("報告列出此題，但依目前判讀紀錄已不需複核（判讀紀錄可能在報告後更新）")
        if r["id"] in only_cur:
            r["flags"].append("報告未列出此題，依目前判讀紀錄需要複核（判讀紀錄可能在報告後更新）")
    recs.sort(key=lambda r: (list(CATEGORIES).index(r["categories"][0]), order.get(r["id"], len(order))))
    any_count, primary = {}, {}
    for r in recs:
        primary[r["categories"][0]] = primary.get(r["categories"][0], 0) + 1
        for c in r["categories"]:
            any_count[c] = any_count.get(c, 0) + 1
    meta = {"generated_at": va.now_iso(), "workspace": str(ws), "report": str(args.report) if args.report else None,
            "checks": checks,
            "category_any": any_count, "category_primary": primary, "flagged": sum(1 for r in recs if r["flags"])}

    gj.write_new_file(out / "review_list.md", render_md(recs, meta) + "\n")
    gj.write_new_file(out / "review_list.csv", render_csv(recs))
    payload = {"meta": meta, "questions": [{k: v for k, v in r.items() if k != "unit"} | {
        "question": r["unit"].question if r["unit"] else None,
        "case_title": ((r["unit"].case or {}).get("caseInfo") or {}).get("title") if r["unit"] and isinstance(r["unit"].case, dict) else None}
        for r in recs]}
    gj.write_new_file(out / "review_list.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print("\n".join(checks))
    print(f"分類（可重複）：{ {CATEGORIES[k]: v for k, v in any_count.items()} }")
    print(f"主要分類：{ {CATEGORIES[k]: v for k, v in primary.items()} }")
    print(f"有資料標記：{meta['flagged']} 題")
    print(f"輸出：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
