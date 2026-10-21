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
# 題組標題：Testlet = 情境題組；Question Set / Topic = 一般題目。上一行通常是章節（domain）標題
RE_GROUP = re.compile(r"^\s*(Testlet|Question Set|Topic)\s*(\d+)\b\s*[,:.\-–—]?\s*(.*)$", re.I)
# 沒有 Testlet 標題時，以單獨一行的 "Overview" 判定情境題組開始
RE_CASE_START = re.compile(r"^\s*Overview\s*$", re.I)
# 情境背景的主分頁標題
CASE_TABS = {"overview", "existing environment", "requirements"}
# PDF 清單符號（常被擷取成 U+FFFD + "E"）
RE_BULLET = re.compile(r"^(?:�\s*E|[•●▪◦■])\s*")
# 題幹提到表格 / 圖片，但內容多半是截圖、無法擷取文字
RE_FIGURE = re.compile(
    r"\bfollowing (?:table|exhibit|diagram|image|graphic|figure|screenshot|configuration|output)s?\b"
    r"|\bshown in the exhibit\b|\bClick the Exhibit\b",
    re.I,
)
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
    lines: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)


@dataclass
class Group:
    """PDF 中的一段題目：情境題組（Testlet）或一般題目集（Question Set / Topic）。"""

    kind: str  # "case" | "set"
    label: str  # 例如 "Testlet 1"、"Question Set 4"
    chapter: str  # chapters[].id
    background: list[str] = field(default_factory=list)  # 情境背景（僅 case）
    pages: list[int] = field(default_factory=list)
    blocks: list[RawBlock] = field(default_factory=list)


def looks_like_heading(s: str) -> bool:
    s = s.strip()
    return (
        0 < len(s) <= 80
        and s[0].isupper()
        and not s.endswith((".", ":", "?", ",", ";"))
        and not any(rx.match(s) for rx in (RE_QUESTION, RE_ANSWER, RE_OPTION, RE_GROUP))
    )


def split_structure(lines: list[str]) -> tuple[list[Group], list[dict]]:
    """切分題組與題目。

    章節（domain）取自 "Testlet N" / "Question Set N" / 情境 "Overview" 上一行的標題，
    例如 "Mitigate threats using Microsoft Sentinel"。
    """
    chapters: dict[str, str] = {}  # title -> id
    groups: list[Group] = []
    page = 0
    group: Group | None = None
    cur: RawBlock | None = None
    chapter: str | None = None
    last: tuple[list[str], int] | None = None  # 最後一個非空內容行的位置
    preamble: list[str] = []

    def take_heading() -> str | None:
        """標題行在遇到 Testlet 等標記前已被收進上一題，這裡把它移出。"""
        nonlocal last
        if last is None:
            return None
        lst, i = last
        if i < len(lst) and looks_like_heading(lst[i]):
            last = None
            return lst.pop(i).strip()
        return None

    def start_group(kind: str, label: str, heading: str | None) -> None:
        nonlocal group, cur, chapter
        if heading:
            chapter = chapters.setdefault(heading, f"d{len(chapters) + 1}")
        elif chapter is None:
            chapter = chapters.setdefault("未分類", f"d{len(chapters) + 1}")
        group = Group(kind, label, chapter, pages=[page])
        groups.append(group)
        cur = None

    for ln in lines:
        if ln.startswith(PAGE_MARK):
            page = int(ln[len(PAGE_MARK):])
            for obj in (group, cur):
                if obj is not None and page not in obj.pages:
                    obj.pages.append(page)
            continue
        s = ln.strip()

        g = RE_GROUP.match(ln)
        if g and len(s) < 100:
            kind, no, title = g.groups()
            heading = take_heading()
            if not heading and kind.lower() == "topic":
                heading = f"Topic {no}" + (f" – {title.strip()}" if title.strip() else "")
            start_group("case" if kind.lower() == "testlet" else "set", f"{kind.title()} {no}", heading)
            continue

        # 沒有 "Testlet N" 標題、直接以 Overview 開始的情境題組
        if RE_CASE_START.match(ln) and not (group and group.kind == "case" and not group.blocks):
            start_group("case", "Testlet", take_heading())
            group.background.append(ln)
            continue

        m = RE_QUESTION.match(ln)
        if m:
            number, topic_no, rest = m.groups()
            if topic_no and (group is None or group.label != f"Topic {topic_no}"):
                start_group("set", f"Topic {topic_no}", f"Topic {topic_no}")
            elif group is None:
                start_group("set", "", None)
            cur = RawBlock(number=number, pages=[page])
            group.blocks.append(cur)
            if rest.strip():
                cur.lines.append(rest.strip())
            continue

        # 第一個題組之前的內容（封面等）放入 preamble，僅供取得章節標題
        holder = cur.lines if cur is not None else group.background if group is not None else preamble
        holder.append(ln)
        if s:
            last = (holder, len(holder) - 1)

    # 未標號的 Testlet：依同章節下一個 Testlet 的編號推算
    for i, g in enumerate(groups):
        if g.kind == "case" and g.label == "Testlet":
            nxt = next((n for n in groups[i + 1:] if n.chapter == g.chapter and n.kind == "case"), None)
            m = re.match(r"Testlet (\d+)", nxt.label) if nxt else None
            g.label = f"Testlet {int(m.group(1)) - 1}" if m and int(m.group(1)) > 1 else "Testlet"

    return groups, [{"id": cid, "title": title} for title, cid in chapters.items()]


def format_rich(lines: list[str]) -> str:
    """把情境背景的 PDF 行轉為 RichText：合併斷行、保留清單與小標題，並標出需補的表格。"""
    entries: list[list[str]] = []  # [kind, text]，kind = para | bullet | heading
    prev_blank = True
    for ln in lines:
        s = ln.strip()
        if not s:
            prev_blank = True
            continue
        b = RE_BULLET.match(s)
        if b:
            entries.append(["bullet", s[b.end():].strip()])
            prev_blank = False
            continue
        if entries and entries[-1] == ["bullet", ""]:  # 清單符號與內容分在兩行
            entries[-1][1] = s
            continue
        after_sentence = not entries or entries[-1][1].endswith((".", ":"))
        if (prev_blank or after_sentence) and looks_like_heading(s) and len(s.split()) <= 7:
            entries.append(["heading", s])
        # 句子未結束（常見於跨頁斷行）時，即使中間有空行也接回同一段
        elif entries and entries[-1][0] in ("para", "bullet") and (
            not prev_blank or not entries[-1][1].endswith((".", ":", "?", "!"))
        ):
            entries[-1][1] += " " + s
        else:
            entries.append(["para", s])
        prev_blank = False

    out: list[str] = []
    prev = None
    for kind, text in entries:
        piece = f"**{text}**" if kind == "heading" else f"- {text}" if kind == "bullet" else text
        if out:
            tight = prev == "heading" or (kind == "bullet" and prev == "bullet")
            out.append("\n" if tight else "\n\n")
        out.append(piece)
        if RE_FIGURE.search(text):
            out.append(f"\n\n{TODO}: 依 PDF 截圖補上表格／圖片內容")
        prev = kind
    return "".join(out).strip()


def case_sections(lines: list[str]) -> list[dict]:
    """依 Overview / Existing Environment / Requirements 等主標題切成左欄分頁。"""
    sections: list[dict] = []
    buf: list[str] = []
    title = "Overview"
    for ln in lines:
        if ln.strip().lower() in CASE_TABS:
            if any(x.strip() for x in buf):
                sections.append({"title": title, "content": format_rich(buf)})
            title, buf = ln.strip(), []
            continue
        buf.append(ln)
    if any(x.strip() for x in buf):
        sections.append({"title": title, "content": format_rich(buf)})
    return sections or [{"title": "Overview", "content": TODO}]


def case_title(sections: list[dict], label: str) -> str:
    first = sections[0]["content"] if sections else ""
    m = re.match(r"(?:A company named\s+)?(.{2,40}?)\s+(?:is|has)\b", first)
    return f"{label} · {m.group(1).strip()}" if m else label


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


def analyze_block(b: RawBlock, qid: str, source: dict, has_images: dict[int, bool], loose_options: bool) -> dict:
    """解析單一題目；reasons 為空代表可直接收錄，否則 question 為待補完的骨架。"""
    raw_text = "\n".join(b.lines).strip()
    parsed = dissect(b, loose_options)
    stem = normalize_text(parsed.stem_lines)
    explanation = normalize_text(parsed.explanation_lines)
    urls = list(dict.fromkeys(u.rstrip(".,;") for u in RE_URL.findall(explanation)))
    base = {
        "id": qid,
        "question": stem,
        "explanation": explanation,
        **({"references": [{"title": u, "url": u} for u in urls]} if urls else {}),
        "source": source,
    }

    markers = detect_markers(raw_text)
    reasons: list[str] = list(markers)
    letters = parse_answer_letters(parsed.answer_raw) if parsed.answer_raw is not None else None
    option_ids = {o["id"] for o in parsed.options}
    page_has_images = any(has_images.get(p) for p in b.pages)

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
        # 題幹引用表格 / 圖片（或出現截圖造成的大段空白），文字題幹不完整
        blank_run = re.search(r"\n\s*\n\s*\n\s*\n", "\n".join(parsed.stem_lines))
        if page_has_images and (RE_FIGURE.search(stem) or blank_run):
            reasons.append("STEM_IMAGE")
    if reasons and page_has_images:
        reasons.append("HAS_IMAGES")

    if not reasons:
        qtype = "multiple_choice" if len(letters) > 1 else "single_choice"
        question = {**base, "type": qtype, "options": parsed.options, "answer": letters if len(letters) > 1 else letters[0]}
    else:
        qtype = suggest_type(raw_text, markers) if markers else (
            "multiple_choice" if letters and len(letters) > 1 else "single_choice"
        )
        question = skeleton(qtype, base, parsed)
    return {
        "reasons": reasons,
        "suggested_type": qtype,
        "answer_raw": parsed.answer_raw,
        "raw_text": raw_text,
        "question": order_keys(question),
    }


def build(groups: list[Group], has_images: dict[int, bool], pdf_name: str, loose_options: bool, id_prefix: str):
    questions: list[dict] = []
    drafts: list[dict] = []
    used: Counter = Counter()
    index = 0  # 在 PDF 中的出現順序，題庫一律依此排序

    def unique(qid: str) -> str:
        used[qid] += 1
        return qid if used[qid] == 1 else f"{qid}-{used[qid]}"

    for g in groups:
        if not g.blocks:
            continue
        slug = re.sub(r"[^a-z0-9]", "", "".join(w[0] if w.isalpha() else w for w in g.label.lower().split()))
        prefix = f"{id_prefix}{g.chapter}{'-' + slug if slug else ''}"

        if g.kind != "case":
            for b in g.blocks:
                index += 1
                source = {"file": pdf_name, "index": index, "section": g.label, "number": b.number, "pages": b.pages}
                r = analyze_block(b, unique(f"{prefix}-q{int(b.number):03d}"), source, has_images, loose_options)
                q = {**r["question"], "chapter": g.chapter}
                if not r["reasons"]:
                    questions.append(order_keys(q))
                else:
                    drafts.append({"id": q["id"], "status": "needs_review", **{k: r[k] for k in ("reasons", "suggested_type", "answer_raw")},
                                   "source": source, "raw_text": r["raw_text"], "question": order_keys(q)})
            continue

        # 情境題組：背景 + 子題整組處理
        index += 1
        case_id = unique(prefix)
        sections = case_sections(g.background)
        subs = []
        for b in g.blocks:
            source = {"file": pdf_name, "section": g.label, "number": b.number, "pages": b.pages}
            r = analyze_block(b, unique(f"{case_id}-q{int(b.number)}"), source, has_images, loose_options)
            subs.append(r)
        reasons = {"CASE_STUDY"} | {x for r in subs for x in r["reasons"]}
        if any(TODO in s["content"] for s in sections):
            reasons.add("BACKGROUND_FIGURE")
        source = {"file": pdf_name, "index": index, "section": g.label, "pages": g.pages}
        case = {
            "id": case_id,
            "type": "case_study",
            "chapter": g.chapter,
            "question": "This is a case study. 請先閱讀左側的情境背景，再回答右側各子題。",
            "caseInfo": {"title": case_title(sections, g.label), "sections": sections},
            "questions": [r["question"] for r in subs],
            "source": source,
        }
        if reasons == {"CASE_STUDY"}:
            questions.append(order_keys(case))
        else:
            drafts.append(
                {
                    "id": case_id,
                    "status": "needs_review",
                    "reasons": sorted(reasons),
                    "suggested_type": "case_study",
                    "source": source,
                    "raw_text": "\n".join(g.background).strip(),
                    "subs": [
                        {"id": r["question"]["id"], **{k: r[k] for k in ("reasons", "suggested_type", "answer_raw", "raw_text")}}
                        for r in subs
                    ],
                    "question": order_keys(case),
                }
            )
    return questions, drafts


KEY_ORDER = ["id", "type", "chapter", "question", "code", "options", "templateType", "template", "blanks",
             "items", "statements", "caseInfo", "questions", "answer", "explanation", "reviewNote", "references", "source"]


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

    groups, chapters = split_structure(lines)
    total = sum(len(g.blocks) for g in groups)
    if not total:
        sys.exit("找不到任何題目。請用 --dump-text 檢查擷取結果，並調整 RE_QUESTION。")

    questions, drafts = build(groups, has_images, pdf.name, args.loose_options, args.id_prefix)

    chapter_ids = {q["chapter"] for q in questions} | {d["question"]["chapter"] for d in drafts}
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
    cases = sum(1 for g in groups if g.kind == "case" and g.blocks)
    print(f"題目：{total}（其中 {cases} 組情境題組共 {sum(len(g.blocks) for g in groups if g.kind == 'case')} 題）")
    print(f"  ✔ 已收錄：{len(questions)} 筆 → {out}")
    print(f"  ✎ 待校正：{len(drafts)} 筆 → {drafts_path}（情境題組以整組為一筆）")
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

    # 維持與 PDF 相同的題目順序（沒有 source.index 的手動題目排在最後）
    bank["questions"].sort(key=lambda q: q.get("source", {}).get("index", sys.maxsize))

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
