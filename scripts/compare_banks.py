#!/usr/bin/env python3
"""比對新來源題庫與現有題庫，找出新增的題目（唯讀；不修改任何題庫）。

新來源由 parse_pdf.py 轉換（bank JSON + drafts JSON）。題號在不同來源間不可靠，一律以內容比對：
- 題幹：移除 HOTSPOT／DRAG DROP、To answer…、NOTE: Each correct…、系列題固定說明等樣板文字後比相似度
  （保留 Solution: 那一行，系列題靠它區分）。
- 選項：兩邊都有文字選項時一併比對；選項在圖片中時只比題幹。
- 題組子題另外比對情境背景。
一對一配對（分數高者先配），並分成：
- existing：與現有題目相符（依使用者決定，略過，不採用新來源的答案）
- new：找不到相近的現有題目
- review：分數介於兩者之間，或最佳與次佳候選太接近，需要人工看 PDF 確認

    python scripts/compare_banks.py --new output/new_bank_478/bank478.json \\
        --new-drafts output/new_bank_478/bank478.drafts.json --out output/new_bank_478/compare
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import verify_answers as va  # noqa: E402

EXISTING, REVIEW_FLOOR, MARGIN = 0.85, 0.60, 0.03
BOILERPLATE = [
    r"note:\s*this (?:question is part of a series|section contains one or more sets of questions).*?review screen\.",
    r"note:\s*each correct (?:selection|match|answer) is worth one point\.?",
    r"each correct answer presents (?:a complete|part of the) solution\.?",
    r"\bto answer,?[^.?!]*[.?!]",
    r"^\s*(?:hotspot|drag drop)\b",
    r"select and place:?", r"hot area:?", r"answer area:?",
]
RE_OPTION = re.compile(r"^\s*([A-H])[.)]\s+(.+)$")
RE_CUT = re.compile(r"(?im)^\s*(?:correct\s+answer|suggested\s+answer|answer)\s*[:：]")


def norm(text: str) -> str:
    t = re.sub(r"[ \t]*\n[ \t]*(?=\S)", " ", (text or "").lower())  # PDF 斷行先合併，樣板句才比對得到
    for pat in BOILERPLATE:
        t = re.sub(pat, " ", t, flags=re.S | re.M)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def option_texts(q: dict) -> list[str]:
    out = [o.get("text", "") for o in q.get("options") or []] + [i.get("text", "") for i in q.get("items") or []]
    out += [s.get("text", "") for s in q.get("statements") or []]
    return [norm(x) for x in out if x and "TODO" not in x]


def from_raw(raw: str) -> tuple[str, list[str]]:
    """待校正題目：raw_text 在 Correct Answer 之前的部分拆成題幹與文字選項。"""
    head = RE_CUT.split(raw or "", maxsplit=1)[0]
    stem, opts = [], []
    for ln in head.splitlines():
        m = RE_OPTION.match(ln)
        (opts.append(norm(m.group(2))) if m else stem.append(ln))
    return norm("\n".join(stem)), [o for o in opts if o]


def case_text(case: dict | None) -> str:
    if not case:
        return ""
    return norm(" ".join(s.get("content", "") for s in (case.get("caseInfo") or {}).get("sections") or []))[:3000]


def old_units(bank: dict) -> list[dict]:
    out = []
    for q in bank["questions"]:
        subs = q.get("questions") if q.get("type") == "case_study" else None
        for u in subs or [q]:
            stem = norm(" ".join(x for x in (u.get("question"), u.get("code")) if x))
            out.append({"id": u["id"], "type": u.get("type"), "stem": stem, "options": option_texts(u),
                        "case": case_text(q) if subs else "", "preview": (u.get("question") or "")[:160]})
    return out


def new_units(bank: dict, drafts: dict) -> list[dict]:
    out = []
    for q in bank["questions"]:
        subs = q.get("questions") if q.get("type") == "case_study" else None
        for u in subs or [q]:
            stem = norm(" ".join(x for x in (u.get("question"), u.get("code")) if x and "TODO" not in x))
            out.append({"id": u["id"], "type": u.get("type"), "stem": stem, "options": option_texts(u),
                        "case": case_text(q) if subs else "", "source": u.get("source") or q.get("source"),
                        "status": "parsed", "preview": (u.get("question") or "")[:160]})
    for d in drafts.get("drafts") or []:
        q = d["question"]
        if d.get("suggested_type") == "case_study" or q.get("type") == "case_study":
            subs = {s["id"]: s for s in d.get("subs") or []}
            for u in q.get("questions") or []:
                stem, opts = from_raw((subs.get(u["id"]) or {}).get("raw_text", ""))
                out.append({"id": u["id"], "type": (subs.get(u["id"]) or {}).get("suggested_type") or u.get("type"),
                            "stem": stem, "options": opts, "case": case_text(q), "source": u.get("source") or d.get("source"),
                            "status": "draft", "preview": re.sub(r"\s+", " ", (subs.get(u["id"]) or {}).get("raw_text", ""))[:160]})
        else:
            stem, opts = from_raw(d.get("raw_text", ""))
            out.append({"id": q["id"], "type": d.get("suggested_type") or q.get("type"), "stem": stem, "options": opts,
                        "case": "", "source": d.get("source"), "status": "draft",
                        "preview": re.sub(r"\s+", " ", d.get("raw_text", ""))[:160]})
    return out


def ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b, autojunk=False).ratio() if a and b else 0.0


MIN_CONTAIN_WORDS = 12  # 較短的題幹至少這麼多字，才用「包含」判斷（避免 What should you use? 這類通用短句）


def stem_score(a: str, b: str) -> float:
    """題幹相似度：整體相似度，或「開頭相同且較短題幹的字都出現在較長題幹」——
    其中一方常多帶表格／程式碼文字（新來源的抽取文字，或舊題庫手動轉寫的圖片內容）。"""
    if not a or not b:
        return 0.0
    ta, tb = a.split(), b.split()
    short = min(len(set(ta)), len(set(tb)))
    if short < MIN_CONTAIN_WORDS:
        return ratio(a, b)
    cover = len(set(ta) & set(tb)) / short
    n = min(len(ta), len(tb), 40)
    head = ratio(" ".join(ta[:n]), " ".join(tb[:n]))
    return max(ratio(a, b), min(head, cover))


def jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    return len(sa & sb) / (len(sa | sb) or 1)


def score(n: dict, o: dict) -> float:
    s = stem_score(n["stem"], o["stem"])
    if n["options"] and o["options"]:
        s = 0.7 * s + 0.3 * ratio(" | ".join(sorted(n["options"])), " | ".join(sorted(o["options"])))
    if n["case"] and o["case"]:
        # 兩邊情境背景的排版不同，只用字詞重疊做輕微的同分區分（同題幹、不同題組）
        s = 0.95 * s + 0.05 * jaccard(n["case"], o["case"])
    elif bool(n["case"]) != bool(o["case"]):
        s *= 0.9  # 題組子題與一般題目互相比對時略降
    return s


def tokens(u: dict) -> set:
    return set(u["stem"].split()) | {w for o in u["options"] for w in o.split()}


def compare(new: list[dict], old: list[dict]) -> list[dict]:
    old_tok = [tokens(o) for o in old]
    cands = []
    for i, n in enumerate(new):
        nt = tokens(n)
        scored = []
        for j, o in enumerate(old):
            jac = len(nt & old_tok[j]) / (len(nt | old_tok[j]) or 1)
            if jac >= 0.25:
                scored.append((score(n, o), j))
        scored.sort(reverse=True)
        cands.append(scored[:5])
    pairs = sorted(((s, i, j) for i, sc in enumerate(cands) for s, j in sc if s >= REVIEW_FLOOR), reverse=True)
    taken_old, match = set(), {}
    for s, i, j in pairs:
        if i in match or j in taken_old:
            continue
        match[i] = (j, s)
        taken_old.add(j)
    rows = []
    for i, n in enumerate(new):
        best = cands[i][0] if cands[i] else (0.0, None)
        second = cands[i][1] if len(cands[i]) > 1 else (0.0, None)
        j, s = match.get(i, (None, 0.0))
        if j is not None and s >= EXISTING and (second[1] in (None, j) or best[0] - second[0] >= MARGIN or s >= 0.97):
            status, why = "existing", ""
        elif j is not None:
            status = "review"
            why = "相似度介於門檻之間" if s < EXISTING else "最佳與次佳候選太接近"
        elif best[0] >= EXISTING:
            status, why = "review", f"最相近的舊題 {old[best[1]]['id']} 已配給新來源另一題（新來源可能有重複題）"
        elif best[0] >= REVIEW_FLOOR:
            # 一對一配對下，最相近的舊題已對應到新來源中更相符的題目 → 本題是相似主題的新題
            status, why = "new", f"與 {old[best[1]]['id']} 相似（{best[0]:.2f}），但該題已對應到新來源中更相符的題目"
        else:
            status, why = "new", ""
        rows.append({"new_id": n["id"], "type": n["type"], "parse_status": n["status"], "status": status, "reason": why,
                     "match": old[j]["id"] if j is not None else None, "score": round(s, 3),
                     "best": old[best[1]]["id"] if best[1] is not None else None, "best_score": round(best[0], 3),
                     "second": old[second[1]]["id"] if second[1] is not None else None, "second_score": round(second[0], 3),
                     "section": (n["source"] or {}).get("section"), "number": (n["source"] or {}).get("number"),
                     "pages": (n["source"] or {}).get("pages"), "preview": n["preview"],
                     "old_preview": old[j]["preview"] if j is not None else (old[best[1]]["preview"] if best[1] is not None else "")})
    return rows


def render(rows: list[dict], unmatched_old: list[str], meta: dict) -> str:
    by = {k: [r for r in rows if r["status"] == k] for k in ("new", "review", "existing")}
    L = ["# 新來源題庫比對結果", "",
         f"> {meta['generated_at']}｜新來源 `{meta['new']}`（{len(rows)} 題）｜現有題庫 `{meta['old']}`（{meta['old_count']} 題）",
         f"> 判定：相似度 ≥ {EXISTING} 且與次佳候選相差 ≥ {MARGIN} 為「已有」；< {REVIEW_FLOOR} 為「新題」；其餘需人工確認。"
         "依使用者決定，已有題目略過，不採用新來源的答案。", "",
         "| 分類 | 題數 |", "|---|---|",
         f"| 新題 | {len(by['new'])} |", f"| 需人工確認 | {len(by['review'])} |", f"| 已有（略過） | {len(by['existing'])} |",
         f"| 現有題庫中未在新來源出現 | {len(unmatched_old)} |", ""]
    L += ["## 新題", "", "| # | 新來源題號 | 段落／題號 | 頁 | 題型 | 最相近舊題（分數） | 說明 | 題幹開頭 |", "|---|---|---|---|---|---|---|---|"]
    for k, r in enumerate(by["new"], 1):
        L.append(f"| {k} | {r['new_id']} | {r['section']} #{r['number']} | {r['pages']} | {r['type']} | "
                 f"{r['best'] or '—'}（{r['best_score']}） | {r['reason'] or '—'} | {r['preview'][:90].replace('|', '/')} |")
    L += ["", "## 需人工確認", "", "| # | 新來源題號 | 頁 | 原因 | 配對／最相近舊題（分數） | 次佳（分數） | 新來源題幹 | 舊題幹 |",
          "|---|---|---|---|---|---|---|---|"]
    for k, r in enumerate(by["review"], 1):
        L.append(f"| {k} | {r['new_id']} | {r['pages']} | {r['reason']} | {r['match'] or r['best']}（{r['score'] or r['best_score']}） | "
                 f"{r['second'] or '—'}（{r['second_score']}） | {r['preview'][:70].replace('|', '/')} | {r['old_preview'][:70].replace('|', '/')} |")
    L += ["", "## 現有題庫中未在新來源出現的題目", "", ", ".join(unmatched_old) or "無", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    va.eq.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", type=Path, default=va.DEFAULT_BANK)
    ap.add_argument("--new", type=Path, required=True)
    ap.add_argument("--new-drafts", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="新的輸出目錄（已存在即拒絕）")
    args = ap.parse_args(argv)
    if args.out.exists():
        print(f"輸出目錄已存在，不覆寫：{args.out}")
        return 2
    old = old_units(json.loads(args.old.read_text(encoding="utf-8")))
    new = new_units(json.loads(args.new.read_text(encoding="utf-8")), json.loads(args.new_drafts.read_text(encoding="utf-8")))
    rows = compare(new, old)
    matched = {r["match"] for r in rows if r["status"] == "existing"} | {r["match"] for r in rows if r["status"] == "review" and r["match"]}
    unmatched_old = [o["id"] for o in old if o["id"] not in matched]
    meta = {"generated_at": datetime.now().isoformat(timespec="seconds"), "new": str(args.new), "old": str(args.old),
            "old_count": len(old), "thresholds": {"existing": EXISTING, "review_floor": REVIEW_FLOOR, "margin": MARGIN}}
    args.out.mkdir(parents=True)
    (args.out / "compare.json").write_text(json.dumps({"meta": meta, "rows": rows, "unmatched_old": unmatched_old},
                                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out / "compare.md").write_text(render(rows, unmatched_old, meta) + "\n", encoding="utf-8")
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("new", "review", "existing")}
    print(f"新來源 {len(rows)} 題、現有 {len(old)} 題 → 新題 {counts['new']}、需人工確認 {counts['review']}、"
          f"已有 {counts['existing']}；現有題庫未出現在新來源 {len(unmatched_old)} 題")
    print(f"輸出：{args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
