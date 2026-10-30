#!/usr/bin/env python3
"""SC-200 題庫匯出成 PDF（仿 SC-200自我整理_Final.pdf 的排版；唯讀，不修改題庫）。

依題庫順序輸出：章節 → Testlet／Question Set → 情境題組背景 → QUESTION N → 題幹 → 選項 → Correct Answer。
- 單選／複選題的 Correct Answer 以淺灰色靠右顯示（同原 PDF，方便自我測驗）。
- 下拉填空題：題目的作答區（Hot Area）使用 --source-pdf（預設 SC-200 478题.pdf）的截圖，舊題目依 --compare
  的比對結果找到對應頁面；Correct Answer 由程式依題庫答案（含更正）填入題目範本，不使用截圖。
  找不到截圖時改用 --snapshots 的裁切截圖，再不行則整題以表格排版。
- 拖拉排序、Yes/No 題以表格重新排版。
- 答案印題庫的練習答案；與 originalAnswer（原題庫答案）不同時一併標示。reviewNote 印為備註。

先產生 HTML，再以 Microsoft Edge（headless）列印成 PDF；輸出檔已存在即停止，不覆寫。

    python scripts/export_pdf.py --bank public/data/sc200.ai.json --out output/sc200_ai.pdf
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]
BLANK = re.compile(r"\{\{(select_\d+)\}\}")

CSS = """
@page { size: A4; margin: 1.6cm 1.5cm; }
body { font-family: Calibri, "Microsoft JhengHei", sans-serif; font-size: 12pt; line-height: 1.5; color: #000; }
h1 { font-size: 16pt; margin: 0 0 4pt; break-before: page; }
h1:first-of-type { break-before: auto; }
h2 { font-size: 12pt; margin: 14pt 0 4pt; }
h3 { font-size: 12pt; margin: 10pt 0 2pt; }
.q { margin-top: 18pt; }
.qno { font-weight: bold; margin-bottom: 2pt; }
.label { font-weight: bold; margin: 8pt 0 2pt; }
p { margin: 0 0 4pt; }
ul { margin: 0 0 4pt 18pt; padding: 0; }
pre, code { font-family: Consolas, "Courier New", monospace; font-size: 10pt; }
pre { white-space: pre-wrap; background: #f4f4f4; padding: 6pt; margin: 4pt 0; }
.opt { margin: 0 0 2pt; }
.ans-gray { color: #bfbfbf; text-align: right; margin-top: 6pt; }
table { border-collapse: collapse; margin: 4pt 0; break-inside: avoid; }
td, th { border: 1px solid #999; padding: 3pt 6pt; vertical-align: top; font-size: 11pt; }
th { background: #eee; }
.blank { border: 1px solid #555; padding: 0 6pt; background: #fff; }
.correct { background: #c6e0b4; }
.note { border-left: 3px solid #f0a000; background: #fff8e5; padding: 4pt 8pt; margin-top: 6pt; font-size: 10.5pt; }
.case { border: 1px solid #ccc; padding: 6pt 10pt; margin: 6pt 0; }
.shot { break-inside: avoid; }
.new { color: #c00000; margin-left: 4pt; }
.shot img { max-width: 100%; display: block; }
"""


def rich(text: str | None) -> str:
    """題庫文字（支援 ```區塊```、`code`、**粗體**、- 清單與換行）轉 HTML。"""
    if not text:
        return ""
    out = []
    for i, chunk in enumerate(re.split(r"```[\w-]*\n?([\s\S]*?)```", text)):
        if i % 2:
            out.append(f"<pre>{html.escape(chunk.rstrip())}</pre>")
            continue
        for para in re.split(r"\n\s*\n", chunk.strip()):
            if not para:
                continue
            lines = para.split("\n")
            if all(l.startswith("- ") for l in lines):
                out.append("<ul>" + "".join(f"<li>{inline(l[2:])}</li>" for l in lines) + "</ul>")
            else:
                out.append("<p>" + "<br>".join(inline(l) for l in lines) + "</p>")
    return "".join(out)


def inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)


def fmt_answer(q: dict, ans) -> str:
    """答案的純文字表示（用於「原題庫答案」標示）。"""
    t = q["type"]
    if t == "single_choice":
        return str(ans)
    if t == "multiple_choice":
        return "".join(ans)
    if t == "drag_drop_order":
        return " → ".join(ans)
    if t == "dropdown_cloze":
        return "；".join(f"第 {i + 1} 格 {ans.get(b['id'])}" for i, b in enumerate(q["blanks"]))
    if t == "statement_matrix":
        return "、".join("Yes" if ans.get(s["id"]) else "No" for s in q["statements"])
    return json.dumps(ans, ensure_ascii=False)


def current_answer(q: dict):
    t = q["type"]
    if t == "dropdown_cloze":
        return {b["id"]: b["answer"] for b in q["blanks"]}
    if t == "statement_matrix":
        return {s["id"]: s["answer"] for s in q["statements"]}
    return q["answer"]


def original_note(q: dict) -> str:
    """練習答案與原題庫答案不同時，附上原題庫答案。"""
    if "originalAnswer" not in q:
        return ""
    if json.dumps(q["originalAnswer"], sort_keys=True) == json.dumps(current_answer(q), sort_keys=True):
        return ""
    return f"（原題庫答案：{html.escape(fmt_answer(q, q['originalAnswer']))}）"


def render_choice(q: dict) -> str:
    opts = "".join(f'<p class="opt">{o["id"]}. {inline(o["text"])}</p>' for o in q["options"])
    ans = q["answer"] if q["type"] == "single_choice" else "".join(q["answer"])
    return opts + f'<p class="ans-gray">Correct Answer: {ans}{original_note(q)}</p>'


def render_dropdown(q: dict) -> str:
    num = {b["id"]: i + 1 for i, b in enumerate(q["blanks"])}
    filled = lambda m, mark: (f'<span class="blank">{num[m.group(1)]}</span>' if not mark else
                              f'<span class="blank correct">{html.escape(next(b["answer"] for b in q["blanks"] if b["id"] == m.group(1)))}</span>')
    tpl = html.escape(q["template"])
    body = lambda mark: BLANK.sub(lambda m: filled(m, mark), tpl)
    wrap = (lambda s: f"<pre>{s}</pre>") if q.get("templateType") == "code" else (lambda s: "<p>" + s.replace("\n", "<br>") + "</p>")
    rows = "".join(f"<tr><td>{num[b['id']]}</td><td>{'<br>'.join(html.escape(o) for o in b['options'])}</td></tr>" for b in q["blanks"])
    return ('<p class="label">Hot Area:</p>' + wrap(body(False)) + f"<table><tr><th>格</th><th>選項</th></tr>{rows}</table>"
            + '<p class="label">Correct Answer:</p>' + wrap(body(True)) + (f"<p>{original_note(q)}</p>" if original_note(q) else ""))


class Screenshots:
    """下拉填空題的作答區截圖：取自題目來源 PDF（預設 SC-200 478题.pdf）。

    舊題目依 compare.json（新來源 → 舊題號的比對結果）找到在來源 PDF 的章節、題號與頁碼；新題目直接使用自己的 source。
    找不到時改用當時裁切的截圖檔（--snapshots），再不行則以表格排版。答案一律由程式依題庫填入（含更正），不用截圖。"""

    def __init__(self, source_pdf: Path | None, compare_json: Path | None, snapshot_dir: Path | None):
        import pymupdf  # 只有需要截圖時才載入
        self.pymupdf = pymupdf
        self.doc = pymupdf.open(source_pdf) if source_pdf and source_pdf.exists() else None
        self.source_name = source_pdf.name if source_pdf else None
        self.watermarks = self._watermark_xrefs() if self.doc else set()
        self.where = {}  # 舊題號 → 來源 PDF 的 {section, number, pages}
        if compare_json and compare_json.exists():
            for row in json.loads(compare_json.read_text(encoding="utf-8")).get("rows", []):
                # existing：已比對為同一題；review 且題號相同：比對分數略低，但已人工確認為同一題
                if row.get("match") and (row.get("status") == "existing"
                                         or (row.get("status") == "review" and row["match"] == row.get("new_id"))):
                    self.where[row["match"]] = row
        self.snapshots = {}
        for f in sorted(snapshot_dir.glob("*.png")) if snapshot_dir and snapshot_dir.is_dir() else []:
            qid, _, page = f.stem.rpartition("_p")
            self.snapshots.setdefault(qid, []).append((int(page) if page.isdigit() else 0, f))
        self.used = {"pdf": 0, "by_position": [], "snapshot": 0, "missing": []}

    def _watermark_xrefs(self) -> set:
        """在多數頁面重複出現的同一張圖片視為浮水印，不當作題目截圖。"""
        pages_of: dict = {}
        for page in self.doc:
            for img in page.get_images(full=True):
                pages_of.setdefault(img[0], set()).add(page.number)
        return {x for x, p in pages_of.items() if len(p) > max(10, self.doc.page_count // 4)}

    def _items(self, pages: list[int]) -> list[tuple]:
        """依閱讀順序列出題號標題、Correct Answer 標籤與圖片位置：(頁序, y, 種類, 內容)。"""
        items = []
        for order, pno in enumerate(pages):
            if not 1 <= pno <= self.doc.page_count:
                continue
            page = self.doc[pno - 1]
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    text = "".join(s["text"] for s in line["spans"]).strip()
                    y = line["bbox"][1]
                    if re.fullmatch(r"QUESTION\s+\d+", text, re.I):
                        items.append((order, y, "q", text.split()[-1]))
                    elif text.startswith("Correct Answer:") and len(text) <= len("Correct Answer:") + 1:
                        items.append((order, y, "ans", None))
            for info in page.get_image_info(xrefs=True):
                r = self.pymupdf.Rect(info["bbox"])
                if info.get("xref") not in self.watermarks and r.width > 40 and r.height > 20:
                    items.append((order, r.y0, "img", (pno, r)))
        return sorted(items, key=lambda i: (i[0], i[1]))

    def _png(self, pno: int, rect) -> str:
        page = self.doc[pno - 1]
        clip = (rect + (-4, -4, 4, 4)) & page.rect
        pix = page.get_pixmap(clip=clip, dpi=150)
        return f'<img src="data:image/png;base64,{base64.b64encode(pix.tobytes("png")).decode()}" style="width:{clip.width:.0f}pt">'

    def hot_area(self, q: dict) -> str | None:
        """作答區截圖：「Correct Answer:」之前的最後一張圖；讀不到標籤時取本題倒數第二張圖（最後一張是答案）。"""
        src = q.get("source") or {}
        loc = src if src.get("file") == self.source_name else self.where.get(q["id"])
        if not self.doc or not loc or not loc.get("pages") or not loc.get("number"):
            return None
        items = self._items(loc["pages"])
        start = next((i for i, it in enumerate(items) if it[2] == "q" and it[3] == str(loc["number"])), None)
        if start is None:
            return None
        images = []
        for it in items[start + 1:]:
            if it[2] == "q":
                break
            if it[2] == "ans":
                if not images:
                    return None
                self.used["pdf"] += 1
                return self._png(*images[-1])
            if it[2] == "img":
                images.append(it[3])
        if len(images) >= 2:
            self.used["pdf"] += 1
            self.used["by_position"].append(q["id"])
            return self._png(*images[-2])
        return None

    def from_snapshots(self, q: dict) -> str | None:
        files = sorted(self.snapshots.get(q["id"], []))
        if not files:
            return None
        self.used["snapshot"] += 1
        return "".join(f'<img src="data:image/png;base64,{base64.b64encode(f.read_bytes()).decode()}" style="width:100%">'
                       for _, f in files)


SHOTS: Screenshots | None = None


def dropdown_answer(q: dict) -> str:
    """依題庫填入答案（含更正）的題目範本，答案格以綠底標示。"""
    tpl = BLANK.sub(lambda m: f'<span class="blank correct">'
                              f'{html.escape(next(b["answer"] for b in q["blanks"] if b["id"] == m.group(1)))}</span>',
                    html.escape(q["template"]))
    body = f"<pre>{tpl}</pre>" if q.get("templateType") == "code" else "<p>" + tpl.replace("\n", "<br>") + "</p>"
    return body + (f"<p>{original_note(q)}</p>" if original_note(q) else "")


def render_dropdown_with_shots(q: dict) -> str:
    if SHOTS:
        hot = SHOTS.hot_area(q)
        if hot:  # 標籤與截圖放在同一個不分頁的區塊
            return (f'<div class="shot"><p class="label">Hot Area:</p>{hot}</div>'
                    f'<div class="shot"><p class="label">Correct Answer:</p>{dropdown_answer(q)}</div>')
        snap = SHOTS.from_snapshots(q)
        if snap:
            return ('<p class="label">原題截圖：</p>' + snap
                    + f'<div class="shot"><p class="label">Correct Answer:</p>{dropdown_answer(q)}</div>')
        SHOTS.used["missing"].append(q["id"])
    return render_dropdown(q)


def render_drag(q: dict) -> str:
    items = {i["id"]: i["text"] for i in q["items"]}
    actions = "".join(f"<tr><td>{inline(t)}</td></tr>" for t in items.values())
    answer = "".join(f"<tr><td>{n}</td><td>{inline(items[a])}</td></tr>" for n, a in enumerate(q["answer"], 1))
    return ('<p class="label">Select and Place:</p>' + f"<table><tr><th>Actions</th></tr>{actions}</table>"
            + '<p class="label">Correct Answer:</p>' + f"<table><tr><th></th><th>Answer Area</th></tr>{answer}</table>"
            + (f"<p>{original_note(q)}</p>" if original_note(q) else ""))


def render_matrix(q: dict) -> str:
    yes, no = (q.get("columns") or ["Yes", "No"])[:2]
    head = f"<tr><th>Statements</th><th>{yes}</th><th>{no}</th></tr>"
    blank = "".join(f"<tr><td>{inline(s['text'])}</td><td>○</td><td>○</td></tr>" for s in q["statements"])
    mark = lambda on: '<td class="correct">●</td>' if on else "<td>○</td>"
    filled = "".join(f"<tr><td>{inline(s['text'])}</td>{mark(s['answer'])}{mark(not s['answer'])}</tr>" for s in q["statements"])
    return ('<p class="label">Hot Area:</p>' + f"<table>{head}{blank}</table>" + '<p class="label">Correct Answer:</p>'
            + f"<table>{head}{filled}</table>" + (f"<p>{original_note(q)}</p>" if original_note(q) else ""))


RENDER = {"single_choice": render_choice, "multiple_choice": render_choice, "dropdown_cloze": render_dropdown_with_shots,
          "drag_drop_order": render_drag, "statement_matrix": render_matrix}


NEW_SOURCE_PREFIX = "SC-200 478"  # 2026-10-30 由新來源併入的題目，題號後標示「新增」


def render_question(q: dict, number) -> str:
    is_new = (q.get("source") or {}).get("file", "").startswith(NEW_SOURCE_PREFIX)
    tag = '<span class="new">（新增）</span>' if is_new else ""
    parts = [f'<div class="q"><div class="qno">QUESTION {number}{tag}</div>', rich(q.get("question"))]
    if q.get("code"):
        parts.append(f"<pre>{html.escape(q['code'])}</pre>")
    parts.append(RENDER[q["type"]](q))
    if q.get("reviewNote"):
        parts.append(f'<div class="note">備註：{inline(q["reviewNote"])}</div>')
    return "".join(parts) + "</div>"


def build_html(bank: dict) -> tuple[str, int]:
    chapters = {c["id"]: c["title"] for c in bank.get("chapters", [])}
    out, chapter, section, count = [], None, None, 0
    for q in bank["questions"]:
        src = q.get("source") or {}
        if q.get("chapter") != chapter:
            chapter, section = q.get("chapter"), None
            out.append(f"<h1>{html.escape(chapters.get(chapter, chapter or ''))}</h1>")
        if q["type"] == "case_study":
            section = None
            out.append(f"<h2>{html.escape(src.get('section') or q['caseInfo'].get('title', 'Case Study'))}</h2>")
            info = q["caseInfo"]
            if info.get("title"):
                out.append(f"<h3>{html.escape(info['title'])}</h3>")
            out.append('<div class="case">' + "".join(f"<h3>{html.escape(s['title'])}</h3>{rich(s['content'])}"
                                                    for s in info["sections"]) + "</div>")
            if q.get("question"):
                out.append(rich(q["question"]))
            for i, sub in enumerate(q["questions"], 1):
                out.append(render_question(sub, (sub.get("source") or {}).get("number") or i))
                count += 1
            continue
        if src.get("section") != section:
            section = src.get("section")
            out.append(f"<h2>{html.escape(section or '')}</h2>")
        out.append(render_question(q, src.get("number") or "?"))
        count += 1
    title = html.escape(bank.get("title", "SC-200"))
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title><style>{CSS}</style></head>'
            f"<body>{''.join(out)}</body></html>"), count


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", type=Path, default=BASE_DIR / "public" / "data" / "sc200.ai.json")
    ap.add_argument("--out", type=Path, default=BASE_DIR / "output" / "sc200_ai.pdf")
    ap.add_argument("--source-pdf", type=Path, default=BASE_DIR / "SC-200 478题.pdf",
                    help="下拉題作答區截圖的來源 PDF")
    ap.add_argument("--compare", type=Path,
                    default=BASE_DIR / "output" / "new_bank_478" / "compare_final" / "compare.json",
                    help="compare_banks.py 的比對結果：舊題號 → 來源 PDF 的章節、題號與頁碼")
    ap.add_argument("--snapshots", type=Path, default=BASE_DIR / "output" / "new_bank_478" / "snapshots",
                    help="備用截圖目錄（檔名 <題號>_p<頁碼>.png）")
    ap.add_argument("--no-screenshots", action="store_true", help="下拉題不放截圖，一律以表格排版")
    ap.add_argument("--html-only", action="store_true", help="只產生 HTML，不列印 PDF")
    args = ap.parse_args(argv)
    global SHOTS
    if not args.no_screenshots:
        SHOTS = Screenshots(args.source_pdf.resolve(), args.compare.resolve(), args.snapshots.resolve())
    out_pdf = args.out.resolve()
    out_html = out_pdf.with_suffix(".html")
    for p in ([out_html] if args.html_only else [out_html, out_pdf]):
        if p.exists():
            print(f"輸出檔已存在，停止，不覆寫：{p}")
            return 2
    bank = json.loads(args.bank.read_text(encoding="utf-8"))
    doc, count = build_html(bank)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(doc, encoding="utf-8")
    print(f"已產生 HTML：{out_html}（{count} 題）")
    if SHOTS:
        u = SHOTS.used
        print(f"下拉題作答區截圖：來源 PDF {u['pdf']} 題、備用截圖檔 {u['snapshot']} 題、找不到截圖改用表格 {len(u['missing'])} 題"
              + (f"：{', '.join(u['missing'])}" if u["missing"] else ""))
        if u.get("by_position"):
            print(f"  其中 {len(u['by_position'])} 題讀不到 Correct Answer 標籤，依「本題最後兩張圖」判定，建議抽查")
    if args.html_only:
        return 0
    edge = next((p for p in EDGE_CANDIDATES if p.exists()), None) or (shutil.which("msedge") and Path(shutil.which("msedge")))
    if not edge:
        print("找不到 Microsoft Edge，請用瀏覽器開啟 HTML 後列印成 PDF")
        return 1
    subprocess.run([str(edge), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={out_pdf}", out_html.as_uri()], check=True, timeout=300,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not out_pdf.exists():
        print("Edge 沒有產生 PDF")
        return 1
    print(f"已產生 PDF：{out_pdf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
