"""gemini_judge.py 離線測試：虛構題目、假的 Learn 搜尋 / 擷取、假的 Gemini client。不連網、不呼叫 API。"""
import copy
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(r"D:\SC-200-platform")
sys.path.insert(0, str(PROJECT / "scripts"))
FAKE_KEY = "FAKEKEY-test-only-0123456789abcdefghijklmnop"
os.environ["GEMINI_API_KEY"] = FAKE_KEY  # 讓 load_api_key 使用假金鑰，不讀真實 .env

import gemini_judge as gj  # noqa: E402

va = gj.va
PASSED, FAILED = [], []

# 本機臨時 Git 儲存庫：所有測試暫存目錄放在其受 .gitignore 排除的 tmp/ 底下（F-02 要求工作區必須受排除）。
# 只在系統暫存區建立，不碰專案的 Git 儲存庫；不連網。
REAL_TEMP = Path(tempfile.gettempdir())
GIT_ROOT = Path(tempfile.mkdtemp(prefix="judge-gitroot-"))
subprocess.run(["git", "init", "-q", str(GIT_ROOT)], check=True)
(GIT_ROOT / ".gitignore").write_text("tmp/\n", encoding="utf-8")
(GIT_ROOT / "tmp").mkdir()
tempfile.tempdir = str(GIT_ROOT / "tmp")
PROJECT_INDEX_SHA = hashlib.sha256((PROJECT / ".git" / "index").read_bytes()).hexdigest()

# 網路防護：任何真實連線都會失敗並被記錄（git 子程序不經過本程序的 socket）
import socket  # noqa: E402

NET_ATTEMPTS = []


def _no_network(*a, **k):
    NET_ATTEMPTS.append(a[1:2])
    raise AssertionError("network access attempted in test")


socket.socket.connect = _no_network
socket.create_connection = _no_network


def current(p):
    """把手工建立的測試證據包標示為「目前版本、快照已驗證」（模擬 dry-run 在正式基準相同時產生的證據包）。"""
    p = dict(p)
    p.update(gj.PACKET_VERSIONS, created_at="2026-10-27T00:00:00+00:00", bank_sha256=p.get("bank_sha256", "x"),
             snapshot_sha256="s" * 64, snapshot_schema_version=va.SCHEMA_VERSION,
             snapshot_trust={"rules_version": gj.SNAPSHOT_TRUST_VERSION, "level": "verified", "format_valid": True,
                             "source_consistent": True, "content_trusted": True, "reasons": []})
    p["microsoft_learn"] = dict(p.get("microsoft_learn") or {}, fetch_log=[])
    return p


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    if not cond:
        print(f"FAIL {name} {detail}")


# ---------------------------------------------------------------------------
# 虛構題庫與文件
# ---------------------------------------------------------------------------

BANK = {"questions": [
    {"id": "m-single", "type": "single_choice",
     "question": "You need to identify which Microsoft Sentinel table contains Microsoft Entra interactive sign-in events for investigation.",
     "options": [{"id": "A", "text": "AuditLogs"}, {"id": "B", "text": "SigninLogs"},
                 {"id": "C", "text": "SecurityEvent"}, {"id": "D", "text": "Heartbeat"}],
     "answer": "B"},
    {"id": "m-multi", "type": "multiple_choice",
     "question": "Which two Microsoft Sentinel data connectors should you enable to collect Defender for Endpoint alerts and Microsoft Entra sign-in logs?",
     "options": [{"id": "A", "text": "Microsoft Defender XDR connector"}, {"id": "B", "text": "Syslog connector"},
                 {"id": "C", "text": "Microsoft Entra ID connector"}, {"id": "D", "text": "AWS S3 connector"}],
     "answer": ["A", "C"]},
    {"id": "m-stmt", "type": "statement_matrix", "question": "For each statement, select Yes or No.",
     "columns": ["Yes", "No"],
     "statements": [{"id": "s1", "text": "Microsoft Sentinel playbooks are built on Azure Logic Apps workflows.", "answer": True},
                    {"id": "s2", "text": "Scheduled analytics rules can run every minute.", "answer": False}]},
    {"id": "m-nodoc", "type": "single_choice", "question": "Which retention option applies to archived data?",
     "options": [{"id": "A", "text": "Option alpha"}, {"id": "B", "text": "Option beta"}], "answer": "A"},
    {"id": "m-tactic", "type": "single_choice",
     "question": "An attacker uses the Microsoft Graph API. Which tactics can you analyze by using the MicrosoftGraphActivityLogs table?",
     "code": "Name     Tactic\nTactic1  Discovers misconfigured mailboxes\nTactic2  Deletes Azure virtual machines",
     "options": [{"id": "A", "text": "Tactic1 only"}, {"id": "B", "text": "Tactic1 and Tactic2"}], "answer": "A"},
    {"id": "m-mitre", "type": "single_choice",
     "question": "Which Microsoft Sentinel node shows MITRE ATT&CK coverage?",
     "options": [{"id": "A", "text": "Threat management > MITRE ATT&CK"}, {"id": "B", "text": "Configuration > Data connectors"}],
     "answer": "A"},
]}
NOTICE_DOC = "https://learn.microsoft.com/en-us/azure/sentinel/notice-page"
NOTICE_TEXT = (
    "The SigninLogs table contains Microsoft Entra sign-in events. SigninLogs supports investigation. AuditLogs is different. "
    "Important Starting June 2025, Microsoft Sentinel began transitioning legacy policies to a new model. Plan accordingly. More details follow. "
    "Important The Contoso widget was deprecated in March 2024. It is unrelated. Nothing else here. "
    "SecurityEvent holds Windows events. Heartbeat holds agent health. AuditLogs holds directory changes. "
    "Last updated on June 2025 for Microsoft Sentinel. SigninLogs table and AuditLogs table listed. SecurityEvent too. "
    "Warning Microsoft Sentinel legacy connectors are retired on 2026-01-15. Use new ones. Thanks. "
    "Caution Microsoft Sentinel deprecated a minor feature in May 2026. Use another. Done.")
NOISY = "https://learn.microsoft.com/en-us/azure/sentinel/noisy-page"
OFFTOPIC = "https://learn.microsoft.com/en-us/power-platform/admin/connectors"
NOISY_RAW = ("Table of contents Exit editor mode Ask Learn Ask Learn Reading mode Table of contents Read in English Add "
             "Add to Plans Edit Copy Markdown Print Note Access to this page requires authorization. You can try signing in "
             "or changing directories . Access to this page requires authorization. You can try changing directories . "
             "Noisy page title Feedback Summarize this article for me "
             "The SigninLogs table contains Microsoft Entra sign-in events. AuditLogs holds directory changes. "
             "SecurityEvent holds Windows events. Related content Tables Feedback Was this page helpful? Yes No "
             "Need help with this topic? Ask Learn Additional resources Last updated on 2026-10-15")

TABLES = "https://learn.microsoft.com/en-us/azure/sentinel/tables-reference"
CONNECTORS = "https://learn.microsoft.com/en-us/azure/sentinel/connectors-reference"
PLAYBOOKS = "https://learn.microsoft.com/en-us/azure/sentinel/automation/playbooks"
DOCS = {
    TABLES: ("The SigninLogs table contains Microsoft Entra sign-in events for interactive users. "
             "Sentinel stores SigninLogs data in the workspace table. Use it for investigation. "
             "The AuditLogs table contains directory audit events, not interactive sign-in events. "
             "AuditLogs records changes such as user creation. It is not used for sign-in investigation. "
             "The SecurityEvent table contains Windows security events from machines. "
             "SecurityEvent does not include Microsoft Entra sign-in events. "
             "The Heartbeat table contains agent health events, not sign-in events. "
             "Pricing depends on retention. Data is billed per GB. See the pricing page."),
    CONNECTORS: ("The Microsoft Defender XDR connector streams Defender for Endpoint alerts into Microsoft Sentinel. "
                 "Enable the Microsoft Defender XDR connector to collect alerts. Incidents are synchronized. "
                 "The Microsoft Entra ID connector collects sign-in logs and audit logs. "
                 "Enable the Microsoft Entra ID connector for sign-in logs. Diagnostic settings are configured. "
                 "The Syslog connector collects events from Linux machines only. "
                 "The Syslog connector does not collect Defender for Endpoint alerts or sign-in logs. Use it for Linux."),
    PLAYBOOKS: ("Playbooks in Microsoft Sentinel are based on workflows built in Azure Logic Apps. "
                "Scheduled analytics rules can run at intervals as short as five minutes. Logic Apps pricing applies."),
    NOISY: NOISY_RAW,
    NOTICE_DOC: NOTICE_TEXT,
    OFFTOPIC: ("Data connectors in Power Platform connect apps. The Syslog connector and Microsoft Entra ID connector "
               "are listed. Defender XDR connector is a premium connector."),
}
SEARCH_CALLS = []


def fake_search(query, top=5):
    SEARCH_CALLS.append((query, top))
    extra = [{"title": "Landing", "url": "https://learn.microsoft.com/en-us/azure/sentinel/"},
             {"title": "Training", "url": "https://learn.microsoft.com/en-us/training/modules/x"}]
    if "SigninLogs" in query or "AuditLogs" in query:
        return [{"title": "Sentinel tables", "url": TABLES}] + extra
    if "Syslog" in query:
        return [{"title": "Data connectors (Power Platform)", "url": OFFTOPIC},
                {"title": "Connectors", "url": CONNECTORS + "?view=x#y"}] + extra
    if "connector" in query.lower() or "XDR" in query:
        return [{"title": "Connectors", "url": CONNECTORS}] + extra
    if "Logic" in query or "Playbook" in query or "playbook" in query:
        return [{"title": "Playbooks", "url": PLAYBOOKS}]
    return []


def offtopic_search(query, top=5):
    return [{"title": "Data connectors (Power Platform)", "url": OFFTOPIC}]


def fake_fetch(url):
    if url not in DOCS:
        raise OSError("not found")
    return DOCS[url]


def failing_search(query, top=5):
    raise OSError("network down")


def setup(ids=("m-single", "m-multi", "m-stmt"), search=fake_search, ver_edit=None, extra_args=()):
    d = Path(tempfile.mkdtemp(prefix="judge-"))
    bank = d / "bank.json"
    bank.write_text(json.dumps(BANK), encoding="utf-8")
    out = d / "output"
    out.mkdir()
    ctx = va.Ctx(bank, out)
    qs = {}
    for u in ctx.units.values():
        qs[u.question_id] = va.new_entry(ctx, u)
    et = qs["m-single"]["examtopics_evidence"]
    et.update(match_status="exact", url="https://www.examtopics.com/discussions/microsoft/view/1-x/", page_answer="B",
              community_vote={"answer": "B", "share_percent": 90, "vote_count": 20, "as_of": "2026-10-27"})
    qs["m-multi"]["examtopics_evidence"]["match_status"] = "not_found"
    if ver_edit:
        ver_edit(qs)
    meta = {"schema_version": va.SCHEMA_VERSION, "bank_sha256": hashlib.sha256(bank.read_bytes()).hexdigest()}
    (out / "verification.json").write_text(json.dumps({"meta": meta, "questions": qs}, ensure_ascii=False), encoding="utf-8")
    # 假的正式基準（與快照相同），dry-run 期間暫時作為 va.DEFAULT_OUT，讓快照通過第三層（內容可信）驗證
    base = d / "official_baseline"
    base.mkdir()
    shutil.copyfile(out / "verification.json", base / "verification.json")
    argv = ["--bank", str(bank), "--output-dir", str(out), "--ids", ",".join(ids), "--delay", "0"]
    buf = io.StringIO()
    saved = va.DEFAULT_OUT
    va.DEFAULT_OUT = base
    try:
        with redirect_stdout(buf):
            rc = gj.main(argv + list(extra_args), search=search, fetch=fake_fetch)
    finally:
        va.DEFAULT_OUT = saved
    return SimpleNamespace(dir=d, bank=bank, out=out, argv=argv, rc=rc, log=buf.getvalue(), base=base)


def packet_of(env, qid):
    p, _, _ = gj.load_packet(gj.latest_packet(env.out / "gemini_judge" / "packets", qid))
    return p


def pid_with(packet, phrase):
    for d in packet["microsoft_learn"]["documents"]:
        for p in d["passages"]:
            if phrase in p["text"]:
                return p["passage_id"]
    raise AssertionError(f"no passage has {phrase!r}")


def cite(packet, phrase, rel):
    return {"passage_id": pid_with(packet, phrase), "quote": phrase, "relation": rel}


def item(tid, assessment, cites, basis="official_direct"):
    return {"target_id": tid, "assessment": assessment, "basis": basis, "citations": cites, "note": ""}


def single_result(pk, answer='"B"', sufficient=True):
    return {"suggested_answer": answer,
            "option_analysis": [
                item("A", "incorrect", [cite(pk, "AuditLogs table contains directory audit events", "refutes_option")]),
                item("B", "correct", [cite(pk, "The SigninLogs table contains Microsoft Entra sign-in events for interactive users", "supports_option")]),
                item("C", "incorrect", [cite(pk, "SecurityEvent does not include Microsoft Entra sign-in events", "refutes_option")]),
                item("D", "incorrect", [cite(pk, "Heartbeat table contains agent health events, not sign-in events", "refutes_option")])],
            "community_assessment": "社群 90% 支持 B", "rationale": "官方文件說明 SigninLogs 存放登入事件",
            "docs_sufficient": sufficient, "conflicts": [], "open_questions": [], "model_confidence": "高"}


def unit(env, qid):
    return va.Ctx(env.bank, env.out).units[qid]


# ---------------------------------------------------------------------------
# 1. 證據包
# ---------------------------------------------------------------------------

env = setup()
check("dryrun rc 0", env.rc == 0, env.log[-500:])
check("dryrun says no API", "未呼叫 Gemini API" in env.log)
pk = packet_of(env, "m-single")
check("question has no answer field", "answer" not in pk["question"])
pkst = packet_of(env, "m-stmt")
check("statements stripped of answer", all("answer" not in s for s in pkst["question"]["statements"]))
check("bank answer present", pk["bank_answer"]["value"] == "B")
check("examtopics available", pk["examtopics"]["status"] == "available"
      and pk["examtopics"]["consensus_threshold"]["met"] is True)
check("discussion summary not fabricated", pk["examtopics"]["discussion_summary"] == {"status": "未取得"})
pkm = packet_of(env, "m-multi")
check("examtopics not_found labelled", pkm["examtopics"]["status"] == "not_found" and "community_vote" not in pkm["examtopics"])
check("stmt not_searched", pkst["examtopics"]["status"] == "not_searched")
check("learn found", pk["microsoft_learn"]["status"] == "found")
urls = [d["url"] for d in pk["microsoft_learn"]["documents"]] + [d["url"] for d in pkm["microsoft_learn"]["documents"]]
check("landing and training filtered", all(not u.endswith("/") and "/training/" not in u for u in urls), urls)
check("url query/fragment stripped", CONNECTORS in urls, urls)
check("filler passage dropped", all("billed per GB" not in p["text"] for d in pk["microsoft_learn"]["documents"] for p in d["passages"]))
check("passage ids stable", all(p["passage_id"].startswith(d["doc_id"] + "-P")
                                for d in pk["microsoft_learn"]["documents"] for p in d["passages"]))
files = list((env.out / "gemini_judge" / "packets").glob("*.json"))
check("one packet per question", len(files) == 3)
raw = "".join(f.read_text(encoding="utf-8") for f in files)
check("no key in packets", FAKE_KEY not in raw and "GEMINI_API_KEY" not in raw)
_, sha, ok = gj.load_packet(gj.latest_packet(env.out / "gemini_judge" / "packets", "m-single"))
check("hash verifies", ok)
mv = gj.model_view(pk)
check("model view excludes scores and sha", "bank_sha256" not in mv
      and all(set(p) == {"passage_id", "text"} for d in mv["microsoft_learn"]["documents"] for p in d["passages"]))
check("token estimate positive", gj.estimate_input_tokens(pk) > 100)
check("no verification.json change", json.loads((env.out / "verification.json").read_text(encoding="utf-8"))["meta"]
      == {"schema_version": va.SCHEMA_VERSION, "bank_sha256": hashlib.sha256(env.bank.read_bytes()).hexdigest()})
check("dry-run creates no results file", not (env.out / "gemini_judgments.json").exists())

env_fail = setup(ids=("m-single",), search=failing_search)
pkf = packet_of(env_fail, "m-single")
check("search failure recorded", pkf["microsoft_learn"]["errors"] and pkf["microsoft_learn"]["status"] == "unavailable")
check("no-doc note", pkf["microsoft_learn"]["note"] == "尚未找到官方依據")

env_h = setup(ids=("m-single",), ver_edit=lambda qs: qs["m-single"]["microsoft_evidence"].append(
    {"url": TABLES, "title": "Tables", "excerpt": "Sentinel stores SigninLogs data in the workspace table.",
     "supports": "bank", "verification": "verified"}))
pkh = packet_of(env_h, "m-single")
check("human evidence url first", pkh["microsoft_learn"]["documents"][0]["origin"] == "human_verified_evidence")
check("human excerpt passage pinned", any(p["pinned_human_excerpt"] for d in pkh["microsoft_learn"]["documents"] for p in d["passages"]))
check("human evidence in packet", len(pkh["human_verified_microsoft_evidence"]) == 1)
check(">5 ids rejected", gj.main(env.argv[:-4] + ["--ids", "a,b,c,d,e,f"]) == 2)

# ---------------------------------------------------------------------------
# 1b. 搜尋品質：短查詢、佔位詞、路徑篩選、去重、雜訊、--doc-url、無相關文件
# ---------------------------------------------------------------------------

units = va.Ctx(env.bank, env.out).units
for qid in ("m-single", "m-multi", "m-stmt", "m-tactic", "m-mitre"):
    qs, prods, _ = gj.build_queries(units[qid])
    check(f"{qid}: short queries", qs and all(len(q.split()) <= gj.QUERY_MAX_WORDS for q in qs), qs)
    check(f"{qid}: queries start with a product", all(any(q.startswith(p["name"]) for p in prods) for q in qs), qs)
check("several queries per question", len(gj.build_queries(units["m-single"])[0]) >= 3)
check("search asks for more results", all(top == gj.SEARCH_TOP for _, top in SEARCH_CALLS))
qs_t, _, ph_t = gj.build_queries(units["m-tactic"])
check("placeholders not in queries", not any(re.search(r"Tactic\d", q) for q in qs_t), qs_t)
check("placeholder options skipped", "A" not in ph_t and "B" not in ph_t, ph_t)
check("table rows become search targets", ph_t.get("Tactic1") == "Discovers misconfigured mailboxes", ph_t)
check("stem tech term with each product", any("Microsoft Graph MicrosoftGraphActivityLogs" == q for q in qs_t), qs_t)
check("tech_terms drops placeholders", gj.tech_terms("Tactic1 User2 VM1 DeviceId ATT&CK") == ["DeviceId"])
qs_m, _, ph_m = gj.build_queries(units["m-mitre"])
check("no chopped ATT token", not any(re.search(r"\bATT\b(?!&)", q) for q in qs_m), qs_m)
check("menu path uses last segment", ph_m.get("B") == "Data connectors", ph_m)
check("product words stripped from tech", "XDR" not in gj.tech_terms(gj.strip_products("Microsoft Defender XDR table", gj.detect_products("Microsoft Defender XDR"))))

ml = pkm["microsoft_learn"]
cand = {c["url"]: c for c in ml["candidates_considered"]}
check("other product path rejected", cand[OFFTOPIC]["decision"] == "rejected:other_product_path")
check("off-topic doc not in packet", OFFTOPIC not in [d["url"] for d in ml["documents"]])
check("url dedup across queries", sum(1 for c in ml["candidates_considered"] if c["url"] == CONNECTORS) == 1)
check("landing/training rejected", all(c["decision"].startswith("rejected") for c in ml["candidates_considered"]
                                       if c["url"].endswith("/") or "/training/" in c["url"]))
check("option coverage reported", ml["option_coverage"].get("A") is True and ml["option_coverage"].get("D") is False,
      ml["option_coverage"])
check("passages carry targets", any(p["targets"] for d in ml["documents"] for p in d["passages"]))
check("allowed_path term in path", gj.allowed_path("https://learn.microsoft.com/en-us/x/microsoftgraphactivitylogs",
                                                   [{"paths": ["/graph/"]}], ["MicrosoftGraphActivityLogs"])[0])

ps = [{"targets": ["A", "B"], "score": 12}, {"targets": ["A"], "score": 9}, {"targets": ["B"], "score": 8},
      {"targets": ["C"], "score": 5}]
picked = gj.pick_covering(ps, 2, set())
check("covering selection prefers new options", [p["score"] for p in picked] == [12, 5], picked)

clean = gj.clean_learn_text(NOISY_RAW)
check("noise: header removed", not any(s in clean for s in ("Ask Learn", "Table of contents", "requires authorization",
                                                             "Summarize this article")), clean)
check("noise: footer removed", "Was this page helpful" not in clean and "Last updated" not in clean, clean)
check("noise: body kept", clean.startswith("The SigninLogs table contains"), clean)
check("title extracted", gj.learn_title(NOISY_RAW) == "Noisy page title")

ver_before = sha_file = hashlib.sha256((env.out / "verification.json").read_bytes()).hexdigest()
e_doc = setup(ids=("m-single",), search=lambda q, top=5: [], extra_args=("--doc-url", NOISY))
pkd = packet_of(e_doc, "m-single")
docs = pkd["microsoft_learn"]["documents"]
check("doc-url kept as user_provided", e_doc.rc == 0 and docs and docs[0]["origin"] == "user_provided", e_doc.log[-400:])
check("doc-url title from page", docs and docs[0]["title"] == "Noisy page title")
check("doc-url passages clean", docs and all("Ask Learn" not in p["text"] for p in docs[0]["passages"]))
check("doc-url not human verified", pkd["human_verified_microsoft_evidence"] == []
      and pkd["verification_status"]["microsoft_consistency"] == "no_verified_evidence")
check("doc-url not written to verification.json",
      NOISY not in (e_doc.out / "verification.json").read_text(encoding="utf-8"))

e_fail = setup(ids=("m-single",), search=lambda q, top=5: [],
               extra_args=("--doc-url", "https://learn.microsoft.com/en-us/azure/sentinel/missing-page"))
mlf = packet_of(e_fail, "m-single")["microsoft_learn"]
check("doc-url fetch failure reported", mlf["documents_failed"] and mlf["documents_failed"][0]["origin"] == "user_provided"
      and "擷取失敗" in e_fail.log and mlf["note"] == "尚未找到官方依據")

e_irr = setup(ids=("m-single",), search=lambda q, top=5: [],
              extra_args=("--doc-url", PLAYBOOKS))
mli = packet_of(e_irr, "m-single")["microsoft_learn"]
check("irrelevant user doc not padded", not mli["documents"]
      and mli["documents_without_relevant_passages"][0]["origin"] == "user_provided")

for bad in ("https://example.com/x", "http://learn.microsoft.com/en-us/x", "https://learn.microsoft.com.evil.com/x",
            "https://user@learn.microsoft.com/x", "https://learn.microsoft.com/", "https://learn.microsoft.com:8443/x"):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = gj.main(env.argv + ["--doc-url", f"m-single={bad}"], search=fake_search, fetch=fake_fetch)
    check(f"bad doc-url rejected {bad}", rc == 2 and "只接受" in buf.getvalue())
buf = io.StringIO()
with redirect_stdout(buf):
    rc = gj.main(env.argv + ["--doc-url", NOISY], search=fake_search, fetch=fake_fetch)
check("multi ids need QID=URL", rc == 2 and "題目ID=網址" in buf.getvalue())
urls, probs = gj.parse_doc_urls([f"m-multi={NOISY}#frag", f"m-multi={NOISY}"], ["m-single", "m-multi"])
check("QID=URL parsed and deduped", urls == {"m-multi": [NOISY]} and not probs, (urls, probs))
buf = io.StringIO()
with redirect_stdout(buf):
    rc = gj.main(env.argv + ["--run", "--tier", "paid", "--doc-url", f"m-single={NOISY}"])
check("doc-url refused with --run", rc == 2 and "只用於 dry-run" in buf.getvalue())

# 產品變更提示段落
pcn = gj.product_change_notice
check("notice: Important + transition + date", pcn("Important Starting June 2025, X began transitioning policies."))
check("notice: deprecated + ISO date", pcn("The connector is deprecated as of 2026-03-01."))
check("notice: Warning + retired", pcn("Warning The legacy agent is retired."))
check("not notice: date only", not pcn("Last updated on June 2025. The table lists columns."))
check("not notice: Important only", not pcn("Important Configure the policy before you enable it."))
check("not notice: lifecycle word only", not pcn("The transition between pages is smooth."))
e_nt = setup(ids=("m-single",), search=lambda q, top=5: [], extra_args=("--doc-url", NOTICE_DOC))
mln = packet_of(e_nt, "m-single")["microsoft_learn"]
ps_n = [p for d in mln["documents"] for p in d["passages"]]
notice_ps = [p for p in ps_n if "product_change_notice" in p["selection_reasons"]]
relev_ps = [p for p in ps_n if "relevance" in p["selection_reasons"]]
check("notice passage kept with reason", any("Starting June 2025" in p["text"] for p in notice_ps), [p["text"][:40] for p in ps_n])
check("notice kept despite low score", any("Starting June 2025" in p["text"] and "relevance" not in p["selection_reasons"]
                                           for p in notice_ps))
check("unrelated notice excluded", not any("Contoso widget" in p["text"] for p in ps_n))
check("date-only passage not a notice", all("product_change_notice" not in p["selection_reasons"]
                                             for p in ps_n if "Last updated" in p["text"]))
check("notices capped", len(notice_ps) == gj.MAX_NOTICES, len(notice_ps))
check("relevance passages still kept", len(relev_ps) >= 2, len(relev_ps))
check("notice marked in dry-run log", "product_change_notice" in e_nt.log)
check("notice text sent verbatim", any("Important Starting June 2025, Microsoft Sentinel began transitioning legacy policies"
                                       in p["text"] for p in gj.model_view(packet_of(e_nt, "m-single"))["microsoft_learn"]["documents"][0]["passages"]))

# 產品變更提示必須被分析
us = unit(env, "m-single")
pkn = packet_of(e_nt, "m-single")
nids = gj.notice_passages(pkn)
check("notice ids found", len(nids) == 2, nids)
check("model view marks notice", sum(1 for d in gj.model_view(pkn)["microsoft_learn"]["documents"] for p in d["passages"]
                                     if p.get("notice") == "product_change_notice") == 2)
check("schema requires product_change_impact", "product_change_impact" in gj.RESPONSE_SCHEMA["required"])
check("prompt version bumped", gj.PROMPT_VERSION == "judge-v4" and gj.EVALUATOR_VERSION == "eval-v5")


def nt_result(pci=None):
    r = {"suggested_answer": "B",
         "option_analysis": [item("A", "incorrect", [cite(pkn, "AuditLogs holds directory changes", "refutes_option")]),
                             item("B", "correct", [cite(pkn, "The SigninLogs table contains Microsoft Entra sign-in events", "supports_option")]),
                             item("C", "incorrect", [cite(pkn, "SecurityEvent holds Windows events", "refutes_option")]),
                             item("D", "incorrect", [cite(pkn, "Heartbeat holds agent health", "refutes_option")])],
         "community_assessment": "", "rationale": "", "docs_sufficient": True, "conflicts": [], "open_questions": [],
         "model_confidence": "高"}
    if pci is not None:
        r["product_change_impact"] = pci
    return r


ev = gj.evaluate(us, pkn, nt_result())
check("notice not analyzed → incomplete", ev["completeness"] == "incomplete" and ev["product_change"]["status"] == "not_analyzed"
      and "缺少 product_change_impact" in ev["incomplete_reasons"][0], ev["incomplete_reasons"])
check("incomplete → 中 max, no review field", ev["confidence"] == "中" and "review_priority" not in ev
      and any("判讀不完整" in x for x in ev["uncertainty"]))
check("incomplete reason names passage", all(n in ev["incomplete_reasons"][0] for n in nids))
check("notice → community rec flagged outdated", any("可能過時" in f for f in ev["community_recommendation"]["flags"]))
check("notice → official may_be_outdated", ev["official_evidence_assessment"]["may_be_outdated"] is True)
ev = gj.evaluate(us, pkn, nt_result({"mentioned": False, "affected_options": [], "impact_assessment": "",
                                      "evidence_refs": [], "unresolved_questions": []}))
check("mentioned=false → not analyzed", ev["product_change"]["status"] == "not_analyzed" and ev["completeness"] == "incomplete")
ev = gj.evaluate(us, pkn, nt_result({"mentioned": True, "affected_options": ["B"], "impact_assessment": "x",
                                      "evidence_refs": ["D9-P9"], "unresolved_questions": []}))
check("refs not to notice → not analyzed", ev["product_change"]["status"] == "not_analyzed")
ev = gj.evaluate(us, pkn, nt_result({"mentioned": True, "affected_options": ["B"], "impact_assessment": "可能過時",
                                      "evidence_refs": [nids[0]], "unresolved_questions": ["是否仍適用"]}))
check("notice analyzed → complete but still 中", ev["completeness"] == "complete" and ev["product_change"]["status"] == "analyzed"
      and ev["confidence"] == "中", (ev["completeness"], ev["confidence"]))
ev = gj.evaluate(us, pk, single_result(pk))
check("no notice → no_notice_in_packet, can be 高", ev["product_change"]["status"] == "no_notice_in_packet" and ev["confidence"] == "高")

# 離線重核：不呼叫 API、不修改原始輸出
e_re = setup(ids=("m-single",))
pkr = packet_of(e_re, "m-single")
pfile = gj.latest_packet(e_re.out / "gemini_judge" / "packets", "m-single")
_, sha_r, _ = gj.load_packet(pfile)
orig = {"at": "2026-10-27T00:00:00+00:00", "model": "gemini-3.1-flash-lite", "tier": "free", "prompt_version": "judge-v1",
        "packet_file": pfile.name, "packet_sha256": sha_r, "status": "invalid_answer", "http_status": 200, "error": None,
        "usage": {"input_tokens": 1, "output_tokens": 1, "thoughts_tokens": 0, "total_tokens": 2},
        "model_output": single_result(pkr, answer="B"), "evaluation": {"status": "invalid_answer"}}
(e_re.out / "gemini_judgments.json").write_text(json.dumps({"meta": {}, "questions": {"m-single": {"runs": [orig]}}}),
                                                encoding="utf-8")
made = []
buf = io.StringIO()
with redirect_stdout(buf):
    rc = gj.main(e_re.argv + ["--reevaluate"], client_factory=lambda k: made.append(k))
res = json.loads((e_re.out / "gemini_judgments.json").read_text(encoding="utf-8"))
runs = res["questions"]["m-single"]["runs"]
check("reevaluate rc 0, no client", rc == 0 and not made, buf.getvalue())
check("original run untouched", runs[0] == orig)
check("reevaluation appended", len(runs) == 2 and runs[1]["kind"] == "offline_reevaluation" and runs[1]["api_called"] is False
      and runs[1]["source_run_index"] == 0 and runs[1]["model_output"] is None)
check("reevaluation parses plain id", runs[1]["evaluation"]["suggested_answer"] == "B"
      and runs[1]["evaluation"]["answer_parse"]["method"] == "plain_option_id")
check("reevaluation zero usage", runs[1]["usage"]["total_tokens"] == 0)
check("reevaluation backup made", any(p.name.startswith("gemini_judgments.") for p in (e_re.out / "backups").iterdir()))
buf = io.StringIO()
with redirect_stdout(buf):
    gj.main(["--bank", str(e_re.bank), "--output-dir", str(e_re.out), "--report"])
md2 = (e_re.out / "gemini_judgments.md").read_text(encoding="utf-8")
check("report shows reevaluation with source output", "離線重核（未呼叫 API）" in md2 and "模型自評 高" in md2)
check("reevaluate with --run refused", gj.main(e_re.argv + ["--reevaluate", "--run", "--tier", "free"]) == 2)
data = json.loads((e_re.out / "gemini_judgments.json").read_text(encoding="utf-8"))
data["questions"]["m-single"]["runs"][0]["packet_sha256"] = "0" * 64
data["questions"]["m-single"]["runs"] = data["questions"]["m-single"]["runs"][:1]
(e_re.out / "gemini_judgments.json").write_text(json.dumps(data), encoding="utf-8")
buf = io.StringIO()
with redirect_stdout(buf):
    rc = gj.main(e_re.argv + ["--reevaluate"])
check("reevaluate refuses packet hash mismatch", rc == 2 and "雜湊與原紀錄不符" in buf.getvalue())

e_off = setup(ids=("m-multi",), search=offtopic_search)
mlo = packet_of(e_off, "m-multi")["microsoft_learn"]
check("only off-topic results → not_found, not padded", mlo["status"] == "not_found" and mlo["documents"] == []
      and mlo["note"] == "尚未找到官方依據", mlo["status"])
check("model view drops candidate list", "candidates_considered" not in json.dumps(gj.model_view(pkm)))

# ---------------------------------------------------------------------------
# 2. 核對與信心
# ---------------------------------------------------------------------------

us = unit(env, "m-single")
ev = gj.evaluate(us, pk, single_result(pk))
check("normal → 高", ev["confidence"] == "高", ev)
check("citations all found", all(c["quote_check"] == "found" for c in ev["citations"]))
check("relation recorded separately", all(c["relation_claimed_by_model"] in ("supports_option", "refutes_option")
                                          and "usable" in c for c in ev["citations"]))
check("valid quote meaning stated", "不代表答案已被官方文件證實" in ev["valid_quote_meaning"])
check("no human review fields", all(k not in ev for k in ("needs_human_review", "review_priority", "human_review_reasons")))

r = single_result(pk)
r["option_analysis"][1]["citations"] = [{"passage_id": pid_with(pk, "SigninLogs"), "quote": "SigninLogs is the only table that stores every sign-in", "relation": "supports_option"},
                                        {"passage_id": "D9-P9", "quote": "The SigninLogs table contains Microsoft Entra sign-in events", "relation": "supports_option"},
                                        {"passage_id": pid_with(pk, "SigninLogs"), "quote": "SigninLogs", "relation": "supports_option"}]
ev = gj.evaluate(us, pk, r)
checks = [c["quote_check"] for c in ev["citations"] if c["target_id"] == "B"]
check("forged quote not_found", checks == ["not_found", "unknown_passage", "too_short"], checks)
check("forged → 低", ev["confidence"] == "低", ev["conflicts"])
check("forged noted", any("無法核對" in x for x in ev["caps"]))

ev = gj.evaluate(us, pk, single_result(pk, answer='"Z"'))
check("invalid answer status", ev["status"] == "invalid_answer" and ev["confidence"] is None)
ev = gj.evaluate(us, pk, single_result(pk, answer="B"))  # 純文字選項 ID
check("plain option id accepted", ev["status"] == "evaluated" and ev["suggested_answer"] == "B"
      and ev["answer_parse"] == {"raw": "B", "method": "plain_option_id", "error": None}, ev["answer_parse"])
check("plain id not auto-high", gj.evaluate(us, pk, single_result(pk, answer="B"))["confidence"]
      == gj.evaluate(us, pk, single_result(pk, answer='"B"'))["confidence"])
check("json string method", gj.evaluate(us, pk, single_result(pk, answer='"B"'))["answer_parse"]["method"] == "json")
for bad in ("Z", '"Z"', "", "B C", "b"):
    check(f"invalid single answer {bad!r}", gj.evaluate(us, pk, single_result(pk, answer=bad))["status"] == "invalid_answer")
um0 = unit(env, "m-multi")
for raw in ("AC", "A, C", "A,C", '["A","C"]', "C A"):
    try:
        val, how = gj.parse_suggested(um0, raw)
    except ValueError:
        val, how = None, None
    check(f"multi answer {raw!r}", val == ["A", "C"], (val, how))
for raw in ("AZ", "AA", "A", "E"):
    ok_ = raw == "A"
    try:
        gj.parse_suggested(um0, raw)
        parsed = True
    except ValueError:
        parsed = False
    check(f"multi answer {raw!r} {'valid' if ok_ else 'invalid'}", parsed == ok_)

r = single_result(pk, answer='"C"')
r["option_analysis"] = [item("A", "incorrect", [cite(pk, "AuditLogs table contains directory audit events", "refutes_option")]),
                        item("B", "incorrect", [cite(pk, "Sentinel stores SigninLogs data in the workspace table", "refutes_option")]),
                        item("C", "correct", [cite(pk, "The SecurityEvent table contains Windows security events", "supports_option")]),
                        item("D", "incorrect", [cite(pk, "Heartbeat table contains agent health events, not sign-in events", "refutes_option")])]
pk_noet = copy.deepcopy(pk)
pk_noet["examtopics"] = gj.examtopics_section(None)
pk_b = copy.deepcopy(pk_noet)
pk_b["bank_answer"]["value"] = "C"  # 題庫答案 C，Gemini 以完整官方證據建議 B
ev = gj.evaluate(us, pk_b, single_result(pk_b))
check("bank differs → not capped", ev["confidence"] == "高" and ev["agrees_with_bank"] == "disagree"
      and not any("題庫" in x for x in ev["caps"] + ev["conflicts"] + ev["uncertainty"]), ev)
pk_c = copy.deepcopy(pk)
pk_c["examtopics"]["community_vote"]["answer"] = "C"  # 社群共識 C，Gemini 以完整官方證據建議 B
ev = gj.evaluate(us, pk_c, single_result(pk_c))
check("consensus differs → official not lowered", ev["confidence"] == "高" and not ev["conflicts"], ev["conflicts"])
check("consensus differs → uncertainty note", any("社群共識" in x and '"C"' in x for x in ev["uncertainty"]), ev["uncertainty"])

r = single_result(pk)
for it in r["option_analysis"]:
    it["citations"], it["basis"] = [], "community"
ev = gj.evaluate(us, pk, r)
check("community alone not 高", ev["confidence"] == "低", ev)

r = single_result(pk)
r["option_analysis"][0]["citations"] = [cite(pk, "The AuditLogs table contains directory audit events, not interactive sign-in events", "supports_option")]
ev = gj.evaluate(us, pk, r)
check("direct support for distractor → conflict recorded, not 低", ev["confidence"] == "中" and not ev["conflicts"]
      and any("未選取的選項 A" in x for x in ev["official_conflicts"]), (ev["confidence"], ev["official_conflicts"]))
check("direct-support distractor still counts as unexcluded (中 cap)", any("選項 A" in g for g in ev["evidence_gaps"]))
r = single_result(pk)
r["option_analysis"][0]["citations"] = [cite(pk, "AuditLogs records changes such as user creation", "supports_option")]
ev = gj.evaluate(us, pk, r)
check("indirect support for distractor → 中", ev["confidence"] == "中" and any("間接支持未選取的選項 A" in x for x in ev["caps"]), ev)

# 證據品質：直接 / 間接 / 未提及選項 / 反證未對應 / 推論用語 / 無引文的官方主張
def cls(ev, tid):
    return ev["official_evidence_assessment"]["per_target"][tid]["classification"]
ev = gj.evaluate(us, pk, single_result(pk))
check("direct support classified", cls(ev, "B") == "direct_support" and cls(ev, "A") == "refuted", ev["official_evidence_assessment"]["per_target"])
check("alignment recorded", all(c["alignment"] in ("mentions_option_and_scenario", "mentions_option") for c in ev["citations"]))
r = single_result(pk)
r["option_analysis"][1]["citations"] = [cite(pk, "Sentinel stores SigninLogs data in the workspace table", "supports_option")]
ev = gj.evaluate(us, pk, r)
check("option-only quote → indirect", cls(ev, "B") == "indirect_support" and ev["confidence"] == "中"
      and ev["citations"][1]["alignment"] == "mentions_option_only", [c["alignment"] for c in ev["citations"]])
r = single_result(pk)
r["option_analysis"][1]["citations"] = [cite(pk, "AuditLogs records changes such as user creation", "supports_option")]
ev = gj.evaluate(us, pk, r)
check("quote not mentioning option → indirect", cls(ev, "B") == "indirect_support"
      and any(c["alignment"] == "does_not_mention_option" for c in ev["citations"]))
r = single_result(pk)
r["option_analysis"][3]["citations"] = [cite(pk, "The SigninLogs table contains Microsoft Entra sign-in events", "refutes_option")]
ev = gj.evaluate(us, pk, r)
check("refute not mentioning option ignored", cls(ev, "D") == "no_support" and ev["confidence"] == "中"
      and any("不計為反證" in f for f in ev["official_evidence_assessment"]["per_target"]["D"]["flags"]))
r = single_result(pk)
r["option_analysis"][1]["note"] = "This is the industry-standard policy name."
ev = gj.evaluate(us, pk, r)
check("inference language flagged", ev["confidence"] == "中"
      and any("推論" in f for f in ev["official_evidence_assessment"]["per_target"]["B"]["flags"]))
r = single_result(pk)
r["option_analysis"][0]["citations"] = []
ev = gj.evaluate(us, pk, r)
check("official claim without citation flagged", any("沒有可核對的引文" in f for f in ev["official_evidence_assessment"]["per_target"]["A"]["flags"]))
check("unexcluded option → not program-sufficient", ev["official_evidence_assessment"]["program_docs_sufficient"] is False
      and any("程式核對結果不足" in x for x in ev["uncertainty"]))

# 情境詞區辨力（關鍵字比對，非語意驗證）
QU = {"id": "m-q", "type": "single_choice",
      "question": ("You need to receive a security alert when a user attempts to sign in from a location that was never "
                   "used by the other users in your organization. Which Microsoft Defender for Cloud Apps anomaly "
                   "detection policy should you use?"),
      "options": [{"id": "A", "text": "Impossible travel"}, {"id": "B", "text": "Activity from anonymous IP addresses"},
                  {"id": "C", "text": "Activity from infrequent country"}, {"id": "D", "text": "Malware detection"}],
      "answer": "C"}
uq = gj.eq.Unit("m-q", "m-q", "single_choice", QU)
mq, scen_q, info_q = gj.evaluation_matchers(uq)
check("generic words excluded", all(w.lower() not in [x.lower() for x in scen_q] for w in ("user", "users", "alert", "security", "organization", "policy")),
      info_q)
check("option words excluded", "detection" not in [x.lower() for x in scen_q] and "detection" in [x.lower() for x in info_q["excluded_option_words"]],
      info_q)
check("distinctive words kept", {"location", "never"} <= {x.lower() for x in scen_q}, scen_q)
al = gj.citation_alignment
q_generic = "For example, you can set the Activity from infrequent country/region detection to ignore a specific user who travels frequently."
check("generic-only hits not direct", al(q_generic, mq["C"], scen_q, "supports_option", True) == "mentions_option_only")
q_optwords = "Activity from infrequent country detection is an anomaly detection activity for users and alerts."
check("option-word-only hits not direct", al(q_optwords, mq["C"], scen_q, "supports_option", True) == "mentions_option_only")
q_good = ("Activity from infrequent country: an alert is raised when activity comes from a location that was never "
          "visited by any user in the organization, so a sign in from that location is flagged.")
check("distinctive scenario words → aligned", al(q_good, mq["C"], scen_q, "supports_option", True) == "mentions_option_and_scenario")
check("insufficient scenario terms → not aligned", al(q_good, mq["C"], ["location"], "supports_option", True) == "insufficient_scenario_terms")
check("plural/singular same term", gj.term_base("Users") == gj.term_base("user") and gj.term_base("policies") == gj.term_base("policy"))
check("generic list configurable", "detection" in gj.GENERIC_SCENARIO_TERMS and isinstance(gj.GENERIC_SCENARIO_TERMS, set))
ev_q = gj.evaluate(uq, {"bank_answer": {"value": "C"}, "examtopics": gj.examtopics_section(None),
                        "verification_status": {"answer_status": "pending", "microsoft_consistency": "no_verified_evidence",
                                                "proposed_answer": None},
                        "microsoft_learn": {"status": "found", "documents": [{"doc_id": "D1", "passages": [
                            {"passage_id": "D1-P1", "text": q_generic, "selection_reasons": ["relevance"]}]}]}},
                   {"suggested_answer": "C", "option_analysis": [
                       item("A", "incorrect", []), item("B", "incorrect", []),
                       item("C", "correct", [{"passage_id": "D1-P1", "quote": q_generic, "relation": "supports_option"}]),
                       item("D", "incorrect", [])], "docs_sufficient": True})
check("generic quote → indirect, not direct", ev_q["official_evidence_assessment"]["per_target"]["C"]["classification"] == "indirect_support")
check("alignment note recorded", ev_q["alignment_method"] == "keyword_heuristic" and "不是語意驗證" in ev_q["alignment_note"])
check("scenario terms recorded", ev_q["scenario_terms"]["used"] == scen_q)

# 社群共識與官方證據分離
r = single_result(pk)
for it in r["option_analysis"]:
    it["citations"], it["basis"] = [], "community"
ev = gj.evaluate(us, pk, r)
er = ev["community_recommendation"]
check("community rec follows consensus", er["answer"] == "B" and er["strength"] == "consensus" and "90%" in er["basis"], er)
check("consensus does not raise official confidence", ev["official_evidence_assessment"]["confidence"] == "低")
pk_noet2 = copy.deepcopy(pk)
pk_noet2["examtopics"] = gj.examtopics_section(None)
er2 = gj.evaluate(us, pk_noet2, single_result(pk_noet2))["community_recommendation"]
check("community rec empty without community (no bank fill)", er2["strength"] == "none" and er2["answer"] is None)
check("community rec computed even if answer invalid", gj.evaluate(us, pk, single_result(pk, answer='"Z"'))["community_recommendation"]["answer"] == "B")

ev = gj.evaluate(us, pk, single_result(pk, sufficient=False))
check("docs insufficient → 中", ev["confidence"] == "中")

r = single_result(pk)
r["option_analysis"][3] = item("D", "incorrect", [], basis="model_inference")
ev = gj.evaluate(us, pk, r)
check("single: distractor not excluded → 中", ev["confidence"] == "中", ev)
check("single: gap recorded, not refuted", any("選項 D" in g and "未提及不視為反駁" in g for g in ev["evidence_gaps"]))
check("single: cap reason stated", any("不足以排除其他選項" in x for x in ev["caps"]))

r = single_result(pk)
r["option_analysis"][1]["basis"] = "official_indirect"
check("indirect → 中", gj.evaluate(us, pk, r)["confidence"] == "中")

r = single_result(pk)
r["option_analysis"] = r["option_analysis"][:3]
ev = gj.evaluate(us, pk, r)
check("missing target → 中", ev["confidence"] == "中" and any("缺少 D" in x for x in ev["caps"]))

pk2 = copy.deepcopy(pk)
pk2["verification_status"].update(microsoft_consistency="proposed_supported", proposed_answer=None)
ev = gj.evaluate(us, pk2, single_result(pk2))
check("human evidence conflict → recorded, not 低", ev["confidence"] == "高"
      and any("人工查證" in x for x in ev["official_conflicts"]) and any("衝突" in x for x in ev["uncertainty"]), ev["official_conflicts"])
pk2["verification_status"].update(microsoft_consistency="consistent", answer_status="disputed")
ev = gj.evaluate(us, pk2, single_result(pk2))
check("disputed → uncertainty only, not capped", ev["confidence"] == "高" and any("disputed" in x for x in ev["uncertainty"]))

# 複選題：D 未提及 → 證據缺口、最高為中
um = unit(env, "m-multi")
rm = {"suggested_answer": '["A","C"]',
      "option_analysis": [item("A", "correct", [cite(pkm, "Enable the Microsoft Defender XDR connector to collect alerts", "supports_option")]),
                          item("B", "incorrect", [cite(pkm, "Syslog connector does not collect Defender for Endpoint alerts", "refutes_option")]),
                          item("C", "correct", [cite(pkm, "Enable the Microsoft Entra ID connector for sign-in logs", "supports_option")]),
                          item("D", "incorrect", [], basis="model_inference")],
      "community_assessment": "未取得", "rationale": "...", "docs_sufficient": True,
      "conflicts": [], "open_questions": ["D 未見於文件"], "model_confidence": "高"}
ev = gj.evaluate(um, pkm, rm)
check("multi distractor gap → 中", ev["confidence"] == "中", ev)
check("not mentioned ≠ refuted", any("選項 D" in g and "未提及不視為反駁" in g for g in ev["evidence_gaps"]))
check("multi gap cap stated", any("不足以排除其他選項" in x for x in ev["caps"]))
rm2 = copy.deepcopy(rm)
rm2["option_analysis"][3]["citations"] = [{"passage_id": pid_with(pkm, "Syslog"), "quote": "AWS S3 connector is not supported for this scenario", "relation": "refutes_option"}]
check("forged refute does not close gap", gj.evaluate(um, pkm, rm2)["confidence"] == "中")

ust = unit(env, "m-stmt")
rs = {"suggested_answer": '{"s1":"Yes","s2":"No"}',
      "option_analysis": [item("s1", "correct", [cite(pkst, "Playbooks in Microsoft Sentinel are based on workflows built in Azure Logic Apps", "supports_option")]),
                          item("s2", "correct", [cite(pkst, "Scheduled analytics rules can run at intervals as short as five minutes", "supports_option")])],
      "community_assessment": "", "rationale": "", "docs_sufficient": True, "conflicts": [], "open_questions": [],
      "model_confidence": "中"}
check("statement matrix → 高", gj.evaluate(ust, pkst, rs)["confidence"] == "高")

uno = va.Ctx(env_fail.bank, env_fail.out).units["m-single"]
rn = single_result(pk)
for it in rn["option_analysis"]:
    it["citations"], it["basis"] = [], "model_inference"
ev = gj.evaluate(uno, pkf, rn)
check("no docs → 低 + gap", ev["confidence"] == "低" and "尚未找到官方依據" in ev["evidence_gaps"])

# ---------------------------------------------------------------------------
# 3. 執行（假 client）
# ---------------------------------------------------------------------------


class FakeAPIError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def resp(obj):
    return SimpleNamespace(text=obj if isinstance(obj, str) else json.dumps(obj),
                           usage_metadata=SimpleNamespace(prompt_token_count=1000, candidates_token_count=200,
                                                          thoughts_token_count=50, total_token_count=1250),
                           candidates=[SimpleNamespace(finish_reason="STOP")])


class FakeClient:
    def __init__(self, behaviours):
        self.behaviours, self.calls = list(behaviours), []
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls.append((model, contents))
        b = self.behaviours.pop(0)
        if isinstance(b, Exception):
            raise b
        return resp(b)


def run(env, client, extra=("--tier", "paid"), confirm=True):
    gj.confirm = lambda prompt: confirm
    made = []

    def factory(key):
        made.append(key)
        return client
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = gj.main(env.argv + ["--run", *extra], client_factory=factory)
    return rc, buf.getvalue(), made


def results(env):
    p = env.out / "gemini_judgments.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


e1 = setup()
before = (sha(e1.bank), sha(e1.out / "verification.json"))
pk1 = packet_of(e1, "m-single")
c = FakeClient([single_result(pk1), FakeAPIError(503, "UNAVAILABLE high demand"), FakeAPIError(503, "UNAVAILABLE")])
rc, log, made = run(e1, c)
res = results(e1)
check("503 x2 stops batch", rc == 1 and "連續 2 次" in log, log)
check("each question called once", len(c.calls) == 3)
check("503 → deferred", res["questions"]["m-multi"]["runs"][0]["status"] == "deferred"
      and res["questions"]["m-multi"]["runs"][0]["http_status"] == 503)
check("success evaluated", res["questions"]["m-single"]["runs"][0]["evaluation"]["confidence"] == "高")
check("usage recorded", res["questions"]["m-single"]["runs"][0]["usage"]["input_tokens"] == 1000)
check("model name recorded", res["questions"]["m-single"]["runs"][0]["model"] == "gemini-3.1-flash-lite")
check("bank and verification untouched", (sha(e1.bank), sha(e1.out / "verification.json")) == before)
check("sent prompt is packet", '"question_id": "m-single"' in c.calls[0][1] and "pinned_human_excerpt" not in c.calls[0][1])

e2 = setup()
c = FakeClient([FakeAPIError(429, f"RESOURCE_EXHAUSTED quota key={FAKE_KEY}"), {}, {}])
rc, log, _ = run(e2, c)
res = results(e2)
check("429 stops immediately", rc == 1 and len(c.calls) == 1 and "429" in log)
check("429 remaining not sent", "m-multi" not in res["questions"] and "未送出" in log)
check("key redacted in record", FAKE_KEY not in (e2.out / "gemini_judgments.json").read_text(encoding="utf-8"))
check("key redacted in log", FAKE_KEY not in log)

e3 = setup()
c = FakeClient([FakeAPIError(401, "API key not valid"), {}, {}])
rc, log, _ = run(e3, c)
check("401 fatal stop", rc == 1 and len(c.calls) == 1 and results(e3)["questions"]["m-single"]["runs"][0]["status"] == "fatal")

e4 = setup()
c = FakeClient(["not json", {"suggested_answer": '"Z"'}, {}])
rc, log, _ = run(e4, c)
res = results(e4)
check("invalid json → invalid_output, no retry", res["questions"]["m-single"]["runs"][0]["status"] == "invalid_output" and len(c.calls) == 3)
check("invalid answer recorded", res["questions"]["m-multi"]["runs"][0]["status"] == "invalid_answer")

e5 = setup()
c = FakeClient([{}])
rc, log, made = run(e5, c, confirm=False)
check("cancel → no client", rc == 2 and not made and not c.calls and results(e5) is None)
rc, log, made = run(e5, c, extra=())
check("--run requires tier", rc == 2 and not made)

e6 = setup(ids=("m-single",))
pf = gj.latest_packet(e6.out / "gemini_judge" / "packets", "m-single")
data = json.loads(pf.read_text(encoding="utf-8"))
data["packet"]["bank_answer"]["value"] = "C"
pf.write_text(json.dumps(data), encoding="utf-8")
c = FakeClient([{}])
rc, log, made = run(e6, c)
check("tampered packet refused", rc == 2 and not made and "雜湊不符" in log)

e7 = setup(ids=("m-single",))
b = json.loads(e7.bank.read_text(encoding="utf-8"))
b["questions"][0]["answer"] = "C"
e7.bank.write_text(json.dumps(b), encoding="utf-8")
c = FakeClient([{}])
rc, log, made = run(e7, c)
check("bank changed refused", rc == 2 and not made and "題庫已在 dry-run 後變更" in log)

e8 = setup(ids=("m-single",))
pk8 = packet_of(e8, "m-single")
run(e8, FakeClient([single_result(pk8)]))
run(e8, FakeClient([single_result(pk8, answer='"C"')]), extra=("--tier", "paid", "--force"))  # 第二次需 --force，否則依 P0-1 跳過
runs = results(e8)["questions"]["m-single"]["runs"]
check("history appended", len(runs) == 2)
check("backup made", any(p.name.startswith("gemini_judgments.") for p in (e8.out / "backups").iterdir()))
buf = io.StringIO()
with redirect_stdout(buf):
    gj.main(["--bank", str(e8.bank), "--output-dir", str(e8.out), "--report"])
md = (e8.out / "gemini_judgments.md").read_text(encoding="utf-8")
check("report has fields", all(s in md for s in ("最終考試推薦", "題庫原始答案", "Gemini 建議", "ExamTopics 社群推薦", "官方證據等級",
                                                  "引句存在", "模型主張", "引文與主張對應", "產品變更提示",
                                                  "不代表答案已被官方文件證實", "不是正確率")), md[:800])
check("report has no human-review wording", "人工複核" not in md)
check("report shows latest run", "\"C\"" in md)

# ---------------------------------------------------------------------------
# 4. 最終考試推薦：單選／多選的選項對應與回退；其他題型不套用回退
# ---------------------------------------------------------------------------

FR = gj.final_recommendation


def fin(u, packet, result, status="done"):
    ev = gj.evaluate(u, packet, result)
    return FR(u, packet, ev, ev["status"] if status == "done" else status, result), ev


# 單選
f, _ = fin(us, pk, single_result(pk))
check("F1 qualifying community + Gemini agrees → community", f["source"] == "community" and f["answer"] == "B"
      and f["agrees_with_gemini"] == "agree" and f["fallback_rule_applies"] is True)
f, _ = fin(us, pk_noet, single_result(pk_noet))
check("F1b no community, Gemini valid same → gemini", f["source"] == "gemini" and f["answer"] == "B" and f["agrees_with_bank"] == "agree")
f, ev_b = fin(us, pk_b, single_result(pk_b))
check("F2 single valid differs → gemini kept", f["source"] == "gemini" and f["answer"] == "B" and f["bank_answer"] == "C"
      and f["agrees_with_bank"] == "disagree" and f["fallback_reason"] is None)
check("F2 no review/caps from bank difference", ev_b["confidence"] == "高" and "review_priority" not in ev_b)
pk_cd = copy.deepcopy(pk)
pk_cd["examtopics"]["community_vote"]["answer"] = "C"  # 社群 C、題庫 B：回退必須回到題庫，不是社群
for bad in ('"Z"', "Z", "", "B C", "b"):
    f, _ = fin(us, pk_cd, single_result(pk_cd, answer=bad))
    check(f"F3/F4 qualifying community, Gemini invalid {bad!r} → community (not bank)", f["source"] == "community"
          and f["answer"] == "C" and f["gemini_status"] == "invalid_answer" and f["fallback_reason"] is None)
    f, _ = fin(us, pk_noet, single_result(pk_noet, answer=bad))
    check(f"F3/F4 no community, Gemini invalid {bad!r} → bank fallback", f["source"] == "bank_fallback" and f["answer"] == "B"
          and f["fallback_reason"] == "invalid_answer")
f, _ = fin(us, pk_noet, single_result(pk_noet, answer="B"))
check("single plain id valid (no community) → gemini", f["source"] == "gemini" and f["answer"] == "B")
f, _ = fin(us, pk_c, single_result(pk_c))
check("F8 Gemini differs from qualifying community → community, Gemini kept", f["source"] == "community" and f["answer"] == "C"
      and f["gemini_answer"] == "B" and f["agrees_with_gemini"] == "disagree"
      and any("依推薦策略採用社群推薦" in x for x in f["uncertainty"]))
f, _ = fin(us, pk, single_result(pk))
check("F7 agrees with consensus", f["agrees_with_community"] == "agree" and f["community_strength"] == "consensus")

# 多選
um_ = unit(env, "m-multi")
for raw, ok_ in (("AZ", False), ("AA", False), ('["A","E"]', False), ("AC", True), ("A,C", True), ('["A","C"]', True)):
    rmx = copy.deepcopy(rm)
    rmx["suggested_answer"] = raw
    f, _ = fin(um_, pkm, rmx)
    if ok_:
        check(f"F5 multi {raw!r} valid → gemini", f["source"] == "gemini" and f["answer"] == ["A", "C"])
    else:
        check(f"F5 multi {raw!r} invalid → bank fallback", f["source"] == "bank_fallback" and f["answer"] == ["A", "C"])

# 其他題型：不套用選項對應與回退
f, _ = fin(ust, pkst, rs)
check("F7 statement valid → gemini", f["source"] == "gemini" and f["answer"] == {"s1": "Yes", "s2": "No"}
      and f["fallback_rule_applies"] is False)
for bad in ("Yes", '{"s1":"Yes"}', '{"s1":"Maybe","s2":"No"}'):
    rsb = copy.deepcopy(rs)
    rsb["suggested_answer"] = bad
    f, ev_s = fin(ust, pkst, rsb)
    check(f"F8 statement invalid {bad!r} → no fallback", ev_s["status"] == "invalid_answer" and f["answer"] is None
          and f["source"] == "none_invalid_output" and f["bank_answer"] == {"s1": "Yes", "s2": "No"})


def synth_packet(bank_value):
    return {"bank_answer": {"value": bank_value}, "examtopics": gj.examtopics_section(None),
            "verification_status": {"answer_status": "pending", "microsoft_consistency": "no_verified_evidence",
                                    "proposed_answer": None},
            "microsoft_learn": {"status": "not_found", "note": "尚未找到官方依據", "documents": []}}


DD = {"id": "m-dd", "type": "dropdown_cloze", "question": "Complete the query.", "templateType": "code",
      "template": "T | {{select_0}} X", "blanks": [{"id": "select_0", "options": ["join", "union"], "answer": "join"}]}
udd = gj.eq.Unit("m-dd", "m-dd", "dropdown_cloze", DD)
pdd = synth_packet({"select_0": "join"})
base_dd = {"option_analysis": [item("select_0", "correct", [], basis="model_inference")], "docs_sufficient": False}
f, _ = fin(udd, pdd, dict(base_dd, suggested_answer='{"select_0":"union"}'))
check("F7 dropdown valid differs → gemini", f["source"] == "gemini" and f["answer"] == {"select_0": "union"}
      and f["agrees_with_bank"] == "disagree")
for bad in ('{"select_0":"merge"}', "union", '{"select_1":"join"}'):
    f, _ = fin(udd, pdd, dict(base_dd, suggested_answer=bad))
    check(f"F8 dropdown invalid {bad!r} → no fallback", f["answer"] is None and f["source"] == "none_invalid_output")
DG = {"id": "m-dg", "type": "drag_drop_order", "question": "Order the steps.",
      "items": [{"id": "a", "text": "Step one"}, {"id": "b", "text": "Step two"}], "answer": ["a", "b"]}
udg = gj.eq.Unit("m-dg", "m-dg", "drag_drop_order", DG)
pdg = synth_packet(["a", "b"])
base_dg = {"option_analysis": [item("a", "correct", [], basis="model_inference"), item("b", "correct", [], basis="model_inference")],
           "docs_sufficient": False}
f, _ = fin(udg, pdg, dict(base_dg, suggested_answer='["b","a"]'))
check("F7 drag valid → gemini", f["source"] == "gemini" and f["answer"] == ["b", "a"])
for bad in ('["a","z"]', "ba", "b,a"):
    f, _ = fin(udg, pdg, dict(base_dg, suggested_answer=bad))
    check(f"F8 drag invalid {bad!r} → no fallback (no choice leniency)", f["answer"] is None and f["source"] == "none_invalid_output")

# API 失敗：直接以 final_recommendation 驗證各種狀態
for st in ("deferred", "rate_limited", "fatal", "invalid_output"):
    f = FR(us, pk, None, st, None)
    check(f"F6 single {st} + qualifying community → community", f["source"] == "community" and f["answer"] == "B"
          and f["gemini_status"] == st and f["fallback_reason"] is None)
    f = FR(us, pk_noet, None, st, None)
    check(f"F6 single {st} + no community → bank fallback", f["source"] == "bank_fallback" and f["answer"] == "B" and f["fallback_reason"] == st)
    f = FR(ust, pkst, None, st, None)
    check(f"F9 statement {st} → no fallback", f["answer"] is None and f["source"] == "none_api_failed" and f["fallback_reason"] == st)

# 實際 --run 紀錄中的最終推薦（e1：503、e2：429、e3：401、e4：invalid_output / invalid_answer）
r1 = results(e1)["questions"]
check("run 200 single (qualifying community) → community", r1["m-single"]["runs"][0]["final_exam_recommendation"]["source"] == "community")
check("run 503 multi → bank fallback", r1["m-multi"]["runs"][0]["final_exam_recommendation"]["answer"] == ["A", "C"]
      and r1["m-multi"]["runs"][0]["final_exam_recommendation"]["source"] == "bank_fallback")
check("run 503 statement → no fallback", r1["m-stmt"]["runs"][0]["final_exam_recommendation"]["source"] == "none_api_failed"
      and r1["m-stmt"]["runs"][0]["final_exam_recommendation"]["answer"] is None)
f2 = results(e2)["questions"]["m-single"]["runs"][0]["final_exam_recommendation"]
check("run 429 single + qualifying community → community (no bank fallback)", f2["source"] == "community" and f2["gemini_status"] == "rate_limited")
f3 = results(e3)["questions"]["m-single"]["runs"][0]["final_exam_recommendation"]
check("run 401 single + qualifying community → community", f3["source"] == "community" and f3["gemini_status"] == "fatal")
r4 = results(e4)["questions"]
check("run invalid_output single + qualifying community → community", r4["m-single"]["runs"][0]["final_exam_recommendation"]["source"] == "community"
      and r4["m-single"]["runs"][0]["final_exam_recommendation"]["gemini_status"] == "invalid_output")
check("run invalid_answer multi → bank fallback", r4["m-multi"]["runs"][0]["final_exam_recommendation"]["source"] == "bank_fallback")
check("run invalid statement → no fallback", r4["m-stmt"]["runs"][0]["final_exam_recommendation"]["source"] == "none_invalid_output")
check("bank never modified by runs", json.loads(e1.bank.read_text(encoding="utf-8")) == BANK)

# 官方文件角色、推薦依據、產品變更未分析仍採用有效建議
check("F10 no community → primary_verification", gj.official_docs_role(pk_noet)["role"] == "primary_verification")
check("F11 consensus, no notice → fact_check_only", gj.official_docs_role(pk)["role"] == "fact_check_only")
check("F11 notice → primary_verification", gj.official_docs_role(pkn)["role"] == "primary_verification"
      and any("產品變更" in x for x in gj.official_docs_role(pkn)["reasons"]))
check("F11 disputed → primary_verification", gj.official_docs_role(pk2)["role"] == "primary_verification")
check("model view has official_docs_role", gj.model_view(pk)["official_docs_role"]["role"] == "fact_check_only")
check("schema requires recommendation_basis", "recommendation_basis" in gj.RESPONSE_SCHEMA["required"])
f, _ = fin(us, pk, dict(single_result(pk), recommendation_basis="community_consensus"))
check("F13 recommendation_basis recorded", f["recommendation_basis"] == "community_consensus")
f, ev_n = fin(us, pkn, nt_result())
check("F14 notice not analyzed → adopted per priority, incomplete recorded", f["source"] == "community"
      and f["completeness"] == "incomplete" and f["may_be_outdated"] is True)
pkn_noet = copy.deepcopy(pkn)
pkn_noet["examtopics"] = gj.examtopics_section(None)
f, _ = fin(us, pkn_noet, nt_result())
check("F14b notice not analyzed, no community → gemini still adopted", f["source"] == "gemini" and f["completeness"] == "incomplete")
check("prompt: community first", "優先參考已確認為同一題" in gj.SYSTEM_INSTRUCTION
      and "不可只因看似矛盾就直接推翻社群推薦" in gj.SYSTEM_INSTRUCTION)

# 離線重核補上最終推薦；報告呈現 API 失敗
buf = io.StringIO()
e_re2 = setup(ids=("m-single",))
pf2 = gj.latest_packet(e_re2.out / "gemini_judge" / "packets", "m-single")
_, sha2, _ = gj.load_packet(pf2)
orig2 = {"at": "2026-10-27T00:00:00+00:00", "model": "gemini-3.1-flash-lite", "tier": "free", "prompt_version": "judge-v2",
         "packet_file": pf2.name, "packet_sha256": sha2, "status": "done", "http_status": 200, "error": None,
         "usage": {"input_tokens": 1, "output_tokens": 1, "thoughts_tokens": 0, "total_tokens": 2},
         "model_output": single_result(packet_of(e_re2, "m-single"), answer='"Z"'), "evaluation": None}
(e_re2.out / "gemini_judgments.json").write_text(json.dumps({"meta": {}, "questions": {"m-single": {"runs": [orig2]}}}), encoding="utf-8")
with redirect_stdout(buf):
    gj.main(e_re2.argv + ["--reevaluate"])
rr = json.loads((e_re2.out / "gemini_judgments.json").read_text(encoding="utf-8"))["questions"]["m-single"]["runs"]
check("F15 reevaluate adds final (invalid Gemini, qualifying community → community)",
      rr[1]["final_exam_recommendation"]["source"] == "community" and rr[1]["final_exam_recommendation"]["answer"] == "B"
      and rr[0] == orig2)
md_fail = gj.render_report(results(e1))
check("report: API failure shows no-fallback for statement", "此題型不套用回退" in md_fail and "回退為題庫原始答案" in md_fail)
check("report: old record without final handled", "舊版紀錄未產生" in gj.render_report(
    {"questions": {"x": {"runs": [dict(orig2, evaluation=None)]}}}))

# ---------------------------------------------------------------------------
# 5. 社群採用門檻（eval-v5）：同一題、≥ 10 票、未四捨五入支持率 ≥ 70%、答案可對應本題
# ---------------------------------------------------------------------------


def with_vote(base, answer, dist=None, share=None, count=None, match="exact"):
    """以 verification.json 的實際 schema（community_vote: answer / share_percent / vote_count / distribution）建立社群資料。"""
    p = copy.deepcopy(base)
    e = {"examtopics_evidence": {"match_status": match, "url": "https://www.examtopics.com/discussions/x/", "captured_at": None,
                                 "page_answer": answer, "main_reasons": [], "counterarguments": [],
                                 "community_vote": {"answer": answer, "share_percent": share, "vote_count": count,
                                                    "as_of": "2026-10-27", "distribution": dist}}}
    p["examtopics"] = gj.examtopics_section(e)
    return p


def D(*pairs):
    tot = sum(v for _, v in pairs)
    return [{"answer": a, "percent": round(v * 100 / tot), "votes": v} for a, v in pairs]


gem_c = single_result(pk_noet, answer='"C"')  # Gemini 有效建議 C（與社群答案 B 不同）
cases = [
    ("T1 10 votes, exactly 70% → community", with_vote(pk_noet, "B", D(("B", 7), ("C", 3)), 70, 10), "community", "B"),
    ("T2 9 votes, 100% → gemini", with_vote(pk_noet, "B", D(("B", 9)), 100, 9), "gemini", "C"),
    ("T3 20 votes, 69% (recorded, no distribution) → gemini", with_vote(pk_noet, "B", None, 69, 20), "gemini", "C"),
    ("T3b 100 votes, 69% (distribution) → gemini", with_vote(pk_noet, "B", D(("B", 69), ("C", 31)), 69, 100), "gemini", "C"),
    ("T3c 69.5% exact, displayed 70 → gemini (no rounding)", with_vote(pk_noet, "B", D(("B", 139), ("C", 61)), 70, 200), "gemini", "C"),
    ("T3d 70.0% exact (displayed 70) → community", with_vote(pk_noet, "B", D(("B", 140), ("C", 60)), 70, 200), "community", "B"),
    ("T4 probable match → gemini", with_vote(pk_noet, "B", D(("B", 30)), 100, 30, match="probable"), "gemini", "C"),
    ("T4b exact but no votes → gemini", with_vote(pk_noet, None, None, None, None), "gemini", "C"),
    ("T4c tie → gemini", with_vote(pk_noet, None, D(("B", 10), ("C", 10)), 50, 20), "gemini", "C"),
    ("T5 community answer not an option → gemini", with_vote(pk_noet, "Z", D(("Z", 30)), 100, 30), "gemini", "C"),
    ("T5b distribution top ≠ recorded answer → gemini", with_vote(pk_noet, "B", D(("C", 25), ("B", 5)), 83, 30), "gemini", "C"),
    ("T5c no votes recorded at all → gemini", with_vote(pk_noet, "B", None, None, None), "gemini", "C"),
]
for name, packet, source, answer in cases:
    f, _ = fin(us, packet, gem_c)
    check(name, f["source"] == source and f["answer"] == answer, (f["source"], f["answer"], f["community"]))
q695 = gj.community_qualification(us, with_vote(pk_noet, "B", D(("B", 139), ("C", 61)), 70, 200)["examtopics"])
check("share computed from distribution, unrounded", q695["source"] == "distribution" and abs(q695["share_exact"] - 69.5) < 1e-9
      and q695["share_displayed"] == 70 and any("69.5%" in r for r in q695["reasons"]), q695)
q3 = gj.community_qualification(us, with_vote(pk_noet, "B", None, 69, 20)["examtopics"])
check("no distribution → recorded share used as-is", q3["source"] == "recorded_share_percent" and q3["share_exact"] == 69
      and q3["total_votes"] == 20)
f, _ = fin(us, with_vote(pk_noet, "B", D(("B", 9)), 100, 9), gem_c)
check("not-adopted reasons recorded", f["community"]["qualifies"] is False and any("未達 10" in r for r in f["community"]["not_adopted_reasons"]))
mm = gj.community_qualification(um_, with_vote(pkm, ["A", "C"], D(("AC", 8), ("AD", 2)), 80, 10)["examtopics"])
check("multi distribution 'AC' maps to ['A','C']", mm["qualifies"] is True, mm)

# 6～8：社群達門檻時的 Gemini 各種狀態
pq = with_vote(pk_noet, "B", D(("B", 18), ("C", 2)), 90, 20)
f, _ = fin(us, pq, single_result(pq))
check("T6 qualifying + Gemini agrees → community", f["source"] == "community" and f["agrees_with_gemini"] == "agree")
f, _ = fin(us, pq, single_result(pq, answer='"C"'))
check("T7 qualifying + Gemini differs → community, Gemini kept", f["source"] == "community" and f["answer"] == "B"
      and f["gemini_answer"] == "C" and f["gemini_status"] == "valid")
f, _ = fin(us, pq, single_result(pq, answer='"Z"'))
check("T8 qualifying + Gemini invalid → community, no bank fallback", f["source"] == "community" and f["fallback_reason"] is None)
pq_bank_c = copy.deepcopy(pq)
pq_bank_c["bank_answer"]["value"] = "C"
f = FR(us, pq_bank_c, None, "deferred", None)
check("T8b qualifying + API failure → community B, not bank C", f["source"] == "community" and f["answer"] == "B" and f["bank_answer"] == "C")
# 9～10：沒有合格社群推薦
f, _ = fin(us, pk_noet, single_result(pk_noet, answer='"C"'))
check("T9 no qualifying community + Gemini valid → gemini", f["source"] == "gemini" and f["answer"] == "C")
f = FR(us, pk_noet, None, "deferred", None)
check("T10 no qualifying community + API failure (single) → bank fallback", f["source"] == "bank_fallback" and f["answer"] == "B")
f = FR(ust, pkst, None, "deferred", None)
check("T10b no community + API failure (statement) → empty", f["source"] == "none_api_failed" and f["answer"] is None)
# 11～14：官方文件衝突與既有品質規則
r = single_result(pk_noet)
r["option_analysis"][1]["citations"].append(cite(pk_noet, "Sentinel stores SigninLogs data in the workspace table", "refutes_option"))
ev = gj.evaluate(us, pk_noet, r)
check("T11 official quote refutes Gemini option → conflict recorded, not 低", ev["confidence"] != "低"
      and any("反駁建議答案中的 B" in x for x in ev["official_conflicts"]), (ev["confidence"], ev["official_conflicts"]))
f = FR(us, pk_noet, ev, "evaluated", r)
check("T11b conflict does not change Gemini final", f["source"] == "gemini" and f["answer"] == "B" and f["official_conflicts"])
pkh = copy.deepcopy(pk_noet)
pkh["bank_answer"]["value"] = "C"
pkh["verification_status"]["microsoft_consistency"] = "consistent"  # 人工查證官方證據支持題庫答案 C
ev = gj.evaluate(us, pkh, single_result(pkh))
check("T12 human-verified evidence vs Gemini → conflict recorded, not 低", ev["confidence"] == "高"
      and any("人工查證" in x for x in ev["official_conflicts"]), (ev["confidence"], ev["official_conflicts"]))
r = single_result(pk_noet)
for it in r["option_analysis"]:
    it["citations"], it["basis"] = [], "model_inference"
check("T13 no verifiable official quote → still 低", gj.evaluate(us, pk_noet, r)["confidence"] == "低")
r = single_result(pk_noet)
r["option_analysis"][3] = item("D", "incorrect", [], basis="model_inference")
check("T14 unexcluded distractor → still 中", gj.evaluate(us, pk_noet, r)["confidence"] == "中")
pkcf = copy.deepcopy(pk_noet)
pkcf["verification_status"]["microsoft_consistency"] = "conflicting"
check("T14b conflicting human evidence (quality rule) → still 低", gj.evaluate(us, pkcf, single_result(pkcf))["confidence"] == "低")
# 15：報告呈現
md_v5 = gj.render_report({"questions": {"m-single": {"runs": [{
    "at": "t", "kind": "api_call", "model": "m", "tier": "free", "prompt_version": "judge-v3", "status": "done",
    "packet_summary": {"bank_answer": "B"}, "model_output": single_result(pq, answer='"C"'),
    "evaluation": gj.evaluate(us, pq, single_result(pq, answer='"C"')),
    "final_exam_recommendation": fin(us, pq, single_result(pq, answer='"C"'))[0]}]}}})
check("T15 report shows community adoption, votes, share, Gemini answer",
      all(s in md_v5 for s in ("採用達門檻的社群推薦", "✅ 達門檻", "票數 20", "支持率 90%", "Gemini 建議：\"C\"", "考試答案推薦策略")), md_v5[:900])
check("bank unchanged after all tests", json.loads(env.bank.read_text(encoding="utf-8")) == BANK)

# ---------------------------------------------------------------------------
# 6. 正式共識資格的一致性：model_view / official_docs_role / packet_summary / 最終推薦 使用同一套判斷
# ---------------------------------------------------------------------------

def D2(*pairs, displayed=None):
    """投票分布；percent 為頁面顯示的整數（displayed 可指定最高票的顯示值）。"""
    tot = sum(v for _, v in pairs)
    out = [{"answer": a, "percent": round(v * 100 / tot), "votes": v} for a, v in pairs]
    if displayed is not None:
        out[0]["percent"] = displayed
    return out


consistency_cases = [
    # 名稱, 社群答案, 分布, 顯示 share_percent, vote_count, 預期正式資格
    ("C1 69.49%", "B", D2(("B", 6949), ("C", 3051)), 69, 10000, False),
    ("C2 69.50% shown 70", "B", D2(("B", 139), ("C", 61), displayed=70), 70, 200, False),
    ("C3 69.99% shown 70", "B", D2(("B", 6999), ("C", 3001), displayed=70), 70, 10000, False),
    ("C4 70.00% (7/10)", "B", D2(("B", 7), ("C", 3)), 70, 10, True),
    ("C5 9 votes 100%", "B", D2(("B", 9)), 100, 9, False),
    ("C6 tie with recorded answer", "B", D2(("B", 10), ("C", 10)), 50, 20, False),
    ("C7 distribution top ≠ recorded", "B", D2(("C", 25), ("B", 5)), 83, 30, False),
    ("C8 no distribution, recorded 69.5", "B", None, 69.5, 20, False),
    ("C9 no distribution, recorded 70", "B", None, 70, 20, True),
]
gem_valid_c = single_result(pk_noet, answer='"C"')
for name, ans, dist, shown, count, expected in consistency_cases:
    pkt = with_vote(pk_noet, ans, dist, shown, count)
    q = gj.community_qualification(us, pkt["examtopics"])
    fc = gj.formal_consensus(pkt)
    mv = gj.model_view(pkt)
    role = gj.official_docs_role(pkt)["role"]
    summ = gj.packet_summary(pkt)["examtopics"]
    f, _ = fin(us, pkt, gem_valid_c)
    check(f"{name}: qualification = {expected}", q["qualifies"] is expected, q["reasons"])
    check(f"{name}: formal_consensus == community_qualification", fc["qualifies"] == q["qualifies"] and fc["reasons"] == q["reasons"]
          and fc["share_exact"] == q["share_exact"])
    check(f"{name}: model_view formal_consensus consistent", mv["examtopics"]["formal_consensus"]["qualifies"] is expected
          and "consensus_threshold" not in mv["examtopics"])
    check(f"{name}: official_docs_role consistent", role == ("fact_check_only" if expected else "primary_verification"), role)
    check(f"{name}: packet_summary consistent", ("正式共識 達成" in summ) is expected and ("正式共識 未達" in summ) is (not expected), summ)
    check(f"{name}: final recommendation consistent", f["source"] == ("community" if expected else "gemini")
          and f["community"]["qualifies"] is expected, (f["source"], f["community"]))

# 69.50%／69.99%：頁面顯示值門檻為「達成」，但送給 Gemini 的正式資格、官方角色、摘要、最終推薦都是「未達」
for name, top, other in (("69.50%", 139, 61), ("69.99%", 6999, 3001)):
    pkt = with_vote(pk_noet, "B", D2(("B", top), ("C", other), displayed=70), 70, top + other)
    mv = gj.model_view(pkt)
    check(f"{name}: display-based threshold kept but labelled", mv["examtopics"]["display_based_threshold"]["met"] is True
          and "僅供參考" in mv["examtopics"]["display_based_threshold"]["note"])
    check(f"{name}: formal consensus not met in Gemini input", mv["examtopics"]["formal_consensus"]["qualifies"] is False
          and any("未達 70%" in r for r in mv["examtopics"]["formal_consensus"]["reasons"]))
    sent = gj.render_prompt(pkt)
    check(f"{name}: prompt text carries formal_consensus, not consensus_threshold",
          '"formal_consensus"' in sent and '"consensus_threshold"' not in sent and '"display_based_threshold"' in sent)
    check(f"{name}: packet itself still has original consensus_threshold", pkt["examtopics"]["consensus_threshold"]["met"] is True)

# 精度限制：沒有投票分布
fc9 = gj.formal_consensus(with_vote(pk_noet, "B", None, 70, 20))
check("C9 precision note kept", fc9["share_source"] == "recorded_share_percent" and "無法還原" in fc9.get("precision_note", ""))
check("C9 summary marks limited precision", "精度有限" in gj.packet_summary(with_vote(pk_noet, "B", None, 70, 20))["examtopics"])
check("distribution case has no precision note", "precision_note" not in gj.formal_consensus(with_vote(pk_noet, "B", D2(("B", 7), ("C", 3)), 70, 10)))
check("tie detectable only with distribution", gj.community_stats({"distribution": D2(("B", 5), ("C", 5))})["tie_detectable"] is True
      and gj.community_stats({"share_percent": 70, "vote_count": 20})["tie_detectable"] is False)
check("prompt mentions formal_consensus", "formal_consensus" in gj.SYSTEM_INSTRUCTION)

# 社群優先與 fallback 規則不變
p70 = with_vote(pk_noet, "B", D2(("B", 7), ("C", 3)), 70, 10)
p695 = with_vote(pk_noet, "B", D2(("B", 139), ("C", 61), displayed=70), 70, 200)
check("qualifying + Gemini differs → community", fin(us, p70, gem_valid_c)[0]["source"] == "community")
check("qualifying + API failure → community", FR(us, p70, None, "deferred", None)["source"] == "community")
check("69.5% + Gemini valid → gemini", fin(us, p695, gem_valid_c)[0]["answer"] == "C")
check("69.5% + API failure (single) → bank fallback", FR(us, p695, None, "deferred", None)["source"] == "bank_fallback")
check("69.5% + API failure (statement) → empty", FR(ust, pkst, None, "deferred", None)["answer"] is None)

# 證據包與雜湊不受影響
before_pkt = gj.canonical(p695)
gj.model_view(p695); gj.official_docs_role(p695); gj.packet_summary(p695); gj.render_prompt(p695)
check("model_view etc. do not mutate packet", gj.canonical(p695) == before_pkt)
pf_any = gj.latest_packet(env.out / "gemini_judge" / "packets", "m-single")
_, sha_any, ok_any = gj.load_packet(pf_any)
gj.model_view(gj.load_packet(pf_any)[0])
check("packet hash verification unaffected", ok_any and gj.load_packet(pf_any)[1] == sha_any and gj.load_packet(pf_any)[2])
real_dir = PROJECT / "output" / "gemini_trial1-20261027" / "gemini_judge" / "packets"
real_ok = [gj.load_packet(x)[2] for x in sorted(real_dir.glob("*.json"))]
check("existing real packets still verify (read-only)", real_ok and all(real_ok), len(real_ok))
check("bank unchanged (final)", json.loads(env.bank.read_text(encoding="utf-8")) == BANK)

# ---------------------------------------------------------------------------
# 7. B-5：packet_unit() 題型對照、多題型共識一致性、缺欄位、舊紀錄報告；B-1～B-4 防禦性修正
# ---------------------------------------------------------------------------

TYPES_BANK = {"questions": [
    {"id": "t-single", "type": "single_choice", "question": "Which option fits the Microsoft Sentinel scenario?",
     "options": [{"id": "A", "text": "Workbooks"}, {"id": "B", "text": "Hunting"}, {"id": "C", "text": "Notebooks"}], "answer": "B"},
    {"id": "t-multi", "type": "multiple_choice", "question": "Which two options apply?",
     "options": [{"id": "A", "text": "alpha"}, {"id": "B", "text": "beta"}, {"id": "C", "text": "gamma"}, {"id": "D", "text": "delta"}],
     "answer": ["A", "C"]},
    {"id": "t-dd", "type": "dropdown_cloze", "question": "Complete the query.", "templateType": "code",
     "template": "T | {{select_0}} | {{select_1}}",
     "blanks": [{"id": "select_0", "options": ["join", "union"], "answer": "join"},
                {"id": "select_1", "options": ["project", "extend"], "answer": "extend"}]},
    {"id": "t-dg", "type": "drag_drop_order", "question": "Order the steps.",
     "items": [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}, {"id": "c", "text": "three"}], "answer": ["b", "a", "c"]},
    {"id": "t-sm", "type": "statement_matrix", "question": "For each statement, select Yes or No.", "columns": ["Yes", "No"],
     "statements": [{"id": "s1", "text": "first", "answer": True}, {"id": "s2", "text": "second", "answer": False}]},
    {"id": "t-case", "type": "case_study", "caseInfo": {"title": "Contoso", "sections": [{"title": "Overview", "content": "x"}]},
     "questions": [
         {"id": "t-case-1", "type": "single_choice", "question": "Case question one?",
          "options": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}], "answer": "A"},
         {"id": "t-case-2", "type": "multiple_choice", "question": "Case question two?",
          "options": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}, {"id": "C", "text": "c"}], "answer": ["A", "B"]},
         {"id": "t-case-3", "type": "dropdown_cloze", "question": "Case question three.", "templateType": "code",
          "template": "{{select_0}}", "blanks": [{"id": "select_0", "options": ["p", "q"], "answer": "q"}]}]},
]}
TU = {u.question_id: u for u in gj.eq.iter_units(TYPES_BANK)}


def mkpkt(u, ex=None, status="pending"):
    return current({"question_id": u.question_id, "type": u.type, "question": gj.question_view(u),
                    "answer_format": gj.ANSWER_FORMATS[u.type], "bank_answer": {"value": va.answer_of(u), "origin": "x"},
                    "verification_status": {"answer_status": status, "microsoft_consistency": "no_verified_evidence", "proposed_answer": None},
                    "examtopics": ex or gj.examtopics_section(None), "human_verified_microsoft_evidence": [],
                    "microsoft_learn": {"status": "found", "note": None, "documents": []}})


def accepts(u, ans):
    try:
        va.parse_answer(u, json.dumps(ans))
        return True
    except ValueError:
        return False


def invalid_answers(u, a):
    t = u.type
    if t == "single_choice":
        return ["ZZ", ["A"], {"A": 1}]
    if t == "multiple_choice":
        return [a + ["ZZ"], [a[0], a[0]], "A,ZZ"]
    if t == "dropdown_cloze":
        k = next(iter(a))
        return [dict(a, **{k: "__nope__"}), dict(a, extra="x"), {}]
    if t == "drag_drop_order":
        return [a + ["zz"], a[:1] + a[:1], []]
    if t == "statement_matrix":
        k = next(iter(a))
        return [dict(a, **{k: "Maybe"}), {}, dict(a, sX="Yes")]
    return []


def target_ids(q, t):
    key = {"single_choice": "options", "multiple_choice": "options", "drag_drop_order": "items",
           "statement_matrix": "statements", "dropdown_cloze": "blanks"}[t]
    out = [x["id"] for x in q.get(key, [])]
    if t == "dropdown_cloze":
        out += [tuple(b["options"]) for b in q.get("blanks", [])]
    return out


def vote_ex(ans, dist=None, shown=None, count=None, match="exact"):
    e = {"examtopics_evidence": {"match_status": match, "url": None, "captured_at": None, "page_answer": ans,
                                 "main_reasons": [], "counterarguments": [],
                                 "community_vote": {"answer": ans, "share_percent": shown, "vote_count": count,
                                                    "as_of": "x", "distribution": dist}}}
    return gj.examtopics_section(e)


check("B5 fixture covers all types incl. case sub-questions",
      {(u.type, u.case is not None) for u in TU.values()} >= {("single_choice", False), ("multiple_choice", False),
                                                              ("dropdown_cloze", False), ("drag_drop_order", False),
                                                              ("statement_matrix", False), ("single_choice", True),
                                                              ("multiple_choice", True), ("dropdown_cloze", True)})
for qid, u in TU.items():
    pu = gj.packet_unit(mkpkt(u))
    a = va.answer_of(u)
    check(f"B5 {qid}: type preserved", pu is not None and pu.type == u.type)
    check(f"B5 {qid}: target ids preserved", target_ids(pu.question, u.type) == target_ids(u.question, u.type))
    check(f"B5 {qid}: bank answer valid in both", accepts(u, a) and accepts(pu, a))
    check(f"B5 {qid}: invalid answers rejected in both",
          all(accepts(u, x) is False and accepts(pu, x) is False for x in invalid_answers(u, a)))
    ex = vote_ex(a, None, 100, 20)
    check(f"B5 {qid}: qualification equal (real vs rebuilt)",
          gj.community_qualification(u, ex)["qualifies"] == gj.community_qualification(pu, ex)["qualifies"] is True)

# 唯讀：若本機有真實題庫，全部子題比對（不寫入任何檔案）
real_bank_path = PROJECT / "public" / "data" / "sc200.json"
if real_bank_path.exists():
    rb = json.loads(real_bank_path.read_text(encoding="utf-8"))
    bad = []
    for u in gj.eq.iter_units(rb):
        pu = gj.packet_unit(mkpkt(u))
        a = va.answer_of(u)
        if pu is None or pu.type != u.type or any(accepts(u, x) != accepts(pu, x) for x in [a] + invalid_answers(u, a)):
            bad.append(u.question_id)
    check("B5 real bank sweep: packet_unit equivalent for all sub-questions (read-only)", not bad, bad[:5])

# 多選、題組子題的共識一致性（69.5% / 69.99% / 70%）
for qid in ("t-multi", "t-case-1", "t-case-2"):
    u = TU[qid]
    a = va.answer_of(u)
    key = gj._answer_key(a)
    for name, top, other, shown, expected in (("69.50%", 139, 61, 70, False), ("69.99%", 6999, 3001, 70, False),
                                               ("70.00%", 7, 3, 70, True)):
        tot = top + other
        dist = [{"answer": key, "percent": shown, "votes": top}, {"answer": "ZZ", "percent": 100 - shown, "votes": other}]
        pkt = mkpkt(u, vote_ex(a, dist, shown, tot))
        snap = gj.canonical(pkt)
        q = gj.community_qualification(u, pkt["examtopics"])
        fc = gj.formal_consensus(pkt)
        mv = gj.model_view(pkt)
        role = gj.official_docs_role(pkt)["role"]
        summ = gj.packet_summary(pkt)["examtopics"]
        fr = gj.final_recommendation(u, pkt, None, "deferred", None)
        check(f"B5 {qid} {name}: six functions agree ({expected})",
              q["qualifies"] is expected and fc["qualifies"] is expected
              and mv["examtopics"]["formal_consensus"]["qualifies"] is expected
              and role == ("fact_check_only" if expected else "primary_verification")
              and ("正式共識 達成" in summ) is expected
              and fr["source"] == ("community" if expected else "bank_fallback"), (q["reasons"], role, summ, fr["source"]))
        check(f"B5 {qid} {name}: packet not mutated", gj.canonical(pkt) == snap)

# B-3：缺欄位時不得略過答案格式檢查（fail-closed）
base_ok = mkpkt(TU["t-single"], vote_ex("B", None, 100, 20))
check("B3 baseline: complete packet qualifies", gj.formal_consensus(base_ok)["qualifies"] is True)
for label, mutate in (("missing question", lambda p: p.pop("question")), ("missing type", lambda p: p.pop("type")),
                      ("question not a dict", lambda p: p.__setitem__("question", "text only"))):
    p = copy.deepcopy(base_ok)
    mutate(p)
    check(f"B3 {label}: packet_unit returns None", gj.packet_unit(p) is None)
    fc = gj.formal_consensus(p)
    check(f"B3 {label}: formal consensus fail-closed", fc["qualifies"] is False
          and any("無法重建題目" in r for r in fc["reasons"]), fc["reasons"])
    check(f"B3 {label}: official_docs_role primary", gj.official_docs_role(p)["role"] == "primary_verification")
    check(f"B3 {label}: summary not met", "正式共識 未達" in gj.packet_summary(p)["examtopics"])
cq_none = gj.community_qualification(None, vote_ex("B", None, 100, 20))
check("B3 community_qualification(None) fail-closed", cq_none["qualifies"] is False
      and any("無法重建題目" in r for r in cq_none["reasons"]))

# B-2：非選擇題帶有投票分布 → 保守不採用；一般非選擇題流程不受影響
for qid in ("t-sm", "t-dd", "t-dg", "t-case-3"):
    u = TU[qid]
    a = va.answer_of(u)
    with_dist = gj.community_qualification(u, vote_ex(a, [{"answer": "X", "percent": 100, "votes": 20}], 100, 20))
    check(f"B2 {qid}: distribution on non-choice → not adopted", with_dist["qualifies"] is False
          and any("非選擇題無法核對投票分布" in r for r in with_dist["reasons"]), with_dist["reasons"])
    no_dist = gj.community_qualification(u, vote_ex(a, None, 100, 20))
    check(f"B2 {qid}: recorded vote without distribution still qualifies", no_dist["qualifies"] is True, no_dist["reasons"])
check("B2 choice with distribution unaffected",
      gj.community_qualification(TU["t-single"], vote_ex("B", [{"answer": "B", "percent": 100, "votes": 20}], 100, 20))["qualifies"] is True)

# B-4：送給 Gemini 的顯示值已標示；證據包不變
p695 = mkpkt(TU["t-single"], vote_ex("B", [{"answer": "B", "percent": 70, "votes": 139}, {"answer": "C", "percent": 30, "votes": 61}], 70, 200))
snap695 = gj.canonical(p695)
mv695 = gj.model_view(p695)
cv695 = mv695["examtopics"]["community_vote"]
check("B4 display note on community_vote", "頁面顯示" in cv695.get("display_value_note", "")
      and "formal_consensus" in cv695.get("display_value_note", "") and "share_exact" in cv695.get("display_value_note", ""))
check("B4 vote values unchanged in sent copy", cv695["share_percent"] == 70 and [d["percent"] for d in cv695["distribution"]] == [70, 30]
      and [d["votes"] for d in cv695["distribution"]] == [139, 61])
check("B4 formal share_exact is the raw-vote share", mv695["examtopics"]["formal_consensus"]["share_exact"] == 69.5)
check("B4 packet not mutated", gj.canonical(p695) == snap695 and "display_value_note" not in p695["examtopics"]["community_vote"])
check("B4 instruction says displayed values are not formal", "share_exact" in gj.SYSTEM_INSTRUCTION
      and "頁面顯示" in gj.SYSTEM_INSTRUCTION)
check("B4 no-vote packet has no note", "display_value_note" not in json.dumps(gj.model_view(mkpkt(TU["t-single"]))))

# B-1：舊紀錄摘要標註；新版摘要不標註；歷史紀錄不被修改
new_summary = gj.packet_summary(p695)
check("B1 new summary carries version marker", new_summary.get("summary_version") == gj.SUMMARY_VERSION)
old_run = {"at": "t0", "kind": "api_call", "model": "m", "tier": "free", "prompt_version": "judge-v2", "status": "done",
           "packet_summary": {"bank_answer": "C", "examtopics": "available｜\"C\" 70% / 200 票｜共識門檻 達成", "learn": "found"},
           "model_output": None, "evaluation": None}
new_run = dict(old_run, at="t1", prompt_version="judge-v4", packet_summary=new_summary)
hist = {"questions": {"q-old": {"runs": [copy.deepcopy(old_run)]}, "q-new": {"runs": [copy.deepcopy(new_run)]}}}
hist_snap = json.dumps(hist, sort_keys=True)
md_b1 = gj.render_report(hist)
sec_old = md_b1.split("## q-old")[1].split("\n## ")[0]
sec_new = md_b1.split("## q-new")[1].split("\n## ")[0]  # 報告依題號排序，需截到下一個題目標題
check("B1 old summary labelled", "舊版摘要，依當時顯示值判斷" in sec_old and "共識門檻 達成" in sec_old)
check("B1 new summary not labelled", "舊版摘要" not in sec_new and "正式共識 未達" in sec_new)
check("B1 history not modified by report", json.dumps(hist, sort_keys=True) == hist_snap)
check("B1 old run keeps its prompt version in report", "prompt judge-v2" in sec_old)
trial_path = PROJECT / "output" / "gemini_trial1-20261027" / "gemini_judgments.json"
if trial_path.exists():
    trial_raw = trial_path.read_text(encoding="utf-8")
    md_trial = gj.render_report(json.loads(trial_raw))
    check("B1 real trial (judge-v2/eval-v3) readable, old summaries labelled (read-only)",
          md_trial.count("舊版摘要，依當時顯示值判斷") == 5 and trial_path.read_text(encoding="utf-8") == trial_raw)
check("versions unchanged", gj.PROMPT_VERSION == "judge-v4" and gj.EVALUATOR_VERSION == "eval-v5")

# ---------------------------------------------------------------------------
# 8. --run 安全保護：A. 必須指定獨立輸出目錄；B. 必須有有效的 verification.json 快照
# ---------------------------------------------------------------------------

OFFICIAL_OUT = gj.va.DEFAULT_OUT


def official_state():
    """正式 output/ 的檔名清單與既有判讀結果雜湊（用於確認拒絕情境沒有寫入正式目錄）。"""
    names = sorted(p.name for p in OFFICIAL_OUT.iterdir()) if OFFICIAL_OUT.exists() else []
    gjf = OFFICIAL_OUT / "gemini_judgments.json"
    return names, (hashlib.sha256(gjf.read_bytes()).hexdigest() if gjf.exists() else None)


def guarded_run(extra_argv, bank=None):
    """以 --run 呼叫 main，記錄是否建立 client、是否進入互動確認。"""
    made, asked = [], []
    gj.confirm = lambda prompt: (asked.append(prompt), True)[1]
    client = FakeClient([single_result(pk)] * 5)
    argv = ["--bank", str(bank or env.bank), "--ids", "m-single", "--run", "--tier", "free"] + list(extra_argv)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = gj.main(argv, client_factory=lambda k: (made.append(k), client)[1])
    return rc, buf.getvalue(), made, asked, client


state_before = official_state()
rc, log, made, asked, cl = guarded_run([])
check("G1 no --output-dir → refused", rc == 2 and "必須以 --output-dir 明確指定獨立的測試目錄" in log, log)
check("G1 no API / no confirm", not made and not asked and not cl.calls)

official_variants = {
    "absolute": str(OFFICIAL_OUT),
    "relative": os.path.relpath(OFFICIAL_OUT, os.getcwd()),
    "trailing separator": str(OFFICIAL_OUT) + os.sep,
    "dot segment": str(OFFICIAL_OUT / "."),
    "dot-dot segment": str(OFFICIAL_OUT / "gemini_judge" / ".."),
    "different case": str(OFFICIAL_OUT).upper(),
    "forward slashes": str(OFFICIAL_OUT).replace("\\", "/"),
    "reserved packets dir": str(OFFICIAL_OUT / "gemini_judge"),
    "reserved packets subdir": str(OFFICIAL_OUT / "gemini_judge" / "packets"),
    "reserved backups dir": str(OFFICIAL_OUT / "backups"),
}
for label, path in official_variants.items():
    rc, log, made, asked, cl = guarded_run(["--output-dir", path])
    check(f"G2 official output ({label}) → refused", rc == 2 and "指向正式輸出位置" in log and "獨立的測試目錄" in log, (path, log))
    check(f"G2 official output ({label}) → no API / no confirm", not made and not asked and not cl.calls)
check("G2 official output dir not touched", official_state() == state_before)

# 正式目錄底下的獨立測試子目錄（例如 output/gemini_trialN）不是正式位置：路徑檢查通過，接著檢查快照
sub = OFFICIAL_OUT / "gemini_trial_preflight_probe_does_not_exist"
probs = gj.run_preflight(sub)
check("G3 output/<trial> subdir passes path check", probs and "缺少社群證據快照" in probs[0] and not sub.exists(), probs)
check("G3 preflight did not create the directory", not sub.exists())

# 獨立暫存目錄：缺快照、無效 JSON、格式不符 → 拒絕；有效快照 → 通過
gd = Path(tempfile.mkdtemp(prefix="judge-guard-"))
rc, log, made, asked, cl = guarded_run(["--output-dir", str(gd)])
check("G4 missing verification.json → refused", rc == 2 and "缺少社群證據快照" in log and "不會自動複製" in log, log)
check("G4 missing snapshot → no API / no confirm, nothing created", not made and not asked and not cl.calls
      and not (gd / "verification.json").exists() and not list(gd.iterdir()))
(gd / "verification.json").write_text("{not valid json", encoding="utf-8")
snap_bad = (gd / "verification.json").read_bytes()
rc, log, made, asked, cl = guarded_run(["--output-dir", str(gd)])
check("G5 invalid JSON → refused", rc == 2 and "不是有效的 JSON" in log, log)
check("G5 invalid JSON → no API, snapshot untouched", not made and not asked and (gd / "verification.json").read_bytes() == snap_bad)
(gd / "verification.json").write_text("[1, 2, 3]", encoding="utf-8")
rc, log, made, asked, cl = guarded_run(["--output-dir", str(gd)])
check("G5b JSON without questions object → refused", rc == 2 and "格式不符" in log and not made, log)
(gd / "verification.json").write_bytes(b"\xff\xfe\x00bad")
rc, log, made, asked, cl = guarded_run(["--output-dir", str(gd)])
check("G5c unreadable (bad encoding) → refused", rc == 2 and ("無法讀取" in log or "不是有效的 JSON" in log) and not made, log)
(gd / "verification.json").write_text(json.dumps({"meta": {}, "questions": {}}), encoding="utf-8")
check("G6 valid snapshot → preflight passes", gj.run_preflight(gd) == [])

# 通過前置檢查後照原流程執行（使用既有 setup 的測試目錄：有快照、有證據包），且快照不被修改
eg = setup(ids=("m-single",))
snap_hash = hashlib.sha256((eg.out / "verification.json").read_bytes()).hexdigest()
check("G7 setup dir passes preflight", gj.run_preflight(eg.out) == [])
rc, log, made, asked, cl = guarded_run(["--output-dir", str(eg.out)], bank=eg.bank)
res_g = results(eg)
check("G7 passes → exactly one API call per question", rc == 0 and len(made) == 1 and asked and len(cl.calls) == 1, log)
check("G7 result written only to the test dir", res_g and res_g["questions"]["m-single"]["runs"][0]["final_exam_recommendation"]["source"] == "community")
check("G7 snapshot not modified by run", hashlib.sha256((eg.out / "verification.json").read_bytes()).hexdigest() == snap_hash)
check("G7 official output dir still untouched", official_state() == state_before)

check("G8 versions unchanged", (gj.PROMPT_VERSION, gj.EVALUATOR_VERSION, gj.SUMMARY_VERSION) == ("judge-v4", "eval-v5", "formal-consensus-v1"))

# ---------------------------------------------------------------------------
# 9. P0：已成功題目跳過、--force、報告保留有效推薦與歷程、每次執行只備份一次
# ---------------------------------------------------------------------------


def bk_count(e):
    d = e.out / "backups"
    return len([p for p in d.iterdir() if p.name.startswith("gemini_judgments.")]) if d.exists() else 0


def nruns(e, qid):
    r = results(e)
    return len(((r or {}).get("questions", {}).get(qid) or {}).get("runs", []))


def mutate_all_runs(e, fn):
    p = e.out / "gemini_judgments.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    for v in d["questions"].values():
        for r in v["runs"]:
            fn(r)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def multi_result(answer):
    r = copy.deepcopy(rm)
    r["suggested_answer"] = answer
    return r


def section(md, qid):
    return md.split(f"## {qid}")[1].split("\n## ")[0]


# P0-1：已成功、一致 → 0 次 API；全部跳過時不建立 client、不新增紀錄、不備份
ep = setup(ids=("m-single", "m-multi"))
pk_s = packet_of(ep, "m-single")
rc, log, made = run(ep, FakeClient([single_result(pk_s), multi_result('["A","C"]')]))
check("P1 first run: both done", rc == 0 and nruns(ep, "m-single") == 1 and nruns(ep, "m-multi") == 1
      and results(ep)["questions"]["m-single"]["runs"][0]["status"] == "done")
h_before, bk_before = sha(ep.out / "gemini_judgments.json"), bk_count(ep)
c_skip = FakeClient([])
rc, log, made = run(ep, c_skip)
check("P1 rerun consistent successes → 0 API calls", rc == 0 and not c_skip.calls, log)
check("P8 all skipped → no client, no new record, no backup", not made and sha(ep.out / "gemini_judgments.json") == h_before
      and bk_count(ep) == bk_before and "未建立 API client" in log and log.count("略過") == 2, log)
# P0-1：--force 才重新判定
c_force = FakeClient([single_result(pk_s), multi_result('["A","C"]')])
rc, log, made = run(ep, c_force, extra=("--tier", "paid", "--force"))
check("P2 --force re-judges skipped questions", rc == 0 and len(c_force.calls) == 2 and nruns(ep, "m-single") == 2
      and nruns(ep, "m-multi") == 2, log)
buf = io.StringIO()
with redirect_stdout(buf):
    rc = gj.main(ep.argv + ["--force"])
check("P2 --force without --run rejected", rc == 2 and "只能搭配 --run" in buf.getvalue())

# P0-1：舊版本、雜湊、模型、缺欄位、判定版本不一致 → 不得跳過
e3p = setup(ids=("m-single",))
pk3 = packet_of(e3p, "m-single")
run(e3p, FakeClient([single_result(pk3)]))
for label, fn, extra in (
        ("old prompt version", lambda r: r.__setitem__("prompt_version", "judge-v3"), ()),
        ("old evaluator version", lambda r: (r.get("evaluation") or {}).__setitem__("evaluator_version", "eval-v4"), ()),
        ("packet hash mismatch", lambda r: r.__setitem__("packet_sha256", "0" * 64), ()),
        ("legacy record without kind", lambda r: r.pop("kind", None), ()),
        ("missing final recommendation", lambda r: r.pop("final_exam_recommendation", None), ()),
        ("evaluation not evaluated", lambda r: (r.get("evaluation") or {}).__setitem__("status", "invalid_answer"), ()),
        ("different model", lambda r: None, ("--model", "gemini-other-model"))):
    mutate_all_runs(e3p, fn)
    cm = FakeClient([single_result(pk3)])
    n_before = nruns(e3p, "m-single")
    rc, log, made = run(e3p, cm, extra=("--tier", "paid") + extra)
    check(f"P3 {label} → not skipped (API called)", len(cm.calls) == 1 and nruns(e3p, "m-single") == n_before + 1, log)
check("P3 completed_run rejects non-dict / empty", gj.completed_run([None, "x"], "a", "m") is None and gj.completed_run([], "a", "m") is None)

# P0-1：同批中已成功者跳過、失敗者補跑
e4p = setup(ids=("m-single", "m-multi"))
pk4 = packet_of(e4p, "m-single")
run(e4p, FakeClient([single_result(pk4), FakeAPIError(503, "UNAVAILABLE")]))
c4 = FakeClient([multi_result('["A","C"]')])
rc, log, made = run(e4p, c4)
check("P4 only failed question re-run", rc == 0 and len(c4.calls) == 1 and '"question_id": "m-multi"' in c4.calls[0][1]
      and nruns(e4p, "m-single") == 1 and nruns(e4p, "m-multi") == 2, log)
e4c = setup(ids=("m-single",))
run(e4c, FakeClient([single_result(packet_of(e4c, "m-single"), answer='"Z"')]))
c4c = FakeClient([single_result(packet_of(e4c, "m-single"))])
rc, log, made = run(e4c, c4c)
check("P4 invalid_answer record is not a skip basis", len(c4c.calls) == 1 and nruns(e4c, "m-single") == 2)

# P0-2：報告區分最新狀態與有效推薦
e5p = setup(ids=("m-multi",))  # 無社群資料：有效推薦來自 Gemini（["A","B"]，與題庫 ["A","C"] 不同）
run(e5p, FakeClient([multi_result('["A","B"]')]))
run(e5p, FakeClient([FakeAPIError(503, "UNAVAILABLE high demand")]), extra=("--tier", "paid", "--force"))
sec5 = section(gj.render_report(results(e5p)), "m-multi")
check("P5 done→503: latest status shows failure", "最新一次執行：" in sec5 and "狀態 deferred" in sec5 and "HTTP 503" in sec5
      and "最近一次執行未取得有效結果" in sec5, sec5[:600])
check("P5 done→503: effective recommendation kept", '最終考試推薦：["A", "B"]**｜採用 Gemini 有效建議' in sec5
      and "回退為題庫原始答案" not in sec5 and "最近一次執行錯誤" in sec5, sec5[:900])
check("P5 history lists both runs", "共 2 筆" in sec5 and " done → 2) " in sec5 and "deferred" in sec5)
e6p = setup(ids=("m-multi",))
run(e6p, FakeClient([FakeAPIError(503, "UNAVAILABLE")]))
sec6a = section(gj.render_report(results(e6p)), "m-multi")
check("P6a only failure: no success disguised", "尚無有效成功紀錄" in sec6a and "回退為題庫原始答案" in sec6a, sec6a[:600])
run(e6p, FakeClient([multi_result('["A","B"]')]))
sec6 = section(gj.render_report(results(e6p)), "m-multi")
check("P6 503→done: latest success used", "以下為最新一次的成功結果" in sec6 and '最終考試推薦：["A", "B"]' in sec6
      and "狀態 done" in sec6, sec6[:600])
e7p = setup(ids=("m-multi",))
run(e7p, FakeClient([multi_result('["A","B"]')]))
run(e7p, FakeClient([multi_result("AZ")]), extra=("--tier", "paid", "--force"))
run(e7p, FakeClient(["not json"]), extra=("--tier", "paid", "--force"))
sec7 = section(gj.render_report(results(e7p)), "m-multi")
check("P7 invalid answer / invalid output do not override", '最終考試推薦：["A", "B"]**｜採用 Gemini 有效建議' in sec7
      and "狀態 invalid_output" in sec7 and "invalid_answer" in sec7 and "共 3 筆" in sec7, sec7[:700])
check("P7 effective_run_index picks last evaluated", gj.effective_run_index(results(e7p)["questions"]["m-multi"]["runs"]) == 0)

# P0-3：每次執行只備份一次；無寫入不備份；仍使用原子寫入
e9p = setup(ids=("m-single", "m-multi", "m-stmt"))
pk9s, pk9t = packet_of(e9p, "m-single"), packet_of(e9p, "m-stmt")
rs9 = copy.deepcopy(rs)
run(e9p, FakeClient([single_result(pk9s), multi_result('["A","C"]'), rs9]))
check("P9 first session on new file → no backup needed", bk_count(e9p) == 0 and nruns(e9p, "m-stmt") == 1)
replaced = []
real_replace = os.replace


def counting_replace(src, dst):
    replaced.append((str(src), str(dst)))
    return real_replace(src, dst)


os.replace = counting_replace
try:
    rc, log, made = run(e9p, FakeClient([single_result(pk9s), multi_result('["A","C"]'), rs9]), extra=("--tier", "paid", "--force"))
finally:
    os.replace = real_replace
check("P9 multi-question session → exactly one backup", rc == 0 and bk_count(e9p) == 1 and nruns(e9p, "m-single") == 2
      and nruns(e9p, "m-multi") == 2 and nruns(e9p, "m-stmt") == 2, (bk_count(e9p), log))
writes = [d for s, d in replaced if d.endswith("gemini_judgments.json")]
tmp_names = [Path(s).name for s, d in replaced if d.endswith("gemini_judgments.json")]
check("P11 every write is atomic (unique tmp + os.replace)", len(writes) == 3
      and all(n.startswith(".gemini_judgments.json.") and n.endswith(".tmp") for n in tmp_names)
      and len(set(tmp_names)) == 3, replaced)
check("P11 no leftover tmp file", not list(e9p.out.glob("*.tmp")))
check("P11 results file still valid JSON with all runs", sum(len(v["runs"]) for v in results(e9p)["questions"].values()) == 6)
bk9 = bk_count(e9p)
run(e9p, FakeClient([]))  # 全部已成功 → 跳過
check("P10 all skipped → no backup", bk_count(e9p) == bk9)
rc, log, made = run(e9p, FakeClient([single_result(pk9s)] * 3), extra=("--tier", "paid", "--force"), confirm=False)
check("P10 cancelled → no backup, no client", rc == 2 and not made and bk_count(e9p) == bk9)
buf = io.StringIO()
with redirect_stdout(buf):
    rc = gj.main(["--bank", str(e9p.bank), "--ids", "m-single", "--run", "--tier", "free"])
check("P10 preflight refused → no backup", rc == 2 and bk_count(e9p) == bk9)

# P12：報告不呼叫 API
real_make_client = gj.eq.make_client


def no_client(*a, **k):
    raise AssertionError("report must not create an API client")


gj.eq.make_client = no_client
try:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = gj.main(["--bank", str(e7p.bank), "--output-dir", str(e7p.out), "--report"],
                     client_factory=lambda k: (_ for _ in ()).throw(AssertionError("no client")))
finally:
    gj.eq.make_client = real_make_client
check("P12 report offline (no client, no API)", rc == 0 and (e7p.out / "gemini_judgments.md").exists(), buf.getvalue())

# ---------------------------------------------------------------------------
# 10. dry-run 安全補強：正式工作區隔離、verification.json 快照檢查、既有證據包保護
# ---------------------------------------------------------------------------

DR_CALLS = []


def counting_search(q, top=5):
    DR_CALLS.append(("search", q))
    return fake_search(q, top)


def counting_fetch(u):
    DR_CALLS.append(("fetch", u))
    return fake_fetch(u)


def raising_search(q, top=5):
    raise AssertionError("dry-run must not search here")


def dry(bank_path, out=None, ids=("m-single",), extra=(), search=counting_search):
    DR_CALLS.clear()
    argv = ["--bank", str(bank_path), "--ids", ",".join(ids), "--delay", "0"] + (["--output-dir", str(out)] if out is not None else [])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = gj.main(argv + list(extra), search=search, fetch=counting_fetch)
    return rc, buf.getvalue(), list(DR_CALLS)


def valid_snapshot(ws_dir, bank_path, questions=None):
    ws_dir.mkdir(parents=True, exist_ok=True)
    meta = {"schema_version": va.SCHEMA_VERSION, "bank_sha256": hashlib.sha256(Path(bank_path).read_bytes()).hexdigest()}
    (ws_dir / "verification.json").write_text(json.dumps({"meta": meta, "questions": questions or {}}), encoding="utf-8")


def packet_files(ws_dir):
    d = ws_dir / "gemini_judge" / "packets"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


DRB = Path(tempfile.mkdtemp(prefix="judge-dr-"))
dr_bank = DRB / "bank.json"
dr_bank.write_text(json.dumps(BANK), encoding="utf-8")

# 假的正式 output/（暫時替換 va.DEFAULT_OUT，確保真實 output/ 不可能被觸及）
FAKE_OFF = DRB / "repo" / "output"
(FAKE_OFF / "gemini_judge" / "packets").mkdir(parents=True)
(FAKE_OFF / "backups").mkdir()
valid_snapshot(FAKE_OFF, dr_bank)
off_pkt = FAKE_OFF / "gemini_judge" / "packets" / "keep.20260101-000000-000000.json"
off_pkt.write_text('{"keep": true}', encoding="utf-8")


def dir_state(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<dir>"
            for p in sorted(root.rglob("*"))}


real_default_out = va.DEFAULT_OUT
real_output_state = dir_state(real_default_out)
# 真實正式 output/：未指定 --output-dir 與指向真實 output/ 都必須在任何請求前拒絕
rc, log, calls = dry(dr_bank, None, search=raising_search)
check("DR1 no --output-dir → refused before any request", rc == 2 and "必須以 --output-dir 明確指定獨立工作區" in log and not calls, log)
rc, log, calls = dry(dr_bank, real_default_out, search=raising_search)
check("DR1 real official output/ → refused", rc == 2 and "指向正式工作區" in log and not calls, log)
rc, log, calls = dry(dr_bank, real_default_out / "gemini_judge" / "packets", search=raising_search)
check("DR1 real official packets dir → refused", rc == 2 and "指向正式工作區" in log and not calls, log)
check("DR1 real output/ untouched", dir_state(real_default_out) == real_output_state)

va.DEFAULT_OUT = FAKE_OFF
try:
    fake_before = dir_state(FAKE_OFF)
    for label, path in (("official root", FAKE_OFF), ("official gemini_judge", FAKE_OFF / "gemini_judge"),
                        ("official packets", FAKE_OFF / "gemini_judge" / "packets"), ("official backups", FAKE_OFF / "backups"),
                        ("non-existent dir under packets", FAKE_OFF / "gemini_judge" / "packets" / "new_sub"),
                        ("non-existent dir under backups", FAKE_OFF / "backups" / "x" / "y"),
                        ("upper-case path", Path(str(FAKE_OFF).upper())), ("trailing dot segment", FAKE_OFF / "."),
                        ("dot-dot segment", FAKE_OFF / "gemini_judge" / ".." / "gemini_judge"),
                        ("forward slashes", Path(str(FAKE_OFF / "gemini_judge").replace("\\", "/")))):
        rc, log, calls = dry(dr_bank, path)
        check(f"DR2 {label} → refused, no request", rc == 2 and "指向正式工作區" in log and not calls, (label, log))
    check("DR2 non-existent dirs not created", not (FAKE_OFF / "gemini_judge" / "packets" / "new_sub").exists()
          and not (FAKE_OFF / "backups" / "x").exists())
    # 相對路徑（切換工作目錄到假 repo）
    old_cwd = os.getcwd()
    os.chdir(FAKE_OFF.parent)
    try:
        for rel in ("output", "./output/gemini_judge", "output/../output", "OUTPUT/Gemini_Judge/packets"):
            rc, log, calls = dry(dr_bank, Path(rel))
            check(f"DR3 relative '{rel}' → refused", rc == 2 and "指向正式工作區" in log and not calls, log)
    finally:
        os.chdir(old_cwd)
    # junction／符號連結指向正式工作區
    import _winapi
    j_root, j_pk = DRB / "junction_to_output", DRB / "junction_to_packets"
    _winapi.CreateJunction(str(FAKE_OFF), str(j_root))
    _winapi.CreateJunction(str(FAKE_OFF / "gemini_judge" / "packets"), str(j_pk))
    try:
        for label, path in (("junction → official root", j_root), ("junction/gemini_judge", j_root / "gemini_judge"),
                            ("junction → official packets", j_pk), ("junction/backups/new", j_root / "backups" / "new")):
            rc, log, calls = dry(dr_bank, path)
            check(f"DR4 {label} → refused, no request", rc == 2 and "指向正式工作區" in log and not calls, (label, log))
        sym = DRB / "symlink_to_output"
        try:
            os.symlink(str(FAKE_OFF), str(sym), target_is_directory=True)
        except OSError:
            print("NOTE symlink test skipped (no privilege to create symlinks); junction tests cover reparse points")
        else:
            rc, log, calls = dry(dr_bank, sym / "gemini_judge")
            check("DR4 symlink → official → refused", rc == 2 and "指向正式工作區" in log and not calls)
            os.rmdir(sym)
    finally:
        os.rmdir(j_root)  # 只移除 junction 本身，不影響目標
        os.rmdir(j_pk)
    check("DR4 fake official tree untouched by all refusals", dir_state(FAKE_OFF) == fake_before)

    # output/ 底下的獨立子目錄（與 --run 規則一致）可通過路徑檢查
    sub_ws = FAKE_OFF / "gemini_trial_x"
    valid_snapshot(sub_ws, dr_bank)
    rc, log, calls = dry(dr_bank, sub_ws)
    check("DR5 independent workspace under output/ allowed", rc == 0 and calls and len(packet_files(sub_ws)) == 1, log)
finally:
    va.DEFAULT_OUT = real_default_out

# 安全的獨立工作區（正式目錄以外）
ws_ok = DRB / "ws_ok"
valid_snapshot(ws_ok, dr_bank)
rc, log, calls = dry(dr_bank, ws_ok)
pf_ok = [n for n in packet_files(ws_ok) if n.endswith(".json")]
check("DR5 safe workspace → dry-run proceeds", rc == 0 and calls and len(pf_ok) == 1 and pf_ok[0].startswith("m-single."), log)
check("DR5 atomic write: no .part left, packet verifies", not [n for n in packet_files(ws_ok) if n.endswith(".part")]
      and gj.load_packet(ws_ok / "gemini_judge" / "packets" / pf_ok[0])[2])

# verification.json 快照檢查
for label, content, expect in (
        ("missing", None, "缺少 verification.json"),
        ("corrupt JSON", "{not json", "不是有效的 JSON"),
        ("not an object", "[1, 2]", "缺少必要結構"),
        ("no meta", json.dumps({"questions": {}}), "缺少必要結構"),
        ("no questions", json.dumps({"meta": {"schema_version": 1}}), "缺少必要結構"),
        ("schema_version missing", json.dumps({"meta": {"bank_sha256": hashlib.sha256(dr_bank.read_bytes()).hexdigest()},
                                               "questions": {}}), "schema_version"),
        ("schema_version mismatch", json.dumps({"meta": {"schema_version": 999,
                                                         "bank_sha256": hashlib.sha256(dr_bank.read_bytes()).hexdigest()},
                                                "questions": {}}), "schema_version"),
        ("bank sha missing", json.dumps({"meta": {"schema_version": va.SCHEMA_VERSION}, "questions": {}}), "題庫雜湊"),
        ("bank sha mismatch", json.dumps({"meta": {"schema_version": va.SCHEMA_VERSION, "bank_sha256": "0" * 64},
                                          "questions": {}}), "題庫雜湊")):
    wsx = DRB / f"snap_{label.replace(' ', '_')}"
    wsx.mkdir()
    if content is not None:
        (wsx / "verification.json").write_text(content, encoding="utf-8")
    before_snap = dir_state(wsx)
    rc, log, calls = dry(dr_bank, wsx)
    check(f"DR6 snapshot {label} → refused before any request", rc == 2 and expect in log and "請自行準備快照" in log
          and not calls and dir_state(wsx) == before_snap, (label, log))
# 快照中該題資料與目前題庫不一致（整批預檢）
for label, entry_edit, expect in (("stale bank answer", lambda e: e["bank_answer"].__setitem__("value", "C"), "快照記錄的題庫答案"),
                                  ("type mismatch", lambda e: e.__setitem__("type", "multiple_choice"), "題號或題型"),
                                  ("entry not object", None, "不是物件")):
    wsx = DRB / f"entry_{label.replace(' ', '_')}"
    ctx_tmp = va.Ctx(dr_bank, DRB / "unused-out")
    ent = va.new_entry(ctx_tmp, ctx_tmp.units["m-single"])
    if entry_edit:
        entry_edit(ent)
    valid_snapshot(wsx, dr_bank, {"m-single": ent if entry_edit else "broken"})
    rc, log, calls = dry(dr_bank, wsx)
    check(f"DR7 snapshot entry {label} → batch refused", rc == 2 and expect in log and not calls and not packet_files(wsx), log)
wsv = DRB / "snap_valid_entry"
ctx_tmp = va.Ctx(dr_bank, DRB / "unused-out")
valid_snapshot(wsv, dr_bank, {"m-single": va.new_entry(ctx_tmp, ctx_tmp.units["m-single"])})
check("DR7 valid snapshot → preflight passes", gj.dry_run_preflight(wsv, dr_bank) == [])

# 既有證據包保護
pk_name = pf_ok[0]
pk_hash = hashlib.sha256((ws_ok / "gemini_judge" / "packets" / pk_name).read_bytes()).hexdigest()
rc, log, calls = dry(dr_bank, ws_ok)
check("DR8 same question again → refused before request", rc == 2 and "不是空白工作區" in log and "gemini_judge/" in log
      and not calls, log)
check("DR8 existing packet untouched, no new packet", packet_files(ws_ok) == [pk_name]
      and hashlib.sha256((ws_ok / "gemini_judge" / "packets" / pk_name).read_bytes()).hexdigest() == pk_hash)
check("DR8 latest_packet unchanged", gj.latest_packet(ws_ok / "gemini_judge" / "packets", "m-single").name == pk_name)
rc, log, calls = dry(dr_bank, ws_ok, ids=("m-multi", "m-single"))
check("DR9 any conflict in batch → whole batch refused, no request", rc == 2 and "不是空白工作區" in log
      and not calls and packet_files(ws_ok) == [pk_name], log)
ws_dup = DRB / "ws_dup"
valid_snapshot(ws_dup, dr_bank)
rc, log, calls = dry(dr_bank, ws_dup, ids=("m-single", "m-single"))
check("DR9 duplicate ids → refused before request", rc == 2 and "題號重複" in log and not calls and not packet_files(ws_dup), log)
(ws_ok / "gemini_judge" / "packets" / ".packet-leftover.part").write_text("partial", encoding="utf-8")
check("DR10 latest_packet ignores .part temp files", gj.latest_packet(ws_ok / "gemini_judge" / "packets", "m-single").name == pk_name)

# 原子寫入失敗：fsync 失敗／不支援不覆寫建立 → 不留下最終檔名，暫存檔移除
for label, attr, exc in (("fsync failure", "fsync", OSError("simulated disk error")),
                         ("link unsupported", "link", OSError("simulated: hard links not supported"))):
    wsf = DRB / f"ws_fail_{attr}"
    valid_snapshot(wsf, dr_bank)
    real_fn = getattr(gj.os, attr)

    def boom(*a, _e=exc, **k):
        raise _e

    setattr(gj.os, attr, boom)
    try:
        rc, log, calls = dry(dr_bank, wsf)
    finally:
        setattr(gj.os, attr, real_fn)
    check(f"DR11 {label} → fail-closed, no final file, no temp left", rc == 2 and "寫入失敗" in log
          and not packet_files(wsf), (log, packet_files(wsf)))
# 檔名已存在 → 不覆寫
coll_dir = DRB / "collision" / "gemini_judge" / "packets"
coll_dir.mkdir(parents=True)
real_stamp = gj.stamp
gj.stamp = lambda: "20260101-000000-000000"
try:
    target = coll_dir / "m-single.20260101-000000-000000.json"
    target.write_text("EXISTING", encoding="utf-8")
    try:
        gj.save_packet(coll_dir, {"question_id": "m-single", "x": 1})
        raised = False
    except FileExistsError:
        raised = True
finally:
    gj.stamp = real_stamp
check("DR12 existing final name → FileExistsError, not overwritten", raised and target.read_text(encoding="utf-8") == "EXISTING"
      and sorted(p.name for p in coll_dir.iterdir()) == [target.name])
path_n, sha_n = gj.save_packet(coll_dir, {"question_id": "m-multi", "y": 2})
check("DR12 normal save_packet still returns verifiable packet", gj.load_packet(path_n)[1] == sha_n and gj.load_packet(path_n)[2]
      and not list(coll_dir.glob("*.part")))
# --run／--report 不受 dry-run 前置檢查影響（行為測試）
ws_run = DRB / "ws_run_meta_empty"
ws_run.mkdir()
(ws_run / "verification.json").write_text(json.dumps({"meta": {}, "questions": {}}), encoding="utf-8")  # dry-run 會拒絕的快照
buf = io.StringIO()
gj.confirm = lambda p: True
with redirect_stdout(buf):
    rc = gj.main(["--bank", str(dr_bank), "--output-dir", str(ws_run), "--ids", "m-single", "--run", "--tier", "free"],
                 client_factory=lambda k: (_ for _ in ()).throw(AssertionError("no client expected")))
check("DR13 --run keeps its own preflight (passes, then stops at missing packet)",
      rc == 2 and "沒有證據包" in buf.getvalue() and "dry-run 安全檢查" not in buf.getvalue(), buf.getvalue())
ws_rep = DRB / "ws_report_no_snapshot"
ws_rep.mkdir()
(ws_rep / "gemini_judgments.json").write_text(json.dumps({"meta": {}, "questions": {}}), encoding="utf-8")
buf = io.StringIO()
with redirect_stdout(buf):
    rc = gj.main(["--bank", str(dr_bank), "--output-dir", str(ws_rep), "--report"])
check("DR13 --report unaffected (no snapshot needed)", rc == 0 and "尚無判讀結果" in buf.getvalue(), buf.getvalue())

# ===========================================================================
# 11. P1 安全修正（F-01／F-02／F-03／F-04／F-05／F-08／F-09／F-10／F-12／F-14）
# ===========================================================================
import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

CHILD = Path(__file__).with_name("judge_child.py")
PY = sys.executable


def mainq(argv, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = gj.main(argv, **kw)
    return rc, buf.getvalue()


def tree(root):
    return dir_state(root) if Path(root).exists() else {}


# 本節的假正式 output/（基準快照與 valid_snapshot(ws, dr_bank) 位元組相同）
P1_BASE = DRB / "p1_repo" / "output"
valid_snapshot(P1_BASE, dr_bank)
va.DEFAULT_OUT = P1_BASE
p1_base_state = tree(P1_BASE)

# ---- F-01：--report／--reevaluate 路徑與寫入保護 ----
ef1 = setup(ids=("m-single",))
run(ef1, FakeClient([single_result(packet_of(ef1, "m-single"))]))
shutil.copyfile(ef1.out / "gemini_judgments.json", P1_BASE / "gemini_judgments.json")  # 舊行為下會被預設寫回的正式結果
official_before = tree(P1_BASE)
for label, argv in (("report", ["--bank", str(ef1.bank), "--report"]),
                    ("reevaluate", ["--bank", str(ef1.bank), "--ids", "m-single", "--reevaluate"])):
    rc, log = mainq(argv)
    check(f"F01 {label} without --output-dir → refused (no default output/)", rc == 2 and "必須以 --output-dir 明確指定" in log, log)
    for target in (P1_BASE, P1_BASE / "backups", P1_BASE / "gemini_judge"):
        rc, log = mainq(argv + ["--output-dir", str(target)])
        check(f"F01 {label} official {target.name} → refused", rc == 2 and "指向正式工作區" in log, log)
check("F01 official output untouched by report/reevaluate refusals", tree(P1_BASE) == official_before)
rc, log = mainq(["--bank", str(ef1.bank), "--output-dir", str(ef1.out), "--report"])
md_path = ef1.out / "gemini_judgments.md"
md_hash, bk_state = sha(md_path), tree(ef1.out / "backups")
check("F01 first report written (exclusive create)", rc == 0 and md_path.exists() and not list(ef1.out.glob(".gemini_judgments.md.*")), log)
rc, log = mainq(["--bank", str(ef1.bank), "--output-dir", str(ef1.out), "--report"])
check("F01 existing report → refused, not overwritten, no backup made", rc == 2 and "已存在" in log and "不覆寫" in log
      and sha(md_path) == md_hash and tree(ef1.out / "backups") == bk_state, log)
real_link = gj.os.link


def link_race(src, dst):  # 模擬檢查後、建立前另一方建立了同名報告
    Path(dst).write_text("RACE", encoding="utf-8")
    return real_link(src, dst)


md_path.rename(ef1.out / "moved-by-user.md")
gj.os.link = link_race
try:
    rc, log = mainq(["--bank", str(ef1.bank), "--output-dir", str(ef1.out), "--report"])
finally:
    gj.os.link = real_link
check("F01 report created by someone else after check → not overwritten", rc == 2 and md_path.read_text(encoding="utf-8") == "RACE"
      and not list(ef1.out.glob(".gemini_judgments.md.*")), log)
ws_nores = DRB / "f01_no_results"
valid_snapshot(ws_nores, dr_bank)
rc, log = mainq(["--bank", str(dr_bank), "--output-dir", str(ws_nores), "--ids", "m-single", "--reevaluate"])
check("F01 reevaluate without results file → refused, nothing written", rc == 2 and "沒有 gemini_judgments.json" in log
      and sorted(p.name for p in ws_nores.iterdir()) == ["verification.json"], log)
rc, log = mainq(["--bank", str(dr_bank), "--output-dir", str(DRB / "f01_missing_dir"), "--report"])
check("F01 report on missing dir → refused, dir not created", rc == 2 and not (DRB / "f01_missing_dir").exists(), log)

# ---- F-02：工作區必須受 .gitignore 排除（只讀取 Git 狀態）----
git_before = tree(GIT_ROOT / ".git")
ws_ni = GIT_ROOT / "notignored" / "ws"
valid_snapshot(ws_ni, dr_bank)
ni_before = tree(ws_ni)
rc, log, calls = dry(dr_bank, ws_ni)
check("F02 dry-run in non-ignored dir → refused before request", rc == 2 and "未受 .gitignore 排除" in log and not calls
      and tree(ws_ni) == ni_before, log)
(ws_ni / "gemini_judgments.json").write_text(json.dumps({"meta": {}, "questions": {}}), encoding="utf-8")
ni_before = tree(ws_ni)
for label, extra in (("run", ["--ids", "m-single", "--run", "--tier", "free"]), ("report", ["--report"]),
                     ("reevaluate", ["--ids", "m-single", "--reevaluate"])):
    made2 = []
    rc, log = mainq(["--bank", str(dr_bank), "--output-dir", str(ws_ni)] + extra, client_factory=lambda k: made2.append(k))
    check(f"F02 {label} in non-ignored dir → refused", rc == 2 and "未受 .gitignore 排除" in log and not made2, log)
check("F02 non-ignored workspace untouched", tree(ws_ni) == ni_before)
check("F02 git checks did not modify the test repo's .git (index unchanged)", tree(GIT_ROOT / ".git") == git_before)
probe = subprocess.run(["git", "-C", str(REAL_TEMP), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
if probe.returncode != 0:
    ws_out = Path(tempfile.mkdtemp(dir=REAL_TEMP, prefix="judge-norepo-"))
    valid_snapshot(ws_out, dr_bank)
    rc, log, calls = dry(dr_bank, ws_out)
    check("F02 workspace outside any git repo → refused (cannot confirm)", rc == 2 and "不在 Git 儲存庫內" in log and not calls, log)
else:
    print("NOTE F02 outside-repo case skipped: system temp is inside a git repo")
ws_trk = GIT_ROOT / "tmp" / "tracked_ws"
valid_snapshot(ws_trk, dr_bank)
subprocess.run(["git", "-C", str(GIT_ROOT), "add", "-f", "tmp/tracked_ws/verification.json"], check=True)  # 測試夾具
git_before = tree(GIT_ROOT / ".git")
rc, log, calls = dry(dr_bank, ws_trk)
check("F02 ignored dir with force-tracked file → refused", rc == 2 and ("已受 Git 追蹤" in log or "未受 .gitignore 排除" in log)
      and not calls, log)
(ws_trk / "sub").mkdir()
(ws_trk / "sub" / "x.txt").write_text("x", encoding="utf-8")
subprocess.run(["git", "-C", str(GIT_ROOT), "add", "-f", "tmp/tracked_ws/sub/x.txt"], check=True)  # 測試夾具
subprocess.run(["git", "-C", str(GIT_ROOT), "rm", "-q", "--cached", "tmp/tracked_ws/verification.json"], check=True)
git_before = tree(GIT_ROOT / ".git")
check("F02 tracked file deeper inside workspace → refused",
      any("已受 Git 追蹤" in p or "未受 .gitignore 排除" in p for p in gj.git_ignore_problems(ws_trk)))
check("F02 tracked check did not modify the index (2)", tree(GIT_ROOT / ".git") == git_before)
check("F02 tracked check did not modify the index", tree(GIT_ROOT / ".git") == git_before)
real_git = gj.GIT_EXE
gj.GIT_EXE = "git-executable-that-does-not-exist-xyz"
try:
    ws_ng = DRB / "f02_no_git"
    valid_snapshot(ws_ng, dr_bank)
    rc, log, calls = dry(dr_bank, ws_ng)
finally:
    gj.GIT_EXE = real_git
check("F02 git unavailable → refused (fail-closed)", rc == 2 and "無法執行 git" in log and not calls, log)
check("F02 helper: ignored temp workspace passes", gj.git_ignore_problems(ws_ng) == [])

# ---- F-03：新批次只接受空白工作區 ----
for i, (label, make) in enumerate((
        ("extra note file", lambda w: (w / "notes.txt").write_text("x", encoding="utf-8")),
        ("results file", lambda w: (w / "gemini_judgments.json").write_text("{}", encoding="utf-8")),
        ("empty gemini_judge dir", lambda w: (w / "gemini_judge").mkdir()),
        ("backups dir", lambda w: (w / "backups").mkdir()),
        ("report file", lambda w: (w / "gemini_judgments.md").write_text("x", encoding="utf-8")),
        ("old fixed tmp", lambda w: (w / "gemini_judgments.json.tmp").write_text("x", encoding="utf-8")),
        ("lock name used by a directory", lambda w: (w / gj.LOCK_NAME).mkdir()))):
    w = DRB / f"f03_{i}"
    valid_snapshot(w, dr_bank)
    make(w)
    before = tree(w)
    rc, log, calls = dry(dr_bank, w)
    check(f"F03 {label} → not an empty workspace, refused before request", rc == 2 and "不是空白工作區" in log and not calls
          and tree(w) == before, (label, log))
w_foreign = DRB / "f03_foreign_lock"
valid_snapshot(w_foreign, dr_bank)
(w_foreign / gj.LOCK_NAME).write_bytes(b"foreign-owner-unknown")
rc, log, calls = dry(dr_bank, w_foreign)
check("F03 leftover / foreign lock file allowed (not deleted, not modified)", rc == 0 and calls
      and (w_foreign / gj.LOCK_NAME).read_bytes() == b"foreign-owner-unknown", log)
check("F03 rule documented", "verification.json" in gj.EMPTY_WORKSPACE_RULE and gj.LOCK_NAME in gj.EMPTY_WORKSPACE_RULE)

# ---- F-04：原子寫入（唯一暫存檔、失敗時原檔完整、只清理自己的暫存檔、保留備份政策）----
ea = setup(ids=("m-single", "m-multi"))
run(ea, FakeClient([single_result(packet_of(ea, "m-single")), multi_result('["A","C"]')]))
res_path = ea.out / "gemini_judgments.json"
orig_bytes = res_path.read_bytes()
foreign_tmp = ea.out / "gemini_judgments.json.tmp"  # 舊版固定暫存檔名、來源不明：不得被移除或覆寫
foreign_tmp.write_text("FOREIGN-FIXED-TMP", encoding="utf-8")
for attr in ("replace", "fsync"):
    real_fn = getattr(gj.os, attr)

    def boom(*a, **k):
        raise OSError(f"simulated {attr} failure")

    setattr(gj.os, attr, boom)
    try:
        try:
            gj.append_run(res_path, ea.out / "backups", "m-single", {"at": "x"}, backup=False)
            raised = False
        except OSError:
            raised = True
    finally:
        setattr(gj.os, attr, real_fn)
    check(f"F04 {attr} failure → exception, original JSON intact", raised and res_path.read_bytes() == orig_bytes)
    check(f"F04 {attr} failure → own temp removed, foreign tmp untouched",
          not list(ea.out.glob(".gemini_judgments.json.*.tmp")) and foreign_tmp.read_text(encoding="utf-8") == "FOREIGN-FIXED-TMP")
bk_before = sorted(p.name for p in (ea.out / "backups").iterdir()) if (ea.out / "backups").exists() else []
made_bk = gj.append_run(res_path, ea.out / "backups", "m-single", {"at": "manual"}, backup=True)
check("F04 backup policy kept (timestamped copy identical to previous file)", made_bk is not None
      and made_bk.read_bytes() == orig_bytes and made_bk.name not in bk_before)
check("F04 appended record present, JSON valid", results(ea)["questions"]["m-single"]["runs"][-1] == {"at": "manual"})

# 送出前的請求紀錄寫入失敗 → 不呼叫 API
eb = setup(ids=("m-single",))
cl_b = FakeClient([single_result(packet_of(eb, "m-single"))])
real_link = gj.os.link
gj.os.link = lambda *a, **k: (_ for _ in ()).throw(OSError("simulated: cannot create attempt record"))
try:
    rc, log, made = run(eb, cl_b)
finally:
    gj.os.link = real_link
check("F04 attempt record cannot be written → no API call, no result written", rc == 2 and not cl_b.calls
      and not (eb.out / "gemini_judgments.json").exists() and "寫入失敗" in log, log)

# API 呼叫後、寫入前中斷 → 留下可辨識的「未完成」狀態；鎖已釋放
ec = setup(ids=("m-single",))
cl_c = FakeClient([single_result(packet_of(ec, "m-single"))])
real_append = gj.append_run
gj.append_run = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
try:
    try:
        run(ec, cl_c)
        interrupted = False
    except KeyboardInterrupt:
        interrupted = True
finally:
    gj.append_run = real_append
att = sorted((ec.out / "gemini_judge" / "attempts").glob("*.json"))
check("F04 interrupted after API call → attempt record kept, no result", interrupted and len(cl_c.calls) == 1 and len(att) == 1
      and not (ec.out / "gemini_judgments.json").exists())
check("F04 lock released after exception (same process can lock again)", not gj.holds_workspace_lock(ec.out)
      and gj.unfinished_attempts(ec.out, {"questions": {}})[0]["question_id"] == "m-single")
cl_c2 = FakeClient([single_result(packet_of(ec, "m-single"))])
rc, log, made = run(ec, cl_c2)
check("F04 next run warns about unfinished attempt (no automatic retry)", rc == 0 and "未完成的請求紀錄" in log
      and len(cl_c2.calls) == 1, log)
rc, log = mainq(["--bank", str(ec.bank), "--output-dir", str(ec.out), "--report"])
md_c = (ec.out / "gemini_judgments.md").read_text(encoding="utf-8")
check("F04 report lists unfinished attempt", rc == 0 and "未完成的請求" in md_c and att[0].name in md_c)
check("F04 recorded attempt not listed as unfinished",
      [u["file"] for u in gj.unfinished_attempts(ec.out, results(ec))] == [att[0].name])

# 同程序：重入不死鎖、其他執行緒忙碌、例外時釋放、不建立目錄
w_lock = DRB / "lock_inproc"
w_lock.mkdir()
with gj.workspace_lock(w_lock):
    with gj.workspace_lock(w_lock):
        nested_ok = gj.holds_workspace_lock(w_lock)
    still_held = gj.holds_workspace_lock(w_lock)
    thread_res = []

    def other_thread():
        try:
            with gj.workspace_lock(w_lock):
                thread_res.append("acquired")
        except gj.WorkspaceBusy:
            thread_res.append("busy")
    t = threading.Thread(target=other_thread)
    t.start()
    t.join()
check("F04 same-thread re-entry does not deadlock", nested_ok and still_held and not gj.holds_workspace_lock(w_lock))
check("F04 other thread in same process → busy", thread_res == ["busy"])
try:
    with gj.workspace_lock(w_lock):
        raise ValueError("boom")
except ValueError:
    pass
check("F04 exception releases lock", not gj.holds_workspace_lock(w_lock))
try:
    with gj.workspace_lock(DRB / "lock_missing_dir"):
        pass
    missing_ok = False
except gj.WorkspaceBusy:
    missing_ok = True
check("F04 missing workspace → refused, directory not created", missing_ok and not (DRB / "lock_missing_dir").exists())

# ---- 跨程序（真的啟動多個程序）----


def spawn(mode, ws, bank, base, hold="-"):
    log = Path(tempfile.mkdtemp(prefix="judge-child-")) / "events.jsonl"
    p = subprocess.Popen([PY, "-I", str(CHILD), mode, str(ws), str(bank), str(base), str(log), str(hold)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return SimpleNamespace(p=p, log=log)


def events(c):
    return [json.loads(x) for x in c.log.read_text(encoding="utf-8").splitlines()] if c.log.exists() else []


def names(c):
    return [e["event"] for e in events(c)]


def wait_event(c, name, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if name in names(c):
            return True
        if c.p.poll() is not None:
            return name in names(c)
        time.sleep(0.05)
    return False


def finish(c, timeout=90):
    try:
        _, err = c.p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        c.p.kill()
        _, err = c.p.communicate()
    rc_ev = [e for e in events(c) if e["event"] == "rc"]
    return c.p.returncode, (rc_ev[-1]["out"] if rc_ev else "") + err.decode("utf-8", "replace")


ALL_CHILDREN = []

# C1／F-05：兩個程序同時 dry-run 同一工作區 → 只有一個發出請求
ws_c1 = DRB / "c1_dry"
valid_snapshot(ws_c1, dr_bank)
rel1 = DRB / "c1.release"
A = spawn("dry", ws_c1, dr_bank, P1_BASE, rel1)
ALL_CHILDREN.append(A)
check("C1 process A holds workspace inside its first Learn request", wait_event(A, "holding"), names(A))
B = spawn("dry", ws_c1, dr_bank, P1_BASE)
ALL_CHILDREN.append(B)
rc_b, out_b = finish(B)
check("C1 concurrent dry-run B → refused (lock busy), zero requests", rc_b == 2 and "無法取得工作區鎖" in out_b
      and not {"search", "fetch"} & set(names(B)), (rc_b, out_b, names(B)))
rel1.write_text("go", encoding="utf-8")
rc_a, out_a = finish(A)
check("C1 process A completes", rc_a == 0 and len(packet_files(ws_c1)) == 1, (rc_a, out_a[-400:]))
C = spawn("dry", ws_c1, dr_bank, P1_BASE)
ALL_CHILDREN.append(C)
rc_c, out_c = finish(C)
check("C1 later dry-run on same workspace → refused (not empty), zero requests", rc_c == 2 and "不是空白工作區" in out_c
      and not {"search", "fetch"} & set(names(C)), out_c)

# C2：--run 執行中，其他程序的 --run／--reevaluate／--report 都在建立 client 與寫入之前被拒絕
ec2 = setup(ids=("m-single",))
run(ec2, FakeClient([single_result(packet_of(ec2, "m-single"))]))  # 先有一筆結果，讓 reevaluate／report 通過預檢
rel2 = DRB / "c2.release"
A2 = spawn("run", ec2.out, ec2.bank, ec2.base, rel2)
ALL_CHILDREN.append(A2)
check("C2 process A is inside the API call holding the lock", wait_event(A2, "holding"), names(A2))
res_mid = sha(ec2.out / "gemini_judgments.json")
for mode in ("run", "reeval", "report"):
    Bx = spawn(mode, ec2.out, ec2.bank, ec2.base)
    ALL_CHILDREN.append(Bx)
    rc_x, out_x = finish(Bx)
    check(f"C2 concurrent {mode} → refused before client / write", rc_x == 2 and "無法取得工作區鎖" in out_x
          and not {"client", "api"} & set(names(Bx)), (rc_x, out_x))
check("C2 results file unchanged while A holds the lock", sha(ec2.out / "gemini_judgments.json") == res_mid
      and not (ec2.out / "gemini_judgments.md").exists())
rel2.write_text("go", encoding="utf-8")
rc_a2, out_a2 = finish(A2)
check("C2 process A finishes and appends exactly one run", rc_a2 == 0 and nruns(ec2, "m-single") == 2
      and not gj.unfinished_attempts(ec2.out, results(ec2)), (rc_a2, out_a2[-300:]))

# C3：持有鎖的程序被強制終止 → 殘留鎖檔不會永久阻擋，也不會被刪除
ws_c3 = DRB / "c3_crash"
valid_snapshot(ws_c3, dr_bank)
K = spawn("hold_lock", ws_c3, dr_bank, P1_BASE, DRB / "c3.never")
ALL_CHILDREN.append(K)
check("C3 child holds OS lock", wait_event(K, "locked"))
T = spawn("try_lock", ws_c3, dr_bank, P1_BASE)
rc_t, out_t = finish(T)
busy_msg = [e.get("msg", "") for e in events(T) if e["event"] == "busy"]
check("C3 while held: lock file exists AND is held → busy", rc_t == 3 and busy_msg and "確實被持有" in busy_msg[0], (rc_t, out_t))
K.p.kill()
K.p.wait(timeout=30)
lock_c3 = ws_c3 / gj.LOCK_NAME
check("C3 after crash: lock file still present (not auto-deleted)", lock_c3.exists())
T2 = spawn("try_lock", ws_c3, dr_bank, P1_BASE)
rc_t2, _ = finish(T2)
check("C3 after crash: lock file exists but not held → acquired", rc_t2 == 0 and "acquired" in names(T2))
D3 = spawn("dry", ws_c3, dr_bank, P1_BASE)
rc_d3, out_d3 = finish(D3)
check("C3 stale lock file does not block a new dry-run", rc_d3 == 0 and lock_c3.exists(), out_d3[-300:])

# C4：同程序重入期間，其他程序取不到鎖；釋放後可取得
w_c4 = DRB / "c4_reentry"
w_c4.mkdir()
with gj.workspace_lock(w_c4):
    with gj.workspace_lock(w_c4):
        T4 = spawn("try_lock", w_c4, dr_bank, P1_BASE)
        rc_t4, _ = finish(T4)
check("C4 other process busy while this process holds (re-entered) lock", rc_t4 == 3)
T5 = spawn("try_lock", w_c4, dr_bank, P1_BASE)
rc_t5, _ = finish(T5)
check("C4 after release → other process acquires", rc_t5 == 0)
check("C children made no network attempts", all("network_attempt" not in names(c) for c in ALL_CHILDREN + [T, T2, D3, T4, T5]))

# ---- F-08：快照信任（格式有效、來源一致、內容可信）----
ctx_tmp = va.Ctx(dr_bank, DRB / "unused-out")
ent_vote = va.new_entry(ctx_tmp, ctx_tmp.units["m-single"])
ent_vote["examtopics_evidence"].update(match_status="exact", url="https://www.examtopics.com/discussions/microsoft/view/1-x/",
                                       page_answer="B", community_vote={"answer": "B", "share_percent": 90, "vote_count": 20,
                                                                        "as_of": "2026-10-27"})
ws_v = DRB / "f08_verified"
valid_snapshot(ws_v, dr_bank, {"m-single": ent_vote})
base_v = DRB / "f08_repo" / "output"
valid_snapshot(base_v, dr_bank, {"m-single": ent_vote})
base_v_state = tree(base_v)
snap_v = (ws_v / "verification.json").read_bytes()
va.DEFAULT_OUT = base_v
rc, log, calls = dry(dr_bank, ws_v, ids=("m-single", "m-multi"))
va.DEFAULT_OUT = P1_BASE
pv = gj.load_packet(gj.latest_packet(ws_v / "gemini_judge" / "packets", "m-single"))[0]
check("F08 identical to trusted baseline → verified", rc == 0 and pv["snapshot_trust"]["level"] == "verified"
      and pv["snapshot_trust"]["content_trusted"] is True and "verified" in log, log[:400])
check("F08 packet records snapshot SHA256 / schema / rules version",
      pv["snapshot_sha256"] == hashlib.sha256(snap_v).hexdigest() and pv["snapshot_schema_version"] == va.SCHEMA_VERSION
      and pv["packet_rules_version"] == gj.PACKET_RULES_VERSION and pv["snapshot_trust"]["rules_version"] == gj.SNAPSHOT_TRUST_VERSION)
check("F08 verified packet → community can qualify", gj.formal_consensus(pv)["qualifies"] is True
      and gj.final_recommendation(us, pv, None, "deferred", None)["source"] == "community")
check("F08 snapshot / baseline not modified, nothing copied", (ws_v / "verification.json").read_bytes() == snap_v
      and tree(base_v) == base_v_state)
for label, prep, expect in (
        ("baseline differs", lambda: valid_snapshot(DRB / "f08b_repo" / "output", dr_bank), "不一致"),
        ("baseline missing", lambda: (DRB / "f08c_repo" / "output").mkdir(parents=True), "找不到正式基準")):
    base_x = DRB / ("f08b_repo" if "differs" in label else "f08c_repo") / "output"
    prep()
    ws_x = DRB / f"f08_{label.replace(' ', '_')}"
    valid_snapshot(ws_x, dr_bank, {"m-single": ent_vote})
    va.DEFAULT_OUT = base_x
    rc, log, calls = dry(dr_bank, ws_x)
    va.DEFAULT_OUT = P1_BASE
    px = gj.load_packet(gj.latest_packet(ws_x / "gemini_judge" / "packets", "m-single"))[0]
    fc = gj.formal_consensus(px)
    check(f"F08 {label} → unverified, community NOT qualified (fail-closed)", rc == 0
          and px["snapshot_trust"]["level"] == "unverified" and any(expect in r for r in px["snapshot_trust"]["reasons"])
          and fc["qualifies"] is False and any("未通過來源驗證" in r for r in fc["reasons"])
          and gj.final_recommendation(us, px, None, "deferred", None)["source"] == "bank_fallback"
          and "unverified" in log, (label, px["snapshot_trust"], fc["reasons"]))
    check(f"F08 {label} → model view / role / summary agree", gj.model_view(px)["examtopics"]["formal_consensus"]["qualifies"] is False
          and gj.official_docs_role(px)["role"] == "primary_verification" and "正式共識 未達" in gj.packet_summary(px)["examtopics"])
j_base = DRB / "f08_junction_repo"
j_base.mkdir()
_winapi.CreateJunction(str(base_v), str(j_base / "output"))
try:
    ws_j = DRB / "f08_junction_ws"
    valid_snapshot(ws_j, dr_bank, {"m-single": ent_vote})
    va.DEFAULT_OUT = j_base / "output"
    rc, log, calls = dry(dr_bank, ws_j)
    va.DEFAULT_OUT = P1_BASE
    pj = gj.load_packet(gj.latest_packet(ws_j / "gemini_judge" / "packets", "m-single"))[0]
    check("F08 baseline reached through junction → location untrusted → unverified", rc == 0
          and pj["snapshot_trust"]["level"] == "unverified" and pj["snapshot_trust"]["baseline"]["location_trusted"] is False
          and any("轉向" in r for r in pj["snapshot_trust"]["reasons"]), pj["snapshot_trust"])
finally:
    os.rmdir(j_base / "output")  # 只移除 junction 本身
src_gj = Path(gj.__file__).read_text(encoding="utf-8")
check("F08 no hard-coded baseline hash in source", not re.search(r"['\"][0-9a-f]{64}['\"]", src_gj))

# ---- F-09：缺少項目 ≠ 沒有社群資料 ----
sec_missing = gj.examtopics_section(None)
check("F09 missing entry labelled entry_missing (unknown), not 'no community'", sec_missing["status"] == "entry_missing"
      and sec_missing["community_data_state"] == "entry_missing" and "不等於沒有社群資料" in sec_missing["note"]
      and sec_missing["note"] != va.NO_COMMUNITY)
pm_missing = gj.load_packet(gj.latest_packet(ws_v / "gemini_judge" / "packets", "m-multi"))[0]
check("F09 dry-run packet for id without snapshot entry → entry_missing", pm_missing["examtopics"]["status"] == "entry_missing"
      and "不等於沒有社群資料" in json.dumps(gj.model_view(pm_missing), ensure_ascii=False))
pkm_states = {qid: packet_of(env, qid)["examtopics"]["community_data_state"] for qid in ("m-single", "m-multi", "m-stmt")}
check("F09 explicit none / not searched / available distinguished",
      pkm_states == {"m-single": "available", "m-multi": "explicit_none", "m-stmt": "not_searched"}, pkm_states)
ent_prob = copy.deepcopy(ent_vote)
ent_prob["examtopics_evidence"].update(match_status="probable", community_vote={"answer": None, "share_percent": None,
                                                                                "vote_count": None, "as_of": None})
check("F09 probable → unconfirmed_match", gj.examtopics_section(ent_prob)["community_data_state"] == "unconfirmed_match")

# ---- F-10：證據包版本欄位與文件擷取紀錄 ----
check("F10 packet carries centralized version fields", all(pv.get(k) == v for k, v in gj.PACKET_VERSIONS.items())
      and all(k in pv for k in gj.PACKET_REQUIRED_FIELDS) and gj.packet_generation(pv) == "current")
flog = pv["microsoft_learn"]["fetch_log"]
ok_rec = [r for r in flog if r["status"] == "ok"]
check("F10 fetch log has one record per fetch attempt (ok + failed)",
      flog and len(flog) == len(ok_rec) + len(pv["microsoft_learn"]["documents_failed"]), flog)
REQ_FETCH_KEYS = {"requested_url", "normalized_url", "final_url", "status", "http_status", "error", "fetched_at_utc",
                  "raw_sha256", "raw_length", "fetched_text_sha256", "cleaned_text_sha256", "cleaned_text_length",
                  "complete", "complete_basis"}
check("F10 fetch records have all fields", flog and all(REQ_FETCH_KEYS <= set(r) for r in flog), flog[:1])
check("F10 injected fetcher → metadata unknown, completeness not claimed", ok_rec and all(
    r["raw_sha256"] is None and r["complete"] is None and r["complete_basis"] == [gj.NO_FETCH_META] for r in ok_rec))
check("F10 text hashes recorded", all(r["fetched_text_sha256"] == hashlib.sha256(DOCS[r["normalized_url"]].encode("utf-8")).hexdigest()
                                      for r in ok_rec))


class FakeResp:
    def __init__(self, body, status=200, final=None, length="auto"):
        self.body, self.status, self.final = body, status, final
        self.headers = {} if length is None else {"Content-Length": str(len(body) if length == "auto" else length),
                                                  "Content-Type": "text/html"}

    def read(self):
        return self.body

    def geturl(self):
        return self.final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


PAGE = ("<html><head><title>x</title></head><body><nav>menu</nav><main><h1>Sentinel tables</h1>"
        "<p>The SigninLogs table contains Microsoft Entra sign-in events &amp; more.</p><script>x()</script></main>"
        "<footer>f</footer></body></html>").encode("utf-8")
LURL = "https://learn.microsoft.com/en-us/azure/sentinel/fake-page"
real_urlopen = urllib.request.urlopen
try:
    urllib.request.urlopen = lambda req, timeout=30: FakeResp(PAGE, final=LURL)
    m_ok = gj.fetch_learn_page(LURL)
    same_as_va = m_ok["text"] == va.fetch_doc_text(LURL)
    urllib.request.urlopen = lambda req, timeout=30: FakeResp(PAGE, final=LURL, length=5)
    m_len = gj.fetch_learn_page(LURL)
    urllib.request.urlopen = lambda req, timeout=30: FakeResp(PAGE, final=LURL, length=None)
    m_nolen = gj.fetch_learn_page(LURL)
    urllib.request.urlopen = lambda req, timeout=30: FakeResp(b"<html><body>partial", final=LURL)
    m_nomain = gj.fetch_learn_page(LURL)
    urllib.request.urlopen = lambda req, timeout=30: FakeResp(PAGE, final="https://evil.example.com/x")
    try:
        gj.fetch_learn_page(LURL)
        offsite = False
    except gj.FetchRejected as err:
        offsite = err.fetch_meta["final_url"] == "https://evil.example.com/x"

    def http404(req, timeout=30):
        raise urllib.error.HTTPError(LURL, 404, "Not Found", {}, None)
    urllib.request.urlopen = http404
    ml404 = gj.gather_learn(us, None, search=lambda q, top=5: [], fetch=None, delay=0, doc_urls=[LURL + "?x=1#y"])
    urllib.request.urlopen = lambda req, timeout=30: FakeResp(PAGE, final="https://evil.example.com/x")
    ml_off = gj.gather_learn(us, None, search=lambda q, top=5: [], fetch=None, delay=0, doc_urls=[LURL])
finally:
    urllib.request.urlopen = real_urlopen
check("F10 default fetcher: same text as verify_answers.fetch_doc_text", same_as_va and "SigninLogs" in m_ok["text"]
      and "menu" not in m_ok["text"])
check("F10 default fetcher: raw SHA256, final URL, status, complete", m_ok["raw_sha256"] == hashlib.sha256(PAGE).hexdigest()
      and m_ok["final_url"] == LURL and m_ok["http_status"] == 200 and m_ok["complete"] is True and m_ok["raw_length"] == len(PAGE))
check("F10 Content-Length mismatch → incomplete", m_len["complete"] is False and any("不符" in b for b in m_len["complete_basis"]))
check("F10 no Content-Length → completeness unknown (not claimed)", m_nolen["complete"] is None)
check("F10 no <main> → incomplete", m_nomain["complete"] is False)
check("F10 off-site redirect → rejected as fetch failure", offsite)
r404 = ml404["fetch_log"][0]
check("F10 HTTP 404 recorded with original / normalized URL", r404["status"] == "failed" and r404["http_status"] == 404
      and r404["requested_url"] == LURL + "?x=1#y" and r404["normalized_url"] == LURL and r404["complete"] is False)
check("F10 off-site redirect recorded with final URL, doc not used", ml_off["fetch_log"][0]["final_url"] == "https://evil.example.com/x"
      and ml_off["fetch_log"][0]["status"] == "failed" and not ml_off["documents"])

# 舊證據包：不修改、不升級；可重用 ≠ 已完成
el = setup(ids=("m-single",))
pk_el = packet_of(el, "m-single")
legacy = {k: v for k, v in pk_el.items() if k not in gj.PACKET_REQUIRED_FIELDS or k in ("created_at", "bank_sha256")}
legacy["packet_version"] = 1
legacy["microsoft_learn"] = {k: v for k, v in legacy["microsoft_learn"].items() if k != "fetch_log"}
legacy_sha = gj.sha256_text(gj.canonical(legacy))
legacy_path = el.out / "gemini_judge" / "packets" / "m-single.29991231-000000-000000.json"  # 檔名排序最後
legacy_path.write_text(json.dumps({"packet_sha256": legacy_sha, "packet": legacy}, ensure_ascii=False), encoding="utf-8")
legacy_bytes = legacy_path.read_bytes()
check("F10 legacy / unknown generation detected", gj.packet_generation(legacy) == "legacy"
      and gj.packet_generation(dict(pk_el, packet_version=3)) == "unknown"
      and gj.packet_generation({k: v for k, v in pk_el.items() if k != "snapshot_trust"}) == "unknown")
fc_leg = gj.formal_consensus(legacy)
check("F10 legacy packet → community not qualified (conservative)", fc_leg["qualifies"] is False
      and any("舊版" in r for r in fc_leg["reasons"]))
cl_leg = FakeClient([single_result(pk_el)])
rc, log, made = run(el, cl_leg)
check("F10 legacy packet not completed → --run refused before client", rc == 2 and "舊版" in log and not made
      and not cl_leg.calls and legacy_path.read_bytes() == legacy_bytes, log)
done = copy.deepcopy(results(ef1)["questions"]["m-single"]["runs"][0])
done.update(packet_sha256=legacy_sha, packet_file=legacy_path.name)
(el.out / "gemini_judgments.json").write_text(json.dumps({"meta": {}, "questions": {"m-single": {"runs": [done]}}}), encoding="utf-8")
cl_leg2 = FakeClient([])
rc, log, made = run(el, cl_leg2)
check("F10 legacy packet already completed → skipped (completion ≠ reusability)", rc == 0 and "略過" in log and not made, log)
rc, log = mainq(el.argv + ["--reevaluate"])
rr_leg = results(el)["questions"]["m-single"]["runs"]
check("F10 reevaluate legacy packet: allowed offline, community not adopted, packet unchanged", rc == 0 and len(rr_leg) == 2
      and rr_leg[1]["final_exam_recommendation"]["source"] != "community" and legacy_path.read_bytes() == legacy_bytes, log)

# ---- F-12：整批預檢（在任何外部請求、建立目錄或寫入之前）----


def ent_with(mut):
    e = copy.deepcopy(ent_vote)
    mut(e)
    return e


ET = lambda e: e["examtopics_evidence"]  # noqa: E731
CV = lambda e: e["examtopics_evidence"]["community_vote"]  # noqa: E731
bad_entries = [
    ("no examtopics_evidence", lambda e: e.pop("examtopics_evidence"), "examtopics_evidence"),
    ("match_status invalid", lambda e: ET(e).__setitem__("match_status", "maybe"), "match_status"),
    ("match_status not str", lambda e: ET(e).__setitem__("match_status", 1), "match_status"),
    ("community_vote not object", lambda e: ET(e).__setitem__("community_vote", ["B"]), "community_vote"),
    ("vote_count string", lambda e: CV(e).__setitem__("vote_count", "20"), "vote_count"),
    ("vote_count bool", lambda e: CV(e).__setitem__("vote_count", True), "vote_count"),
    ("vote_count negative", lambda e: CV(e).__setitem__("vote_count", -1), "vote_count"),
    ("share > 100", lambda e: CV(e).__setitem__("share_percent", 150), "share_percent"),
    ("distribution not list", lambda e: CV(e).__setitem__("distribution", "B:20"), "distribution"),
    ("distribution votes string", lambda e: CV(e).__setitem__("distribution", [{"answer": "B", "percent": 100, "votes": "20"}]), "distribution"),
    ("distribution percent > 100", lambda e: CV(e).__setitem__("distribution", [{"answer": "B", "percent": 101, "votes": 20}]), "distribution"),
    ("vote_count ≠ distribution total", lambda e: CV(e).__setitem__("distribution", [{"answer": "B", "percent": 100, "votes": 19}]), "不一致"),
    ("answer on probable match", lambda e: ET(e).__setitem__("match_status", "probable"), "未確認同一題"),
    ("answer incompatible with type", lambda e: CV(e).__setitem__("answer", "Z"), "不相容"),
    ("multi-style answer on single", lambda e: CV(e).__setitem__("answer", ["A", "B"]), "不相容"),
    ("main_reasons not list", lambda e: ET(e).__setitem__("main_reasons", "text"), "main_reasons"),
    ("official url not Learn", lambda e: e["microsoft_evidence"].append({"url": "https://example.com/x", "verification": "unverified"}), "url"),
    ("official verification invalid", lambda e: e["microsoft_evidence"].append({"url": TABLES, "verification": "yes"}), "verification"),
    ("official verified without excerpt", lambda e: e["microsoft_evidence"].append({"url": TABLES, "verification": "verified"}), "原文摘錄"),
    ("official supports invalid", lambda e: e["microsoft_evidence"].append({"url": TABLES, "verification": "unverified", "supports": "x"}), "supports"),
    ("candidates bad url", lambda e: e.__setitem__("microsoft_candidates", [{"url": "http://x"}]), "microsoft_candidates"),
]
for label, mut, expect in bad_entries:
    w = DRB / f"f12_{re.sub(r'[^A-Za-z0-9]+', '_', label)}"
    valid_snapshot(w, dr_bank, {"m-single": ent_with(mut)})
    before = tree(w)
    rc, log, calls = dry(dr_bank, w, ids=("m-multi", "m-single"))
    check(f"F12 {label} → whole batch refused before any request / write", rc == 2 and "整批預檢未通過" in log and expect in log
          and not calls and tree(w) == before, (label, log))
check("F12 well-formed entry passes", gj.evidence_problems("m-single", us, ent_vote) == [])
real_snap = PROJECT / "output" / "verification.json"
if real_snap.exists():  # 唯讀：以真實快照做結構檢查（不寫入、不連網）
    rdata = json.loads(real_snap.read_text(encoding="utf-8"))
    rbank = va.Ctx(PROJECT / "public" / "data" / "sc200.json", DRB / "unused-real").units
    rprob = {q: gj.evidence_problems(q, rbank[q], e) for q, e in rdata["questions"].items() if q in rbank}
    print(f"NOTE F12 real snapshot (read-only): {sum(1 for v in rprob.values() if not v)}/{len(rprob)} entries pass evidence checks;"
          f" failing: {[(q, v[:1]) for q, v in rprob.items() if v][:5]}")
    check("F12 real snapshot sweep ran without exceptions (read-only)", len(rprob) == len(rdata["questions"]))

# ---- F-14：檢查與使用之間的時間差（鎖內重新檢查）----
real_batch = gj.dry_run_batch_problems


def intruder_batch(ctx, ids):
    if not hasattr(intruder_batch, "done"):
        intruder_batch.done = True
        (ctx.out_dir / "intruder.txt").write_text("appeared after the first check", encoding="utf-8")
    return real_batch(ctx, ids)


ws_t1 = DRB / "f14_intruder"
valid_snapshot(ws_t1, dr_bank)
gj.dry_run_batch_problems = intruder_batch
try:
    rc, log, calls = dry(dr_bank, ws_t1)
finally:
    gj.dry_run_batch_problems = real_batch
check("F14 workspace changed between check and lock → re-check refuses, zero requests", rc == 2 and "不是空白工作區" in log
      and "intruder.txt" in log and not calls and not packet_files(ws_t1), log)


def swap_snapshot_batch(ctx, ids):
    if not hasattr(swap_snapshot_batch, "done"):
        swap_snapshot_batch.done = True
        valid_snapshot(ctx.out_dir, dr_bank, {"m-single": ent_vote})
    return real_batch(ctx, ids)


ws_t2 = DRB / "f14_swap"
valid_snapshot(ws_t2, dr_bank)
gj.dry_run_batch_problems = swap_snapshot_batch
try:
    rc, log, calls = dry(dr_bank, ws_t2)
finally:
    gj.dry_run_batch_problems = real_batch
pt2 = gj.load_packet(gj.latest_packet(ws_t2 / "gemini_judge" / "packets", "m-single"))[0]
check("F14 snapshot replaced before lock → packet content and recorded hash come from the same bytes", rc == 0
      and pt2["snapshot_sha256"] == hashlib.sha256((ws_t2 / "verification.json").read_bytes()).hexdigest()
      and pt2["examtopics"]["status"] == "available", (log[-300:], pt2["examtopics"].get("status")))


def bank_change_batch(ctx, ids):
    if not hasattr(bank_change_batch, "done"):
        bank_change_batch.done = True
        b2 = json.loads(ctx.bank_path.read_text(encoding="utf-8"))
        b2["questions"][0]["answer"] = "C"
        ctx.bank_path.write_text(json.dumps(b2), encoding="utf-8")
    return real_batch(ctx, ids)


TB = DRB / "f14_bank"
TB.mkdir()
tbank = TB / "bank.json"
tbank.write_text(json.dumps(BANK), encoding="utf-8")
ws_t3 = DRB / "f14_bank_ws"
valid_snapshot(ws_t3, tbank)
gj.dry_run_batch_problems = bank_change_batch
try:
    rc, log, calls = dry(tbank, ws_t3)
finally:
    gj.dry_run_batch_problems = real_batch
check("F14 bank changed before lock → refused, zero requests", rc == 2 and not calls and not packet_files(ws_t3), log)

# ---- 全域確認 ----
va.DEFAULT_OUT = real_default_out
check("P1 fake official output untouched after setup", tree(P1_BASE) == official_before)
check("real output/ untouched by the whole test run", dir_state(real_default_out) == real_output_state)
check("project git index unchanged", hashlib.sha256((PROJECT / ".git" / "index").read_bytes()).hexdigest() == PROJECT_INDEX_SHA)
check("zero network attempts in this process", not NET_ATTEMPTS, NET_ATTEMPTS)

print(f"{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
sys.exit(1 if FAILED else 0)
