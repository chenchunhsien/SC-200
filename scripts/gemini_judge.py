#!/usr/bin/env python3
"""SC-200 Gemini 答案判讀（獨立模組）：把題目、題庫答案、ExamTopics 社群資料與 Microsoft Learn 原文段落
整理成「證據包」交給 Gemini 判讀，再由程式核對引句、評估官方證據並產生「最終考試推薦」，結果另存。

分工
----
- 取得證據：沿用 verify_answers.py（verification.json 的 ExamTopics 紀錄、learn_search、fetch_doc_text）。
- 判讀：Gemini 只閱讀證據包並提出建議答案與引句；不負責搜尋或爬取網站。所有題型都走同一判讀流程。
- 核對：由本程式計算官方證據等級（描述證據品質，不是正確率）。Gemini 的信心自評只記錄、不採用。
- 本模組不修改題庫（sc200.json）、verification.json 或 explanations.json；最終考試推薦與題庫答案分開保存。

原則
----
- 最終考試推薦的優先順序（推薦策略，不代表技術事實必然正確）：
  1. 達採用門檻的 ExamTopics 社群推薦：已確認同一題、總票數 ≥ 10、未四捨五入支持率 ≥ 70%、答案可對應本題；
     無論 Gemini 是否同意、是否有效或 API 是否失敗，都採用社群推薦。
  2. Gemini 有效建議：可不同於題庫答案或官方文件判讀；不因差異降低官方證據等級，也不要求人工複核。
  3. 所有題型：Gemini API 失敗或建議無效（依各題型既有格式解析失敗）時，保留題庫原始答案。
- 官方文件與 Gemini 建議衝突時記錄於 official_conflicts，不因衝突本身降低官方證據等級。
- 有效引句只代表「引文確實存在於本次證據包的段落中」，不代表答案已被官方文件證實；有效選項也不代表答案已被證實。
- 官方文件「未提及」某選項不視為反駁；無法排除的干擾選項會列為證據缺口。

用法
----
    python scripts/gemini_judge.py --ids qd6-qs1-q019,qd1-qs4-q002          # dry-run（預設）：產生證據包，不呼叫 Gemini
    python scripts/gemini_judge.py --ids qd6-qs1-q019 --doc-url https://learn.microsoft.com/...   # 加入指定文件
    python scripts/gemini_judge.py --ids A,B --doc-url A=https://learn.microsoft.com/...           # 多題時指定題目
    python scripts/gemini_judge.py --ids ... --run --tier free|paid           # 送出最近一次 dry-run 的證據包（需互動確認）
    python scripts/gemini_judge.py --output-dir output/WS --report             # 產生 WS/gemini_judgments.md（已存在即拒絕）
    python scripts/gemini_judge.py --output-dir output/WS --ids ... --reevaluate   # 以已保存的模型輸出離線重核（不呼叫 API）

所有指令都必須以 --output-dir 明確指定獨立工作區：不得是正式 output/ 或其 gemini_judge/、backups/，
且必須位於 Git 儲存庫內、已確認受 .gitignore 排除的位置。dry-run 接受「空白工作區」（只有 verification.json
快照，以及可能殘留的工作區鎖檔），或由本程式累積多批的工作區（只含本程式建立的結果檔、證據包、請求紀錄與備份，
既有證據包的快照與題庫須與本批相同）；同一題已有證據包時拒絕，不覆寫。同一工作區同時間只允許一個程序寫入（OS 檔案鎖）。
--run 對已達社群共識門檻（formal_consensus）的題目直接採用社群答案、不呼叫 Gemini，記錄為 kind=community_adopted。

--run 每題只呼叫一次：不重試、不切換模型；一次最多 5 題。503 / 逾時標為 deferred（連續 2 次即停止），
429 及 400 / 401 / 403 / 404 立即停止。錯誤訊息一律遮蔽 API Key。
遠端 API 呼叫與本機寫入不是單一交易：每次送出前先在 gemini_judge/attempts/ 建立請求紀錄，
結果檔中沒有相同 attempt_id 的紀錄，即代表該次請求未完成或結果未寫入（可能已送出、已計費）。
"""

from __future__ import annotations

import argparse
import copy
import functools
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import verify_answers as va  # noqa: E402 — 唯讀沿用驗證工具的題目展開、答案格式與 Learn 擷取

eq = va.eq

# 證據包版本資訊（集中管理；新證據包一律寫入這些欄位，舊證據包不修改、不自動升級）
PACKET_VERSIONS = {
    "packet_version": 2,                          # 1 = 舊版（沒有快照來源與文件擷取紀錄）
    "packet_rules_version": "packet-rules-v2",    # 快照信任、社群資格閘門、版本欄位的規則
    "query_strategy_version": "learn-query-v2",   # build_queries() / gather_learn() 的檢索與篩選策略（v2：AIP 規則、題組情境查詢）
    "document_cleaning_version": "learn-clean-v1",  # fetch_learn_page() 的 HTML 轉文字與 clean_learn_text()
}
PACKET_VERSION = PACKET_VERSIONS["packet_version"]
PACKET_RULES_VERSION = PACKET_VERSIONS["packet_rules_version"]
SNAPSHOT_TRUST_VERSION = "snapshot-trust-v1"
PACKET_REQUIRED_FIELDS = ("packet_version", "packet_rules_version", "query_strategy_version", "document_cleaning_version",
                          "created_at", "bank_sha256", "snapshot_sha256", "snapshot_schema_version", "snapshot_trust")
RESULTS_VERSION = 1
PROMPT_VERSION = "judge-v4"
EVALUATOR_VERSION = "eval-v5"
MAX_IDS_PER_RUN = 5          # 防止誤啟動大批次
MAX_DOCS = 4                 # 每題最多擷取的 Learn 文件數
MAX_PASSAGES = 8             # 每題送出的段落上限
PER_DOC_PASSAGES = 3         # 每篇文件最多段落數（人工查證摘錄所在段落另計）
SENTENCES_PER_PASSAGE = 3
MAX_PASSAGE_CHARS = 900
MIN_PASSAGE_SCORE = 3
MIN_QUOTE_CHARS = 20
TRANSIENT_STATUS = {500, 502, 503, 504}
FATAL_STATUS = {400, 401, 403, 404}
MAX_CONSECUTIVE_TRANSIENT = 2
VALID_QUOTE_MEANING = "有效引句只代表引文可在本次證據包的段落中找到，不代表答案已被官方文件證實。"

ANSWER_FORMATS = {
    "single_choice": '單一選項 ID，例如 "B"',
    "multiple_choice": '選項 ID 陣列，例如 ["A","D"]',
    "dropdown_cloze": '物件 {挖空 ID: 該挖空的選項文字}，例如 {"select_0":"join"}',
    "drag_drop_order": '依序排列的項目 ID 陣列，例如 ["d","a","f"]',
    "statement_matrix": '物件 {陳述句 ID: 欄位文字}，例如 {"s1":"Yes","s2":"No"}',
}

MAX_QUERIES = 16             # Learn 搜尋為免費公開 API；每個選項一組短查詢
SEARCH_TOP = 15             # Learn 搜尋排序不穩定，多取結果再以產品路徑與段落相關性篩選
MAX_FETCH = 6                # 每題最多擷取的搜尋結果文件數（人工查證 / 使用者提供的文件另計）
QUERY_MAX_WORDS = 8

# 產品規則：(名稱, 題目文字的辨識式, 段落中的別名, 允許的 Learn 路徑前綴)
PRODUCT_RULES = [
    ("Microsoft Defender for Cloud Apps", r"Defender for Cloud Apps|Cloud App Security", ["Defender for Cloud Apps"],
     ["/defender-cloud-apps/", "/cloud-app-security/"]),
    ("Microsoft Defender for Endpoint", r"Defender for Endpoint", ["Defender for Endpoint"],
     ["/defender-endpoint/", "/microsoft-365/security/defender-endpoint/"]),
    ("Microsoft Defender for Identity", r"Defender for Identity", ["Defender for Identity"],
     ["/defender-for-identity/"]),
    ("Microsoft Defender for Office 365", r"Defender for Office 365", ["Defender for Office 365"],
     ["/defender-office-365/", "/microsoft-365/security/office-365-security/"]),
    ("Microsoft Defender for Cloud", r"Defender for Cloud(?! Apps)", ["Defender for Cloud"], ["/azure/defender-for-cloud/"]),
    ("Microsoft Defender XDR", r"Defender XDR|Microsoft 365 Defender|advanced hunting|Microsoft Defender portal",
     ["Defender XDR", "advanced hunting", "Defender portal"],
     ["/defender-xdr/", "/microsoft-365/security/defender/", "/unified-secops/", "/unified-secops-platform/"]),
    ("Microsoft Sentinel", r"(?:Microsoft|Azure) Sentinel", ["Sentinel"],
     ["/azure/sentinel/", "/unified-secops/", "/unified-secops-platform/"]),
    ("Microsoft Entra", r"Microsoft Entra|Azure Active Directory|Azure AD\b", ["Entra"],
     ["/entra/", "/azure/active-directory/"]),
    ("Microsoft Graph", r"Microsoft Graph", ["Microsoft Graph"], ["/graph/"]),
    ("Azure Monitor", r"Azure Monitor|Log Analytics|[Aa]ctivity [Ll]og", ["Azure Monitor", "Log Analytics"],
     ["/azure/azure-monitor/"]),
    ("Microsoft Purview", r"Purview", ["Purview"], ["/purview/"]),
    ("Microsoft Intune", r"Intune", ["Intune"], ["/intune/", "/mem/intune/"]),
    ("Azure Logic Apps", r"Logic Apps", ["Logic Apps"], ["/azure/logic-apps/"]),
    ("Kusto Query Language", r"\bKQL\b|Kusto", ["KQL", "Kusto"], ["/kusto/", "/azure/data-explorer/kusto/"]),
    ("Microsoft Security Copilot", r"Security Copilot", ["Security Copilot"], ["/copilot/security/"]),
    ("Azure Arc", r"Azure Arc", ["Azure Arc"], ["/azure/azure-arc/"]),
]
# 官方文件檢索專用的產品規則（learn-query-v2）：與 PRODUCT_RULES 相同，只把 AIP（Azure / Microsoft Information
# Protection，已併入 Purview）併入 Purview。判讀端（evaluation_matchers 等）仍使用 PRODUCT_RULES，核對結果不受影響。
PURVIEW_SEARCH_PATTERN = r"Purview|(?:Azure|Microsoft) Information Protection|\bAIP\b"
SEARCH_PRODUCT_RULES = [("Microsoft Purview", PURVIEW_SEARCH_PATTERN, ["Purview", "Information Protection"],
                         ["/purview/", "/azure/information-protection/"]) if r[0] == "Microsoft Purview" else r
                        for r in PRODUCT_RULES]
ALWAYS_EXCLUDED_PATHS = ("/training/", "/credentials/", "/answers/", "/shows/", "/whats-new", "/release-notes")
PLACEHOLDER = re.compile(r"^(?:[A-Z][a-z]+|[A-Z]{1,3})\d+$")       # Tactic1、User2、VM1 等佔位詞
WEAK_ACRONYMS = {"API", "AND", "THE", "NOT", "URL"}
QUESTION_WORDS = set("""which what how should would could you need want two three four five use used perform performs
actions action nodes node solution correct answer answers part presents following shown table select each statement
statements true otherwise yes from must ensure minimize meet requirement requirements recommend""".split())
LEARN_NOISE = re.compile(
    r"\b(?:Table of contents|Exit editor mode|Exit focus mode|Ask Learn|Reading mode|Read in English|Add to Plans"
    r"|Add to Collections|Copy Markdown|In this article|Save Add|Print)\b")
LEARN_HOSTS = {"learn.microsoft.com"}
# 產品變更提示：生命週期用語，加上「重要提示標記」或「明確年月日期」其中之一；只有日期不算
NOTICE_MARK = re.compile(r"\b(?:Important|Caution|Warning)\b")
LIFECYCLE_TERMS = re.compile(
    r"\b(?:deprecat\w*|retir(?:e|ed|es|ing|ement)|transition\w*|end of (?:support|life)|no longer (?:supported|available)"
    r"|replaced (?:by|with)|legacy|sunset\w*|phas(?:e|ed|ing) out|migrat(?:e|ed|ing|ion) to)\b", re.I)
DATE_TERMS = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"(?:\s+\d{1,2},)?\s+20\d{2}\b|\b20\d{2}-\d{2}(?:-\d{2})?\b")
MAX_NOTICES = 2              # 每題最多保留的產品變更提示段落，避免擠掉一般相關段落

SYSTEM_INSTRUCTION = """你是 Microsoft SC-200（Security Operations Analyst）答案判讀助理。只能根據使用者提供的「證據包」判讀題目答案。

目標：根據可取得的證據，推薦「最可能正確的考試答案」並說明依據與不確定性；不要求 100% 正確。

證據使用順序：
1. examtopics：優先參考已確認為同一題（match_status=exact）的社群答案、投票數、支持比例與討論摘要。疑似相同（match_unconfirmed）或找不到相同題目時，沒有可用的社群答案。是否達到社群共識以 examtopics.formal_consensus 為準（依原始票數計算）；display_based_threshold 只是頁面顯示值的參考。community_vote.share_percent 與 distribution[].percent 是頁面顯示的整數值，支持比例請以 formal_consensus.share_exact 為準。
2. microsoft_learn.documents[].passages 與 human_verified_microsoft_evidence：Microsoft 官方原文，用來核實技術事實、產品行為與版本變更。official_docs_role 說明本題官方文件的角色：
   - fact_check_only：社群已有共識，官方文件用來核實技術事實。
   - primary_verification：社群資料不足、投票未達共識、題目有爭議或可能過時，官方文件是主要的核實依據。
3. 社群推薦與官方文件「看似」矛盾時，不可只因看似矛盾就直接推翻社群推薦；請綜合判斷（例如題目版本、產品更名或功能轉移、適用條件），在 rationale 說明取捨，並把不確定之處寫入 conflicts／open_questions。
4. 你自己的一般知識：只能作為推論，必須在 basis 標示 model_inference，不可當成官方依據。
5. bank_answer 是題庫原始答案，僅供參考；你可以推薦與題庫答案或社群推薦不同的答案，但必須在 rationale 說明理由。

引用規則：
- citations 只能使用證據包中存在的 passage_id；quote 必須逐字複製該段落中的一段連續原文（至少 20 個字元），不可改寫、翻譯、刪節或合併多段。
- relation=supports_option：該段落顯示此選項（或此挖空 / 陳述句 / 項目在建議答案中的值）正確或適用。引文必須同時對應該選項與題目情境；只提到選項名稱、或只說明功能存在，請把 basis 標為 official_indirect。
- relation=refutes_option：該段落明確顯示此選項錯誤或不適用。官方文件「沒有提到」不是反駁，不得標為 refutes_option；這種情況請不附引用並把 assessment 設為 undetermined 或依推論判斷但 basis 標 model_inference。
- 業界慣例、功能名稱的字面意思、或你自己的知識都是推論，basis 必須標 model_inference，不可標為官方依據。
- 不得編造文件、網址或段落。

產品變更提示：
- passages 中標有 "notice": "product_change_notice" 的段落是官方的產品變更提示（功能轉換、淘汰、停用等）。
- 只要證據包中有這類段落，就必須在 product_change_impact 中分析：mentioned=true、affected_options（受影響的選項 id）、impact_assessment（對題目或答案的可能影響，例如題目是否可能過時、某選項是否已停用）、evidence_refs（引用的 passage_id）、unresolved_questions。
- 證據包中沒有這類段落時，mentioned=false，其餘欄位留空。

輸出規則：
- option_analysis 必須涵蓋 question 中每一個選項（options）、挖空（blanks）、陳述句（statements）或項目（items），target_id 使用其 id。
- suggested_answer 依 answer_format；單選直接寫選項 ID（例如 C），複選寫 JSON 陣列（例如 ["A","E"]）。
- docs_sufficient：官方段落是否足以直接支持結論並排除其他選項；無法以官方段落排除其他選項時必須為 false。
- recommendation_basis：推薦的主要依據——community_consensus（社群共識）、official_docs（官方文件）、combined（兩者綜合）或 model_inference（主要依賴推論）。
- 證據不足時，請在 open_questions 說明無法確定的部分，不要為了給答案而猜測。
- community_assessment、rationale、conflicts、open_questions 使用繁體中文，精簡、可追溯。
- model_confidence 只是你的自評，程式不會採用。"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "suggested_answer": {"type": "string", "description": "依 answer_format 的 JSON 文字"},
        "option_analysis": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "assessment": {"type": "string", "enum": ["correct", "incorrect", "undetermined"]},
                    "basis": {"type": "string",
                              "enum": ["official_direct", "official_indirect", "community", "model_inference", "none"]},
                    "citations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "passage_id": {"type": "string"},
                                "quote": {"type": "string"},
                                "relation": {"type": "string", "enum": ["supports_option", "refutes_option"]},
                            },
                            "required": ["passage_id", "quote", "relation"],
                        },
                    },
                    "note": {"type": "string"},
                },
                "required": ["target_id", "assessment", "basis", "citations", "note"],
            },
        },
        "community_assessment": {"type": "string"},
        "rationale": {"type": "string"},
        "docs_sufficient": {"type": "boolean"},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "model_confidence": {"type": "string", "enum": ["高", "中", "低"]},
        "recommendation_basis": {"type": "string",
                                 "enum": ["community_consensus", "official_docs", "combined", "model_inference"]},
        "product_change_impact": {
            "type": "object",
            "properties": {
                "mentioned": {"type": "boolean"},
                "affected_options": {"type": "array", "items": {"type": "string"}},
                "impact_assessment": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "unresolved_questions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["mentioned", "affected_options", "impact_assessment", "evidence_refs", "unresolved_questions"],
        },
    },
    "required": ["suggested_answer", "option_analysis", "community_assessment", "rationale",
                 "docs_sufficient", "conflicts", "open_questions", "model_confidence", "recommendation_basis",
                 "product_change_impact"],
}


# ---------------------------------------------------------------------------
# 證據包
# ---------------------------------------------------------------------------


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def question_view(unit) -> dict:
    """送給 Gemini 的題目內容：沿用 build_payload，但移除各處的答案欄位（題庫答案另列於 bank_answer）。"""
    p = copy.deepcopy(eq.build_payload(unit))
    p.pop("answer", None)
    for key in ("blanks", "statements"):
        for x in p.get(key) or []:
            if isinstance(x, dict):
                x.pop("answer", None)
    return p


def bank_origin(unit) -> str:
    note = unit.question.get("reviewNote") or ""
    return "pdf_author_corrected" if note.startswith(va.CORRECTION_PREFIX) else "original_or_unknown"


# 社群資料狀態（F-09）：「快照缺少本題項目」不等於「沒有社群資料」；快照結構不完整或疑似過時
# 由 dry-run 預檢拒絕（結構）或 snapshot_trust 標示為 unverified（與正式基準不一致）。
COMMUNITY_DATA_STATES = {
    "available": "已確認同一題，且有社群投票答案",
    "matched_without_votes": "已確認同一題，但沒有記錄投票答案",
    "explicit_none": "已記錄搜尋結果：ExamTopics 找不到相同題目（明確沒有社群資料）",
    "unconfirmed_match": "有疑似相同的題目，尚未確認為同一題",
    "not_searched": "尚未搜尋 ExamTopics（社群資料狀態未知）",
    "entry_missing": "快照缺少本題的驗證項目（社群資料狀態未知，不等於沒有社群資料）",
}
ENTRY_MISSING_NOTE = "verification.json 快照中沒有本題的驗證項目：社群資料狀態未知，不等於沒有社群資料"


def examtopics_section(e: dict | None) -> dict:
    if e is None:
        return {"status": "entry_missing", "community_data_state": "entry_missing", "note": ENTRY_MISSING_NOTE}
    et = e["examtopics_evidence"]
    ms = et["match_status"]
    vote = et["community_vote"]
    sec = {"match_status": ms, "url": et.get("url"), "captured_at": et.get("captured_at"),
           "source_note": "由 verify_answers.py 匯入或人工記錄；本模組未連線 ExamTopics"}
    sec["community_data_state"] = {"exact": "available" if vote.get("answer") is not None else "matched_without_votes",
                                   "not_found": "explicit_none", "probable": "unconfirmed_match",
                                   "not_searched": "not_searched"}.get(ms, "unknown")
    if ms == "exact":
        th = va.community_threshold(e)
        sec["status"] = "available" if vote.get("answer") is not None else "matched_without_votes"
        sec["page_answer"] = et.get("page_answer")
        sec["community_vote"] = {k: vote.get(k) for k in ("answer", "share_percent", "vote_count", "as_of", "distribution")}
        sec["consensus_threshold"] = {"met": th["met"], "reasons": th["reasons"],
                                      "rule": f"同一題、總票數 ≥ {va.CONSENSUS_MIN_VOTES}、最高票比例 ≥ {va.CONSENSUS_MIN_SHARE}%"}
        if vote.get("answer") is None:
            sec["note"] = va.NO_COMMUNITY
    else:
        sec["status"] = {"not_found": "not_found", "probable": "match_unconfirmed",
                         "not_searched": "not_searched"}.get(ms, ms)
        sec["note"] = {"not_found": "ExamTopics 找不到相同題目",
                       "probable": "尚未確認為同一題，社群答案不列入",
                       "not_searched": "尚未搜尋 ExamTopics"}.get(ms, va.NO_COMMUNITY)
    reasons, counter = et.get("main_reasons") or [], et.get("counterarguments") or []
    sec["discussion_summary"] = ({"supporting": reasons, "counter": counter, "source": "使用者人工摘要（非討論原文）"}
                                 if reasons or counter else {"status": "未取得"})
    return sec


def human_evidence(e: dict | None) -> list[dict]:
    if not e:
        return []
    proposed = (e.get("proposed_answer") or {}).get("value")
    meaning = {"bank": "支持題庫答案", "neither": "與答案無直接關係",
               "proposed": f"支持題庫以外的答案（候選答案：{json.dumps(proposed, ensure_ascii=False) if proposed is not None else '未記錄'}）"}
    return [{"title": x.get("title"), "url": x.get("url"), "excerpt": x.get("excerpt"),
             "supports": x.get("supports"), "supports_meaning": meaning.get(x.get("supports"), "未知"),
             "verification": x.get("verification"), "retrieved_at": x.get("retrieved_at")}
            for x in e.get("microsoft_evidence") or []]


def norm(s: str) -> str:
    s = (s or "").translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', " ": " "}))
    return " ".join(s.split()).casefold().strip(" .…\"'")


def unit_texts(unit) -> tuple[str, list[tuple[str, str]]]:
    """(題幹 + 程式碼 / 表格, [(對象 ID, 文字)])；對象為選項、排序項目、挖空選項或陳述句。"""
    q = unit.question
    stem = " ".join([q.get("question", ""), q.get("code", "") or ""])
    targets = [(o["id"], o["text"]) for o in q.get("options", [])] + [(i["id"], i["text"]) for i in q.get("items", [])]
    targets += [(f"{b['id']}:{x}", x) for b in q.get("blanks", []) for x in b["options"]]
    targets += [(s["id"], s["text"]) for s in q.get("statements", [])]
    for line in (q.get("code") or "").splitlines():  # 表格中「Tactic1  說明」這類佔位詞對照列：只用於檢索與段落比對
        m = re.match(r"^\s*((?:[A-Z][a-z]+|[A-Z]{1,3})\d+)\s{2,}(\S.*)$", line)
        if m:
            targets.append((m.group(1), m.group(2).strip()))
    return stem, targets


def detect_products(text: str, rules: list | None = None) -> list[dict]:
    """依出現次數（其次為首次出現位置）排序；第一個視為主要產品。rules 預設為 PRODUCT_RULES（判讀端）；
    官方文件檢索傳入 SEARCH_PRODUCT_RULES。每個結果記錄所用的比對式（pattern），供 strip_products() 使用。"""
    found = []
    for name, pat, aliases, paths in (rules or PRODUCT_RULES):
        hits = list(re.finditer(pat, text))
        if hits:
            found.append({"name": name, "aliases": aliases, "paths": paths, "count": len(hits), "pos": hits[0].start(),
                          "pattern": pat})
    found.sort(key=lambda p: (-p["count"], p["pos"]))
    return found


def search_products(stem: str, targets: list[tuple[str, str]]) -> list[dict]:
    """官方文件檢索用的產品（learn-query-v2）：以 SEARCH_PRODUCT_RULES 辨識。唯一的排序例外：Purview／AIP 只出現在
    選項、而題幹已明確提到其他產品時，題幹中的產品維持在前、Purview 改列其後（仍保留其允許路徑）；
    題幹本身提到 Purview／AIP 時不調整。其他產品的排序規則不變。"""
    products = detect_products(stem + " " + " ".join(t for _, t in targets), SEARCH_PRODUCT_RULES)
    if products and products[0]["name"] == "Microsoft Purview" and not re.search(PURVIEW_SEARCH_PATTERN, stem):
        in_stem = {p["name"] for p in detect_products(stem, SEARCH_PRODUCT_RULES)}
        if in_stem:
            products = [p for p in products if p["name"] in in_stem] + [p for p in products if p["name"] not in in_stem]
    return products


def tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z_][A-Za-z0-9_\-]*(?:&[A-Za-z]+)?", text)


def is_placeholder(word: str) -> bool:
    return bool(PLACEHOLDER.match(word))


def tech_terms(text: str) -> list[str]:
    """具技術特徵的詞：CamelCase、含數字或底線、全大寫縮寫；排除 Tactic1 這類佔位詞與被 & 切開的片段。"""
    out = []
    for w in tokens(text):
        if "&" in w or is_placeholder(w) or w.upper() in WEAK_ACRONYMS or len(w) < 3:
            continue
        if re.search(r"[a-z][A-Z]|\d|_", w) or re.fullmatch(r"[A-Z]{3,}", w):
            if w not in out:
                out.append(w)
    return out


def camel_words(term: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", term)


def strip_products(text: str, products: list[dict]) -> str:
    for p in products:
        text = re.sub(p.get("pattern") or PRODUCT_RULES[[r[0] for r in PRODUCT_RULES].index(p["name"])][1], " ", text)
    return re.sub(r"\bMicrosoft\b", " ", text)


def keywords(text: str, limit: int) -> list[str]:
    out = []
    for w in tokens(text):
        lw = w.lower()
        if ("&" in w or is_placeholder(w) or len(w) < 3 or lw in va.STOPWORDS or lw in QUESTION_WORDS
                or w in out):
            continue
        out.append(w)
    return out[:limit]


def target_phrase(text: str, products: list[dict]) -> str:
    """選項的檢索詞：取「A > B」的最後一段，去掉佔位詞；含技術詞時只用技術詞。"""
    t = text.split(">")[-1].strip().rstrip(".")
    t = " ".join(w for w in t.split() if not is_placeholder(w.strip(",.")))
    tech = tech_terms(t)
    if tech:
        return " ".join(tech[:2])
    words = keywords(strip_products(t, products), 4)
    return " ".join(words) if words else ""


def target_matcher(text: str, products: list[dict]) -> dict:
    """段落比對用：原始選項詞組（去佔位詞）、關鍵詞與技術詞。檢索詞會刪掉停用詞，不能直接拿來比對原文。"""
    t = text.split(">")[-1].strip().rstrip(".")
    t = " ".join(w for w in t.split() if not is_placeholder(w.strip(",.")))
    return {"phrase": t if keywords(t, 1) else "", "keywords": keywords(strip_products(t, products), 4),
            "tech": tech_terms(t)}


# 題組子題：題幹指向題組情境（需求、問題、規劃、哪個團隊……）時，才從情境中挑出相關句子補充 Learn 查詢與段落評分。
# 只影響官方文件檢索；送給 Gemini 的題目內容（question_view，已含 case_study）不變。
CASE_REFERENCE = re.compile(r"\b(?:requirements?|issues?|problems?|planned|technical|business|which team|for which)\b", re.I)
MAX_CASE_SENTENCES = 3
CASE_NOISE = set("""team teams member members user users company department departments past several currently plan plans
planned requirement requirements issue issues problem problems resolve resolved recently addition also only all must has
have had been being""".split())
CASE_ISSUE_STEM = re.compile(r"\b(?:issues?|problems?)\b", re.I)
CASE_ISSUE_TERMS = re.compile(r"\b(?:problems?|issues?|incidents?|attacks?|phishing|malware|breach\w*|threats?|compromis\w*|suspicious)\b",
                              re.I)


def case_sentences(unit) -> list[str]:
    """題組情境（caseInfo.sections[].content）切成句子；粗體小標與換行視為分隔，太短的片段略過。"""
    out = []
    for sec in ((unit.case or {}).get("caseInfo") or {}).get("sections") or []:
        content = re.sub(r"\*\*[^*\n]+\*\*", "\n", str(sec.get("content") or "") if isinstance(sec, dict) else "")
        for line in content.splitlines():
            for s in re.split(r"(?<=[.!?])\s+", line.strip()):
                s = " ".join(s.lstrip("-•* ").split())
                if len(s.split()) >= 4:
                    out.append(s)
    return out


def _case_excluded_words(unit, targets: list[tuple[str, str]]) -> set[str]:
    """不可當成查詢詞的字：選項標籤（例如團隊名稱）與題組標題中的公司名稱。"""
    title = ((unit.case or {}).get("caseInfo") or {}).get("title") or ""
    words = {w.lower() for _, t in targets for w in tokens(t or "")} | {w.lower() for w in tokens(title)}
    return words | CASE_NOISE


def case_context_sentences(unit, products: list[dict], targets: list[tuple[str, str]]) -> list[str]:
    """最多 MAX_CASE_SENTENCES 句與本題相關的題組情境；非題組子題或題幹未指向情境時回傳空清單。
    相關性：提到選項文字（每個 2 分）、提到主要產品（2 分）、題幹關鍵詞（每個 1 分）、含技術詞（1 分）；
    題幹問的是 issue／problem 時，描述問題的句子（攻擊、釣魚、惡意程式、外洩……）另加 2 分。
    先挑能涵蓋新選項的句子（避免全部集中在同一選項），再依分數補滿；保留情境中的原始順序。"""
    q = unit.question.get("question", "")
    if unit.case is None or not CASE_REFERENCE.search(q):
        return []
    excluded = _case_excluded_words(unit, targets)
    stem_kw = [w for w in keywords(strip_products(q, products), 12) if w.lower() not in CASE_NOISE]
    primary = products[0] if products else None
    prim_pat = primary.get("pattern") if primary else None
    labels = [(tid, t) for tid, t in targets if t and len(t.strip()) >= 3]
    cands = []
    for i, s in enumerate(case_sentences(unit)):
        opts = {tid for tid, t in labels if has_phrase(s, t)}
        prod = bool(prim_pat and re.search(prim_pat, s))
        kw = sum(1 for w in stem_kw if has_phrase(s, w))
        tech = bool([t for t in tech_terms(strip_products(s, products)) if t.lower() not in excluded])
        issue = bool(CASE_ISSUE_STEM.search(q) and CASE_ISSUE_TERMS.search(s))
        score = 2 * len(opts) + (2 if prod else 0) + kw + (1 if tech else 0) + (2 if issue else 0)
        if opts or prod or kw >= 2:
            cands.append((score, i, s, opts))
    cands.sort(key=lambda c: (-c[0], c[1]))
    picked, covered = [], set()
    for c in cands:  # 先涵蓋不同選項
        if len(picked) < MAX_CASE_SENTENCES and c[3] - covered:
            picked.append(c)
            covered |= c[3]
    for c in cands:  # 再依分數補滿
        if len(picked) < MAX_CASE_SENTENCES and c not in picked:
            picked.append(c)
    return [c[2] for c in sorted(picked, key=lambda c: c[1])]


def case_sentence_terms(unit, sentence: str, products: list[dict], targets: list[tuple[str, str]]) -> list[str]:
    """情境句子的檢索詞：技術詞優先，再接一般關鍵詞；排除產品名稱、選項標籤、公司名稱與情境常用字。"""
    excluded = _case_excluded_words(unit, targets)
    s = strip_products(sentence, products)
    tech = [t for t in tech_terms(s) if t.lower() not in excluded]
    words = [w for w in keywords(s, 12) if w.lower() not in excluded and w not in tech]
    return list(dict.fromkeys(tech + words))


def build_queries(unit) -> tuple[list[str], list[dict], dict[str, str]]:
    """多組短查詢：主要產品 + 1～2 個關鍵詞。回傳 (查詢, 產品, {對象 ID: 檢索詞})。
    題組子題若題幹指向情境，先加入最多 MAX_CASE_SENTENCES 組情境查詢，再加入原有查詢（總數仍受 MAX_QUERIES 限制）。
    產品以 search_products()（檢索專用規則）辨識。"""
    stem, targets = unit_texts(unit)
    products = search_products(stem, targets)
    primary = products[0]["name"] if products else ""
    qs: list[str] = []

    def add(*parts):
        words = " ".join(p for p in parts if p).split()
        q = " ".join(words[:QUERY_MAX_WORDS])
        if q and q not in qs and len(qs) < MAX_QUERIES and q != primary:
            qs.append(q)

    for s in case_context_sentences(unit, products, targets):
        add(primary, *case_sentence_terms(unit, s, products, targets))
    stem_tech = tech_terms(strip_products(stem, products))
    for p in products[:2]:  # 題幹技術詞分別搭配前兩個產品（例如 MicrosoftGraphActivityLogs 屬 Microsoft Graph）
        if stem_tech:
            add(p["name"], *stem_tech[:2])
    question_sentence = ([s for s in re.split(r"(?<=[.?!])\s+", " ".join(unit.question.get("question", "").split()))
                          if s.lower().startswith(("which", "what", "how", "you need"))] or [""])[-1]
    add(primary, *keywords(strip_products(question_sentence, products), 3))
    if len(products) > 1:
        add(primary, products[1]["name"])
    phrases = {}
    for tid, text in targets:
        ph = target_phrase(text, products)
        if ph:
            phrases[tid] = ph
            add(primary, ph)
    return qs, products, phrases


def allowed_path(url: str, products: list[dict], terms: list[str]) -> tuple[bool, str]:
    path = re.sub(r"^https://learn\.microsoft\.com(/[a-z]{2}-[a-z]{2})?", "", url).lower()
    if any(x in path for x in ALWAYS_EXCLUDED_PATHS) or url.endswith("/"):
        return False, "excluded_path"
    if not products:
        return True, "no_product_filter"
    if any(path.startswith(p) for pr in products for p in pr["paths"]):
        return True, "product_path"
    if any(len(t) >= 6 and t.lower() in path for t in terms):
        return True, "term_in_path"
    return False, "other_product_path"


def valid_learn_url(url: str) -> bool:
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (u.scheme == "https" and u.hostname in LEARN_HOSTS and u.port is None and not u.username
            and len(u.path) > 1 and not re.search(r"\s", url))


def clean_url(url: str) -> str:
    return url.split("#")[0].split("?")[0]


def learn_title(raw: str) -> str:
    """Learn 頁面的 H1 位於最後一個授權提示之後、「Feedback Summarize this article for me」之前。"""
    i = raw.find("Summarize this article for me")
    if i == -1:
        return ""
    head = re.sub(r"\s*Feedback\s*$", "", raw[:i].rstrip())
    title = head.rsplit("changing directories .", 1)[-1]
    title = LEARN_NOISE.sub(" ", title)
    title = " ".join(title.split())
    return title if 3 <= len(title) <= 200 else ""


def clean_learn_text(raw: str) -> str:
    """去除 Learn 頁面的導覽、授權提示、回饋區與相關連結，只留正文。"""
    t = raw
    i = t.find("Summarize this article for me")
    if i != -1:
        t = t[i + len("Summarize this article for me"):]
    t = re.sub(r"(?:Note )?Access to this page requires authorization\. You can try (?:signing in or )?changing directories \.",
               " ", t)
    for marker in ("Feedback Was this page helpful?", "Was this page helpful?", "Additional resources Last updated"):
        j = t.find(marker)
        if j != -1:
            t = t[:j]
    j = t.rfind("Related content")
    if j != -1 and j > len(t) * 0.7:
        t = t[:j]
    t = LEARN_NOISE.sub(" ", t)
    return " ".join(t.split())


def split_passages(text: str) -> list[str]:
    sents = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    out = []
    for i in range(0, len(sents), SENTENCES_PER_PASSAGE):
        out.append(" ".join(sents[i:i + SENTENCES_PER_PASSAGE])[:MAX_PASSAGE_CHARS])
    return out


def has_phrase(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])", text, re.I))


def target_hit(text: str, m: dict) -> bool:
    """含技術詞的選項命中任一技術詞即可；否則需原始詞組出現，或兩個以上關鍵詞全部出現。"""
    if m["tech"]:
        return any(has_phrase(text, w) for w in m["tech"])
    if m["phrase"] and has_phrase(text, m["phrase"]):
        return True
    return len(m["keywords"]) >= 2 and all(has_phrase(text, k) for k in m["keywords"])


def strong_target(m: dict) -> bool:
    """命中技術詞、完整的多字詞組，或兩個以上關鍵詞同時出現，才算強訊號；單一常見字（如 Hunting）不算。"""
    return bool(m["tech"]) or len(m["phrase"].split()) >= 2 or len(m["keywords"]) >= 2


def score_passage(text: str, matchers: dict[str, dict], tech: list[str], products: list[dict],
                  stem_words: list[str]) -> dict:
    """多字選項詞 3 分、單字選項詞 1 分、技術詞 2 分、產品別名 2 分、題幹關鍵詞每個 1 分（上限 3）。
    合格條件：分數 ≥ 4，且命中多字選項詞、技術詞，或至少兩個不同選項；只有產品名稱不算相關。"""
    hit_targets = sorted(tid for tid, m in matchers.items() if target_hit(text, m))
    strong = [t for t in hit_targets if strong_target(matchers[t])]
    hit_tech = [t for t in tech if has_phrase(text, t) or (camel_words(t) != t and has_phrase(text, camel_words(t)))]
    prod = any(has_phrase(text, a) for p in products for a in p["aliases"])
    stem_hits = [w for w in stem_words if has_phrase(text, w)]
    score = (3 * len(strong) + (len(hit_targets) - len(strong)) + 2 * len(hit_tech) + (2 if prod else 0)
             + min(len(stem_hits), 3))
    eligible = score >= 4 and bool(strong or hit_tech or len(hit_targets) >= 2)
    terms = {matchers[t]["phrase"] or " ".join(matchers[t]["keywords"]) for t in hit_targets}
    related = bool(prod or hit_targets or hit_tech)  # 產品變更提示需與題目的產品或選項有關
    return {"score": score, "eligible": eligible, "related": related, "targets": hit_targets,
            "matched_terms": sorted(terms | set(hit_tech) | set(stem_hits[:3]))}


def product_change_notice(text: str) -> bool:
    """段落是否為產品變更提示（功能轉換、淘汰、退役等）。只有日期或只有 Important 都不算。"""
    return bool(LIFECYCLE_TERMS.search(text)) and bool(NOTICE_MARK.search(text) or DATE_TERMS.search(text))


def pick_covering(cands: list[dict], limit: int, covered: set[str]) -> list[dict]:
    """依序挑選「能涵蓋最多尚未出現的選項」的段落，同分再比分數；避免高分段落重複同一選項而擠掉其他選項的定義。"""
    pool, out, covered = list(cands), [], set(covered)
    while pool and len(out) < limit:
        best = max(pool, key=lambda p: (len(set(p["targets"]) - covered), p["score"]))
        out.append(best)
        covered |= set(best["targets"])
        pool.remove(best)
    return out


def gather_learn(unit, e: dict | None, search=None, fetch=None, delay: float = 1.0,
                 doc_urls: list[str] | None = None) -> dict:
    search = search or va.learn_search
    fetch = fetch or fetch_learn_page
    queries, products, phrases = build_queries(unit)
    stem, targets = unit_texts(unit)
    matchers = {tid: target_matcher(text, products) for tid, text in targets if tid in phrases}
    tech = list(dict.fromkeys(tech_terms(strip_products(stem, products))
                              + [t for m in matchers.values() for t in m["tech"]]))
    stem_words = keywords(strip_products(unit.question.get("question", ""), products), 12)
    # 題組情境（v2）：相關句子的關鍵詞與技術詞只加入段落評分的既有輸入，評分規則不變；路徑篩選仍只用原本的 tech
    case_sents = case_context_sentences(unit, products, targets)
    case_terms = list(dict.fromkeys(w for s in case_sents for w in case_sentence_terms(unit, s, products, targets)))
    stem_words += [w for w in case_terms if w not in stem_words]
    score_tech = list(dict.fromkeys(tech + [w for w in case_terms if tech_terms(w)]))
    errors: list[str] = []
    considered: dict[str, dict] = {}  # url → 候選紀錄（去重）

    def consider(url, title, origin, query=None, rank=None):
        raw_url = url or ""
        url = clean_url(raw_url)
        if not url:
            return
        c = considered.get(url)
        if c is None:
            c = considered[url] = {"url": url, "raw_url": raw_url, "title": title or "", "origin": origin, "queries": [],
                                   "best_rank": rank if rank is not None else 99, "decision": None}
        if query and query not in c["queries"]:
            c["queries"].append(query)
        if rank is not None:
            c["best_rank"] = min(c["best_rank"], rank)

    for x in (e or {}).get("microsoft_evidence") or []:
        if x.get("verification") == "verified":
            consider(x.get("url"), x.get("title"), "human_verified_evidence")
    for u in doc_urls or []:
        consider(u, "", "user_provided")
    for qtext in queries:
        try:
            for rank, r in enumerate(search(qtext, top=SEARCH_TOP)[:SEARCH_TOP]):
                if str(r.get("url", "")).startswith("https://learn.microsoft.com/"):
                    consider(r["url"], r.get("title"), "learn_search", qtext, rank)
        except (urllib.error.URLError, OSError, ValueError) as err:
            errors.append(f"搜尋失敗（{qtext}）：{type(err).__name__}")
        time.sleep(delay)
    for c in (e or {}).get("microsoft_candidates") or []:
        consider(c.get("url"), c.get("title"), "ms_search_candidate")

    fixed = [c for c in considered.values() if c["origin"] in ("human_verified_evidence", "user_provided")]
    pool = []
    for c in considered.values():
        if c in fixed:
            c["decision"] = "always_fetch"
            continue
        ok, why = allowed_path(c["url"], products, tech)
        if not ok:
            c["decision"] = f"rejected:{why}"
            continue
        where = c["title"] + " " + re.sub(r"[-/]", " ", c["url"])
        title_hits = sum(has_phrase(where, t)
                         for t in [m["phrase"] for m in matchers.values() if m["phrase"]] + tech
                         + [a for p in products for a in p["aliases"]])
        # 技術詞拆字後出現在標題或網址（MicrosoftGraphActivityLogs → microsoft graph activity logs）權重較高
        title_hits += 3 * sum(has_phrase(where, camel_words(t)) for t in tech if camel_words(t) != t)
        c["title_score"] = title_hits
        pool.append(c)
    pool.sort(key=lambda c: (-(2 * c["title_score"] + len(c["queries"])), c["best_rank"]))
    for c in pool[MAX_FETCH:]:
        c["decision"] = "not_fetched:limit"

    pins = [norm(x.get("excerpt") or "")[:60] for x in (e or {}).get("microsoft_evidence") or []
            if x.get("excerpt") and x.get("verification") == "verified"]
    fetched, failed, fetch_log = [], [], []
    for c in fixed + pool[:MAX_FETCH]:
        try:
            got = fetch(c["url"])
            raw = got["text"] if isinstance(got, dict) else got
            if not isinstance(raw, str):
                raise ValueError("擷取結果不是文字")
        except (urllib.error.URLError, OSError, ValueError) as err:
            c["decision"] = f"fetch_failed:{type(err).__name__}"
            failed.append({"url": c["url"], "title": c["title"], "origin": c["origin"], "error": type(err).__name__})
            errors.append(f"文件擷取失敗（{c['origin']}，{c['url']}）：{type(err).__name__}")
            fetch_log.append(fetch_record(c, getattr(err, "fetch_meta", None), err=err))
            continue
        finally:
            time.sleep(delay)
        c["title"] = c["title"] or learn_title(raw) or "（未取得標題）"
        text = clean_learn_text(raw)
        fetch_log.append(fetch_record(c, got if isinstance(got, dict) else None, raw=raw, text=text))
        cand = []
        for k, ptext in enumerate(split_passages(text), start=1):
            s = score_passage(ptext, matchers, score_tech, products, stem_words)
            pinned = any(p and p in norm(ptext) for p in pins)
            notice = s["related"] and product_change_notice(ptext)
            if pinned or notice or s["eligible"]:
                reasons = (["human_verified_excerpt"] if pinned else []) + (["product_change_notice"] if notice else []) \
                          + (["relevance"] if s["eligible"] else [])
                cand.append({"passage_id": None, "index": k, "text": ptext, "score": s["score"],
                             "matched_terms": s["matched_terms"], "targets": s["targets"],
                             "pinned_human_excerpt": pinned, "selection_reasons": reasons})
        pinned = [p for p in cand if p["pinned_human_excerpt"]]
        notices = sorted((p for p in cand if not p["pinned_human_excerpt"]
                          and "product_change_notice" in p["selection_reasons"]),
                         key=lambda p: -p["score"])[:MAX_NOTICES]
        first = pinned + notices
        keep = first + pick_covering([p for p in cand if p not in first and "relevance" in p["selection_reasons"]],
                                     PER_DOC_PASSAGES, {t for p in first for t in p["targets"]})
        best = max((p["score"] for p in keep), default=0)
        fetched.append({"c": c, "keep": keep, "best": best})

    fixed_docs = [f for f in fetched if f["c"]["origin"] in ("human_verified_evidence", "user_provided")]
    search_docs = sorted((f for f in fetched if f not in fixed_docs and f["keep"]), key=lambda f: -f["best"])
    for f in fetched:
        if f not in fixed_docs and not f["keep"]:
            f["c"]["decision"] = "no_relevant_passages"
    for f in search_docs[MAX_DOCS:]:
        f["c"]["decision"] = "not_kept:limit"
    chosen = fixed_docs + search_docs[:MAX_DOCS]
    all_kept = [p for f in chosen for p in f["keep"]]
    pinned_all = [p for p in all_kept if p["pinned_human_excerpt"]]
    notices_all = sorted((p for p in all_kept if not p["pinned_human_excerpt"]
                          and "product_change_notice" in p["selection_reasons"]), key=lambda p: -p["score"])[:MAX_NOTICES]
    first = pinned_all + notices_all
    rest = pick_covering([p for p in all_kept if p not in first and "relevance" in p["selection_reasons"]],
                         max(MAX_PASSAGES - len(first), 0), {t for p in first for t in p["targets"]})
    picked = {id(p) for p in first + rest}

    docs, no_hit = [], []
    for n, f in enumerate(chosen, start=1):
        c = f["c"]
        passages = sorted((p for p in f["keep"] if id(p) in picked), key=lambda p: p["index"])
        if not passages:
            c["decision"] = c["decision"] if c["decision"] == "not_kept:limit" else "no_relevant_passages"
            no_hit.append({"title": c["title"], "url": c["url"], "origin": c["origin"]})
            continue
        c["decision"] = "kept"
        doc_id = f"D{len(docs) + 1}"
        for p in passages:
            p["passage_id"] = f"{doc_id}-P{p.pop('index')}"
        docs.append({"doc_id": doc_id, "title": c["title"], "url": c["url"], "origin": c["origin"],
                     "retrieved_at": va.now_iso(), "fetch_status": "ok", "passages": passages})
    coverage = {tid: any(tid in p["targets"] for d in docs for p in d["passages"]) for tid in phrases}
    status = "found" if docs else ("unavailable" if errors and not fetched else "not_found")
    return {"status": status, "note": None if docs else "尚未找到官方依據", "queries": queries,
            "products": [p["name"] for p in products], "target_terms": phrases,
            "option_coverage": coverage, "documents": docs, "documents_without_relevant_passages": no_hit,
            "documents_failed": failed,
            "candidates_considered": [{k: c[k] for k in ("url", "title", "origin", "decision")}
                                      for c in considered.values()],
            "skipped_beyond_limit": [c["url"] for c in pool[MAX_FETCH:]], "errors": errors,
            "fetch_log": fetch_log}


# ---------------------------------------------------------------------------
# 文件擷取紀錄（F-10）
# ---------------------------------------------------------------------------

NO_FETCH_META = "擷取函式未提供 HTTP 中繼資料（最終網址、狀態碼、原始位元組），無法判斷內容是否完整"


class FetchRejected(ValueError):
    """擷取結果不可採用（例如被轉址到 learn.microsoft.com 以外）；保留中繼資料供擷取紀錄使用。"""

    def __init__(self, msg: str, meta: dict):
        super().__init__(msg)
        self.fetch_meta = meta


def page_text(page: str) -> str:
    """HTML 轉文字：與 verify_answers.fetch_doc_text() 相同的步驟（document_cleaning_version 記錄此規則）。"""
    main = re.search(r"(?s)<main.*?</main>", page)
    page = main.group(0) if main else page
    page = re.sub(r"(?s)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", page)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def fetch_learn_page(url: str) -> dict:
    """擷取 Learn 文件：請求內容與 verify_answers.fetch_doc_text() 相同（相同 User-Agent、逾時、不重試），
    另外回傳最終網址、HTTP 狀態、原始位元組 SHA256 與完整性判定依據。轉址到其他網站時視為擷取失敗。"""
    req = urllib.request.Request(url, headers={"User-Agent": va.USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        status = getattr(resp, "status", None)
        final = resp.geturl()
        declared = resp.headers.get("Content-Length")
        ctype = resp.headers.get("Content-Type")
    page = body.decode("utf-8", errors="replace")
    text = page_text(page)
    basis, complete = [], True
    if status != 200:
        complete = False
        basis.append(f"HTTP 狀態 {status}")
    if declared is None:
        basis.append("回應未提供 Content-Length，無法以長度確認是否完整")
        complete = None if complete else complete
    elif not declared.strip().isdigit() or int(declared) != len(body):
        complete = False
        basis.append(f"Content-Length {declared} 與實際位元組數 {len(body)} 不符")
    else:
        basis.append("Content-Length 與實際位元組數一致")
    if re.search(r"(?s)<main.*?</main>", page):
        basis.append("找到完整的 <main> 元素")
    else:
        complete = False
        basis.append("找不到完整的 <main> 元素（頁面可能不完整或不是文件頁）")
    if not text:
        complete = False
        basis.append("轉換後沒有文字")
    meta = {"text": text, "final_url": final, "http_status": status, "content_type": ctype,
            "raw_sha256": hashlib.sha256(body).hexdigest(), "raw_length": len(body),
            "complete": complete, "complete_basis": basis, "fetcher": "gemini_judge.fetch_learn_page"}
    if not valid_learn_url(clean_url(final or "")):
        raise FetchRejected(f"最終網址不是 learn.microsoft.com 文件：{final}", dict(meta, text=None))
    return meta


def fetch_record(c: dict, meta: dict | None, raw: str | None = None, text: str | None = None, err=None) -> dict:
    """單一文件的擷取紀錄：原始 / 正規化 / 最終網址、HTTP 或失敗狀態、原始內容與清理後文字的 SHA256、
    UTC 擷取時間、內容長度、是否完整及判定依據。沒有中繼資料的欄位記為 None，不推測。"""
    meta = meta or {}
    rec = {"requested_url": c.get("raw_url") or c["url"], "normalized_url": c["url"], "final_url": meta.get("final_url"),
           "origin": c["origin"], "status": "failed" if err is not None else "ok",
           "http_status": meta.get("http_status", getattr(err, "code", None)),
           "error": f"{type(err).__name__}: {str(err)[:160]}" if err is not None else None,
           "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "raw_sha256": meta.get("raw_sha256"), "raw_length": meta.get("raw_length"),
           "fetched_text_sha256": sha256_text(raw) if raw is not None else None,
           "cleaned_text_sha256": sha256_text(text) if text is not None else None,
           "cleaned_text_length": len(text) if text is not None else None,
           "fetcher": meta.get("fetcher") or ("injected_or_legacy" if err is None else None)}
    if err is not None:
        rec["complete"], rec["complete_basis"] = False, ["擷取失敗"] + list(meta.get("complete_basis") or [])
    elif meta:
        rec["complete"], rec["complete_basis"] = meta.get("complete"), list(meta.get("complete_basis") or [])
    else:
        rec["complete"], rec["complete_basis"] = None, [NO_FETCH_META]
    return rec


def build_packet(ctx, qid: str, search=None, fetch=None, delay: float = 1.0, doc_urls: list[str] | None = None,
                 provenance: dict | None = None) -> dict:
    """provenance：本批次 verification.json 快照的 SHA256、schema 版本與信任判定（snapshot_provenance()）。
    未提供時記為未驗證（社群答案不會取得正式共識資格）。"""
    unit = ctx.units[qid]
    e = ctx.data["questions"].get(qid)
    prov = provenance or {"snapshot_sha256": None, "snapshot_schema_version": None,
                          "snapshot_trust": {"rules_version": SNAPSHOT_TRUST_VERSION, "level": "unverified",
                                             "format_valid": None, "source_consistent": None, "content_trusted": None,
                                             "reasons": ["未提供快照來源資訊"]}}
    return {
        **PACKET_VERSIONS,
        "created_at": va.now_iso(),
        "question_id": qid,
        "type": unit.type,
        "bank_sha256": ctx.bank_sha,
        "snapshot_sha256": prov["snapshot_sha256"],
        "snapshot_schema_version": prov["snapshot_schema_version"],
        "snapshot_trust": prov["snapshot_trust"],
        "question": question_view(unit),
        "answer_format": ANSWER_FORMATS[unit.type],
        "bank_answer": {"value": va.answer_of(unit), "origin": bank_origin(unit)},
        "verification_status": {
            "answer_status": e["answer_status"] if e else "no_verification_entry",
            "microsoft_consistency": va.microsoft_consistency(e) if e else "no_verified_evidence",
            "proposed_answer": (e.get("proposed_answer") or {}).get("value") if e else None,
        },
        "examtopics": examtopics_section(e),
        "human_verified_microsoft_evidence": [x for x in human_evidence(e) if x["verification"] == "verified"],
        "microsoft_learn": gather_learn(unit, e, search, fetch, delay, doc_urls),
    }


def model_view(packet: dict) -> dict:
    """實際送給模型的內容：去掉檔案管理欄位與段落評分，只留判讀需要的資料。"""
    v = copy.deepcopy({k: packet[k] for k in ("question_id", "type", "question", "answer_format", "bank_answer",
                                               "verification_status", "examtopics",
                                               "human_verified_microsoft_evidence")})
    ml = packet["microsoft_learn"]
    v["microsoft_learn"] = {
        "status": ml["status"], "note": ml["note"],
        "documents": [{"doc_id": d["doc_id"], "title": d["title"], "url": d["url"], "origin": d["origin"],
                       "passages": [dict({"passage_id": p["passage_id"], "text": p["text"]},
                                         **({"notice": "product_change_notice"}
                                            if "product_change_notice" in (p.get("selection_reasons") or []) else {}))
                                    for p in d["passages"]]}
                      for d in ml["documents"]],
    }
    # 送出前調整社群資訊（只改送出的副本，不修改證據包，雜湊驗證不受影響）：
    # 原始 consensus_threshold 依頁面顯示的整數支持率計算，改名並註明僅供參考；正式資格以 formal_consensus 為準
    ex_v = v["examtopics"]
    if "consensus_threshold" in ex_v:
        ex_v["display_based_threshold"] = dict(
            ex_v.pop("consensus_threshold"),
            note="依頁面顯示的整數支持率計算（verify_answers），僅供參考，不是正式共識資格")
    if isinstance(ex_v.get("community_vote"), dict):
        ex_v["community_vote"]["display_value_note"] = (
            "share_percent 與 distribution[].percent 是頁面顯示的整數支持率，不作為正式門檻判斷；"
            "正式資格以 formal_consensus 為準，有原始票數分布時正式支持率為 formal_consensus.share_exact")
    ex_v["formal_consensus"] = formal_consensus(packet)
    v["official_docs_role"] = official_docs_role(packet)
    return v


def render_prompt(packet: dict) -> str:
    return (f"以下是題目 {packet['question_id']} 的證據包（JSON）。請依系統指示判讀並輸出 JSON。\n\n"
            + json.dumps(model_view(packet), ensure_ascii=False, indent=1))


def rough_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + (len(text) - cjk) // 4


def estimate_input_tokens(packet: dict) -> int:
    return rough_tokens(SYSTEM_INSTRUCTION + render_prompt(packet) + json.dumps(RESPONSE_SCHEMA))


def save_packet(packet_dir: Path, packet: dict) -> tuple[Path, str]:
    """原子且不覆寫地建立證據包：先寫入同目錄的唯一暫存檔（.packet-*.part，latest_packet() 不會選到），
    fsync 後以 os.link 建立最終檔名（目標已存在即失敗，不覆寫）。無法以不覆寫方式建立時 fail-closed，
    不退回直接寫入；任何失敗都不留下最終檔名的檔案，暫存檔會移除。"""
    sha = sha256_text(canonical(packet))
    packet_dir.mkdir(parents=True, exist_ok=True)
    final = packet_dir / f"{packet['question_id']}.{stamp()}.json"
    data = (json.dumps({"packet_sha256": sha, "packet": packet}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=packet_dir, prefix=".packet-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, final)
        except FileExistsError as err:
            raise FileExistsError(f"證據包檔名已存在，未覆寫：{final}") from err
        except (AttributeError, NotImplementedError, OSError) as err:
            raise OSError(f"無法以不覆寫方式建立證據包（{type(err).__name__}），已中止且未寫入：{final.name}") from err
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return final, sha


def latest_packet(packet_dir: Path, qid: str) -> Path | None:
    files = sorted(packet_dir.glob(f"{qid}.*.json"))
    return files[-1] if files else None


def load_packet(path: Path) -> tuple[dict, str, bool]:
    data = json.loads(path.read_text(encoding="utf-8"))
    packet, sha = data["packet"], data["packet_sha256"]
    return packet, sha, sha256_text(canonical(packet)) == sha


def packet_generation(packet: dict) -> str:
    """證據包世代：current（目前版本且版本欄位齊全）／legacy（packet_version 1）／unknown（其他或欄位不全）。
    只判斷，不修改、不升級舊證據包。"""
    v = packet.get("packet_version")
    if v == 1 and not isinstance(v, bool):
        return "legacy"
    if (isinstance(v, bool) or v != PACKET_VERSION or any(k not in packet for k in PACKET_REQUIRED_FIELDS)
            or packet.get("packet_rules_version") != PACKET_RULES_VERSION
            or not isinstance((packet.get("microsoft_learn") or {}).get("fetch_log"), list)):
        return "unknown"
    return "current"


GENERATION_LABEL = {"legacy": "舊版", "unknown": "版本不明"}


def packet_trust_reasons(packet: dict) -> list[str]:
    """社群資料能否取得正式共識資格的證據包層級條件（fail-closed）：必須是目前版本的證據包，
    且其 verification.json 快照已通過三層驗證（格式有效、來源一致、內容與受信任的正式基準相同）。"""
    gen = packet_generation(packet)
    if gen != "current":
        return [f"證據包為{GENERATION_LABEL[gen]}（packet_version={packet.get('packet_version')}），社群資料來源未經驗證"]
    t = packet.get("snapshot_trust")
    if not isinstance(t, dict) or t.get("level") != "verified":
        why = (t.get("reasons") if isinstance(t, dict) else None) or ["未記錄驗證結果"]
        return ["社群資料快照未通過來源驗證（unverified：" + "；".join(str(x) for x in why) + "）"]
    return []


def packet_reuse_problems(packet: dict, ctx, qid: str) -> list[str]:
    """證據包能否「再次送出」（與「題目是否已完成判讀」是不同的判斷，見 completed_run()）：
    必須是目前版本的證據包，且題庫未在 dry-run 後變更。舊版或版本不明的證據包一律不送出。"""
    problems = []
    if packet.get("bank_sha256") != ctx.bank_sha or packet["bank_answer"]["value"] != va.answer_of(ctx.units[qid]):
        problems.append(f"{qid} 的題庫已在 dry-run 後變更，拒絕送出。請重新 dry-run。")
    gen = packet_generation(packet)
    if gen != "current":
        problems.append(f"{qid} 的證據包為{GENERATION_LABEL[gen]}（packet_version={packet.get('packet_version')}），"
                        "缺少快照來源與文件擷取紀錄，不再送出；請在新的空白工作區重新 dry-run（舊證據包不修改、不升級）。")
    return problems


# ---------------------------------------------------------------------------
# 核對與信心（不採用模型自評）
# ---------------------------------------------------------------------------


def passage_index(packet: dict) -> dict[str, str]:
    return {p["passage_id"]: p["text"] for d in packet["microsoft_learn"]["documents"] for p in d["passages"]}


def quote_check(passages: dict[str, str], pid: str, quote: str) -> str:
    if pid not in passages:
        return "unknown_passage"
    q = norm(quote)
    if len(q) < MIN_QUOTE_CHARS:
        return "too_short"
    return "found" if q in norm(passages[pid]) else "not_found"


def fmt(v) -> str:
    return json.dumps(v, ensure_ascii=False)


INFERENCE_LANGUAGE = re.compile(
    r"industry|standard (?:name|practice|setting|policy)|typically|commonly|usually|generally|best practice"
    r"|業界|慣例|通常|一般而言|標準設定|名稱推論|顧名思義", re.I)
MIN_SCENARIO_TERMS = 2
CHOICE_TYPES = ("single_choice", "multiple_choice")
# 判定「引文對應題目情境」時不採計的泛用詞（可依需要增修）；選項文字本身的詞也一律不採計
GENERIC_SCENARIO_TERMS = set("""user users account accounts activity activities detection detections detect alert alerts
policy policies rule rules security event events log logs data organization organizations tenant tenants subscription
subscriptions resource resources setting settings receive attempt attempts identify investigate investigation contain
contains report reports query queries portal service services solution feature features option options device devices
admin administrator administrators""".split())
ALIGNMENT_NOTE = ("引文與主張的對應只是關鍵字比對，不是語意驗證：無法保證辨識「該使用者」與「組織內其他使用者」、"
                  "條件適用範圍或否定語氣等差異，仍需人工判讀。")


def term_base(word: str) -> str:
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def parse_suggested(unit, raw) -> tuple[object, str]:
    """回傳 (答案, 解析方式)。先依 JSON 解析；單選接受純文字選項 ID（C），複選接受 AE / A,E。
    答案仍須對應題目中存在的選項；原始輸出不修改。無法解析時拋出 ValueError。"""
    text = raw if isinstance(raw, str) else json.dumps(raw)
    try:
        return va.parse_answer(unit, text), "json"
    except ValueError as err:
        first = err
    s = text.strip().strip('"').strip("'").strip()
    ids = [o["id"] for o in unit.question.get("options", [])]
    if unit.type == "single_choice" and s in ids:
        return s, "plain_option_id"
    if unit.type == "multiple_choice" and s:
        parts = [p for p in re.split(r"[\s,;/、]+", s) if p]
        if len(parts) == 1 and len(parts[0]) > 1 and all(ch in ids for ch in parts[0]):
            parts = list(parts[0])
        if parts and all(p in ids for p in parts) and len(set(parts)) == len(parts):
            return va.parse_answer(unit, json.dumps(parts)), "plain_option_list"
    raise first


def notice_passages(packet: dict) -> list[str]:
    return [p["passage_id"] for d in packet["microsoft_learn"]["documents"] for p in d["passages"]
            if "product_change_notice" in (p.get("selection_reasons") or [])]


def scenario_terms(unit, products: list[dict], targets: list[tuple[str, str]]) -> dict:
    """具區辨力的題目情境詞：題幹關鍵詞去掉選項文字中的詞與泛用詞（單複數視為同一詞）。"""
    raw = keywords(strip_products(unit.question.get("question", ""), products), 40)
    option_bases = {term_base(w) for _, text in targets for w in tokens(text)}
    generic_bases = {term_base(w) for w in GENERIC_SCENARIO_TERMS}
    used, from_options, generic = [], [], []
    for w in raw:
        b = term_base(w)
        if b in option_bases:
            from_options.append(w)
        elif b in generic_bases:
            generic.append(w)
        elif b not in {term_base(x) for x in used}:
            used.append(w)
    return {"used": used, "excluded_option_words": from_options, "excluded_generic": generic}


def evaluation_matchers(unit) -> tuple[dict[str, dict], list[str], dict]:
    """對象比對器與具區辨力的題目情境詞（用來檢查引文是否同時對應選項與題目情境）。"""
    stem, targets = unit_texts(unit)
    products = detect_products(stem + " " + " ".join(t for _, t in targets))
    matchers = {tid: target_matcher(text, products) for tid, text in targets}
    terms = scenario_terms(unit, products, targets)
    return matchers, terms["used"], terms


def citation_alignment(quote: str, m: dict | None, scenario: list[str], relation: str, need_scenario: bool) -> str:
    """引文與主張是否一致（關鍵字啟發式，非語意驗證）：支持須同時提到該選項與至少兩個具區辨力的情境詞；
    反駁須提到該選項。題目可用的情境詞不足時，不得判為對應情境。"""
    if not m or not target_hit(quote, m):
        return "does_not_mention_option"
    if relation == "refutes_option":
        return "mentions_option"
    if need_scenario and len(scenario) < MIN_SCENARIO_TERMS:
        return "insufficient_scenario_terms"
    hits = {term_base(w) for w in scenario if has_phrase(quote, w)}
    if need_scenario and len(hits) < MIN_SCENARIO_TERMS:
        return "mentions_option_only"
    return "mentions_option_and_scenario"


RECOMMENDATION_NOTE = "有效選項不代表答案已被證據證實；官方證據等級描述證據品質，不是正確率。"


COMMUNITY_MIN_VOTES = va.CONSENSUS_MIN_VOTES    # 10
COMMUNITY_MIN_SHARE = va.CONSENSUS_MIN_SHARE    # 70（%）
STRATEGY_NOTE = ("考試答案推薦策略：ExamTopics 社群共識 ＞ Gemini 有效建議 ＞ 題庫原始答案（所有題型）；"
                 "不代表社群或 Gemini 在技術事實上必然正確。")


def _answer_key(ans) -> str:
    """把選擇題答案統一成排序後的字串（"AE"、["A","E"] 皆為 "AE"），用來比對投票分布與社群答案。"""
    if isinstance(ans, list):
        return "".join(sorted(str(x) for x in ans))
    return "".join(sorted(str(ans))) if isinstance(ans, str) else json.dumps(ans, sort_keys=True)


def community_stats(cv: dict) -> dict:
    """社群票數與支持率。有投票分布時以「最高票數 ÷ 各答案票數加總」計算未四捨五入的比例；
    沒有分布時如實使用已記錄的 vote_count 與 share_percent（不自行推算）。"""
    dist = cv.get("distribution")
    if isinstance(dist, list) and dist and all(isinstance(d.get("votes"), (int, float)) for d in dist):
        total = sum(d["votes"] for d in dist)
        top = max(d["votes"] for d in dist)
        leaders = [d for d in dist if d["votes"] == top]
        return {"total_votes": total, "top_votes": top, "share_exact": (top * 100 / total) if total else None,
                "share_displayed": cv.get("share_percent"), "tied": len(leaders) > 1, "tie_detectable": True,
                "top_answer_key": None if len(leaders) > 1 else _answer_key(leaders[0].get("answer")),
                "source": "distribution"}
    share = cv.get("share_percent")
    return {"total_votes": cv.get("vote_count"), "top_votes": None, "share_exact": share, "share_displayed": share,
            "tied": False, "tie_detectable": False, "top_answer_key": None,
            "source": "recorded_share_percent" if share is not None else "missing"}


def community_qualification(unit, ex: dict, trust_reasons: list[str] | None = None) -> dict:
    """社群推薦能否作為最終考試推薦：已確認同一題、總票數 ≥ 10、未四捨五入支持率 ≥ 70%、非同票、答案可對應本題。
    trust_reasons：證據包層級的未通過原因（packet_trust_reasons()）；以證據包判斷時一律由
    formal_consensus() / community_recommendation() 傳入，有任何原因即不具資格。"""
    cv = ex.get("community_vote") or {}
    answer = cv.get("answer")
    stats = community_stats(cv)
    reasons = list(trust_reasons or [])
    if ex.get("match_status") != "exact":
        reasons.append(f"未確認為同一題（{ex.get('status')}）")
    if answer is None:
        reasons.append("沒有最高票答案（沒有投票或同票）")
    if stats["tied"]:
        reasons.append("最高票同票")
    if stats["total_votes"] is None:
        reasons.append("缺少總票數")
    elif stats["total_votes"] < COMMUNITY_MIN_VOTES:
        reasons.append(f"總票數 {stats['total_votes']} 未達 {COMMUNITY_MIN_VOTES}")
    if stats["share_exact"] is None:
        reasons.append("缺少支持率")
    elif stats["share_exact"] < COMMUNITY_MIN_SHARE:
        reasons.append(f"支持率 {stats['share_exact']:.4g}% 未達 {COMMUNITY_MIN_SHARE}%")
    if unit is None:  # fail-closed：無法取得題目結構時不放行社群答案
        reasons.append("無法重建題目，無法驗證答案格式")
    elif answer is not None:
        try:
            va.parse_answer(unit, json.dumps(answer))
        except (ValueError, KeyError, TypeError):  # 題目結構異常也視為無法對應，不中止程式
            reasons.append(f"社群答案 {fmt(answer)} 無法對應本題的答案格式或選項")
        else:
            if unit.type in CHOICE_TYPES:
                if stats["top_answer_key"] is not None and stats["top_answer_key"] != _answer_key(answer):
                    reasons.append("投票分布的最高票答案與記錄的社群答案不一致")
            elif stats["source"] == "distribution":
                reasons.append("非選擇題無法核對投票分布與記錄答案")
    return {"qualifies": not reasons, "reasons": reasons, **stats}


SUMMARY_VERSION = "formal-consensus-v1"  # packet_summary 依正式共識資格產生；沒有此標記的摘要視為舊版
OLD_SUMMARY_LABEL = "舊版摘要，依當時顯示值判斷，不是正式共識資格"
PRECISION_NOTE = ("沒有逐選項投票分布：總票數與支持率為已記錄值，無法還原未四捨五入的原始比例，也無法判斷是否同票")


def packet_unit(packet: dict):
    """由證據包的題目內容（含選項 / 挖空 / 項目 / 陳述句 ID，不含答案）重建子題，供社群答案格式檢查使用。
    缺少必要欄位或格式不符時回傳 None（不以預設題型或虛構欄位補齊），由 community_qualification() fail-closed。"""
    question, qtype = packet.get("question"), packet.get("type")
    if not isinstance(question, dict) or not isinstance(qtype, str) or not qtype:
        return None
    qid = packet.get("question_id")
    return eq.Unit(qid, qid, qtype, question)


def formal_consensus(packet: dict, unit=None) -> dict:
    """正式社群共識資格；唯一判斷來源為 community_qualification()（與最終推薦相同）。
    供 model_view()、official_docs_role()、packet_summary() 使用，不再依賴 verify_answers 以頁面顯示值計算的 consensus_threshold。"""
    q = community_qualification(unit if unit is not None else packet_unit(packet), packet["examtopics"],
                                packet_trust_reasons(packet))
    view = {"qualifies": q["qualifies"], "reasons": q["reasons"], "total_votes": q["total_votes"],
            "share_exact": q["share_exact"], "share_displayed": q["share_displayed"], "share_source": q["source"],
            "rule": (f"同一題（exact）、總票數 ≥ {COMMUNITY_MIN_VOTES}、未四捨五入支持率 ≥ {COMMUNITY_MIN_SHARE}%、"
                     "最高票不同票、答案符合本題格式且與投票分布一致、社群資料快照已通過來源驗證")}
    if q["source"] != "distribution":
        view["precision_note"] = PRECISION_NOTE
    return view


def community_recommendation(packet: dict, suggested, notices: list[str], unit=None) -> dict:
    """社群推薦：只依已確認同一題的 ExamTopics 投票；沒有社群答案時 answer 為 None，不以題庫答案填補。
    qualifies 表示是否達到採用門檻（可作為最終考試推薦）。不受模型或官方證據影響。"""
    ex, bank = packet["examtopics"], packet["bank_answer"]["value"]
    vote = (ex.get("community_vote") or {}).get("answer")
    q = community_qualification(unit, ex, packet_trust_reasons(packet))
    if ex.get("status") == "available" and vote is not None:
        share = f"{q['share_exact']:.4g}%" if q["share_exact"] is not None else "比例不明"
        rec = {"answer": vote, "strength": "consensus" if q["qualifies"] else "below_threshold",
               "basis": f"ExamTopics 社群{'共識' if q['qualifies'] else '最高票（未達採用門檻）'} {fmt(vote)}，"
                        f"{share}／{q['total_votes']} 票（{ex.get('match_status')}；比例來源 {q['source']}）"}
    else:
        rec = {"answer": None, "strength": "none",
               "basis": f"無可用的社群答案（ExamTopics：{ex.get('status')}；{ex.get('note') or va.NO_COMMUNITY}）"}
    rec["qualifies"] = q["qualifies"] and rec["answer"] is not None
    rec["qualification"] = q
    rec["agrees_with_bank"] = va.compare_answers(bank, rec["answer"]) if rec["answer"] is not None else "no_community"
    rec["agrees_with_model"] = (va.compare_answers(rec["answer"], suggested)
                                if rec["answer"] is not None and suggested is not None else "undetermined")
    rec["flags"] = [f"官方文件有產品變更提示（{', '.join(notices)}），社群答案可能過時"] if notices and rec["answer"] is not None else []
    return rec


def official_docs_role(packet: dict) -> dict:
    """官方文件在本題的角色：社群已有共識時只用於核實技術事實；社群資料不足、未達共識、有爭議或可能過時時為主要核實依據。"""
    ex, reasons = packet["examtopics"], []
    if ex.get("status") != "available":
        reasons.append(f"社群資料不足（{ex.get('status')}）")
    else:
        fc = formal_consensus(packet)  # 與最終推薦相同的正式資格，不使用頁面顯示值門檻
        if not fc["qualifies"]:
            reasons.append("社群投票未達正式共識門檻（" + "；".join(fc["reasons"]) + "）")
    if packet["verification_status"].get("answer_status") == "disputed":
        reasons.append("題目已標記有爭議（disputed）")
    notices = notice_passages(packet)
    if notices:
        reasons.append(f"官方文件有產品變更提示（{', '.join(notices)}），題目可能過時")
    return {"role": "primary_verification" if reasons else "fact_check_only", "reasons": reasons}


def final_recommendation(unit, packet: dict, ev: dict | None, run_status: str, result: dict | None) -> dict:
    """最終考試推薦（與題庫答案分開保存），優先順序：
    1. 達採用門檻的 ExamTopics 社群推薦（同一題、≥ 10 票、未四捨五入支持率 ≥ 70%、答案可對應本題）——
       無論 Gemini 是否同意、是否有效或 API 是否失敗；
    2. Gemini 有效建議（依該題型既有格式解析成功），可不同於題庫答案或官方文件判讀；
    3. 所有題型：Gemini API 失敗或建議無效時，保留題庫原始答案（不另找答案、不增加請求）。"""
    bank = packet["bank_answer"]["value"]
    notices = notice_passages(packet)
    gem_valid = ev is not None and ev.get("status") == "evaluated"
    gem_answer = ev.get("suggested_answer") if gem_valid else None
    gem_status = "valid" if gem_valid else ((ev or {}).get("status") or run_status or "no_result")
    comm = community_recommendation(packet, gem_answer, notices, unit)
    oe = (ev or {}).get("official_evidence_assessment") or {}
    rec = {"question_type": unit.type, "fallback_rule_applies": True, "answer": None, "source": None,
           "fallback_reason": None, "bank_answer": bank,
           "community": {"answer": comm["answer"], "qualifies": comm["qualifies"],
                         "total_votes": comm["qualification"]["total_votes"],
                         "share_exact": comm["qualification"]["share_exact"],
                         "share_displayed": comm["qualification"]["share_displayed"],
                         "share_source": comm["qualification"]["source"],
                         "not_adopted_reasons": [] if comm["qualifies"] else comm["qualification"]["reasons"]},
           "community_answer": comm["answer"], "community_strength": comm["strength"],
           "gemini_answer": gem_answer, "gemini_status": gem_status,
           "recommendation_basis": (result or {}).get("recommendation_basis") if gem_valid else None,
           "official_evidence_level": oe.get("confidence"), "official_conflicts": list((ev or {}).get("official_conflicts") or []),
           "completeness": (ev or {}).get("completeness"), "may_be_outdated": bool(notices),
           "uncertainty": list((ev or {}).get("uncertainty") or []) if gem_valid else [],
           "strategy_note": STRATEGY_NOTE, "note": RECOMMENDATION_NOTE}
    if comm["qualifies"]:
        rec.update(answer=comm["answer"], source="community")
        if gem_valid and va.compare_answers(comm["answer"], gem_answer) != "agree":
            rec["uncertainty"].append(f"Gemini 建議 {fmt(gem_answer)} 與社群推薦 {fmt(comm['answer'])} 不同；"
                                      "依推薦策略採用社群推薦，Gemini 建議另行保留")
        elif not gem_valid:
            rec["uncertainty"].append(f"Gemini 沒有有效建議（{gem_status}）；依推薦策略採用社群推薦")
    elif gem_valid:
        rec.update(answer=gem_answer, source="gemini")
    else:
        reason = gem_status
        rec.update(answer=bank, source="bank_fallback", fallback_reason=reason)
        rec["uncertainty"].append(f"沒有達採用門檻的社群推薦，且 Gemini 沒有可對應本題的有效建議（{reason}），"
                                  "最終推薦回退為題庫原始答案")
    rec["agrees_with_bank"] = va.compare_answers(bank, rec["answer"]) if rec["answer"] is not None else None
    rec["agrees_with_community"] = (va.compare_answers(comm["answer"], rec["answer"])
                                    if comm["answer"] is not None and rec["answer"] is not None else "no_community")
    rec["agrees_with_gemini"] = (va.compare_answers(gem_answer, rec["answer"])
                                 if gem_answer is not None and rec["answer"] is not None else "no_gemini")
    return rec


def evaluate(unit, packet: dict, result: dict) -> dict:
    """核對模型輸出並分別產生「考試建議」與「官方證據評估」。
    引句存在（quote_check）、引文與主張一致（alignment）、模型主張的關係（relation）分開記錄；信心由程式計算。"""
    notices = notice_passages(packet)
    ev = {"status": "evaluated", "evaluator_version": EVALUATOR_VERSION, "valid_quote_meaning": VALID_QUOTE_MEANING,
          "answer_parse": {"raw": result.get("suggested_answer"), "method": None, "error": None},
          "suggested_answer": None, "agrees_with_bank": None, "citations": [], "evidence_gaps": [], "conflicts": [],
          "caps": [], "confidence": None, "completeness": "complete", "incomplete_reasons": [],
          "uncertainty": [], "community_recommendation": None, "official_evidence_assessment": None,
          "official_conflicts": []}
    try:
        suggested, method = parse_suggested(unit, result.get("suggested_answer") or "")
        ev["answer_parse"]["method"] = method
    except ValueError as err:
        ev.update(status="invalid_answer", error=f"建議答案格式不符：{err}")
        ev["answer_parse"]["error"] = str(err)
        ev["uncertainty"].append("模型輸出的答案無法依本題型格式解析或對應現有選項")
        ev["community_recommendation"] = community_recommendation(packet, None, notices, unit)
        return ev
    bank = packet["bank_answer"]["value"]
    cmp = va.compare_answers(bank, suggested)
    ev.update(suggested_answer=suggested, agrees_with_bank=cmp)

    targets = eq.answerable_targets(unit)
    matchers, scenario, scen_info = evaluation_matchers(unit)
    ev["scenario_terms"] = scen_info
    ev["alignment_method"] = "keyword_heuristic"
    ev["alignment_note"] = ALIGNMENT_NOTE
    choice = unit.type in CHOICE_TYPES
    need_scenario = choice  # 選擇題的直接支持一律要求情境詞；情境詞不足時不得升級為 direct_support
    passages = passage_index(packet)
    low, mid, gaps = [], [], []
    per: dict[str, dict] = {t: {"model_assessment": None, "model_basis": None, "direct": 0, "indirect": 0,
                                "refute": 0, "flags": []} for t in targets}
    unverifiable = 0
    for item in result.get("option_analysis") or []:
        tid = item.get("target_id")
        if tid not in per:
            mid.append(f"模型分析了不存在的對象 {tid}")
            continue
        t = per[tid]
        t["model_assessment"], t["model_basis"] = item.get("assessment"), item.get("basis")
        m = matchers.get(tid) or (matchers.get(f"{tid}:{suggested.get(tid)}") if isinstance(suggested, dict) else None)
        usable_any = False
        for c in item.get("citations") or []:
            check = quote_check(passages, c.get("passage_id"), c.get("quote") or "")
            usable = check == "found"
            rel = c.get("relation")
            align = citation_alignment(c.get("quote") or "", m, scenario, rel, need_scenario) if usable else None
            ev["citations"].append({"target_id": tid, "passage_id": c.get("passage_id"), "quote": c.get("quote"),
                                    "quote_check": check, "usable": usable, "relation_claimed_by_model": rel,
                                    "alignment": align})
            if not usable:
                unverifiable += 1
                continue
            usable_any = True
            if rel == "supports_option":
                if align == "mentions_option_and_scenario" and item.get("basis") == "official_direct":
                    t["direct"] += 1
                else:
                    t["indirect"] += 1
            elif rel == "refutes_option" and align == "mentions_option":
                t["refute"] += 1
            elif rel == "refutes_option":
                t["flags"].append("反駁引文未提到該選項，不計為反證")
        if item.get("basis") in ("official_direct", "official_indirect") and not usable_any:
            t["flags"].append("標為官方依據但沒有可核對的引文")
        if item.get("basis") in ("official_direct", "official_indirect") and INFERENCE_LANGUAGE.search(item.get("note") or ""):
            t["flags"].append("說明含業界慣例或名稱推論用語，該部分不視為官方證據")
    missing = [t for t in targets if per[t]["model_assessment"] is None]
    if missing:
        mid.append(f"模型輸出缺少 {', '.join(missing)} 的分析")
    if unverifiable:
        mid.append(f"有 {unverifiable} 筆引句無法核對（已排除，不作為官方佐證）")

    selected = set(suggested if isinstance(suggested, list) else [suggested]) if choice else set(targets)
    for tid, t in per.items():
        t["role"] = "selected" if tid in selected else "distractor"
        t["classification"] = ("refuted" if t["refute"] else "direct_support" if t["direct"]
                               else "indirect_support" if t["indirect"] else "no_support")
    sel = [t for t in targets if t in selected]
    distractors = [t for t in targets if t not in selected]

    supported = [t for t in sel if per[t]["direct"] or per[t]["indirect"]]
    if not supported:
        low.append("沒有可核對的官方引句支持建議答案（主要依賴模型推論或社群資料）")
    elif len(supported) < len(sel):
        mid.append(f"建議答案中的 {', '.join(t for t in sel if t not in supported)} 缺少可核對的官方引句")
    only_indirect = [t for t in supported if not per[t]["direct"]]
    if only_indirect:
        mid.append(f"{', '.join(only_indirect)} 只有間接依據（引文未同時對應選項與題目情境，或被標為間接）")
    # 官方文件與 Gemini 建議衝突：只記錄於 official_conflicts，不因衝突本身降為低
    official_conflicts = ev["official_conflicts"]
    for t in sel:
        if per[t]["refute"]:
            official_conflicts.append(f"可核對的官方引句被模型標為反駁建議答案中的 {t}")
    for t in distractors:
        if per[t]["direct"]:
            official_conflicts.append(f"可核對且對應選項的官方引句支持未選取的選項 {t}")
            gaps.append(f"選項 {t}：有直接支持的官方引文，且未被排除")
        elif per[t]["indirect"] and not per[t]["refute"]:
            mid.append(f"官方引句被模型標為間接支持未選取的選項 {t}")
            gaps.append(f"選項 {t}：有間接支持的引文，且未被排除")
        elif not per[t]["refute"]:
            gaps.append(f"選項 {t}：官方段落未明確排除（未提及不視為反駁）")
    if any(g.startswith("選項") for g in gaps):  # 單選與複選一致：無法排除其他選項時最高為中
        mid.append("官方證據不足以排除其他選項，不能視為官方文件已充分證明答案")
    flagged = [t for t in targets if per[t]["flags"]]
    if flagged:
        mid.append(f"{', '.join(flagged)} 的官方依據主張有疑慮（見逐項 flags）")
    program_sufficient = bool(sel) and all(per[t]["direct"] for t in sel) and not gaps and not flagged
    if result.get("docs_sufficient") is not True:
        mid.append("模型判斷官方段落不足以直接支持結論並排除其他選項")
    elif not program_sufficient:
        ev["uncertainty"].append("模型宣稱官方文件足以支持結論，但程式核對結果不足")
    if packet["microsoft_learn"]["status"] != "found":
        gaps.append("尚未找到官方依據")

    pci = result.get("product_change_impact")
    pc = {"notice_passages": notices, "model_output": pci, "status": "no_notice_in_packet"}
    if notices:
        refs = set((pci or {}).get("evidence_refs") or []) if isinstance(pci, dict) else set()
        analyzed = isinstance(pci, dict) and pci.get("mentioned") is True and bool(refs & set(notices))
        pc["status"] = "analyzed" if analyzed else "not_analyzed"
        mid.append(f"官方文件有產品變更提示（{', '.join(notices)}），答案可能過時")
        if not analyzed:
            ev["completeness"] = "incomplete"
            ev["incomplete_reasons"].append(
                f"證據包含產品變更提示 {', '.join(notices)}，但模型輸出未分析（"
                + ("缺少 product_change_impact 欄位" if not isinstance(pci, dict) else "mentioned 不為 true 或未引用提示段落")
                + "）；不得視為沒有影響")
            mid.append("產品變更提示未被分析，判讀不完整")
    ev["product_change"] = pc

    # 與題庫答案或社群推薦不同不影響官方證據等級；只記錄為不確定性說明
    comm = community_recommendation(packet, suggested, notices, unit)
    if comm["answer"] is not None and comm["agrees_with_model"] != "agree":
        ev["uncertainty"].append(f"Gemini 建議 {fmt(suggested)} 與 ExamTopics 社群"
                                 f"{'共識' if comm['strength'] == 'consensus' else '最高票（未達採用門檻）'} "
                                 f"{fmt(comm['answer'])} 不同；取捨理由見模型 rationale")

    vs = packet["verification_status"]
    consistency, proposed = vs["microsoft_consistency"], vs.get("proposed_answer")
    if consistency == "consistent" and cmp != "agree":
        official_conflicts.append("已人工查證的官方證據支持題庫答案，但 Gemini 建議不同")
    elif consistency == "proposed_supported":
        if proposed is not None and va.compare_answers(proposed, suggested) == "agree":
            pass
        elif cmp == "agree":
            official_conflicts.append("已人工查證的官方證據支持題庫以外的答案，但 Gemini 建議與題庫相同")
        else:
            mid.append("已人工查證的官方證據支持題庫以外的答案，但未記錄候選答案，需確認是否與建議答案相同")
    elif consistency in ("conflicting", "invalid_evidence"):
        low.append(f"已人工查證的官方證據狀態為 {consistency}")
    if vs["answer_status"] == "disputed":
        ev["uncertainty"].append("此題題庫答案已被標記 disputed")

    confidence = "低" if low else "中" if mid else "高"
    ev.update(conflicts=low, caps=mid, evidence_gaps=gaps, confidence=confidence)
    if ev["completeness"] == "incomplete":
        ev["uncertainty"].append("判讀不完整：" + "；".join(ev["incomplete_reasons"]))
    ev["uncertainty"] += [f"官方文件與 Gemini 建議衝突：{c}" for c in official_conflicts]
    ev["community_recommendation"] = comm
    ev["official_evidence_assessment"] = {
        "confidence": confidence, "suggested_answer": suggested, "per_target": per,
        "model_claimed_docs_sufficient": result.get("docs_sufficient"), "program_docs_sufficient": program_sufficient,
        "may_be_outdated": bool(notices), "completeness": ev["completeness"],
        "note": "依官方段落的可核對引文、與選項及題目情境的對應、反證與產品變更計算；社群共識不提高此信心",
    }
    return ev


# ---------------------------------------------------------------------------
# Gemini 呼叫（每題一次，不重試）
# ---------------------------------------------------------------------------


def make_config(args):
    from google.genai import types

    cfg = {"system_instruction": SYSTEM_INSTRUCTION, "response_mime_type": "application/json",
           "response_json_schema": RESPONSE_SCHEMA, "max_output_tokens": args.max_output_tokens}
    if args.thinking_level and args.thinking_level.lower() != "none":
        cfg["thinking_config"] = types.ThinkingConfig(thinking_level=args.thinking_level)
    return types.GenerateContentConfig(**cfg)


def classify_error(e: Exception) -> tuple[str, int | None]:
    status = eq.error_status(e)
    if status == 429:
        return "rate_limited", status
    if status in FATAL_STATUS:
        return "fatal", status
    name = type(e).__name__.lower()
    if status in TRANSIENT_STATUS or (status is None and ("timeout" in name or "connect" in name)):
        return "unavailable", status
    return "error", status


def call_once(client, args, packet: dict, api_key: str) -> dict:
    zero = {"input_tokens": 0, "output_tokens": 0, "thoughts_tokens": 0, "total_tokens": 0}
    try:
        resp = client.models.generate_content(model=args.model, contents=render_prompt(packet), config=make_config(args))
    except Exception as e:  # noqa: BLE001 — SDK 例外型別依版本而異
        kind, status = classify_error(e)
        return {"status": kind, "http_status": status, "usage": zero, "result": None,
                "error": eq.redact(f"{type(e).__name__}({status}): {e}", api_key)[:300]}
    usage = eq.usage_of(resp)
    finish = str(getattr((getattr(resp, "candidates", None) or [None])[0], "finish_reason", "") or "")
    try:
        result = json.loads(resp.text or "")
        if not isinstance(result, dict):
            raise ValueError("not an object")
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"status": "invalid_output", "http_status": 200, "usage": usage, "result": None,
                "error": f"輸出不是有效 JSON 物件（finish_reason={finish}）"}
    return {"status": "done", "http_status": 200, "usage": usage, "result": result, "error": None,
            "finish_reason": finish}


# ---------------------------------------------------------------------------
# 結果保存與報告
# ---------------------------------------------------------------------------


# ---- 工作區獨佔鎖（跨程序 OS 鎖）----------------------------------------------------------
# 鎖檔只是 OS 鎖的載體：「鎖檔存在」不代表有人持有鎖；是否被持有以能否取得 OS 鎖判斷。
# 程序結束（含當掉、被終止）時 OS 會釋放鎖，殘留的鎖檔不會永久阻擋；本程式不刪除、不改寫鎖檔（包括來源不明的鎖檔）。
LOCK_NAME = ".gemini_judge.lock"
_LOCKS: dict[str, dict] = {}            # 本程序目前持有的工作區鎖：正規化路徑 → {owner, depth, fd}
_LOCKS_GUARD = threading.Lock()


class WorkspaceBusy(RuntimeError):
    """無法取得工作區鎖（另一個程序或本程序的其他執行緒正在使用）。"""


def _os_lock_acquire(lock_path: Path) -> int:
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise WorkspaceBusy(f"鎖檔位置不是一般檔案（{lock_path.name}），為安全起見不使用、不刪除，拒絕執行")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # 非阻塞；鎖定第 0 位元組（可超出檔尾，不需寫入內容）
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as err:
        os.close(fd)
        raise WorkspaceBusy("另一個程序正持有此工作區的 OS 鎖（鎖檔存在且確實被持有）") from err
    return fd


def _os_lock_release(fd: int) -> None:
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def workspace_lock(out_dir: Path):
    """取得工作區獨佔鎖（非阻塞；取不到即拋出 WorkspaceBusy，不等待）。同一執行緒可重入（計數），不會死鎖；
    本程序其他執行緒視為忙碌。離開（含例外）時釋放。工作區目錄必須已存在：本函式不建立目錄。"""
    key = _norm_path(out_dir)
    me = threading.get_ident()
    with _LOCKS_GUARD:
        held = _LOCKS.get(key)
        if held is not None:
            if held["owner"] != me:
                raise WorkspaceBusy("本程序的其他執行緒正在使用此工作區")
            held["depth"] += 1
    if held is not None:
        try:
            yield
        finally:
            with _LOCKS_GUARD:
                held["depth"] -= 1
        return
    if not Path(out_dir).is_dir():
        raise WorkspaceBusy(f"工作區不存在或不是目錄（{out_dir}），不建立目錄、不取得鎖")
    fd = _os_lock_acquire(Path(out_dir) / LOCK_NAME)
    with _LOCKS_GUARD:
        _LOCKS[key] = {"owner": me, "depth": 1, "fd": fd}
    try:
        yield
    finally:
        with _LOCKS_GUARD:
            _LOCKS.pop(key, None)
        _os_lock_release(fd)


def holds_workspace_lock(out_dir: Path) -> bool:
    with _LOCKS_GUARD:
        held = _LOCKS.get(_norm_path(out_dir))
        return held is not None and held["owner"] == threading.get_ident()


def locked_command(fn):
    """指令包裝：整個指令（讀取既有紀錄 → 外部請求 → 寫入）都在工作區鎖內執行；取不到鎖即停止，
    不建立 client、不發出請求、不寫入。"""
    @functools.wraps(fn)
    def wrapper(ctx, *a, **k):
        try:
            with workspace_lock(ctx.out_dir):
                return fn(ctx, *a, **k)
        except WorkspaceBusy as err:
            print(f"無法取得工作區鎖：{err}。未建立 API client、未發出任何請求、未寫入任何檔案。")
            return 2
    return wrapper


# ---- 寫入 ------------------------------------------------------------------------------------


def load_results(path: Path) -> dict:
    """讀取結果檔（在工作區鎖內讀取，讀取與後續寫入之間不會有其他合作程序寫入）。"""
    with workspace_lock(path.parent):
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("questions"), dict) or not isinstance(data.get("meta"), dict):
                raise ValueError(f"結果檔格式不符（需要 meta 與 questions 物件）：{path}")
            return data
        return {"meta": {"schema_version": RESULTS_VERSION, "created_at": va.now_iso()}, "questions": {}}


def atomic_write(path: Path, text: str) -> None:
    """原子取代寫入：寫入同目錄的唯一暫存檔（mkstemp，不使用固定檔名）→ flush → fsync → os.replace。
    任何步驟失敗時原檔維持原狀，只移除本次建立的暫存檔；不建立備份（備份由呼叫端決定）。
    換行沿用文字模式（與先前 Path.write_text 相同）。目錄項目的 fsync 在 Windows 上無法執行，未做。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_new_file(path: Path, text: str) -> None:
    """不覆寫地建立新檔：唯一暫存檔 → fsync → os.link 到最終檔名（已存在即 FileExistsError）→ 移除暫存檔。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError as err:
            raise FileExistsError(f"檔案已存在，未覆寫：{path}") from err
        except (AttributeError, NotImplementedError, OSError) as err:
            raise OSError(f"無法以不覆寫方式建立檔案（{type(err).__name__}），已中止且未寫入：{path.name}") from err
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def append_run(path: Path, backup_dir: Path, qid: str, run: dict, backup: bool = True) -> Path | None:
    """新增一筆紀錄（只新增、不覆寫既有紀錄），讀取到寫入全程持有工作區鎖（同程序可重入）。
    backup=True 時先以 verify_answers.backup_file() 備份既有結果檔（既有備份政策：時間戳記檔名、SHA256 驗證）；
    寫入一律使用 atomic_write()（唯一暫存檔、fsync、os.replace）。"""
    with workspace_lock(path.parent):
        data = load_results(path)
        data["questions"].setdefault(qid, {"runs": []})["runs"].append(run)
        data["meta"]["updated_at"] = va.now_iso()
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        made = va.backup_file(path, backup_dir) if backup else None
        atomic_write(path, text)
        return made


# ---- 請求紀錄（遠端呼叫與本機寫入不是單一交易）--------------------------------------------------
ATTEMPTS_SUBDIR = ("gemini_judge", "attempts")


def attempts_dir(out_dir: Path) -> Path:
    return Path(out_dir).joinpath(*ATTEMPTS_SUBDIR)


def record_attempt(out_dir: Path, qid: str, packet_file: str, packet_sha: str, model: str, tier: str) -> str:
    """送出 API 請求「之前」建立請求紀錄（不覆寫、fsync）；寫入失敗即拋出例外，呼叫端不得送出請求。"""
    aid = uuid.uuid4().hex
    rec = {"attempt_id": aid, "question_id": qid, "started_at": va.now_iso(), "pid": os.getpid(),
           "packet_file": packet_file, "packet_sha256": packet_sha, "model": model, "tier": tier,
           "note": "送出 API 請求前建立；gemini_judgments.json 中沒有相同 attempt_id 的紀錄，"
                   "代表該次請求未完成或結果未寫入（請求可能已送出、可能已計費）"}
    write_new_file(attempts_dir(out_dir) / f"{qid}.{stamp()}.{aid[:8]}.json",
                   json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    return aid


def unfinished_attempts(out_dir: Path, results: dict) -> list[dict]:
    """請求紀錄中，結果檔沒有對應 attempt_id 的項目（未完成）。無法讀取的請求紀錄也列為未完成。"""
    d = attempts_dir(out_dir)
    if not d.is_dir():
        return []
    recorded = {r.get("attempt_id") for q in (results.get("questions") or {}).values() if isinstance(q, dict)
                for r in q.get("runs") or [] if isinstance(r, dict)}
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            aid = rec.get("attempt_id") if isinstance(rec, dict) else None
        except (OSError, UnicodeDecodeError, ValueError):
            rec, aid = {}, None
        if aid is None or aid not in recorded:
            out.append({"file": f.name, "question_id": rec.get("question_id"), "started_at": rec.get("started_at"),
                        "attempt_id": aid, "readable": aid is not None})
    return out


def completed_run(runs: list, packet_sha: str, model: str) -> dict | None:
    """可作為 --run 跳過依據的成功紀錄。必須同時符合：真正的 API 呼叫（kind=api_call）、status=done、
    模型輸出與核對結果完整（evaluation.status=evaluated、有最終推薦），且證據包雜湊、模型、prompt 版本、
    判定規則版本都與目前設定一致。任何一項無法證明（例如舊紀錄缺欄位）即不視為已完成。"""
    for r in reversed(runs or []):
        if not isinstance(r, dict):
            continue
        ev = r.get("evaluation")
        if (r.get("kind") == "api_call" and r.get("api_called") is True and r.get("status") == "done"
                and isinstance(r.get("model_output"), dict) and isinstance(ev, dict) and ev.get("status") == "evaluated"
                and ev.get("evaluator_version") == EVALUATOR_VERSION and r.get("prompt_version") == PROMPT_VERSION
                and r.get("model") == model and r.get("packet_sha256") == packet_sha
                and isinstance(r.get("final_exam_recommendation"), dict)):
            return r
        if community_adoption_valid(r, packet_sha):
            return r
    return None


COMMUNITY_KIND = "community_adopted"  # M-B：達社群共識門檻、未呼叫 Gemini 而直接採用社群答案的紀錄


def is_community_adoption(r) -> bool:
    """紀錄本身是否為格式完整的社群採用紀錄（不比對證據包雜湊）。"""
    fin = r.get("final_exam_recommendation") if isinstance(r, dict) else None
    q = r.get("community_qualification") if isinstance(r, dict) else None
    return (isinstance(r, dict) and r.get("kind") == COMMUNITY_KIND and r.get("api_called") is False
            and r.get("status") == COMMUNITY_KIND and r.get("model_output") is None
            and r.get("evaluator_version") == EVALUATOR_VERSION and r.get("packet_rules_version") == PACKET_RULES_VERSION
            and isinstance(q, dict) and q.get("qualifies") is True
            and isinstance(fin, dict) and fin.get("source") == "community" and fin.get("answer") is not None)


def community_adoption_valid(r, packet_sha: str) -> bool:
    """社群採用紀錄可作為完成依據：格式完整、判定與證據包規則版本與目前一致，且證據包雜湊相同。"""
    return is_community_adoption(r) and r.get("packet_sha256") == packet_sha


def effective_run_index(runs: list) -> int:
    """目前有效推薦所依據的紀錄：最新一筆核對成功（evaluation.status=evaluated）或格式完整的社群採用紀錄；
    沒有任何成功紀錄時，使用最新一筆（其最終推薦為依規則產生的回退或不推薦結果）。"""
    for i in range(len(runs) - 1, -1, -1):
        ev = runs[i].get("evaluation") if isinstance(runs[i], dict) else None
        if (isinstance(ev, dict) and ev.get("status") == "evaluated") or is_community_adoption(runs[i]):
            return i
    return len(runs) - 1


def render_report(results: dict, unfinished: list[dict] | None = None) -> str:
    lines = ["# Gemini 答案判讀結果", "",
             f"> {VALID_QUOTE_MEANING}", f"> {ALIGNMENT_NOTE}", f"> {RECOMMENDATION_NOTE}", f"> {STRATEGY_NOTE}",
             "> 最終考試推薦與題庫原始答案分開保存；社群採用門檻以未四捨五入的實際比例判斷。"
             "官方證據等級由程式計算，不採用模型自評；本檔不修改題庫或驗證狀態。", ""]
    if unfinished:
        lines += ["## ⚠ 未完成的請求", "",
                  "以下請求在送出前已建立紀錄，但結果檔中沒有對應結果（程序可能在呼叫 API 後、寫入前中斷）。"
                  "請求可能已送出、可能已計費；本程式不會自動重試。", ""]
        lines += [f"- {u.get('question_id') or '（無法讀取）'}｜開始 {u.get('started_at') or '—'}｜{u['file']}" for u in unfinished]
        lines.append("")
    for qid, q in sorted(results.get("questions", {}).items()):
        runs = q["runs"]
        latest_i, eff_i = len(runs) - 1, effective_run_index(runs)
        latest = runs[latest_i]
        run = runs[eff_i]  # 有效推薦所依據的紀錄（後續失敗不會讓先前成功的推薦消失）
        eff_ok = (run.get("evaluation") or {}).get("status") == "evaluated" or is_community_adoption(run)
        label = {"offline_reevaluation": "離線重核", COMMUNITY_KIND: "社群採用"}
        history = " → ".join(f"{i + 1}) {x.get('at')} {label.get(x.get('kind'), 'API')} {x.get('status')}"
                             for i, x in enumerate(runs))
        if eff_ok and eff_i == latest_i:
            latest_note = "以下為最新一次的成功結果"
        elif eff_ok:
            latest_note = f"⚠ 最近一次執行未取得有效結果；以下有效推薦沿用第 {eff_i + 1} 筆（{run.get('at')}）的成功結果"
        else:
            latest_note = "尚無有效成功紀錄；以下為最新一次執行依規則產生的結果（非成功判讀）"
        ev, r, pk = run.get("evaluation") or {}, run.get("model_output") or {}, run.get("packet_summary") or {}
        if not r and run.get("source_run_index") is not None:
            r = q["runs"][run["source_run_index"]].get("model_output") or {}
        er = ev.get("community_recommendation") or ev.get("exam_recommendation") or {}  # 舊紀錄為 exam_recommendation
        oe, pc = ev.get("official_evidence_assessment") or {}, ev.get("product_change") or {}
        fin = run.get("final_exam_recommendation")
        kind = {"offline_reevaluation": "離線重核（未呼叫 API）",
                COMMUNITY_KIND: "社群採用（達共識門檻，未呼叫 API）"}.get(run.get("kind"), "API 呼叫")
        if fin:
            src = {"community": "採用達門檻的社群推薦", "gemini": "採用 Gemini 有效建議",
                   "bank_fallback": f"回退為題庫原始答案（{fin.get('fallback_reason')}）",
                   "none_invalid_output": f"未產生推薦（此題型不套用回退；{fin.get('fallback_reason')}）",
                   "none_api_failed": f"未產生推薦（此題型不套用回退；{fin.get('fallback_reason')}）"}.get(fin["source"], fin["source"])
            fin_line = (f"- **最終考試推薦：{fmt(fin['answer'])}**｜{src}｜Gemini 推薦依據 {fin.get('recommendation_basis') or '—'}"
                        f"｜與題庫 {fin.get('agrees_with_bank') or '—'}｜與社群 {fin.get('agrees_with_community')}"
                        f"｜與 Gemini {fin.get('agrees_with_gemini', '—')}")
            c = fin.get("community")
            if c:
                share = f"{c['share_exact']:.4g}%" if c.get("share_exact") is not None else "不明"
                comm_line = (f"- 社群採用門檻：{'✅ 達門檻' if c['qualifies'] else '❌ 未採用'}｜票數 {c.get('total_votes') if c.get('total_votes') is not None else '不明'}"
                             f"｜支持率 {share}（顯示值 {c.get('share_displayed') if c.get('share_displayed') is not None else '—'}，來源 {c.get('share_source')}）"
                             + (f"｜未採用原因：{'；'.join(c['not_adopted_reasons'])}" if c.get("not_adopted_reasons") else ""))
            else:
                comm_line = None
        else:
            fin_line = "- 最終考試推薦：—（舊版紀錄未產生，可用 --reevaluate 離線補上）"
            comm_line = None
        lines += [f"## {qid}", "",
                  f"- 最新一次執行：{latest.get('at')}｜狀態 {latest.get('status')}"
                  + (f"｜HTTP {latest.get('http_status')}" if latest.get("http_status") else "") + f"｜{latest_note}",
                  f"- 執行歷程（共 {len(runs)} 筆）：{history}",
                  f"- 有效推薦依據紀錄：{run['at']}｜{kind}｜模型 {run['model']}（{run['tier']}）｜prompt {run['prompt_version']}"
                  f"｜狀態 {run['status']}｜答案解析 {(ev.get('answer_parse') or {}).get('method') or '—'}",
                  fin_line,
                  f"- 題庫原始答案：{fmt(pk.get('bank_answer'))}｜Gemini 建議：{fmt(ev.get('suggested_answer'))}",
                  f"- ExamTopics 社群推薦：{fmt(er.get('answer')) if er.get('answer') is not None else '無'}｜{er.get('basis', '—')}",
                  f"- **官方證據等級：{oe.get('confidence') or ev.get('confidence') or '—'}**"
                  f"（描述證據品質，不是正確率；模型自評 {r.get('model_confidence', '—')}，不採用）"
                  f"｜完整性 {ev.get('completeness', '—')}｜可能過時 {oe.get('may_be_outdated', '—')}",
                  f"- 產品變更提示：{', '.join(pc.get('notice_passages') or []) or ('未判讀' if not ev else '無')}"
                  f"｜模型分析：{pc.get('status', '—')}",
                  f"- ExamTopics：{pk.get('examtopics')}"
                  + ("" if pk.get("summary_version") == SUMMARY_VERSION else f"（{OLD_SUMMARY_LABEL}）"),
                  f"- Microsoft Learn：{pk.get('learn')}"]
        if comm_line:
            lines.insert(lines.index(fin_line) + 3, comm_line)
        if ev.get("official_conflicts"):
            lines += ["", "**官方文件與 Gemini 建議的衝突**（只記錄，不因此降低官方證據等級）"] + [f"- {x}" for x in ev["official_conflicts"]]
        if oe.get("per_target"):
            lines += ["", "| 選項 | 角色 | 程式分類 | 模型判斷 | 模型依據 | 疑慮 |", "|---|---|---|---|---|---|"]
            for tid, t in oe["per_target"].items():
                lines.append(f"| {tid} | {t.get('role')} | {t.get('classification')} | {t.get('model_assessment')} | "
                             f"{t.get('model_basis')} | {'；'.join(t.get('flags') or [])} |")
        if ev.get("incomplete_reasons"):
            lines += ["", "**判讀不完整**"] + [f"- {x}" for x in ev["incomplete_reasons"]]
        if run.get("error"):
            lines.append(f"- 錯誤：{run['error']}")
        if latest is not run and latest.get("error"):
            lines.append(f"- 最近一次執行錯誤：{latest['error']}")
        if r:
            lines += ["", f"**理由**：{r.get('rationale', '')}", "",
                      f"**Gemini 對社群資料的說明**（模型文字，不是 ExamTopics 投票結果）：{r.get('community_assessment', '')}",
                      f"**官方文件足以直接支持**（模型判斷）：{r.get('docs_sufficient')}"]
        uncertainty = (fin or {}).get("uncertainty") or ev.get("uncertainty") or ev.get("human_review_reasons") or []
        for title, items in (("官方證據衝突（等級降為低）", ev.get("conflicts")), ("官方證據等級上限因素", ev.get("caps")),
                             ("證據缺口", ev.get("evidence_gaps")), ("不確定性說明", uncertainty)):
            if items:
                lines += ["", f"**{title}**"] + [f"- {x}" for x in items]
        if r.get("open_questions"):
            lines += ["", "**尚未確認的事項**"] + [f"- {x}" for x in r["open_questions"]]
        if ev.get("citations"):
            lines += ["", "| 對象 | 段落 | 引句存在 | 模型主張 | 引文與主張對應 | 引句 |", "|---|---|---|---|---|---|"]
            for c in ev["citations"]:
                quote = (c.get("quote") or "").replace("|", "\\|")[:160]
                lines.append(f"| {c['target_id']} | {c['passage_id']} | {c['quote_check']} | "
                             f"{c['relation_claimed_by_model']} | {c.get('alignment') or '—'} | {quote} |")
        docs = run.get("documents") or []
        if docs:
            lines += ["", "**本次提供的官方文件**"] + [f"- {d['doc_id']} [{d['title']}]({d['url']})（{d['origin']}）"
                                                     for d in docs]
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 指令
# ---------------------------------------------------------------------------


def packet_summary(packet: dict) -> dict:
    ex, ml = packet["examtopics"], packet["microsoft_learn"]
    vote = ex.get("community_vote") or {}
    if ex["status"] == "available":
        fc = formal_consensus(packet)  # 正式資格，與最終推薦一致
        share = f"{fc['share_exact']:.4g}%" if fc["share_exact"] is not None else "比例不明"
        ex_text = (f"{ex['status']}｜{fmt(vote.get('answer'))} 實際 {share}（顯示 {vote.get('share_percent')}%）"
                   f" / {fc['total_votes']} 票｜正式共識 {'達成' if fc['qualifies'] else '未達'}"
                   + ("" if fc["qualifies"] else f"（{'；'.join(fc['reasons'])}）")
                   + ("｜比例來源為已記錄值，精度有限" if fc["share_source"] != "distribution" else ""))
    else:
        ex_text = f"{ex['status']}｜{ex.get('note', '')}"
    n = sum(len(d["passages"]) for d in ml["documents"])
    return {"summary_version": SUMMARY_VERSION, "bank_answer": packet["bank_answer"]["value"], "examtopics": ex_text,
            "learn": f"{ml['status']}｜{len(ml['documents'])} 篇文件、{n} 段" + (f"｜{ml['note']}" if ml["note"] else "")}


def parse_doc_urls(values: list[str], ids: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """--doc-url URL（只指定一題時）或 --doc-url 題目ID=URL；只接受 https://learn.microsoft.com/ 文件。"""
    out: dict[str, list[str]] = {}
    problems = []
    for v in values:
        qid, url = (v.split("=", 1) if "=" in v and v.split("=", 1)[0] in ids else (None, v))
        if qid is None:
            if len(ids) != 1:
                problems.append(f"{v}：指定多題時請寫成 題目ID=網址")
                continue
            qid = ids[0]
        url = clean_url(url.strip())
        if not valid_learn_url(url):
            problems.append(f"{v}：只接受 https://learn.microsoft.com/ 的文件網址")
            continue
        if url not in out.setdefault(qid, []):
            out[qid].append(url)
    return out, problems


@locked_command
def cmd_dry_run(ctx, args, ids: list[str], search=None, fetch=None, doc_urls: dict | None = None) -> int:
    # 鎖內重新檢查（降低檢查與使用之間的時間差）：工作區、快照與題庫都以鎖內讀到的內容為準
    problems = dry_run_preflight(ctx.out_dir, ctx.bank_path)
    snap = None
    if not problems:
        snap_problems, snap = load_snapshot_checked(ctx.out_dir, ctx.bank_sha)
        problems += snap_problems
    if not problems and eq.sha256_file(ctx.bank_path) != ctx.bank_sha:
        problems.append("題庫檔在讀取後已變更，請重新執行")
    if not problems:
        ctx.data = snap["data"]  # 證據包內容與記錄的 snapshot_sha256 來自同一份位元組
        problems += dry_run_batch_problems(ctx, ids)  # 整批預檢：任一題不通過即在任何外部請求前拒絕
    if problems:
        print("dry-run 整批預檢未通過，未發出任何請求、未建立任何證據包：\n  " + "\n  ".join(problems))
        return 2
    provenance = snapshot_provenance(snap, ctx.bank_sha)
    trust = provenance["snapshot_trust"]
    levels = accumulated_trust_levels(ctx.out_dir)  # M-A：同一工作區的各批必須使用相同信任等級的快照
    if levels and levels != {trust["level"]}:
        print("dry-run 整批預檢未通過，未發出任何請求、未建立任何證據包：\n  "
              f"既有證據包的快照信任等級為 {'、'.join(sorted(str(x) for x in levels))}，本批為 {trust['level']}"
              "（正式基準 output/verification.json 可能已變更，或其位置不再受信任）；拒絕繼續使用此工作區，"
              "不自動更新快照。請改用新的空白工作區。")
        return 2
    packet_dir = ctx.out_dir / "gemini_judge" / "packets"
    price, price_note = eq.load_price(args.model)
    total_in = 0
    print("=" * 64)
    print(f"Gemini 判讀 dry-run（不呼叫 Gemini API）｜模型 {args.model}")
    if trust["level"] == "verified":
        print(f"社群資料快照：verified（格式有效、來源一致、與正式基準相同）｜SHA256 {provenance['snapshot_sha256'][:12]}…")
    else:
        print("⚠ 社群資料快照：unverified（" + "；".join(trust["reasons"]) + "）；本批社群答案不會取得正式共識資格")
    for qid in ids:
        packet = build_packet(ctx, qid, search, fetch, args.delay, (doc_urls or {}).get(qid), provenance)
        path, sha = save_packet(packet_dir, packet)
        tok = estimate_input_tokens(packet)
        total_in += tok
        s = packet_summary(packet)
        ml = packet["microsoft_learn"]
        print("-" * 64)
        print(f"{qid}（{packet['type']}）題庫答案 {fmt(s['bank_answer'])}｜來源：{packet['bank_answer']['origin']}"
              f"｜狀態 {packet['verification_status']['answer_status']}")
        print(f"  ExamTopics：{s['examtopics']}")
        hv = packet["human_verified_microsoft_evidence"]
        if hv:
            print(f"  人工查證官方證據：{len(hv)} 筆（一致性 {packet['verification_status']['microsoft_consistency']}）")
        print(f"  產品：{'、'.join(ml['products']) or '（未辨識，不做路徑篩選）'}")
        print(f"  Learn 查詢（{len(ml['queries'])} 組）：{' ／ '.join(ml['queries'])}")
        for d in ml["documents"]:
            print(f"  {d['doc_id']} {d['title']}（{d['origin']}）\n      {d['url']}")
            for p in d["passages"]:
                pin = "★" if p["pinned_human_excerpt"] else ("!" if "product_change_notice" in p.get("selection_reasons", []) else " ")
                tg = f"［{','.join(p['targets'])}］" if p["targets"] else ""
                why = "、".join(p.get("selection_reasons", []))
                print(f"     {pin}{p['passage_id']}（分數 {p['score']}｜{why}）{tg}{p['text'][:110]}…")
        for d in ml["documents_without_relevant_passages"]:
            print(f"  ✗ 無相關段落：{d['title']}（{d['origin']}）{d['url']}")
        for d in ml["documents_failed"]:
            print(f"  ✗ 擷取失敗：{d['url']}（{d['origin']}，{d['error']}）")
        rejected = [c for c in ml["candidates_considered"] if (c["decision"] or "").startswith("rejected")]
        if rejected:
            print(f"  已排除 {len(rejected)} 篇其他產品或非文件頁面，例如："
                  + "；".join(c["title"] for c in rejected[:3]))
        cov = ml["option_coverage"]
        if cov:
            print(f"  段落提及的選項：{', '.join(t for t, v in cov.items() if v) or '無'}"
                  f"｜未提及：{', '.join(t for t, v in cov.items() if not v) or '無'}")
        for err in ml["errors"]:
            print(f"  ⚠ {err}")
        if ml["note"]:
            print(f"  ⚠ {ml['note']}")
        print(f"  預估輸入 token：約 {tok:,}（字元粗估）")
        print(f"  證據包：{path}\n  SHA256：{sha}")
    out_tok = args.assume_output_tokens * len(ids)
    cost = eq.cost_usd(price, total_in, out_tok)
    print("=" * 64)
    print(f"合計 {len(ids)} 題、{len(ids)} 次請求｜輸入約 {total_in:,}、輸出約 {out_tok:,}（每題假設 {args.assume_output_tokens:,}，含 thinking）")
    print(f"付費層估計：{'—' if cost is None else f'約 US${cost:.4f}'}；免費層不計費但內容可能用於改善 Google 產品；{price_note}")
    print("未呼叫 Gemini API。確認證據包後，才可用 --run --tier 送出。")
    return 0


def confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        print("非互動模式：不開始呼叫 API。")
        return False
    try:
        return input(prompt).strip().lower() == "yes"
    except EOFError:  # Windows 的 NUL 也會被視為 tty；讀不到輸入一律視為取消
        return False


@locked_command
def cmd_run(ctx, args, ids: list[str], client_factory=None) -> int:
    if not args.tier:
        print("--run 必須指定 --tier free 或 --tier paid。")
        return 2
    problems = mode_preflight("run", ctx.out_dir, ctx.bank_path)  # 鎖內重新檢查
    if problems:
        print("--run 安全檢查未通過，未呼叫 API：\n  " + "\n  ".join(problems))
        return 2
    packet_dir = ctx.out_dir / "gemini_judge" / "packets"
    plan = []
    for qid in ids:
        path = latest_packet(packet_dir, qid)
        if not path:
            print(f"{qid} 沒有證據包，請先執行 dry-run。")
            return 2
        packet, sha, ok = load_packet(path)
        if not ok:
            print(f"{qid} 的證據包雜湊不符（{path.name}），拒絕送出。請重新 dry-run。")
            return 2
        if packet["bank_sha256"] != ctx.bank_sha or packet["bank_answer"]["value"] != va.answer_of(ctx.units[qid]):
            print(f"{qid} 的題庫已在 dry-run 後變更，拒絕送出。請重新 dry-run。")
            return 2
        plan.append((qid, path, packet, sha))
    # 已有一致成功紀錄的題目預設跳過（--force 才重新判定）；無法讀取既有紀錄時保守停止
    results_path = ctx.out_dir / "gemini_judgments.json"
    try:
        existing = load_results(results_path)
    except (OSError, UnicodeDecodeError, ValueError) as err:  # json.JSONDecodeError 為 ValueError 子類別
        print(f"無法讀取既有判讀紀錄 {results_path}（{type(err).__name__}），無法判斷哪些題目已完成，未呼叫 API。")
        return 2
    todo, skipped = [], []
    for item in plan:
        prev = None if args.force else completed_run((existing["questions"].get(item[0]) or {}).get("runs"),
                                                     item[3], args.model)
        (skipped if prev else todo).append((item, prev))
    for (qid, *_), prev in skipped:
        print(f"  略過 {qid}：已有一致的成功紀錄（{prev.get('at')}，{PROMPT_VERSION}／{EVALUATOR_VERSION}）；如需重新判定請加 --force")
    if not todo:
        print("指定的題目都已有一致的成功紀錄，未建立 API client、未呼叫 API、未新增紀錄。")
        return 0
    # 「題目已完成判讀」（上方）與「證據包可再次送出」是不同的判斷：要送出的題目一律需要目前版本的證據包
    reuse = [p for (qid, _, packet, _), _ in todo for p in packet_reuse_problems(packet, ctx, qid)]
    if reuse:
        print("證據包不可送出，整批未呼叫 API、未建立 API client：\n  " + "\n  ".join(reuse))
        return 2
    # M-B：送出任何請求之前，以與最終推薦相同的正式資格（formal_consensus：community_qualification、答案格式、
    # 快照信任與證據包版本）判斷；達門檻者直接採用社群答案，不建立 client、不呼叫 Gemini
    community, plan = [], []
    for item, _ in todo:
        qid, _, packet, _ = item
        fc = formal_consensus(packet, ctx.units[qid])
        if fc["qualifies"]:
            community.append((item, fc))
        else:
            plan.append(item)
    pending = [u for u in unfinished_attempts(ctx.out_dir, existing) if u.get("question_id") in {q for q, *_ in plan}]
    for u in pending:
        print(f"  ⚠ {u['question_id']} 有未完成的請求紀錄（{u.get('started_at')}，{u['file']}）：先前的請求可能已送出、"
              "可能已計費，但結果未寫入。本程式不自動重試；本次送出會是另一次請求。")
    price, price_note = eq.load_price(args.model)
    total_in = sum(estimate_input_tokens(p) for _, _, p, _ in plan)
    cost = eq.cost_usd(price, total_in, args.assume_output_tokens * len(plan))
    print("=" * 64)
    print(f"Gemini 判讀計畫｜模型 {args.model}｜{args.tier}｜{len(plan)} 題 = {len(plan)} 次請求（不重試）"
          f"｜社群採用 {len(community)} 題（不呼叫 API）")
    for (qid, path, _, sha), fc in community:
        print(f"  = {qid}  {path.name}  {sha[:12]}…｜社群共識 {fc['share_exact']:.4g}%／{fc['total_votes']} 票，直接採用，不呼叫 API")
    for qid, path, _, sha in plan:
        print(f"  - {qid}  {path.name}  {sha[:12]}…")
    if plan:
        print(f"  預估輸入約 {total_in:,} token；付費層估計 {'—' if cost is None else f'約 US${cost:.4f}'}；{price_note}")
        if args.tier == "free":
            print("  ※ 免費層：不計費，但送出的題目與證據可能被用於改善 Google 產品。")
        if not confirm("確認送出以上證據包？輸入 yes 繼續："):
            print("已取消，未呼叫 Gemini API，也未寫入社群採用紀錄。")
            return 2
    backup_dir = ctx.out_dir / "backups"
    session_backed_up = False  # 每次 --run 只在第一次實際寫入前備份一次既有結果檔
    for (qid, path, packet, sha), fc in community:
        final = final_recommendation(ctx.units[qid], packet, None, "not_called", None)
        if final.get("source") != "community":  # 防禦：與 formal_consensus 應一致；不一致即停止，不寫入、不送出
            print(f"{qid}：社群資格判斷與最終推薦不一致，已停止，未寫入、未呼叫 API。")
            return 2
        run = {"at": va.now_iso(), "kind": COMMUNITY_KIND, "api_called": False, "attempt_id": None,
               "model": None, "tier": None, "prompt_version": None, "evaluator_version": EVALUATOR_VERSION,
               "packet_file": path.name, "packet_sha256": sha, "packet_version": packet.get("packet_version"),
               "packet_rules_version": packet.get("packet_rules_version"), "packet_summary": packet_summary(packet),
               "documents": [{k: d[k] for k in ("doc_id", "title", "url", "origin", "retrieved_at")}
                             for d in packet["microsoft_learn"]["documents"]],
               "status": COMMUNITY_KIND, "http_status": None, "error": None,
               "usage": {"input_tokens": 0, "output_tokens": 0, "thoughts_tokens": 0, "total_tokens": 0},
               "model_output": None, "evaluation": None, "cost_usd_estimate": 0,
               "community_qualification": fc, "final_exam_recommendation": final}
        made = append_run(results_path, backup_dir, qid, run, backup=not session_backed_up)
        if not session_backed_up and made:
            print(f"  已備份既有結果檔（本次執行僅備份一次）：{made}")
        session_backed_up = True
        print(f"  {qid}：社群採用（未呼叫 API）｜最終推薦 {fmt(final['answer'])}（community）")
    if not plan:
        print(f"未建立 API client、未呼叫 API。結果：{results_path}（可用 --report 產生 Markdown）")
        return 0
    api_key = eq.load_api_key()
    if not api_key:
        print("找不到 GEMINI_API_KEY。")
        return 2
    client = (client_factory or (lambda k: eq.make_client(k, args.timeout)))(api_key)
    transient, stopped = 0, None
    for n, (qid, path, packet, sha) in enumerate(plan, start=1):
        if n > 1 and args.min_interval > 0:
            time.sleep(args.min_interval)
        attempt_id = record_attempt(ctx.out_dir, qid, path.name, sha, args.model, args.tier)  # 失敗即拋出，不送出請求
        out = call_once(client, args, packet, api_key)
        run = {"at": va.now_iso(), "kind": "api_call", "api_called": True, "attempt_id": attempt_id,
               "model": args.model, "tier": args.tier, "prompt_version": PROMPT_VERSION,
               "packet_file": path.name, "packet_sha256": sha, "packet_summary": packet_summary(packet),
               "documents": [{k: d[k] for k in ("doc_id", "title", "url", "origin", "retrieved_at")}
                             for d in packet["microsoft_learn"]["documents"]],
               "status": out["status"], "http_status": out["http_status"], "error": out["error"],
               "usage": out["usage"], "model_output": out["result"], "evaluation": None,
               "cost_usd_estimate": eq.cost_usd(price, out["usage"]["input_tokens"],
                                                out["usage"]["output_tokens"] + out["usage"]["thoughts_tokens"])}
        if out["status"] == "done":
            run["evaluation"] = evaluate(ctx.units[qid], packet, out["result"])
            if run["evaluation"]["status"] != "evaluated":
                run["status"] = run["evaluation"]["status"]
        elif out["status"] in ("unavailable", "error"):
            run["status"] = "deferred"
        run["final_exam_recommendation"] = final_recommendation(ctx.units[qid], packet, run["evaluation"],
                                                                run["status"], out["result"])
        made = append_run(results_path, backup_dir, qid, run, backup=not session_backed_up)
        if not session_backed_up and made:
            print(f"  已備份既有結果檔（本次執行僅備份一次）：{made}")
        session_backed_up = True
        conf = (run["evaluation"] or {}).get("confidence")
        fin = run["final_exam_recommendation"]
        print(f"  [{n}/{len(plan)}] {qid}：{run['status']}" + (f"｜官方證據 {conf}" if conf else "")
              + f"｜最終推薦 {fmt(fin['answer'])}（{fin['source']}）"
              + (f"｜{run['error']}" if run["error"] else "")
              + f"｜in {out['usage']['input_tokens']:,} / out {out['usage']['output_tokens']:,}"
                f" / think {out['usage']['thoughts_tokens']:,}")
        transient = transient + 1 if run["status"] == "deferred" else 0
        if out["status"] == "rate_limited":
            stopped = "429 速率或配額限制（免費層每日額度於太平洋時間午夜重置）"
        elif out["status"] == "fatal":
            stopped = f"HTTP {out['http_status']}：請檢查 API Key、權限或模型名稱"
        elif transient >= MAX_CONSECUTIVE_TRANSIENT:
            stopped = f"連續 {transient} 次服務暫時不可用或逾時"
        if stopped:
            rest = [q for q, *_ in plan[n:]]
            print(f"已停止：{stopped}。" + (f"未送出：{', '.join(rest)}" if rest else ""))
            break
    print(f"結果：{results_path}（可用 --report 產生 Markdown）")
    return 1 if stopped else 0


def source_run(runs: list[dict]) -> tuple[int, dict] | None:
    """最近一筆真正呼叫 API 且有模型輸出的紀錄（舊紀錄沒有 kind 欄位，視為 API 呼叫）。"""
    for i in range(len(runs) - 1, -1, -1):
        r = runs[i]
        if r.get("api_called", True) and r.get("model_output") is not None:
            return i, r
    return None


@locked_command
def cmd_reevaluate(ctx, ids: list[str]) -> int:
    """以已保存的模型輸出離線重新核對：不呼叫 API、不修改原始輸出，結果另存為新的一筆紀錄。
    舊版或版本不明的證據包仍可重核，但其社群資料視為未驗證（不取得正式共識資格）。"""
    problems = mode_preflight("reevaluate", ctx.out_dir, ctx.bank_path)  # 鎖內重新檢查
    if problems:
        print("--reevaluate 安全檢查未通過，未寫入任何檔案：\n  " + "\n  ".join(problems))
        return 2
    path = ctx.out_dir / "gemini_judgments.json"
    results = load_results(path)
    packet_dir = ctx.out_dir / "gemini_judge" / "packets"
    for qid in ids:
        found = source_run((results["questions"].get(qid) or {}).get("runs") or [])
        if not found:
            print(f"{qid}：沒有可重新核對的模型輸出。")
            return 2
        idx, src = found
        pfile = packet_dir / src["packet_file"]
        if not pfile.exists():
            print(f"{qid}：找不到原始證據包 {src['packet_file']}，停止。")
            return 2
        packet, sha, ok = load_packet(pfile)
        if not ok or sha != src["packet_sha256"]:
            print(f"{qid}：證據包雜湊與原紀錄不符，停止。")
            return 2
        ev = evaluate(ctx.units[qid], packet, src["model_output"])
        final = final_recommendation(ctx.units[qid], packet, ev, ev["status"], src["model_output"])
        run = {"at": va.now_iso(), "kind": "offline_reevaluation", "api_called": False,
               "source_run_index": idx, "source_run_at": src["at"], "model": src["model"], "tier": src["tier"],
               "prompt_version": src["prompt_version"], "packet_file": src["packet_file"], "packet_sha256": sha,
               "packet_summary": packet_summary(packet), "documents": src.get("documents"),
               "status": ev["status"], "http_status": None, "error": None,
               "usage": {"input_tokens": 0, "output_tokens": 0, "thoughts_tokens": 0, "total_tokens": 0},
               "model_output": None, "model_output_ref": f"runs[{idx}]", "evaluation": ev, "cost_usd_estimate": 0,
               "final_exam_recommendation": final}
        append_run(path, ctx.out_dir / "backups", qid, run)
        oe = ev.get("official_evidence_assessment") or {}
        print(f"{qid}：離線重核（來源 runs[{idx}]，未呼叫 API）｜解析 {ev['answer_parse']['method'] or '失敗'}"
              f"｜Gemini 建議 {fmt(ev['suggested_answer'])}｜官方證據 {oe.get('confidence') or '—'}"
              f"｜完整性 {ev['completeness']}｜最終推薦 {fmt(final['answer'])}（{final['source']}）")
    print(f"結果：{path}")
    return 0


@locked_command
def cmd_report(ctx) -> int:
    """產生 Markdown 報告。既有報告一律不覆寫（沒有安全確認機制，因此不提供覆寫選項）。"""
    problems = mode_preflight("report", ctx.out_dir, ctx.bank_path)  # 鎖內重新檢查
    if problems:
        print("--report 安全檢查未通過，未寫入任何檔案：\n  " + "\n  ".join(problems))
        return 2
    results = load_results(ctx.out_dir / "gemini_judgments.json")
    if not results["questions"]:
        print("尚無判讀結果。")
        return 0
    path = ctx.out_dir / "gemini_judgments.md"
    write_new_file(path, render_report(results, unfinished_attempts(ctx.out_dir, results)))
    print(f"已寫入：{path}")
    return 0


def _norm_path(p: Path) -> str:
    """解析為絕對路徑（處理相對路徑、..、符號連結）並依 Windows 規則統一大小寫與分隔符號，供路徑比較。"""
    return os.path.normcase(os.path.normpath(str(Path(p).expanduser().resolve())))


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def run_preflight(output_dir: Path | None) -> list[str]:
    """--run 的安全前置檢查（在讀取資料與任何 API 呼叫之前執行）：
    A. 必須明確指定 --output-dir，且不得是正式輸出目錄或其保留子目錄（gemini_judge/、backups/）。
    B. 輸出目錄內必須已有有效的 verification.json（社群證據快照）；不自動複製、不修改。"""
    if output_dir is None:
        return ["--run 必須以 --output-dir 明確指定獨立的測試目錄（例如 output/gemini_trialN-日期），"
                "不會使用預設的正式 output/。"]
    target = _norm_path(output_dir)
    official = _norm_path(va.DEFAULT_OUT)
    reserved = [official] + [_norm_path(va.DEFAULT_OUT / name) for name in ("gemini_judge", "backups")]
    if target == official or any(_within(target, r) for r in reserved[1:]):
        return [f"--output-dir 指向正式輸出位置（{output_dir} → {target}）；請改用獨立的測試目錄，"
                "例如 output/gemini_trialN-日期。"]
    snap = Path(output_dir) / "verification.json"
    if not snap.is_file():
        return [f"測試目錄缺少社群證據快照 {snap}；請先把 verification.json 的唯讀快照放入測試目錄。"
                "程式不會自動複製，也不會把題目當成沒有社群資料繼續執行。"]
    try:
        data = json.loads(snap.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as err:
        return [f"無法讀取社群證據快照 {snap}：{type(err).__name__}"]
    except json.JSONDecodeError as err:
        return [f"社群證據快照 {snap} 不是有效的 JSON（第 {err.lineno} 行）"]
    if not isinstance(data, dict) or not isinstance(data.get("questions"), dict):
        return [f"社群證據快照 {snap} 格式不符：缺少 questions 物件"]
    return []


OFFICIAL_RESERVED_SUBDIRS = ("gemini_judge", "backups")
SNAPSHOT_HELP = ("請自行準備快照：確認正式 output/verification.json 與目前題庫一致後，手動複製到獨立工作區並比對 SHA256；"
                 "程式不會自動複製或修改快照。")


def official_path_problems(output_dir: Path, label: str) -> list[str]:
    """路徑解析（含相對路徑、..、大小寫、已存在的符號連結／junction）後不得是正式 output/、其 gemini_judge/
    或 backups/（含其下任何位置）；無法可靠解析時 fail-closed。"""
    try:
        target = _norm_path(output_dir)
        official = _norm_path(va.DEFAULT_OUT)
        reserved = [_norm_path(va.DEFAULT_OUT / name) for name in OFFICIAL_RESERVED_SUBDIRS]
    except (OSError, RuntimeError, ValueError) as err:
        return [f"無法可靠解析輸出路徑（{type(err).__name__}），為安全起見拒絕執行。"]
    if target == official or any(_within(target, r) for r in reserved):
        return [f"--output-dir 解析後指向正式工作區（{output_dir} → {target}）；{label} 不得寫入正式 output/、"
                "其 gemini_judge/ 或 backups/，請改用獨立工作區。"]
    return []


# ---- F-02：工作區必須位於已確認受 .gitignore 排除的位置（只讀取 Git 狀態，不修改索引）------------------
GIT_EXE = "git"
GIT_TIMEOUT = 20
GIT_HELP = "試驗輸出只能放在已確認受 .gitignore 排除的目錄（例如 output/ 底下的獨立子目錄）。"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # GIT_OPTIONAL_LOCKS=0：避免 git 在唯讀查詢時順手更新索引；check-ignore / rev-parse / ls-files 都不寫入索引
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    return subprocess.run([GIT_EXE, "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=GIT_TIMEOUT, env=env, stdin=subprocess.DEVNULL)


def git_ignore_problems(output_dir: Path) -> list[str]:
    """確認工作區位於 Git 儲存庫內、受 .gitignore 排除，且其中沒有已受追蹤的檔案。無法確認一律拒絕。"""
    try:
        target = Path(output_dir).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as err:
        return [f"無法可靠解析輸出路徑（{type(err).__name__}），為安全起見拒絕執行。"]
    anchor = target
    while not anchor.is_dir():
        if anchor.parent == anchor:
            return [f"找不到工作區或其上層目錄，無法確認 .gitignore 排除狀態，拒絕執行。{GIT_HELP}"]
        anchor = anchor.parent
    try:
        top = _git(["rev-parse", "--show-toplevel"], anchor)
        if top.returncode != 0 or not top.stdout.strip():
            return [f"工作區不在 Git 儲存庫內，無法確認受 .gitignore 排除，拒絕執行。{GIT_HELP}"]
        root = Path(top.stdout.strip()).resolve()
        try:
            rel = target.relative_to(root)
        except ValueError:
            return [f"工作區解析後不在 Git 儲存庫的工作目錄內（{target}），無法確認受 .gitignore 排除，拒絕執行。{GIT_HELP}"]
        if rel == Path("."):
            return [f"工作區是 Git 儲存庫根目錄，不是受 .gitignore 排除的目錄，拒絕執行。{GIT_HELP}"]
        rel_posix = rel.as_posix()
        ign = _git(["check-ignore", "-q", "--", rel_posix + "/"], root)
        tracked = _git(["ls-files", "-z", "--", rel_posix], root)
    except (OSError, subprocess.SubprocessError) as err:
        return [f"無法執行 git 確認 .gitignore 排除狀態（{type(err).__name__}），拒絕執行。{GIT_HELP}"]
    if ign.returncode == 1:
        return [f"工作區 {rel_posix}/ 未受 .gitignore 排除（輸出可能被誤加入版本控制），拒絕執行。{GIT_HELP}"]
    if ign.returncode != 0:
        return [f"無法確認 .gitignore 排除狀態（git check-ignore 結束碼 {ign.returncode}），拒絕執行。{GIT_HELP}"]
    if tracked.returncode != 0:
        return [f"無法確認工作區內是否有受 Git 追蹤的檔案（git ls-files 結束碼 {tracked.returncode}），拒絕執行。"]
    if tracked.stdout.strip("\0\r\n "):
        return [f"工作區 {rel_posix}/ 內有已受 Git 追蹤的檔案（.gitignore 對已追蹤檔案無效），拒絕執行。"]
    return []


# ---- F-03／M-A：dry-run 只接受空白工作區，或由本程式累積多批的工作區 ------------------------------------
SNAPSHOT_NAME = "verification.json"
EMPTY_WORKSPACE_ALLOWED = (SNAPSHOT_NAME, LOCK_NAME)
WORKSPACE_TOP_FILES = (SNAPSHOT_NAME, LOCK_NAME, "gemini_judgments.json", "gemini_judgments.md")
_STAMP = r"\d{8}-\d{6}-\d{6}"                                   # stamp() 的格式
PACKET_FILE = re.compile(rf"^[^.\\/]+\.{_STAMP}\.json$")         # save_packet()：<題號>.<時間>.json
ATTEMPT_FILE = re.compile(rf"^[^.\\/]+\.{_STAMP}\.[0-9a-f]{{8}}\.json$")  # record_attempt()：<題號>.<時間>.<8 碼>.json
BACKUP_FILE = re.compile(rf"^gemini_judgments\.{_STAMP}\.json$")  # va.backup_file()：結果檔備份
EMPTY_WORKSPACE_RULE = (
    "可接受的工作區＝空白工作區（只有一般檔案 verification.json 快照，以及可能殘留的工作區鎖檔 "
    f"{LOCK_NAME}），或由本程式累積多批的工作區：另外只能有 gemini_judgments.json（可解析的結果檔）、"
    "gemini_judgments.md、gemini_judge/packets/<題號>.<時間>.json、gemini_judge/attempts/<題號>.<時間>.<8 碼>.json、"
    "backups/gemini_judgments.<時間>.json；其他任何檔案、目錄、暫存檔、未完成的寫入或連結一律拒絕")


def _plain_file(entry) -> bool:
    return entry.is_file(follow_symlinks=False)


def _plain_dir(entry) -> bool:
    return entry.is_dir(follow_symlinks=False) and not entry.is_symlink()


def workspace_entry_problems(output_dir: Path) -> list[str]:
    """dry-run 的工作區內容檢查（白名單，見 EMPTY_WORKSPACE_RULE）。只列出目錄內容；不清空、不搬移、不複製。
    累積工作區中既有證據包的一致性另由 accumulated_packet_problems() 檢查。
    工作區不存在時不在此回報（由快照檢查回報缺少 verification.json）。"""
    d = Path(output_dir)
    if not d.exists():
        return []
    if not d.is_dir():
        return [f"--output-dir 不是目錄（{d}）"]
    bad = []

    def scan_files(sub, prefix: str, pattern) -> None:
        for x in sorted(os.scandir(sub), key=lambda y: y.name):
            if not (_plain_file(x) and pattern.match(x.name)):
                bad.append(prefix + x.name + ("/" if x.is_dir(follow_symlinks=False) else ""))

    for entry in sorted(os.scandir(d), key=lambda x: x.name):
        if entry.name in WORKSPACE_TOP_FILES and _plain_file(entry):
            continue
        if entry.name == "gemini_judge" and _plain_dir(entry):
            for sub in sorted(os.scandir(entry.path), key=lambda y: y.name):
                if sub.name == "packets" and _plain_dir(sub):
                    scan_files(sub.path, "gemini_judge/packets/", PACKET_FILE)
                elif sub.name == "attempts" and _plain_dir(sub):
                    scan_files(sub.path, "gemini_judge/attempts/", ATTEMPT_FILE)
                else:
                    bad.append("gemini_judge/" + sub.name + ("/" if sub.is_dir(follow_symlinks=False) else ""))
            continue
        if entry.name == "backups" and _plain_dir(entry):
            scan_files(entry.path, "backups/", BACKUP_FILE)
            continue
        bad.append(entry.name + ("/" if entry.is_dir(follow_symlinks=False) else ""))
    results = d / "gemini_judgments.json"
    if results.is_file() and not results.is_symlink() and "gemini_judgments.json" not in bad:
        try:
            data = json.loads(results.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("meta"), dict) or not isinstance(data.get("questions"), dict):
                raise ValueError("缺少 meta 或 questions 物件")
        except (OSError, UnicodeDecodeError, ValueError):
            bad.append("gemini_judgments.json（格式不符）")
    if bad:
        shown = "、".join(bad[:8]) + ("…" if len(bad) > 8 else "")
        return [f"工作區不是空白工作區，且含有本程式不會建立或格式不符的項目（{shown}）；新批次不得混入來源不明的資料。"
                f"{EMPTY_WORKSPACE_RULE}。請改用新的空白工作區；程式不會清空、搬移或複製任何資料。"]
    return []


def accumulated_packet_problems(output_dir: Path, snapshot_sha: str, bank_sha: str) -> list[str]:
    """累積工作區（M-A）：既有證據包必須全部可驗證、為目前版本，且建立時使用的 verification.json 快照與題庫
    都與本批相同。任何不一致即拒絕繼續使用此工作區——不自動更新快照、不修改或取代既有證據包。
    （舊 trial 工作區的 v1 證據包會在此被拒絕，不會被自動轉換成可重用工作區。）"""
    pdir = Path(output_dir) / "gemini_judge" / "packets"
    if not pdir.is_dir():
        return []
    probs = []
    for f in sorted(pdir.glob("*.json")):
        try:
            packet, _, ok = load_packet(f)
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as err:
            probs.append(f"{f.name}：無法讀取（{type(err).__name__}）")
            continue
        gen = packet_generation(packet) if isinstance(packet, dict) else "unknown"
        if not ok:
            probs.append(f"{f.name}：雜湊不符")
        elif gen != "current":
            probs.append(f"{f.name}：證據包為{GENERATION_LABEL[gen]}（packet_version={packet.get('packet_version')}）")
        else:
            if f.name.split(".", 1)[0] != packet.get("question_id"):
                probs.append(f"{f.name}：檔名與證據包題號不符")
            if packet.get("snapshot_sha256") != snapshot_sha:
                probs.append(f"{f.name}：建立時使用的 verification.json 快照與目前快照不同")
            if packet.get("bank_sha256") != bank_sha:
                probs.append(f"{f.name}：建立時的題庫與目前題庫不同")
    if probs:
        shown = "；".join(probs[:6]) + ("…" if len(probs) > 6 else "")
        return [f"既有累積工作區的證據包與本批條件不一致，拒絕繼續使用此工作區（不自動更新快照、不修改既有證據包）：{shown}。"
                "請改用新的空白工作區。"]
    return []


def accumulated_trust_levels(output_dir: Path) -> set[str]:
    """累積工作區中既有證據包的快照信任等級（只在 accumulated_packet_problems() 通過後使用）。"""
    pdir = Path(output_dir) / "gemini_judge" / "packets"
    levels = set()
    for f in sorted(pdir.glob("*.json")) if pdir.is_dir() else []:
        packet, _, _ = load_packet(f)
        levels.add((packet.get("snapshot_trust") or {}).get("level"))
    return levels


# ---- F-08：verification.json 快照的三層信任：格式有效、來源一致、內容可信 -------------------------------


def load_snapshot_checked(output_dir: Path, bank_sha: str) -> tuple[list[str], dict | None]:
    """讀取工作區快照一次（同一份位元組用於驗證、雜湊與後續使用）。第一層：格式有效（JSON 物件、questions 與 meta
    物件、schema_version 相符）；第二層：來源一致（meta.bank_sha256 與目前題庫相同）。任一不符即拒絕。"""
    snap = Path(output_dir) / SNAPSHOT_NAME
    if snap.is_symlink():
        return [f"verification.json 快照不可是符號連結（{snap}）。{SNAPSHOT_HELP}"], None
    if not snap.is_file():
        return [f"獨立工作區缺少 verification.json 快照（{snap}）。{SNAPSHOT_HELP}"], None
    try:
        raw = snap.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError) as err:
        return [f"無法讀取 verification.json 快照（{type(err).__name__}）。{SNAPSHOT_HELP}"], None
    except json.JSONDecodeError as err:
        return [f"verification.json 快照不是有效的 JSON（第 {err.lineno} 行）。{SNAPSHOT_HELP}"], None
    if not isinstance(data, dict) or not isinstance(data.get("questions"), dict) or not isinstance(data.get("meta"), dict):
        return [f"verification.json 快照缺少必要結構（questions 物件與 meta 物件）。{SNAPSHOT_HELP}"], None
    meta = data["meta"]
    if meta.get("schema_version") != va.SCHEMA_VERSION:
        return [f"verification.json 快照的 schema_version（{meta.get('schema_version')}）與目前版本（{va.SCHEMA_VERSION}）不一致。"
                f"{SNAPSHOT_HELP}"], None
    if meta.get("bank_sha256") != bank_sha:
        return [f"verification.json 快照的題庫雜湊（meta.bank_sha256）與目前題庫不一致或缺少。{SNAPSHOT_HELP}"], None
    return [], {"sha256": hashlib.sha256(raw).hexdigest(), "schema_version": meta["schema_version"], "data": data}


def official_baseline() -> tuple[Path | None, list[str]]:
    """正式基準 output/verification.json 必須位於受信任的位置：設定的正式目錄中的一般檔案，路徑解析後不經
    符號連結／junction 轉向。不符合時不比對（不硬編碼雜湊、不複製、不修改任何檔案）。"""
    base = Path(va.DEFAULT_OUT) / SNAPSHOT_NAME
    try:
        expected = os.path.normcase(os.path.normpath(os.path.abspath(base)))
        actual = _norm_path(base)
    except (OSError, RuntimeError, ValueError) as err:
        return None, [f"無法解析正式基準路徑（{type(err).__name__}）"]
    if base.is_symlink() or (base.exists() and not base.is_file()):
        return None, ["正式基準 verification.json 不是一般檔案，位置不受信任"]
    if not base.is_file():
        return None, ["找不到正式基準 output/verification.json，無法比對快照內容"]
    if actual != expected:
        return None, ["正式基準的路徑經過符號連結或 junction 轉向，位置不受信任"]
    return base, []


def snapshot_provenance(snap: dict, bank_sha: str) -> dict:
    """第三層：內容可信＝工作區快照與受信任位置的正式基準位元組完全相同。不一致或無法比對時標示為 unverified
    （fail-closed：社群答案不取得正式共識資格），不拒絕 dry-run，也不自動複製或覆寫快照。"""
    base, reasons = official_baseline()
    content_trusted, base_sha = None, None
    if base is not None:
        try:
            base_sha = hashlib.sha256(base.read_bytes()).hexdigest()
        except OSError as err:
            reasons.append(f"無法讀取正式基準（{type(err).__name__}）")
        else:
            content_trusted = base_sha == snap["sha256"]
            if not content_trusted:
                reasons.append("快照內容與正式基準 output/verification.json 不一致（快照可能過時，或正式基準已更新）")
    return {"snapshot_sha256": snap["sha256"], "snapshot_schema_version": snap["schema_version"],
            "snapshot_trust": {"rules_version": SNAPSHOT_TRUST_VERSION,
                               "level": "verified" if content_trusted is True else "unverified",
                               "format_valid": True, "source_consistent": True, "content_trusted": content_trusted,
                               "baseline": {"location_trusted": base is not None, "sha256": base_sha},
                               "reasons": reasons,
                               "note": "verified 只代表快照與正式基準的位元組相同，不代表 ExamTopics 社群資料本身正確"}}


def dry_run_preflight(output_dir: Path | None, bank_path: Path) -> list[str]:
    """dry-run 的安全前置檢查（在建立目錄、寫入與任何網路請求之前執行；唯讀）：
    A. 必須明確指定 --output-dir，且不得是正式 output/、其 gemini_judge/ 或 backups/（official_path_problems）。
    B. 工作區必須位於已確認受 .gitignore 排除的位置（git_ignore_problems）。
    C. 工作區必須是空白工作區，或由本程式累積多批的工作區（workspace_entry_problems，白名單）。
    D. verification.json 快照格式有效、來源一致（load_snapshot_checked）。
    E. 累積工作區：既有證據包全部為目前版本、雜湊正確，且快照與題庫都與本批相同（accumulated_packet_problems）。
    同一題已有證據包時由 dry_run_batch_problems() 拒絕，不覆寫。"""
    if output_dir is None:
        return ["dry-run 必須以 --output-dir 明確指定獨立工作區（例如 output/gemini_trialN-日期），不會使用預設的正式 output/。"]
    for check_fn in (lambda: official_path_problems(output_dir, "dry-run"), lambda: git_ignore_problems(output_dir),
                     lambda: workspace_entry_problems(output_dir)):
        problems = check_fn()
        if problems:
            return problems
    bank_sha = eq.sha256_file(bank_path)
    problems, snap = load_snapshot_checked(output_dir, bank_sha)
    if problems:
        return problems
    return accumulated_packet_problems(output_dir, snap["sha256"], bank_sha)


def mode_preflight(mode: str, output_dir: Path | None, bank_path: Path) -> list[str]:
    """各指令的唯讀前置檢查（main() 在讀取工作區資料前執行一次，各指令取得工作區鎖後再執行一次）。"""
    if mode == "dry_run":
        return dry_run_preflight(output_dir, bank_path)
    if mode == "run":
        return run_preflight(output_dir) or git_ignore_problems(output_dir)
    label = {"report": "--report", "reevaluate": "--reevaluate"}[mode]
    if output_dir is None:
        return [f"{label} 必須以 --output-dir 明確指定獨立工作區，不會使用預設的正式 output/。"]
    problems = official_path_problems(output_dir, label)
    if problems:
        return problems
    if not Path(output_dir).is_dir():
        return [f"工作區不存在或不是目錄（{output_dir}）；{label} 不建立目錄。"]
    problems = git_ignore_problems(output_dir)
    if problems:
        return problems
    results = Path(output_dir) / "gemini_judgments.json"
    if results.is_symlink() or (results.exists() and not results.is_file()):
        return [f"結果檔 {results} 不是一般檔案，拒絕執行。"]
    if mode == "reevaluate" and not results.is_file():
        return [f"工作區沒有 gemini_judgments.json（{results}），沒有可重新核對的紀錄。"]
    if mode == "report":
        md = Path(output_dir) / "gemini_judgments.md"
        if md.exists() or md.is_symlink():
            return [f"報告 {md} 已存在；--report 不覆寫既有報告（沒有安全確認機制）。請先自行另存或移走既有報告後再執行。"]
    return []


# ---- F-12：社群證據與官方來源欄位的整批預檢 -----------------------------------------------------
MATCH_STATUSES = ("exact", "probable", "not_found", "not_searched")


def _is_count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_percent(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 100


def evidence_problems(qid: str, unit, e: dict) -> list[str]:
    """快照中本題 examtopics_evidence 的結構、票數、分布、百分比、match_status、社群答案與題型的相容性，
    以及官方來源欄位。只檢查、不補造或修改任何證據。"""
    p = []
    et = e.get("examtopics_evidence")
    if not isinstance(et, dict):
        return [f"{qid}：快照缺少 examtopics_evidence 物件"]
    ms = et.get("match_status")
    if not isinstance(ms, str) or ms not in MATCH_STATUSES:
        p.append(f"{qid}：match_status 不是有效值（{ms!r}；允許 {', '.join(MATCH_STATUSES)}）")
    cv = et.get("community_vote")
    if not isinstance(cv, dict):
        p.append(f"{qid}：community_vote 不是物件")
    else:
        ans, cnt, share, dist = cv.get("answer"), cv.get("vote_count"), cv.get("share_percent"), cv.get("distribution")
        if cnt is not None and not _is_count(cnt):
            p.append(f"{qid}：vote_count 不是非負整數（{cnt!r}）")
        if share is not None and not _is_percent(share):
            p.append(f"{qid}：share_percent 不是 0～100 的數字（{share!r}）")
        if dist is not None:
            if not isinstance(dist, list) or not dist or not all(isinstance(d, dict) for d in dist):
                p.append(f"{qid}：distribution 不是非空的物件陣列")
            else:
                bad = [d for d in dist if not _is_count(d.get("votes"))
                       or (d.get("percent") is not None and not _is_percent(d.get("percent")))
                       or not isinstance(d.get("answer"), (str, list))]
                if bad:
                    p.append(f"{qid}：distribution 有 {len(bad)} 筆的票數、百分比或答案格式不正確")
                elif cnt is not None and cnt != sum(d["votes"] for d in dist):
                    p.append(f"{qid}：vote_count（{cnt}）與 distribution 票數加總（{sum(d['votes'] for d in dist)}）不一致")
        if ans is not None:
            if ms != "exact":
                p.append(f"{qid}：match_status 為 {ms!r}（未確認同一題）卻記錄了社群答案")
            try:
                va.parse_answer(unit, json.dumps(ans))
            except (ValueError, KeyError, TypeError):
                p.append(f"{qid}：社群答案 {fmt(ans)} 與本題題型（{unit.type}）或選項不相容")
    for key in ("main_reasons", "counterarguments"):
        v = et.get(key)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            p.append(f"{qid}：examtopics_evidence.{key} 不是文字陣列")
    mev = e.get("microsoft_evidence")
    if mev is not None and not isinstance(mev, list):
        p.append(f"{qid}：microsoft_evidence 不是陣列")
    for i, x in enumerate(mev if isinstance(mev, list) else []):
        if not isinstance(x, dict):
            p.append(f"{qid}：microsoft_evidence[{i}] 不是物件")
            continue
        if not isinstance(x.get("url"), str) or not va.MS_URL.match(x["url"]):
            p.append(f"{qid}：microsoft_evidence[{i}].url 不是 https://learn.microsoft.com/ 文件網址")
        if x.get("verification") not in ("verified", "unverified"):
            p.append(f"{qid}：microsoft_evidence[{i}].verification 不是 verified／unverified")
        elif x["verification"] == "verified" and not (isinstance(x.get("excerpt"), str) and x["excerpt"].strip()):
            p.append(f"{qid}：microsoft_evidence[{i}] 標記為 verified 卻沒有原文摘錄")
        if x.get("supports") is not None and x.get("supports") not in va.KNOWN_SUPPORTS:
            p.append(f"{qid}：microsoft_evidence[{i}].supports 不是有效值（{x.get('supports')!r}）")
    cands = e.get("microsoft_candidates")
    if cands is not None and not isinstance(cands, list):
        p.append(f"{qid}：microsoft_candidates 不是陣列")
    for i, c in enumerate(cands if isinstance(cands, list) else []):
        if not isinstance(c, dict) or not isinstance(c.get("url"), str) or not c["url"].startswith("https://learn.microsoft.com/"):
            p.append(f"{qid}：microsoft_candidates[{i}] 缺少 https://learn.microsoft.com/ 網址")
    return p


def dry_run_batch_problems(ctx, ids: list[str]) -> list[str]:
    """整批 dry-run 開始前的題目層級檢查（任何外部請求、建立目錄或寫入之前）：題號重複、快照中該題資料與目前題庫
    不一致、社群證據與官方來源欄位格式（evidence_problems）、工作區已有該題證據包。任一題不通過即整批拒絕。
    快照缺少本題項目不算錯誤（標示為 entry_missing，社群資料狀態未知）。"""
    problems = []
    dup = sorted({q for q in ids if ids.count(q) > 1})
    if dup:
        problems.append(f"題號重複：{', '.join(dup)}")
    packet_dir = ctx.out_dir / "gemini_judge" / "packets"
    for qid in dict.fromkeys(ids):
        unit = ctx.units[qid]
        e = ctx.data["questions"].get(qid)
        if e is not None:
            if not isinstance(e, dict):
                problems.append(f"{qid}：快照中的驗證項目不是物件")
            elif e.get("question_id") != qid or e.get("type") != unit.type:
                problems.append(f"{qid}：快照中的題號或題型與目前題庫不一致")
            elif (e.get("bank_answer") or {}).get("value") != va.answer_of(unit):
                problems.append(f"{qid}：快照記錄的題庫答案與目前題庫不一致（快照可能過時）")
            else:
                problems += evidence_problems(qid, unit, e)
        if packet_dir.is_dir():
            existing = sorted(p.name for p in packet_dir.glob(f"{qid}.*.json"))
            if existing:
                problems.append(f"{qid}：工作區已有證據包（{', '.join(existing)}），不再產生新證據包以免取代既有證據包；"
                                "如需重新產生，請使用新的獨立工作區")
    return problems


def main(argv=None, client_factory=None, search=None, fetch=None) -> int:
    eq.setup_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", type=Path, default=va.DEFAULT_BANK)
    ap.add_argument("--output-dir", type=Path, default=None,
                    help="獨立工作區（所有指令必填；不得是正式 output/，必須受 .gitignore 排除；dry-run 需為只含 "
                         "verification.json 快照的空白工作區）")
    ap.add_argument("--ids", help=f"逗號分隔的子題 ID（一次最多 {MAX_IDS_PER_RUN} 題）")
    ap.add_argument("--run", action="store_true", help="送出最近一次 dry-run 的證據包（需互動確認）")
    ap.add_argument("--report", action="store_true", help="由 gemini_judgments.json 產生 Markdown 報告")
    ap.add_argument("--reevaluate", action="store_true",
                    help="以已保存的模型輸出離線重新核對（不呼叫 API，結果另存一筆紀錄）")
    ap.add_argument("--tier", choices=["free", "paid"], help="--run 必填")
    ap.add_argument("--force", action="store_true",
                    help="--run 時重新判定已有一致成功紀錄的題目（預設會跳過，不呼叫 API）")
    ap.add_argument("--model", default=eq.DEFAULT_MODEL)
    ap.add_argument("--thinking-level", default="low")
    ap.add_argument("--max-output-tokens", type=int, default=8192)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--min-interval", type=float, default=0, help="每題間隔秒數（免費層建議 5 以上）")
    ap.add_argument("--assume-output-tokens", type=int, default=2500, help="估算用的每題輸出 token（含 thinking）")
    ap.add_argument("--delay", type=float, default=1.0, help="dry-run 時 Learn 請求間隔秒數")
    ap.add_argument("--doc-url", action="append", default=[],
                    help="dry-run 時加入指定的 Microsoft Learn 文件（可重複）；多題時寫成 題目ID=網址。"
                         "標記為 user_provided，不寫入 verification.json，也不代表答案已證實")
    args = ap.parse_args(argv)
    # 1. 純參數檢查（不讀取任何檔案）
    if args.force and not args.run:
        print("--force 只能搭配 --run 使用。")
        return 2
    if args.report and (args.run or args.reevaluate):
        print("--report 不可與 --run 或 --reevaluate 併用。")
        return 2
    mode = "report" if args.report else "reevaluate" if args.reevaluate else "run" if args.run else "dry_run"
    ids = [x.strip() for x in (args.ids or "").split(",") if x.strip()]
    if mode != "report":
        if not ids:
            print("請以 --ids 指定題目。")
            return 2
        if len(ids) > MAX_IDS_PER_RUN:
            print(f"一次最多 {MAX_IDS_PER_RUN} 題（收到 {len(ids)} 題）。")
            return 2
    if args.reevaluate and (args.run or args.doc_url):
        print("--reevaluate 不可與 --run 或 --doc-url 併用。")
        return 2
    doc_urls, problems = parse_doc_urls(args.doc_url, ids)
    if problems:
        print("--doc-url 有誤，未執行：\n  " + "\n  ".join(problems))
        return 2
    if doc_urls and args.run:
        print("--doc-url 只用於 dry-run；--run 一律送出 dry-run 時已確認的證據包。")
        return 2
    # 2. 唯讀前置檢查：讀取工作區資料、建立目錄或鎖檔、任何外部請求之前（取得工作區鎖後各指令會再檢查一次）
    problems = mode_preflight(mode, args.output_dir, args.bank)
    if problems:
        header = {"run": "--run 安全檢查未通過，未呼叫 API：",
                  "dry_run": "dry-run 安全檢查未通過，未發出任何請求、未建立任何目錄或檔案：",
                  "report": "--report 安全檢查未通過，未寫入任何檔案：",
                  "reevaluate": "--reevaluate 安全檢查未通過，未寫入任何檔案："}[mode]
        print(header + "\n  " + "\n  ".join(problems))
        return 2
    ctx = va.Ctx(args.bank.resolve(), args.output_dir.resolve())  # 唯讀
    unknown = [i for i in ids if i not in ctx.units]
    if unknown:
        print(f"題庫中找不到：{', '.join(unknown)}")
        return 2
    if mode == "dry_run":
        problems = dry_run_batch_problems(ctx, ids)  # 整批預檢（唯讀）；鎖內會再檢查一次
        if problems:
            print("dry-run 整批預檢未通過，未發出任何請求、未建立任何證據包：\n  " + "\n  ".join(problems))
            return 2
    # 3. 執行：各指令在工作區鎖內重新檢查後才寫入或發出請求
    try:
        if mode == "report":
            return cmd_report(ctx)
        if mode == "reevaluate":
            return cmd_reevaluate(ctx, ids)
        if mode == "run":
            return cmd_run(ctx, args, ids, client_factory)
        return cmd_dry_run(ctx, args, ids, search, fetch, doc_urls)
    except (OSError, ValueError) as err:  # FileExistsError 為 OSError 子類別；結果檔格式錯誤為 ValueError
        print(f"寫入失敗或資料無法讀取，已停止：{err}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
