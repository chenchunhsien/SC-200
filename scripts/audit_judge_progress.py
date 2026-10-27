#!/usr/bin/env python3
"""SC-200 Gemini 判讀進度離線盤點（唯讀）。

只讀取正式題庫、一個判讀工作區（--workspace：其中的 gemini_judgments.json、gemini_judge/packets/、
verification.json），把盤點結果寫入一個全新的報告目錄（--out，必須不存在）。
不建立 Gemini client、不讀取 API 金鑰、不發送任何網路請求、不修改任何輸入檔案。

分母：正式題庫的獨立判讀單位（enrich_questions.iter_units()：一般根題各 1 個單位；案例題本身不計，
其子題各 1 個單位）。單位題號重複時以不重複題號計數，重複者列為 review_required。

主要狀態（互斥，依下列優先順序判定，先符合者為準）：
  1. review_required  紀錄結構異常或彼此矛盾（紀錄不是物件、runs 不是清單、缺 status、
                      status=done 卻沒有成功核對結果、離線重核的來源索引無效、題號重複）。
  2. completed        工作區最新證據包驗證通過，且有符合 gemini_judge.completed_run() 的紀錄
                      （API 呼叫、done、核對成功、有最終推薦，證據包雜湊／模型／prompt 版本／判定版本一致）。
  3. blocked          題目資料不完整，或缺少可用證據包（不存在、無法讀取、雜湊不符、題號不符、
                      dry-run 後題庫或題庫答案已變更），或工作區未通過 --run 前置檢查而無法補跑。
  4. needs_rejudge    證據包可用，但只有與目前條件不一致的成功紀錄（舊版本、其他模型、舊證據包，
                      或僅有離線重核）。需以 --run --force 重新判定。
  5. failed_retryable 證據包可用，只有失敗紀錄（API 失敗、無效輸出、無效答案等）。
  6. pending          證據包可用，尚無任何判讀紀錄。

completed 只代表「有符合目前設定的有效判讀紀錄」，不代表答案已被獨立證實正確；模型自評信心不採用。

用法：
    python scripts/audit_judge_progress.py --workspace output/gemini_trial_20261027_01 \\
        --out output/gemini_progress_audit_20261027_01
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import gemini_judge as gj  # noqa: E402 — 只使用其唯讀函式，不建立 client

va, eq = gj.va, gj.eq

STATUSES = ("completed", "pending", "failed_retryable", "needs_rejudge", "blocked", "review_required")
DIFFERENCES_FROM_RUN = [
    "盤點以 --model（預設 gemini-3.1-flash-lite）判斷模型一致性；--run 以當次指定的 --model 判斷。",
    "盤點即使工作區未通過 --run 前置檢查，仍會把已有一致成功紀錄的題目計為 completed；--run 則會在前置檢查失敗時直接拒絕執行。",
    "status=done 卻沒有成功核對結果的紀錄，盤點列為 review_required；completed_run() 只會略過這類紀錄。",
    "盤點對所有題目分類；--run 只處理 --ids 指定的題目（每次最多 5 題）。",
]


def load_json_checked(path: Path) -> tuple[object, list[str]]:
    """讀取 JSON，並記錄任何重複的物件鍵（標準 json 會靜默保留最後一個）。"""
    dups: list[str] = []

    def hook(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                dups.append(k)
            obj[k] = v
        return obj

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=hook), dups


def unit_problem(unit) -> str | None:
    """題目資料是否足以判讀（題型受支援、有題幹、題庫答案可依題型解析）。"""
    if unit.type not in gj.ANSWER_FORMATS:
        return f"不支援的題型 {unit.type}"
    if not isinstance(unit.question.get("question"), str) or not unit.question["question"].strip():
        return "缺少題幹"
    try:
        va.parse_answer(unit, json.dumps(va.answer_of(unit)))
    except (ValueError, KeyError, TypeError) as err:
        return f"題庫答案或選項結構不完整（{type(err).__name__}）"
    return None


def packet_state(packet_dir: Path, unit, bank_sha: str) -> dict:
    """工作區中該題最新證據包（與 --run 使用同一份）的狀態。"""
    p = gj.latest_packet(packet_dir, unit.question_id) if packet_dir.is_dir() else None
    if p is None:
        return {"status": "missing", "file": None, "sha256": None}
    info = {"file": p.name, "sha256": None}
    try:
        packet, sha, ok = gj.load_packet(p)
    except (OSError, ValueError, KeyError, TypeError) as err:
        return {**info, "status": "unreadable", "detail": type(err).__name__}
    info["sha256"] = sha
    if not ok:
        return {**info, "status": "hash_mismatch"}
    if packet.get("question_id") != unit.question_id:
        return {**info, "status": "question_mismatch"}
    if packet.get("bank_sha256") != bank_sha:
        return {**info, "status": "stale_bank"}
    if (packet.get("bank_answer") or {}).get("value") != va.answer_of(unit):
        return {**info, "status": "bank_answer_changed"}
    return {**info, "status": "ok"}


PACKET_REASONS = {"missing": "缺少證據包（需先 dry-run）", "unreadable": "證據包無法讀取", "hash_mismatch": "證據包雜湊不符",
                  "question_mismatch": "證據包題號不符", "stale_bank": "dry-run 後題庫已變更（需重新 dry-run）",
                  "bank_answer_changed": "dry-run 後題庫答案已變更（需重新 dry-run）"}


def run_anomalies(entry) -> list[str]:
    if not isinstance(entry, dict):
        return ["判讀紀錄不是物件"]
    runs = entry.get("runs")
    if not isinstance(runs, list):
        return ["runs 不是清單"]
    out = []
    for i, r in enumerate(runs):
        if not isinstance(r, dict):
            out.append(f"第 {i + 1} 筆紀錄不是物件")
            continue
        if "status" not in r:
            out.append(f"第 {i + 1} 筆紀錄缺少 status")
        ev = r.get("evaluation")
        if r.get("status") == "done" and not (isinstance(ev, dict) and ev.get("status") == "evaluated"):
            out.append(f"第 {i + 1} 筆紀錄 status=done 但沒有成功核對結果")
        if r.get("kind") == "offline_reevaluation":
            src = r.get("source_run_index")
            if not isinstance(src, int) or not 0 <= src < len(runs) or not isinstance(runs[src], dict) \
                    or runs[src].get("api_called") is False:
                out.append(f"第 {i + 1} 筆離線重核紀錄的來源索引無效")
    return out


def classify(unit, entry, pkt: dict, model: str, unit_err: str | None, dup: bool, workspace_problems: list[str]) -> dict:
    runs = entry.get("runs") if isinstance(entry, dict) and isinstance(entry.get("runs"), list) else []
    runs_ok = [r for r in runs if isinstance(r, dict)]
    detail = {
        "id": unit.question_id, "type": unit.type, "root_id": unit.root_id, "case_sub_question": unit.case is not None,
        "packet": pkt, "run_count": len(runs),
        "runs": [{"index": i, "kind": r.get("kind") or "legacy(未標示)", "status": r.get("status"),
                  "prompt_version": r.get("prompt_version"), "evaluator_version": (r.get("evaluation") or {}).get("evaluator_version")
                  if isinstance(r.get("evaluation"), dict) else None, "model": r.get("model"),
                  "packet_sha256": r.get("packet_sha256"),
                  "final_source": (r.get("final_exam_recommendation") or {}).get("source")
                  if isinstance(r.get("final_exam_recommendation"), dict) else None}
                 for i, r in enumerate(runs) if isinstance(r, dict)],
        "effective_run_index": gj.effective_run_index(runs_ok) if runs_ok and len(runs_ok) == len(runs) else None,
        "completed_run_index": None, "reasons": [],
    }
    anomalies = (["題號重複（題庫中出現多次）"] if dup else []) + (run_anomalies(entry) if entry is not None else [])
    if anomalies:
        detail.update(status="review_required", reasons=anomalies)
        return detail
    if unit_err:
        detail.update(status="blocked", reasons=[f"題目資料不完整：{unit_err}"])
        return detail
    if pkt["status"] == "ok":
        done = gj.completed_run(runs, pkt["sha256"], model)
        if done is not None:
            detail.update(status="completed", completed_run_index=runs.index(done),
                          reasons=[f"第 {runs.index(done) + 1} 筆紀錄符合目前設定（{gj.PROMPT_VERSION}／{gj.EVALUATOR_VERSION}／{model}）"])
            return detail
    if pkt["status"] != "ok":
        detail.update(status="blocked", reasons=[PACKET_REASONS.get(pkt["status"], pkt["status"])])
        return detail
    successes = [r for r in runs if isinstance(r.get("evaluation"), dict) and r["evaluation"].get("status") == "evaluated"]
    if successes:
        why = set()
        for r in successes:
            if r.get("kind") != "api_call" or r.get("api_called") is not True:
                why.add("僅有離線重核或未標示類型的舊紀錄" if r.get("kind") != "api_call" else "紀錄未標示 api_called")
            if r.get("prompt_version") != gj.PROMPT_VERSION:
                why.add(f"prompt 版本 {r.get('prompt_version')} ≠ {gj.PROMPT_VERSION}")
            if r["evaluation"].get("evaluator_version") != gj.EVALUATOR_VERSION:
                why.add(f"判定版本 {r['evaluation'].get('evaluator_version')} ≠ {gj.EVALUATOR_VERSION}")
            if r.get("model") != model:
                why.add(f"模型 {r.get('model')} ≠ {model}")
            if r.get("packet_sha256") != pkt["sha256"]:
                why.add("成功紀錄使用的證據包與目前最新證據包不同")
            if not isinstance(r.get("final_exam_recommendation"), dict):
                why.add("成功紀錄缺少最終推薦（舊版）")
        detail.update(status="needs_rejudge", reasons=sorted(why) or ["成功紀錄不符合目前條件"])
        return detail
    if workspace_problems:
        detail.update(status="blocked", reasons=["工作區未通過 --run 前置檢查：" + "；".join(workspace_problems)])
        return detail
    if runs:
        detail.update(status="failed_retryable",
                      reasons=["只有非成功紀錄：" + "、".join(str(r.get("status")) for r in runs)])
        return detail
    detail.update(status="pending", reasons=["證據包可用，尚無判讀紀錄"])
    return detail


def audit(bank_path: Path, workspace: Path, model: str) -> dict:
    bank, bank_dups = load_json_checked(bank_path)
    bank_sha = eq.sha256_file(bank_path)
    units = list(eq.iter_units(bank))
    id_count = collections.Counter(u.question_id for u in units)
    results_path = workspace / "gemini_judgments.json"
    results_dups: list[str] = []
    results, results_error = {"questions": {}}, None
    if results_path.exists():
        try:
            results, results_dups = load_json_checked(results_path)
            if not isinstance(results, dict) or not isinstance(results.get("questions"), dict):
                raise ValueError("缺少 questions 物件")
        except (OSError, UnicodeDecodeError, ValueError) as err:
            results_error = f"{type(err).__name__}: {err}"[:200]
            results = {"questions": {}}
    workspace_problems = gj.run_preflight(workspace)
    packet_dir = workspace / "gemini_judge" / "packets"
    seen, details = set(), []
    for u in units:
        if u.question_id in seen:
            continue
        seen.add(u.question_id)
        entry = results["questions"].get(u.question_id)
        dup = id_count[u.question_id] > 1 or u.question_id in results_dups
        d = classify(u, entry, packet_state(packet_dir, u, bank_sha), model, unit_problem(u), dup, workspace_problems)
        if results_error:
            d.update(status="review_required", reasons=[f"判讀結果檔無法可靠讀取：{results_error}"] + d["reasons"])
        details.append(d)
    orphans = sorted(set(results["questions"]) - seen)
    counts = collections.Counter(d["status"] for d in details)
    return {
        "generated_at": va.now_iso(), "bank": {"path": str(bank_path), "sha256": bank_sha, "root_questions": len(bank["questions"]),
                                               "case_studies": sum(1 for q in bank["questions"] if q.get("type") == "case_study"),
                                               "case_sub_questions": sum(1 for u in units if u.case is not None),
                                               "unit_entries": len(units), "unique_units": len(seen),
                                               "duplicate_unit_ids": sorted(k for k, v in id_count.items() if v > 1),
                                               "duplicate_json_keys": sorted(set(bank_dups))},
        "workspace": {"path": str(workspace), "results_file_exists": results_path.exists(), "results_error": results_error,
                      "results_duplicate_keys": sorted(set(results_dups)), "preflight_problems": workspace_problems,
                      "packet_files": len(list(packet_dir.glob("*.json"))) if packet_dir.is_dir() else 0},
        "settings": {"model": model, "prompt_version": gj.PROMPT_VERSION, "evaluator_version": gj.EVALUATOR_VERSION},
        "counts": {s: counts.get(s, 0) for s in STATUSES}, "total": len(details),
        "orphan_record_ids": orphans, "differences_from_run_skip": DIFFERENCES_FROM_RUN, "units": details,
    }


def other_workspaces(project: Path, workspace: Path) -> list[dict]:
    out = []
    for f in sorted((project / "output").glob("**/gemini_judgments.json")):
        if f.parent.resolve() == workspace.resolve():
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            runs = [r for v in d.get("questions", {}).values() for r in v.get("runs", []) if isinstance(r, dict)]
            vers = collections.Counter((r.get("prompt_version"), (r.get("evaluation") or {}).get("evaluator_version")
                                        if isinstance(r.get("evaluation"), dict) else None) for r in runs)
            out.append({"path": str(f.parent.relative_to(project)), "questions": len(d.get("questions", {})), "runs": len(runs),
                        "versions": {f"{p}/{e}": n for (p, e), n in vers.items()}})
        except (OSError, ValueError, AttributeError) as err:
            out.append({"path": str(f.parent.relative_to(project)), "error": type(err).__name__})
    return out


def pct(n: int, d: int) -> str:
    return f"{n / d * 100:.1f}%" if d else "—"


def render_summary(rep: dict, others: list[dict]) -> str:
    c, t, b = rep["counts"], rep["total"], rep["bank"]
    packet_ok = sum(1 for u in rep["units"] if u["packet"]["status"] == "ok")
    reasons = collections.Counter(r for u in rep["units"] if u["status"] in ("blocked", "review_required", "needs_rejudge")
                                  for r in u["reasons"])
    L = ["# SC-200 Gemini 判讀進度離線盤點", "",
         f"> 產生時間：{rep['generated_at']}｜工作區：`{rep['workspace']['path']}`｜離線產生，未呼叫任何 API，未修改任何輸入檔案。",
         "> completed 只代表「有符合目前設定的有效判讀紀錄」，不代表答案已被獨立證實正確；模型自評信心不採用。", "",
         "## 計數方式", "",
         f"- 正式題庫根題 {b['root_questions']} 題，其中案例題 {b['case_studies']} 題（案例題本身不計），案例子題 {b['case_sub_questions']} 題。",
         f"- 獨立判讀單位 = 一般根題 {b['root_questions'] - b['case_studies']} ＋ 案例子題 {b['case_sub_questions']} ＝ **{b['unique_units']}**"
         f"（單位題號重複：{', '.join(b['duplicate_unit_ids']) or '無'}）。",
         f"- 目前設定：模型 `{rep['settings']['model']}`、prompt `{rep['settings']['prompt_version']}`、判定 `{rep['settings']['evaluator_version']}`。", "",
         "## 狀態總覽", "", "| 狀態 | 題數 | 比例 | 說明 |", "|---|---|---|---|"]
    desc = {"completed": "有符合目前設定的有效成功紀錄", "pending": "證據包可用、尚無紀錄（可安全補跑）",
            "failed_retryable": "證據包可用、只有失敗紀錄（可安全補跑）", "needs_rejudge": "只有不符目前條件的成功紀錄（需 --force 重判）",
            "blocked": "缺題目資料／證據包，或工作區無法執行（不能直接補跑）", "review_required": "紀錄異常或矛盾（需人工檢視）"}
    for s in STATUSES:
        L.append(f"| `{s}` | {c[s]} | {pct(c[s], t)} | {desc[s]} |")
    L += [f"| **合計** | **{sum(c.values())}** | | 應等於獨立判讀單位 {t}：{'✅ 一致' if sum(c.values()) == t else '❌ 不一致'} |", "",
          "## 關鍵指標", "",
          f"- 有效判讀完成率：{c['completed']}／{t}（{pct(c['completed'], t)}）",
          f"- 證據包完整率（工作區最新證據包驗證通過）：{packet_ok}／{t}（{pct(packet_ok, t)}）",
          f"- 可安全補跑：{c['pending'] + c['failed_retryable']} 題（pending {c['pending']}、failed_retryable {c['failed_retryable']}）→ `pending_ids.txt`",
          f"- 需要重判：{c['needs_rejudge']} 題 → `needs_rejudge_ids.txt`",
          f"- 受阻與人工檢視：{c['blocked'] + c['review_required']} 題（blocked {c['blocked']}、review_required {c['review_required']}）→ `blocked_review_ids.txt`",
          "", "## 主要原因（blocked／review_required／needs_rejudge）", ""]
    L += [f"- {r}：{n} 題" for r, n in reasons.most_common()] or ["- 無"]
    w = rep["workspace"]
    L += ["", "## 資料限制", "",
          f"- 只盤點單一工作區 `{w['path']}`；`--run` 的跳過判斷同樣只看這個工作區的紀錄與最新證據包。",
          f"- 工作區 --run 前置檢查：{'通過' if not w['preflight_problems'] else '未通過：' + '；'.join(w['preflight_problems'])}。",
          f"- 判讀結果檔：{'存在' if w['results_file_exists'] else '不存在'}"
          + (f"（讀取錯誤：{w['results_error']}）" if w["results_error"] else "")
          + (f"；重複鍵：{', '.join(w['results_duplicate_keys'])}" if w["results_duplicate_keys"] else "") + "。",
          f"- 工作區證據包檔案：{w['packet_files']} 份；沒有證據包的題目需先 dry-run（會呼叫 Microsoft Learn 公開搜尋）才能補跑。",
          f"- 不屬於題庫的判讀紀錄題號：{', '.join(rep['orphan_record_ids']) or '無'}（不計入分母）。", "",
          "## 與 --run 跳過條件的差異", ""] + [f"- {x}" for x in rep["differences_from_run_skip"]]
    L += ["", "## 其他工作區（僅供參考，不計入本次盤點）", ""]
    L += [f"- `{o['path']}`：" + (f"讀取錯誤 {o['error']}" if "error" in o else
                                   f"{o['questions']} 題、{o['runs']} 筆紀錄，版本 {o['versions']}") for o in others] or ["- 無"]
    L += ["", "## 建議", "",
          "- 先處理 review_required，再處理 blocked（多數為缺少證據包，需在同一工作區先 dry-run）。",
          "- pending／failed_retryable 可在同一工作區以 `--run`（每次最多 5 題）補跑；已完成者會自動跳過。",
          "- needs_rejudge 需明確加 `--force` 重新判定。", ""]
    return "\n".join(L)


def id_list(title: str, items: list[str]) -> str:
    return "\n".join([f"# {title}", f"# {len(items)} 筆" + ("（空集合）" if not items else "")] + items) + "\n"


def write_outputs(out: Path, rep: dict, others: list[dict]) -> None:
    by = collections.defaultdict(list)
    for u in rep["units"]:
        by[u["status"]].append(u)
    out.mkdir(parents=False, exist_ok=False)  # 目錄必須是新的；已存在即拋出例外，不覆寫
    (out / "progress_detail.json").write_text(json.dumps({**rep, "other_workspaces": others}, ensure_ascii=False, indent=2) + "\n",
                                              encoding="utf-8")
    (out / "progress_summary.md").write_text(render_summary(rep, others), encoding="utf-8")
    (out / "pending_ids.txt").write_text(id_list("可安全補跑（pending ＋ failed_retryable）",
                                                 [u["id"] for u in by["pending"] + by["failed_retryable"]]), encoding="utf-8")
    (out / "needs_rejudge_ids.txt").write_text(id_list("需要重新判讀（--run --force）", [u["id"] for u in by["needs_rejudge"]]),
                                               encoding="utf-8")
    (out / "blocked_review_ids.txt").write_text(
        id_list("受阻（blocked）或需人工檢視（review_required）：題號<TAB>狀態<TAB>原因",
                [f"{u['id']}\t{u['status']}\t{'；'.join(u['reasons'])}" for u in by["blocked"] + by["review_required"]]
                + [f"{q}\torphan_record\t判讀紀錄的題號不在正式題庫中" for q in rep["orphan_record_ids"]]),
        encoding="utf-8")


def main(argv=None) -> int:
    eq.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", type=Path, default=va.DEFAULT_BANK)
    ap.add_argument("--workspace", type=Path, required=True, help="判讀工作區（含 gemini_judgments.json 與 gemini_judge/packets/）")
    ap.add_argument("--out", type=Path, required=True, help="新的報告目錄（必須不存在）")
    ap.add_argument("--model", default=eq.DEFAULT_MODEL)
    args = ap.parse_args(argv)
    out, ws = args.out.resolve(), args.workspace.resolve()
    if out.exists():
        print(f"報告目錄已存在，未寫入任何檔案：{out}")
        return 2
    if out == ws or ws in out.parents or out in ws.parents:
        print("報告目錄不可與工作區相同、位於工作區內或包含工作區。")
        return 2
    if not args.bank.is_file() or not ws.is_dir():
        print("找不到題庫或工作區。")
        return 2
    rep = audit(args.bank.resolve(), ws, args.model)
    others = other_workspaces(SCRIPTS.parent, ws)
    if sum(rep["counts"].values()) != rep["total"]:
        print("內部錯誤：狀態合計與獨立判讀單位數不一致，未寫入報告。")
        return 3
    write_outputs(out, rep, others)
    print(f"獨立判讀單位 {rep['total']}｜" + "｜".join(f"{s} {rep['counts'][s]}" for s in STATUSES))
    print(f"報告：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
