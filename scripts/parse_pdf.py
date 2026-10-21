#!/usr/bin/env python3
"""SC-200 PDF 題庫 → JSON 半自動轉換工具。

子命令
------
parse   讀取 PDF，抽出標準選擇題寫入題庫 JSON；無法自動解析的題目
        （HOTSPOT / DRAG DROP / Case Study / 格式異常）寫入 Draft JSON 待校正。
merge   將 Draft JSON 中 status 改為 "approved" 的題目併入題庫。

範例
----
    python scripts/parse_pdf.py parse SC-200.pdf -o public/data/sc200.json \\
        --title "SC-200 題庫" --snapshots drafts_img/
    # 手動或交給 LLM 補完 public/data/sc200.drafts.json 後：
    python scripts/parse_pdf.py merge public/data/sc200.json public/data/sc200.drafts.json

輸出的題庫符合 public/data/questions.schema.json；完成後記得在
public/data/index.json 加入該題庫。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    sys.exit("缺少 PyMuPDF，請先執行：pip install -r scripts/requirements.txt")

# ---------------------------------------------------------------------------
# 規則（不同來源的 PDF 排版不同，必要時調整這裡即可）
# ---------------------------------------------------------------------------

# 題目起點，例如 "QUESTION 12"、"QUESTION NO: 12"、"Question #12 Topic 2"、"Question: 12"
RE_QUESTION = re.compile(
    r"^\s*(?:QUESTION|Question)\s*(?:NO\s*[:.]?|#|:)?\s*(\d+)\b\s*(?:Topic\s*(\d+))?\s*(.*)$"
)
# 章節 / 主題標題，例如 "Topic 2, Contoso Ltd"、"Topic 3 - Mixed Questions"、"Section 1: ..."
RE_TOPIC = re.compile(r"^\s*(Topic|Section|Testlet)\s*(\d+)\s*[,:.\-–—]?\s*(.*)$", re.I)
# 選項，例如 "A. text"、"B) text"、"C text"（後者需 --loose-options）
RE_OPTION = re.compile(r"^\s*([A-H])[.)]\s+(.+)$")
RE_OPTION_LOOSE = re.compile(r"^\s*([A-H])\s+(\S.+)$")
# 答案行，例如 "Correct Answer: AC"、"Answer: B, D"、"Suggested Answer: A"
RE_ANSWER = re.compile(r"^\s*(?:Correct\s+|Suggested\s+)?Answers?\s*[:：]\s*(.*)$", re.I)
# 解析開頭，例如 "Explanation:"、"Explanation/Reference:"、"Reference:"
RE_EXPLAIN = re.compile(r"^\s*(?:Explanation(?:/Reference)?|Reference|Rationale)\s*[:：]?\s*(.*)$", re.I)
# 雜訊行（題目中繼資料）
RE_NOISE = re.compile(r"^\s*(?:Section\s*:\s*\(none\)|Section\s*:\s*\[none\]|Community vote distribution.*|\d+\s*/\s*\d+)\s*$", re.I)
RE_URL = re.compile(r"https?://[^\s)\]>\"']+")
RE_ANSWER_LETTERS = re.compile(r"^[A-H](?:\s*[,，、&]?\s*(?:and\s+)?[A-H])*$", re.I)

# 需人工 / LLM 校正的複雜題型標記
COMPLEX_MARKERS = {
    "HOTSPOT": re.compile(r"\bHOT\s?SPOT\b|\bHot Area\s*:", re.I),
    "DRAG_DROP": re.compile(r"\bDRAG\s*DROP\b|\bSelect and Place\s*:", re.I),
    "CASE_STUDY": re.compile(r"^\s*(?:Case Study|Overview\s*$|Existing Environment|Requirements\s*$)", re.I | re.M),
    "ANSWER_AREA": re.compile(r"\bAnswer Area\b", re.I),
}
RE_ORDER_HINT = re.compile(r"\bin sequence\b|\bcorrect order\b|\barrange\b|\bwhich (?:three|four|five|six) actions\b", re.I)
RE_YESNO_HINT = re.compile(r"\bselect Yes if\b|\bstatements?\b.*\bYes\b|\bYes\s+No\b", re.I)

PAGE_MARK = "\x00PAGE:"
TODO = "TODO"


# ---------------------------------------------------------------------------
# PDF 文字擷取
# ---------------------------------------------------------------------------


def extract_lines(pdf: Path, first: int | None, last: int | None, header_ratio: float) -> tuple[list[str], dict[int, bool]]:
    """回傳全文行陣列（插入頁碼標記）與每頁是否含圖片。

    出現在超過 header_ratio 比例頁面上的短行會被視為頁首 / 頁尾並移除。
    """
    doc = fitz.open(pdf)
    start = max(1, first or 1)
    end = min(doc.page_count, last or doc.page_count)
    pages: list[tuple[int, list[str]]] = []
    has_images: dict[int, bool] = {}
    for no in range(start, end + 1):
        page = doc[no - 1]
        text = page.get_text("text", sort=True)
        lines = [ln.rstrip() for ln in text.replace(" ", " ").splitlines()]
        pages.append((no, lines))
        has_images[no] = bool(page.get_images(full=False))
    doc.close()

    # 頁首 / 頁尾偵測
    freq = Counter()
    for _, lines in pages:
        freq.update({ln.strip() for ln in lines if ln.strip()})
    threshold = max(3, int(len(pages) * header_ratio))
    boilerplate = {ln for ln, n in freq.items() if n >= threshold and len(ln) < 120 and not RE_ANSWER.match(ln)}

    out: list[str] = []
    for no, lines in pages:
        out.append(f"{PAGE_MARK}{no}")
        out.extend(ln for ln in lines if ln.strip() not in boilerplate and not RE_NOISE.match(ln))
    return out, has_images


# ---------------------------------------------------------------------------
# 題目切分與解析
# ---------------------------------------------------------------------------


@dataclass
class RawBlock:
    number: str
    topic: str | None
    lines: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)


def split_blocks(lines: list[str]) -> tuple[list[RawBlock], dict[str, str]]:
    blocks: list[RawBlock] = []
    topics: dict[str, str] = {}
    current_topic: str | None = None
    page = 0
    cur: RawBlock | None = None

    for ln in lines:
        if ln.startswith(PAGE_MARK):
            page = int(ln[len(PAGE_MARK):])
            if cur and page not in cur.pages:
                cur.pages.append(page)
            continue
        m = RE_QUESTION.match(ln)
        if m:
            number, topic_no, rest = m.groups()
            if topic_no:
                current_topic = topic_no
                topics.setdefault(topic_no, f"Topic {topic_no}")
            cur = RawBlock(number=number, topic=current_topic, pages=[page])
            blocks.append(cur)
            if rest.strip():
                cur.lines.append(rest.strip())
            continue
        t = RE_TOPIC.match(ln)
        if t and len(ln) < 100:
            kind, no, title = t.groups()
            current_topic = no
            topics[no] = f"{kind.title()} {no}" + (f" – {title.strip()}" if title.strip() else "")
            continue
        if cur is not None:
            cur.lines.append(ln)
    return blocks, topics


def parse_answer_letters(raw: str) -> list[str] | None:
    raw = raw.strip().rstrip(".")
    if not raw or not RE_ANSWER_LETTERS.match(raw):
        return None
    return sorted(set(re.findall(r"[A-H]", raw.upper())))


def normalize_text(lines: list[str]) -> str:
    """合併 PDF 斷行：空行分段，其餘行接續成同一段。"""
    paras: list[str] = []
    buf: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            if buf:
                paras.append(" ".join(buf))
                buf = []
            continue
        buf.append(s)
    if buf:
        paras.append(" ".join(buf))
    return "\n\n".join(paras).strip()


@dataclass
class Parsed:
    stem_lines: list[str]
    options: list[dict]
    answer_raw: str | None
    explanation_lines: list[str]


def dissect(block: RawBlock, loose_options: bool) -> Parsed:
    stem: list[str] = []
    options: list[dict] = []
    answer_raw: str | None = None
    explanation: list[str] = []
    state = "stem"
    expected = "A"

    for ln in block.lines:
        a = RE_ANSWER.match(ln)
        if a and state != "explanation":
            answer_raw = a.group(1).strip()
            state = "answer"
            continue
        e = RE_EXPLAIN.match(ln)
        if e and state in ("answer", "options", "stem") and (answer_raw is not None or state == "answer"):
            state = "explanation"
            if e.group(1).strip():
                explanation.append(e.group(1).strip())
            continue

        if state in ("stem", "options"):
            o = RE_OPTION.match(ln) or (RE_OPTION_LOOSE.match(ln) if loose_options else None)
            # 只接受依序出現的選項字母，避免把題幹內 "A user ..." 誤判為選項
            if o and o.group(1) == expected:
                options.append({"id": o.group(1), "text": o.group(2).strip()})
                expected = chr(ord(expected) + 1)
                state = "options"
                continue
            if state == "options":
                if ln.strip():
                    options[-1]["text"] += " " + ln.strip()
                continue
            stem.append(ln)
        elif state == "answer":
            # 答案字母可能落在下一行
            if answer_raw == "" and ln.strip():
                answer_raw = ln.strip()
            else:
                explanation.append(ln)
        else:
            # 解析區內重複出現的標題（"Explanation" 後接 "Explanation/Reference:"、"Reference:"）
            # 只保留其後的內容，並另起一段
            e = RE_EXPLAIN.match(ln)
            if e:
                explanation.append("")
                if e.group(1).strip():
                    explanation.append(e.group(1).strip())
                continue
            explanation.append(ln)

    return Parsed(stem, options, answer_raw, explanation)


def detect_markers(text: str) -> list[str]:
    return [name for name, rx in COMPLEX_MARKERS.items() if rx.search(text)]


def suggest_type(text: str, markers: list[str]) -> str:
    if "CASE_STUDY" in markers and not ("HOTSPOT" in markers or "DRAG_DROP" in markers):
        return "case_study"
    if "DRAG_DROP" in markers:
        return "drag_drop_order" if RE_ORDER_HINT.search(text) else "dropdown_cloze"
    if "HOTSPOT" in markers or "ANSWER_AREA" in markers:
        return "statement_matrix" if RE_YESNO_HINT.search(text) else "dropdown_cloze"
    return "single_choice"


def skeleton(qtype: str, base: dict, parsed: Parsed) -> dict:
    """依建議題型產生待補完的題目骨架；TODO / null 欄位需校正後才能 merge。"""
    q = dict(base)
    q["type"] = qtype
    if qtype in ("single_choice", "multiple_choice"):
        q["options"] = parsed.options or [{"id": "A", "text": TODO}, {"id": "B", "text": TODO}]
        q["answer"] = TODO if qtype == "single_choice" else [TODO]
    elif qtype == "dropdown_cloze":
        q.update(
            templateType="code",
            template=f"{TODO}: 以 {{{{select_0}}}} 標示挖空位置",
            blanks=[{"id": "select_0", "options": [TODO, TODO], "answer": TODO}],
        )
    elif qtype == "drag_drop_order":
        q.update(items=[{"id": "a", "text": TODO}, {"id": "b", "text": TODO}], answer=["a", "b"])
    elif qtype == "statement_matrix":
        q["statements"] = [{"id": "s1", "text": TODO, "answer": None}]
    elif qtype == "case_study":
        q.update(
            caseInfo={"title": TODO, "sections": [{"title": "Overview", "content": TODO}]},
            questions=[],
        )
    return q


def build(blocks: list[RawBlock], has_images: dict[int, bool], pdf_name: str, loose_options: bool, id_prefix: str):
    questions: list[dict] = []
    drafts: list[dict] = []
    seen: Counter = Counter()

    for b in blocks:
        seen[(b.topic, b.number)] += 1
        qid = f"{id_prefix}{'t' + b.topic + '-' if b.topic else ''}{int(b.number):04d}"
        if seen[(b.topic, b.number)] > 1:
            qid += f"-{seen[(b.topic, b.number)]}"

        raw_text = "\n".join(b.lines).strip()
        parsed = dissect(b, loose_options)
        stem = normalize_text(parsed.stem_lines)
        explanation = normalize_text(parsed.explanation_lines)
        urls = list(dict.fromkeys(u.rstrip(".,;") for u in RE_URL.findall(explanation)))
        source = {"file": pdf_name, "number": b.number, "pages": b.pages}
        base = {
            "id": qid,
            "chapter": f"topic{b.topic}" if b.topic else "imported",
            "question": stem,
            "explanation": explanation,
            **({"references": [{"title": u, "url": u} for u in urls]} if urls else {}),
            "source": source,
        }

        reasons: list[str] = []
        markers = detect_markers(raw_text)
        reasons += markers
        letters = parse_answer_letters(parsed.answer_raw) if parsed.answer_raw is not None else None
        option_ids = {o["id"] for o in parsed.options}

        if not markers:
            if len(parsed.options) < 2:
                reasons.append("NO_OPTIONS")
            if parsed.answer_raw is None:
                reasons.append("NO_ANSWER")
            elif letters is None:
                reasons.append("ANSWER_NOT_LETTERS")
            elif not set(letters) <= option_ids:
                reasons.append("ANSWER_NOT_IN_OPTIONS")
            if not stem:
                reasons.append("EMPTY_STEM")
            want = re.search(r"\b(?:choose|select)\s+(two|three|four|2|3|4)\b", stem, re.I)
            if letters and want and len(letters) == 1:
                reasons.append("ANSWER_COUNT_MISMATCH")
        if any(has_images.get(p) for p in b.pages):
            # 有截圖時文字可能不完整，但標準選擇題仍可先收錄，僅在 draft 中提示
            if reasons:
                reasons.append("HAS_IMAGES")

        if not reasons:
            qtype = "multiple_choice" if len(letters) > 1 else "single_choice"
            q = {**base, "type": qtype, "options": parsed.options, "answer": letters if len(letters) > 1 else letters[0]}
            questions.append(order_keys(q))
        else:
            qtype = suggest_type(raw_text, markers) if markers else (
                "multiple_choice" if letters and len(letters) > 1 else "single_choice"
            )
            drafts.append(
                {
                    "id": qid,
                    "status": "needs_review",
                    "reasons": reasons,
                    "suggested_type": qtype,
                    "answer_raw": parsed.answer_raw,
                    "source": source,
                    "raw_text": raw_text,
                    "question": order_keys(skeleton(qtype, base, parsed)),
                }
            )
    return questions, drafts


KEY_ORDER = ["id", "type", "chapter", "question", "code", "options", "templateType", "template", "blanks",
             "items", "statements", "caseInfo", "questions", "answer", "explanation", "references", "source"]


def order_keys(q: dict) -> dict:
    return {k: q[k] for k in sorted(q, key=lambda k: KEY_ORDER.index(k) if k in KEY_ORDER else len(KEY_ORDER))}


# ---------------------------------------------------------------------------
# 驗證（merge 用）
# ---------------------------------------------------------------------------


def find_todos(obj, path="question") -> list[str]:
    hits: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            hits += find_todos(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += find_todos(v, f"{path}[{i}]")
    elif obj is None or (isinstance(obj, str) and TODO in obj):
        hits.append(path)
    return hits


def check_question(q: dict) -> list[str]:
    """與前端 grading.js 對應的最低限度檢查；完整格式請以 questions.schema.json 驗證。"""
    errs = [f"{p} 尚未填寫" for p in find_todos({k: v for k, v in q.items() if k not in ("explanation", "source")})]
    t = q.get("type")
    if t in ("single_choice", "multiple_choice"):
        ids = {o.get("id") for o in q.get("options", [])}
        ans = q.get("answer")
        ans_list = ans if isinstance(ans, list) else [ans]
        if t == "single_choice" and not isinstance(ans, str):
            errs.append("single_choice 的 answer 必須是字串")
        if t == "multiple_choice" and not isinstance(ans, list):
            errs.append("multiple_choice 的 answer 必須是陣列")
        if not set(ans_list) <= ids:
            errs.append(f"answer {ans_list} 不在 options 中")
    elif t == "dropdown_cloze":
        holes = set(re.findall(r"\{\{(\w+)\}\}", q.get("template", "")))
        blanks = {b["id"]: b for b in q.get("blanks", [])}
        if holes != set(blanks):
            errs.append(f"template 挖空 {sorted(holes)} 與 blanks {sorted(blanks)} 不一致")
        errs += [f"blank {b['id']} 的 answer 不在 options 中" for b in blanks.values() if b.get("answer") not in b.get("options", [])]
    elif t == "drag_drop_order":
        ids = {i["id"] for i in q.get("items", [])}
        if not set(q.get("answer", [])) <= ids:
            errs.append("answer 含有不存在的 items id")
    elif t == "statement_matrix":
        errs += [f"statement {s.get('id')} 的 answer 必須是 true/false" for s in q.get("statements", []) if not isinstance(s.get("answer"), bool)]
    elif t == "case_study":
        if not q.get("questions"):
            errs.append("case_study 至少需要一個子題")
        for sub in q.get("questions", []):
            errs += [f"子題 {sub.get('id')}: {e}" for e in check_question(sub)]
    else:
        errs.append(f"未知題型 {t}")
    return errs


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_parse(args) -> None:
    pdf = Path(args.pdf)
    lines, has_images = extract_lines(pdf, args.first_page, args.last_page, args.header_ratio)
    if args.dump_text:
        Path(args.dump_text).write_text("\n".join(lines), encoding="utf-8")
        print(f"已輸出擷取文字：{args.dump_text}")

    blocks, topics = split_blocks(lines)
    if not blocks:
        sys.exit("找不到任何題目。請用 --dump-text 檢查擷取結果，並調整 RE_QUESTION。")

    questions, drafts = build(blocks, has_images, pdf.name, args.loose_options, args.id_prefix)

    chapter_ids = {q["chapter"] for q in questions} | {d["question"].get("chapter") for d in drafts}
    chapters = [{"id": f"topic{no}", "title": title} for no, title in sorted(topics.items(), key=lambda x: int(x[0]))]
    if "imported" in chapter_ids:
        chapters.append({"id": "imported", "title": "未分類"})
    chapters = [c for c in chapters if c["id"] in chapter_ids]

    out = Path(args.output)
    bank = {
        "$schema": "./questions.schema.json",
        "id": args.bank_id or out.stem,
        "title": args.title or pdf.stem,
        "description": f"由 {pdf.name} 自動轉換",
        "chapters": chapters,
        "questions": questions,
    }
    if args.snapshots and drafts:
        render_snapshots(pdf, drafts, Path(args.snapshots), args.dpi)

    write_json(out, bank)
    drafts_path = Path(args.drafts) if args.drafts else out.with_suffix(".drafts.json")
    write_json(drafts_path, {"bank": out.name, "drafts": drafts})

    reasons = Counter(r for d in drafts for r in d["reasons"])
    print(f"題目區塊：{len(blocks)}")
    print(f"  ✔ 已收錄：{len(questions)} → {out}")
    print(f"  ✎ 待校正：{len(drafts)} → {drafts_path}")
    for r, n in reasons.most_common():
        print(f"      {r:<22}{n}")


def render_snapshots(pdf: Path, drafts: list[dict], outdir: Path, dpi: int) -> None:
    """將 draft 題目所在頁面輸出成 PNG，方便對照截圖校正。"""
    outdir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf)
    for d in drafts:
        files = []
        for p in d["source"]["pages"]:
            if 1 <= p <= doc.page_count:
                name = f"{d['id']}_p{p}.png"
                doc[p - 1].get_pixmap(dpi=dpi).save(outdir / name)
                files.append(str(outdir / name))
        d["snapshots"] = files
    doc.close()
    print(f"已輸出 draft 頁面截圖：{outdir}")


def cmd_merge(args) -> None:
    bank_path, drafts_path = Path(args.bank), Path(args.drafts)
    bank = json.loads(bank_path.read_text(encoding="utf-8"))
    store = json.loads(drafts_path.read_text(encoding="utf-8"))
    by_id = {q["id"]: i for i, q in enumerate(bank["questions"])}
    chapter_ids = {c["id"] for c in bank.get("chapters", [])}

    remaining, merged, rejected = [], 0, 0
    for d in store.get("drafts", []):
        if d.get("status") != "approved":
            remaining.append(d)
            continue
        q = d["question"]
        errs = check_question(q)
        if q.get("chapter") and q["chapter"] not in chapter_ids:
            errs.append(f"chapter {q['chapter']} 不存在於題庫 chapters")
        if errs:
            rejected += 1
            d["status"] = "needs_review"
            d["merge_errors"] = errs
            remaining.append(d)
            print(f"✘ {d['id']}：")
            for e in errs:
                print(f"    - {e}")
            continue
        d.pop("merge_errors", None)
        if q["id"] in by_id:
            bank["questions"][by_id[q["id"]]] = q
        else:
            by_id[q["id"]] = len(bank["questions"])
            bank["questions"].append(q)
        merged += 1

    if not args.dry_run:
        write_json(bank_path, bank)
        store["drafts"] = remaining
        write_json(drafts_path, store)
    print(f"併入 {merged} 題，退回 {rejected} 題，剩餘 draft {len(remaining)} 題{'（dry run，未寫檔）' if args.dry_run else ''}")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("parse", help="PDF → 題庫 JSON + Draft JSON")
    p.add_argument("pdf", help="SC-200 PDF 檔案路徑")
    p.add_argument("-o", "--output", required=True, help="輸出題庫 JSON，例如 public/data/sc200.json")
    p.add_argument("--drafts", help="Draft JSON 路徑（預設：<output>.drafts.json）")
    p.add_argument("--title", help="題庫標題（預設：PDF 檔名）")
    p.add_argument("--bank-id", help="題庫 id（預設：輸出檔名）")
    p.add_argument("--id-prefix", default="q", help="題目 id 前綴（預設：q）")
    p.add_argument("--first-page", type=int, help="起始頁（1 起算）")
    p.add_argument("--last-page", type=int, help="結束頁")
    p.add_argument("--header-ratio", type=float, default=0.5, help="出現於此比例以上頁面的行視為頁首/頁尾（預設 0.5）")
    p.add_argument("--loose-options", action="store_true", help="允許沒有標點的選項格式，例如 'A text'")
    p.add_argument("--snapshots", help="將 draft 題目所在頁面輸出為 PNG 的目錄")
    p.add_argument("--dpi", type=int, default=110, help="截圖解析度（預設 110）")
    p.add_argument("--dump-text", help="另存擷取後的純文字，方便除錯正規表示式")
    p.set_defaults(func=cmd_parse)

    m = sub.add_parser("merge", help="將 status=approved 的 draft 併入題庫")
    m.add_argument("bank", help="題庫 JSON")
    m.add_argument("drafts", help="Draft JSON")
    m.add_argument("--dry-run", action="store_true", help="只檢查，不寫檔")
    m.set_defaults(func=cmd_merge)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
