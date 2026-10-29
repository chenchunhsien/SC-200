#!/usr/bin/env python3
"""為正式題庫的系列題加上 series 欄位（只新增欄位，不改答案或其他內容）。

系列題：同一情境、每題提出不同 Solution（Does this meet the goal?）的題目。前端出題時會把同一 series 的題目
綁在一起連續出現。分組清單寫在 SERIES；寫入前逐組驗證：題號存在、為含 Solution: 的頂層單選題、同組情境文字相同、
不屬於其他系列。任何一項不通過即停止，不寫入。

寫入前先備份（verify_answers.backup_file），以原檔相同格式（縮排 2、原換行字元、結尾換行）寫回，
寫入後重新讀取確認除了 series 以外完全相同。已有相同 series 的題目不重寫；全部已存在時不寫檔。

    python scripts/add_series.py --dry-run     # 只檢查並列出將新增的欄位
    python scripts/add_series.py               # 備份後寫入
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import verify_answers as va  # noqa: E402

# 系列分組（依情境）。qd4-qs2-q013 與 qd6-qs1-q012 內容相同，依使用者決定獨立、不分組。
SERIES = {
    "series-d1-mdi-accounts": ["qd1-qs4-q010", "qd1-qs4-q011", "qd1-qs4-q012", "qd1-qs4-q022"],
    "series-d1-mde-antivirus": ["qd1-qs4-q059", "qd1-qs4-q060", "qd1-qs4-q061", "qd1-qs4-q063"],
    "series-d2-asc-alert": ["qd2-qs5-q008", "qd2-qs5-q009", "qd2-qs5-q054"],
    "series-d2-aws-linux": ["qd2-qs5-q024", "qd2-qs5-q025"],
    "series-d3-sentinel-incident": ["qd3-qs5-q006", "qd3-qs5-q007", "qd3-qs5-q008", "qd3-qs5-q022"],
    "series-d6-xdr-audit": ["qd6-qs1-q011", "qd6-qs1-q012", "qd6-qs1-q013"],
}


def scenario(text: str) -> str | None:
    """Solution: 之前的情境文字（去除系列題固定說明與空白差異）；沒有 Solution: 時回傳 None。"""
    m = re.search(r"(?is)^(.*?)\bSolution:", text or "")
    if not m:
        return None
    body = re.sub(r"(?is)Note:.*?review screen\.", "", m.group(1))
    return re.sub(r"\s+", " ", body).strip()


def plan(bank: dict, series: dict) -> tuple[dict, list[str]]:
    """回傳 ({題號: 系列代號}（需要新增者）, 問題清單)。"""
    top = {q.get("id"): q for q in bank.get("questions") or []}
    owner, problems, todo = {}, [], {}
    for name, ids in series.items():
        if len(ids) < 2:
            problems.append(f"{name}：系列至少需要 2 題")
        scen = set()
        for qid in ids:
            if qid in owner:
                problems.append(f"{qid}：同時列在 {owner[qid]} 與 {name}")
            owner[qid] = name
            q = top.get(qid)
            if q is None:
                problems.append(f"{qid}：不是正式題庫的頂層題目")
                continue
            if q.get("type") != "single_choice":
                problems.append(f"{qid}：不是單選題（{q.get('type')}）")
            s = scenario(q.get("question"))
            if s is None:
                problems.append(f"{qid}：題幹沒有 Solution:，不是系列題")
            scen.add(s)
            if q.get("series") not in (None, name):
                problems.append(f"{qid}：已屬於其他系列 {q['series']}")
            elif q.get("series") is None:
                todo[qid] = name
        if len(scen) > 1:
            problems.append(f"{name}：各題的情境文字不同，可能列錯題號")
    return todo, problems


def with_series(q: dict, name: str) -> dict:
    """在 chapter（沒有則 type）之後插入 series，其餘欄位與順序不變。"""
    anchor = "chapter" if "chapter" in q else "type"
    out = {}
    for k, v in q.items():
        out[k] = v
        if k == anchor:
            out["series"] = name
    return out


def dump(bank: dict, newline: str) -> bytes:
    return (json.dumps(bank, ensure_ascii=False, indent=2) + "\n").replace("\n", newline).encode("utf-8")


def write_bytes_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def strip_series(bank: dict) -> dict:
    b = copy.deepcopy(bank)
    for q in b["questions"]:
        q.pop("series", None)
    return b


def main(argv=None) -> int:
    va.eq.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", type=Path, default=va.DEFAULT_BANK)
    ap.add_argument("--backup-dir", type=Path, default=va.DEFAULT_OUT / "backups")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    path = args.bank.resolve()
    raw = path.read_bytes()
    bank = json.loads(raw.decode("utf-8"))
    newline = "\r\n" if b"\r\n" in raw else "\n"
    if dump(bank, newline) != raw:
        print("題庫檔的格式無法原樣重現（縮排、換行或字元跳脫不同），為避免產生無關差異，停止。")
        return 2
    todo, problems = plan(bank, SERIES)
    if problems:
        print("系列分組驗證未通過，未寫入：\n  " + "\n  ".join(problems))
        return 2
    total = sum(len(v) for v in SERIES.values())
    print(f"系列 {len(SERIES)} 組、{total} 題；需要新增 series 的題目 {len(todo)} 題")
    for qid, name in todo.items():
        print(f"  {qid} → {name}")
    if not todo:
        print("全部已標記，未寫入。")
        return 0
    if args.dry_run:
        print("--dry-run：未寫入。")
        return 0
    new = copy.deepcopy(bank)
    new["questions"] = [with_series(q, todo[q["id"]]) if q.get("id") in todo else q for q in new["questions"]]
    if strip_series(new) != strip_series(bank):
        print("內部檢查失敗：除了 series 以外的內容有變動，未寫入。")
        return 2
    backup = va.backup_file(path, args.backup_dir.resolve())
    print(f"已備份：{backup}")
    write_bytes_atomic(path, dump(new, newline))
    check = json.loads(path.read_text(encoding="utf-8"))
    if strip_series(check) != strip_series(bank) or {q["id"]: q.get("series") for q in check["questions"] if q.get("series")} != \
            {qid: name for name, ids in SERIES.items() for qid in ids}:
        print(f"寫入後驗證失敗！請以備份還原：{backup}")
        return 2
    print(f"已寫入：{path}（只新增 series 欄位；寫入後驗證通過）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
