"""人工修訂鎖定、備份與覆寫防護測試（假 Gemini client，完全不連網；只在暫存目錄寫入）。"""
import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
REAL_OUT = ROOT / "output"
OUT = Path(__file__).parent / "out_locks"
LOCKED = ["qd1-t1-q1", "qd1-qs4-q004", "qd1-qs4-q007", "qd1-qs4-q006", "qd1-qs4-q044"]
FREE_ID = "qd1-qs4-q002"  # 尚無解析的一般題目
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()  # noqa: E731

spec = importlib.util.spec_from_file_location("eq", ROOT / "scripts" / "enrich_questions.py")
eq = importlib.util.module_from_spec(spec)
sys.modules["eq"] = eq
spec.loader.exec_module(eq)
eq.load_api_key = lambda: "FAKE-KEY"
eq.time.sleep = lambda s: None
REAL_COPY2 = eq.shutil.copy2
bank = json.loads((ROOT / "public/data/sc200.json").read_text(encoding="utf-8"))
UNITS = {u.question_id: u for u in eq.iter_units(bank)}


class FakeModels:
    def __init__(self, mode):
        self.mode, self.gen_calls = mode, []

    def count_tokens(self, model, contents, config):
        return SimpleNamespace(total_tokens=100)

    def generate_content(self, model, contents, config):
        qid = contents.split("「", 1)[1].split("」", 1)[0]
        self.gen_calls.append(qid)
        targets = eq.answer_targets(UNITS[qid])
        if self.mode == "bad":
            targets = targets[:-1]
        body = {"question_id": qid, "explanation": "GEMINI 新解析", "key_points": ["k"],
                "option_explanations": [{"target_id": t, "official_answer": a, "explanation": "x"} for t, a in targets],
                "references": [], "answer_concern": {"has_concern": False, "detail": None}, "uncertainty": None}
        usage = SimpleNamespace(prompt_token_count=10, candidates_token_count=10, thoughts_token_count=0, total_token_count=20)
        return SimpleNamespace(text=json.dumps(body, ensure_ascii=False), usage_metadata=usage,
                               candidates=[SimpleNamespace(finish_reason="STOP")])


def seed():
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    for f in ("explanations.json", "progress.json"):
        shutil.copy2(REAL_OUT / f, OUT / f)


def run(argv, mode="ok"):
    fake = SimpleNamespace(models=FakeModels(mode))
    eq.make_client = lambda key, timeout: fake
    buf = io.StringIO()
    sys.argv = ["enrich_questions.py", "--output-dir", str(OUT), "--tier", "free"] + argv
    with contextlib.redirect_stdout(buf):
        try:
            code = eq.main()
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue(), fake.models


exp = lambda: json.loads((OUT / "explanations.json").read_text(encoding="utf-8"))["explanations"]  # noqa: E731
prog = lambda: json.loads((OUT / "progress.json").read_text(encoding="utf-8"))["items"]  # noqa: E731
backups = lambda: sorted((OUT / "backups").glob("*.json")) if (OUT / "backups").exists() else []  # noqa: E731
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" — {detail[:600]}"))


real_before = {f: sha(REAL_OUT / f) for f in ("explanations.json", "progress.json")}
bank_before = sha(ROOT / "public/data/sc200.json")
IDS = ",".join(LOCKED)

# 1. 相同 input_hash：鎖定題目跳過（加 / 不加 --resume 都一樣，且不因「已有解析」而中止）
seed(); orig = exp()
code, out, m = run(["--run", "--yes", "--resume", "--ids", IDS])
check("1. 相同 hash 下鎖定題目跳過（--resume）", code == 0 and not m.gen_calls and "人工修訂鎖定  : 5 題" in out, out)
code, out, m = run(["--run", "--yes", "--ids", IDS])
check("1b. 相同 hash 下鎖定題目跳過（未加 --resume 也不報錯）", code == 0 and not m.gen_calls, out)

# 2. 不同 input_hash（換模型、改 SYSTEM_INSTRUCTION）：仍跳過
code, out, m = run(["--run", "--yes", "--ids", IDS, "--model", "gemini-3.8-flash"])
check("2. 換模型（hash 不同）仍跳過", code == 0 and not m.gen_calls, out)
saved = eq.SYSTEM_INSTRUCTION
eq.SYSTEM_INSTRUCTION = saved + "（修改）"
code, out, m = run(["--run", "--yes", "--ids", IDS])
eq.SYSTEM_INSTRUCTION = saved
check("2b. 改 SYSTEM_INSTRUCTION（hash 不同）仍跳過", code == 0 and not m.gen_calls, out)

# 3/4. --ids 指定鎖定題目、未加 --overwrite-manual：不可覆寫；題組 ID 展開也一樣
code, out, m = run(["--run", "--yes", "--ids", "qd1-t1", "--model", "gemini-3.8-flash"])
check("3. 以題組 ID 指定鎖定子題仍跳過", code == 0 and not m.gen_calls, out)
check("4. 未加 --overwrite-manual 時內容未被覆寫", exp() == orig)
code, out, m = run(["--run", "--yes", "--overwrite-manual", "--ids", "qd1-t1"])
check("4b. --overwrite-manual 只指定題組 ID 時不覆寫子題", code == 0 and not m.gen_calls and "逐一列出" in out, out)
code, out, m = run(["--run", "--yes", "--overwrite-manual"])
check("4c. --overwrite-manual 未搭配 --ids 時拒絕", code == 2 and not m.gen_calls)

# 5. --overwrite-manual + 明確 ID + 確認：只覆寫指定那一題
code, out, m = run(["--run", "--overwrite-manual", "--ids", "qd1-qs4-q004"])  # 沒有 --yes：非互動不確認
check("5a. 未完成確認時不覆寫", code == 2 and not m.gen_calls and exp() == orig, out)
code, out, m = run(["--run", "--yes", "--overwrite-manual", "--ids", "qd1-qs4-q004,qd1-qs4-q007"])
e = exp()
check("5. 只覆寫明確指定的鎖定題目",
      code == 0 and m.gen_calls == ["qd1-qs4-q004", "qd1-qs4-q007"]
      and e["qd1-qs4-q004"]["explanation"] == "GEMINI 新解析"
      and all(e[q] == orig[q] for q in LOCKED if q not in ("qd1-qs4-q004", "qd1-qs4-q007")), out)
check("5b. 覆寫前的人工修訂內容保存在備份中",
      any(json.loads(b.read_text(encoding="utf-8")).get("explanations", {}).get("qd1-qs4-q004") == orig["qd1-qs4-q004"]
          for b in backups()))

# 6. 未鎖定題目正常生成
seed()
code, out, m = run(["--run", "--yes", "--ids", FREE_ID + "," + IDS])
e = exp()
check("6. 未鎖定題目正常生成，鎖定題目同批跳過",
      code == 0 and m.gen_calls == [FREE_ID] and FREE_ID in e and all(e[q] == orig[q] for q in LOCKED), out)

# 7. 備份成功後才寫入：備份內容 = 寫入前的檔案
seed()
pre = {f: sha(OUT / f) for f in ("explanations.json", "progress.json")}
code, out, m = run(["--run", "--yes", "--ids", FREE_ID])
bk = backups()
check("7. 寫入前已建立帶時間戳記的備份且內容一致",
      code == 0 and len(bk) == 2 and {sha(b) for b in bk} == set(pre.values()) and "已備份" in out, out)
code, out, m = run(["--run", "--yes", "--ids", "qd1-qs4-q003"])
check("7b. 第二次執行建立新備份，不覆蓋舊備份", len(backups()) == 4 and all(b.exists() for b in bk))

# 8. 備份失敗：不寫入、不呼叫生成
seed()
pre = {f: sha(OUT / f) for f in ("explanations.json", "progress.json")}
def broken_copy(*a, **k):
    raise OSError("模擬磁碟錯誤")
eq.shutil.copy2 = broken_copy
code, out, m = run(["--run", "--yes", "--ids", FREE_ID])
eq.shutil.copy2 = REAL_COPY2
check("8. 備份失敗時不寫入、不生成",
      code == 2 and not m.gen_calls and pre == {f: sha(OUT / f) for f in pre} and "備份失敗" in out, out)
store = eq.Store(OUT, ROOT / "public/data/sc200.json")
try:
    store.save(UNITS[FREE_ID], {"status": "done"}, {"question_id": FREE_ID})
    check("8b. 未備份時 Store.save 拒絕寫入", False)
except RuntimeError:
    check("8b. 未備份時 Store.save 拒絕寫入", True)
store.backed_up = True
try:
    store.save(UNITS["qd1-qs4-q044"], {"status": "done"}, {"question_id": "qd1-qs4-q044"})
    check("8c. 未授權時 Store.save 拒絕覆寫鎖定題目", False)
except RuntimeError:
    check("8c. 未授權時 Store.save 拒絕覆寫鎖定題目", pre == {f: sha(OUT / f) for f in pre})

# 9. 生成失敗不破壞既有成功解析（覆寫鎖定題目失敗、一般題目重生失敗）
seed()
code, out, m = run(["--run", "--yes", "--overwrite-manual", "--retries", "0", "--ids", "qd1-qs4-q004"], mode="bad")
e, p = exp(), prog()
check("9. 覆寫失敗時鎖定解析原樣保留，進度不被標成 failed",
      code == 1 and e["qd1-qs4-q004"] == orig["qd1-qs4-q004"] and p["qd1-qs4-q004"]["status"] == "done"
      and p["qd1-qs4-q004"]["last_failed_attempt"]["status"] == "failed", out)
run(["--run", "--yes", "--ids", FREE_ID])
good = exp()[FREE_ID]
code, out, m = run(["--run", "--yes", "--retries", "0", "--ids", FREE_ID, "--model", "gemini-3.8-flash"], mode="bad")
check("9b. 一般題目重生失敗時保留既有成功解析",
      code == 1 and exp()[FREE_ID] == good and prog()[FREE_ID]["status"] == "done", out)

# 10. 舊紀錄（沒有 manual_revision）仍可讀取、視為未鎖定
store = eq.Store(OUT, ROOT / "public/data/sc200.json")
check("10. 沒有 manual_revision 的紀錄可讀取且未鎖定",
      "manual_revision" not in store.exp["explanations"][FREE_ID] and not store.locked(UNITS[FREE_ID])
      and store.valid(UNITS[FREE_ID], eq.DEFAULT_MODEL))

# 11. --resume 與 hash 比對照舊
code, out, m = run(["--run", "--yes", "--ids", FREE_ID])
check("11. 未鎖定且 hash 相符、未加 --resume：照舊拒絕（避免重複付費）", code == 2 and not m.gen_calls)
code, out, m = run(["--run", "--yes", "--resume", "--ids", FREE_ID])
check("11b. 加 --resume 照舊跳過", code == 0 and not m.gen_calls)
code, out, m = run(["--run", "--yes", "--resume", "--ids", FREE_ID, "--model", "gemini-3.8-flash"])
check("11c. hash 不符的未鎖定題目照舊重新生成", code == 0 and m.gen_calls == [FREE_ID])

# 12. review_status 與 official_answer 不被自動變更
seed()
run(["--run", "--yes", "--ids", FREE_ID + "," + IDS, "--model", "gemini-3.8-flash"])
e = exp()
check("12. 鎖定題目的 review_status / official_answer / manual_revision 不變",
      all(e[q]["review_status"] == "pending" and e[q]["option_explanations"] == orig[q]["option_explanations"]
          and e[q]["manual_revision"]["locked"] is True for q in LOCKED)
      and e[FREE_ID]["review_status"] == "pending")

# 真實檔案未被動到
check("真實 output/ 檔案未被修改", real_before == {f: sha(REAL_OUT / f) for f in real_before})
check("題庫 SHA256 不變", bank_before == sha(ROOT / "public/data/sc200.json"))
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
