"""test_judge.py 的跨程序測試子程序：只使用本機暫存工作區、假的 Learn 搜尋與假的 Gemini client。不連網。

用法：python judge_child.py MODE WORKSPACE BANK BASELINE LOG HOLD
  MODE      dry | run | reeval | report | hold_lock | try_lock
  BASELINE  暫時作為 va.DEFAULT_OUT 的假正式目錄
  LOG       每行一筆 JSON 事件（search / client / api / holding / locked / acquired / busy / rc）
  HOLD      釋放檔路徑；存在前，第一次外部請求（或持有鎖）會停住。"-" 表示不停
"""
import io
import json
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

os.environ["GEMINI_API_KEY"] = "FAKEKEY-child-only-0123456789abcdefghij"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gemini_judge as gj  # noqa: E402

mode, ws, bank, baseline, log_path, hold = sys.argv[1:7]
gj.va.DEFAULT_OUT = Path(baseline)


def _no_network(*a, **k):
    note("network_attempt")
    raise AssertionError("network access attempted in child")


import socket  # noqa: E402

socket.socket.connect = _no_network
socket.create_connection = _no_network


def note(event, **kw):
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(kw, event=event, pid=os.getpid()), ensure_ascii=False) + "\n")
        fh.flush()


def wait_release():
    if hold == "-":
        return
    note("holding")
    deadline = time.time() + 120
    while not Path(hold).exists():
        if time.time() > deadline:
            note("timeout")
            sys.exit(99)
        time.sleep(0.05)


def fake_search(q, top=5):
    note("search", q=q)
    wait_release()
    return []


def fake_fetch(u):
    note("fetch", u=u)
    raise OSError("offline test")


class FakeClient:
    def __init__(self):
        self.models = self

    def generate_content(self, model, contents, config):
        note("api")
        wait_release()
        return SimpleNamespace(text="not json",
                               usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=1,
                                                              thoughts_token_count=0, total_token_count=11),
                               candidates=[SimpleNamespace(finish_reason="STOP")])


base = ["--bank", bank, "--output-dir", ws]
buf = io.StringIO()
rc = None
with redirect_stdout(buf):
    if mode == "dry":
        rc = gj.main(base + ["--ids", "m-single", "--delay", "0"], search=fake_search, fetch=fake_fetch)
    elif mode == "run":
        gj.confirm = lambda prompt: True
        rc = gj.main(base + ["--ids", "m-single", "--run", "--tier", "paid", "--force"],
                     client_factory=lambda key: (note("client"), FakeClient())[1])
    elif mode == "reeval":
        rc = gj.main(base + ["--ids", "m-single", "--reevaluate"])
    elif mode == "report":
        rc = gj.main(base + ["--report"])
    elif mode == "hold_lock":
        with gj.workspace_lock(Path(ws)):
            note("locked")
            wait_release()
        rc = 0
    elif mode == "try_lock":
        try:
            with gj.workspace_lock(Path(ws)):
                note("acquired")
            rc = 0
        except gj.WorkspaceBusy as err:
            note("busy", msg=str(err))
            rc = 3
note("rc", rc=rc, out=buf.getvalue())
sys.exit(0 if rc is None else rc)
