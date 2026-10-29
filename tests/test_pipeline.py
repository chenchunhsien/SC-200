"""以假的 Gemini client 測試 enrich_questions.py 第二階段流程（完全不連網）。"""
import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(r"D:\SC-200-platform")
OUT = Path(__file__).parent / "out"
FAKE_KEY = "FAKE-KEY-should-never-appear-123"
IDS = "qd1-t1-q1,qd1-qs4-q004,qd1-qs4-q007,qd1-qs4-q006,qd1-qs4-q044"

spec = importlib.util.spec_from_file_location("eq", ROOT / "scripts" / "enrich_questions.py")
eq = importlib.util.module_from_spec(spec)
sys.modules["eq"] = eq
spec.loader.exec_module(eq)
eq.load_api_key = lambda: FAKE_KEY
eq.time.sleep = lambda s: None


class ApiErr(Exception):
    def __init__(self, code):
        super().__init__(f"boom {code} key={FAKE_KEY}")
        self.code = code


class FakeModels:
    def __init__(self, mode):
        self.mode, self.gen_calls, self.count_calls = mode, [], 0

    def count_tokens(self, model, contents, config):
        self.count_calls += 1
        if self.mode == "count-fail":
            raise ApiErr(400)
        assert config.system_instruction == eq.SYSTEM_INSTRUCTION
        return SimpleNamespace(total_tokens=len(contents) // 3)

    def generate_content(self, model, contents, config):
        qid = contents.split("「", 1)[1].split("」", 1)[0]
        self.gen_calls.append(qid)
        assert model == eq.DEFAULT_MODEL == "gemini-3.1-flash-lite", model
        assert not getattr(config, "tools", None), "不得啟用 grounding / tools"
        assert config.response_mime_type == "application/json" and config.max_output_tokens == 8192
        assert "reviewNote" not in contents, "reviewNote 不可送出"
        n = self.gen_calls.count(qid)
        if self.mode == "fatal":
            raise ApiErr(400)
        if self.mode == "429-always":
            raise ApiErr(429)
        if self.mode == "429-then-ok" and n == 1:
            raise ApiErr(429)
        unit = next(u for u in UNITS if u.question_id == qid)
        targets = eq.answer_targets(unit)
        if self.mode == "bad-then-ok" and n == 1:
            targets = targets[:-1]  # 漏掉一個選項
        if self.mode == "always-bad":
            targets = [(t, "WRONG") for t, _ in targets]
        body = {
            "question_id": qid,
            "explanation": "正式答案正確的理由。",
            "option_explanations": [{"target_id": t, "official_answer": a, "explanation": "說明"} for t, a in targets],
            "key_points": ["重點一", "重點二"],
            "references": [{"title": "Microsoft Learn 文件", "url": None, "relevance": "相關"}],
            "answer_concern": {"has_concern": qid == "qd1-qs4-q007", "detail": "疑點說明" if qid == "qd1-qs4-q007" else None},
            "uncertainty": None,
        }
        usage = SimpleNamespace(prompt_token_count=1000, candidates_token_count=600, thoughts_token_count=200,
                                total_token_count=1800)
        return SimpleNamespace(text=json.dumps(body, ensure_ascii=False), usage_metadata=usage,
                               candidates=[SimpleNamespace(finish_reason="STOP")])


def run(argv, mode="ok"):
    fake = SimpleNamespace(models=FakeModels(mode))
    eq.make_client = lambda key, timeout: fake
    buf = io.StringIO()
    sys.argv = ["enrich_questions.py", "--output-dir", str(OUT)] + argv
    with contextlib.redirect_stdout(buf):
        code = eq.main()
    out = buf.getvalue()
    assert FAKE_KEY not in out, "API key 出現在輸出中！"
    return code, out, fake.models


bank = json.loads((ROOT / "public/data/sc200.json").read_text(encoding="utf-8"))
UNITS = list(eq.iter_units(bank))
before = hashlib.sha256((ROOT / "public/data/sc200.json").read_bytes()).hexdigest()
shutil.rmtree(OUT, ignore_errors=True)
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))


# 1. --estimate：只呼叫 count_tokens，不生成
code, out, m = run(["--estimate", "--ids", IDS])
check("estimate 不呼叫生成 API", code == 0 and not m.gen_calls and m.count_calls == 5, out[-300:])
check("estimate 不建立輸出檔", not OUT.exists())

# 2. --run 但非互動且無 --yes：不生成
code, out, m = run(["--run", "--tier", "paid", "--ids", IDS])
check("無 --yes 的非互動執行不生成", code == 2 and not m.gen_calls)

# 3. 正常執行 5 題
code, out, m = run(["--run", "--tier", "free", "--yes", "--ids", IDS])
exp = json.loads((OUT / "explanations.json").read_text(encoding="utf-8"))["explanations"]
prog = json.loads((OUT / "progress.json").read_text(encoding="utf-8"))
check("5 題全部成功", code == 0 and len(m.gen_calls) == 5 and set(exp) == set(IDS.split(",")), out[-400:])
check("review_status 皆為 pending", all(r["review_status"] == "pending" for r in exp.values()))
check("references 皆標示待核對", all(x["verification"] == "待核對" for r in exp.values() for x in r["references"]))
check("題組子題保留 case_id", exp["qd1-t1-q1"]["case_id"] == "qd1-t1" and exp["qd1-qs4-q004"]["case_id"] is None)
check("answer_concern 有保留", exp["qd1-qs4-q007"]["answer_concern"]["has_concern"] is True)
check("progress 記錄 token 與費用", all(i["usage"]["total_tokens"] == 1800 and i["cost_usd_estimate"] for i in prog["items"].values()))
check("progress 記錄題庫 SHA256", prog["meta"]["bank_sha256"] == before)

# 4. 未加 --resume 重跑：拒絕（避免重複付費）
code, out, m = run(["--run", "--tier", "free", "--yes", "--ids", IDS])
check("已完成題目未加 --resume 時拒絕", code == 2 and not m.gen_calls)

# 5. --resume：全部跳過，不呼叫 API
code, out, m = run(["--run", "--tier", "free", "--yes", "--resume", "--ids", IDS])
check("--resume 跳過已完成題目", code == 0 and not m.gen_calls and not m.count_calls)

# 6. --limit 以子題計（題組 id 展開）
shutil.rmtree(OUT)
code, out, m = run(["--run", "--tier", "free", "--yes", "--ids", "qd3-t4", "--limit", "5"])  # 9 道子題的題組
check("--limit 5 只處理 5 道子題（題組 id 展開為子題）",
      m.gen_calls == [f"qd3-t4-q{i}" for i in range(1, 6)], str(m.gen_calls))

# 7. 驗證失敗後重試成功；token 累計兩次
shutil.rmtree(OUT)
code, out, m = run(["--run", "--tier", "free", "--yes", "--ids", "qd1-qs4-q004"], mode="bad-then-ok")
prog = json.loads((OUT / "progress.json").read_text(encoding="utf-8"))["items"]["qd1-qs4-q004"]
check("缺選項的輸出被擋下並重試成功", code == 0 and prog["attempts"] == 2 and prog["usage"]["total_tokens"] == 3600)

# 8. 永遠不合格：有限次後標記失敗，不寫入解析
shutil.rmtree(OUT)
code, out, m = run(["--run", "--tier", "free", "--yes", "--retries", "1", "--ids", "qd1-qs4-q004"], mode="always-bad")
prog = json.loads((OUT / "progress.json").read_text(encoding="utf-8"))["items"]["qd1-qs4-q004"]
exists = (OUT / "explanations.json").exists()
check("答案對應錯誤時有限次重試後標記失敗", code == 1 and len(m.gen_calls) == 2 and prog["status"] == "failed" and not exists)

# 9. 429 後重試成功
shutil.rmtree(OUT)
code, out, m = run(["--run", "--tier", "free", "--yes", "--ids", "qd1-qs4-q004"], mode="429-then-ok")
check("429 錯誤有限次重試後成功", code == 0 and len(m.gen_calls) == 2)

# 10. 400 致命錯誤：立即停止整批，不重試
shutil.rmtree(OUT)
code, out, m = run(["--run", "--tier", "free", "--yes", "--ids", IDS], mode="fatal")
check("400 錯誤立即停止整批", code == 1 and len(m.gen_calls) == 1)

# 11. --run 未指定 --tier：參數錯誤，不呼叫任何 API
try:
    run(["--run", "--yes", "--ids", IDS])
    check("--run 缺 --tier 時拒絕", False)
except SystemExit as e:
    check("--run 缺 --tier 時拒絕", e.code == 2)

# 12. count_tokens 不可用：改用粗估並清楚標示，仍可生成
shutil.rmtree(OUT, ignore_errors=True)
code, out, m = run(["--run", "--tier", "free", "--yes", "--ids", "qd1-qs4-q004"], mode="count-fail")
check("count_tokens 失敗時改用粗估並標示", code == 0 and "字元數粗估" in out and len(m.gen_calls) == 1, out[-500:])

# 13. 持續 429（配額用盡）：重試後停止整批，不繼續打後面的題目
shutil.rmtree(OUT, ignore_errors=True)
code, out, m = run(["--run", "--tier", "free", "--yes", "--retries", "1", "--ids", IDS], mode="429-always")
check("持續 429 時停止整批", code == 1 and m.gen_calls == ["qd1-t1-q1", "qd1-t1-q1"] and "太平洋時間" in out, str(m.gen_calls))

# 14. 估算輸出同時列出付費層費用與免費層條件
code, out, m = run(["--estimate", "--ids", IDS])
check("估算區分付費層與免費層", "付費層（Standard）估計費用" in out and "免費層" in out and "改善 Google 產品" in out)

# 15. 題庫未被修改
after = hashlib.sha256((ROOT / "public/data/sc200.json").read_bytes()).hexdigest()
check("題庫 SHA256 不變", before == after)

print(f"\n{sum(ok for _, ok in results)}/{len(results)} passed")
sys.exit(0 if all(ok for _, ok in results) else 1)

