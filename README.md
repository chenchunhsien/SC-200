# SC-200 題庫自測平台

Vite + React + Tailwind CSS + lucide-react 的純前端 SPA，部署於 GitHub Pages。

```bash
npm install
npm run dev      # 本機開發
npm run build    # 輸出至 dist/
```

推送到 `main` 分支後，`.github/workflows/deploy.yml` 會自動建置並部署。首次使用請到 repo 的 **Settings → Pages → Source** 選擇 **GitHub Actions**。

## 新增題庫

1. 在 `public/data/` 新增題庫 JSON（格式見下方）。
2. 在 `public/data/index.json` 的 `banks` 加入一筆 `{ "id", "title", "file" }`。題庫多於一個時，頁首會出現切換選單。

> 作答紀錄以 `題庫 id::題目 id` 作為 key，存在 localStorage。已發布的題目請勿更改 id。

## 題庫 JSON Schema

```jsonc
{
  "title": "題庫名稱",
  "description": "說明（選填）",
  "chapters": [{ "id": "ch1", "title": "章節名稱", "description": "選填" }],
  "questions": [ /* 題目 */ ]
}
```

### 共用欄位

| 欄位 | 說明 |
| --- | --- |
| `id` | 題庫內唯一 |
| `type` | 題型，見下表 |
| `chapter` | 對應 `chapters[].id`（題組子題不需要） |
| `question` | 題幹；支援 `` `code` ``、`**粗體**`、`- ` 清單與換行 |
| `code` | 選填，題幹下方的程式碼區塊 |
| `explanation` | 解析（格式同 `question`） |
| `references` | 選填，`[{ "title", "url" }]` |
| `difficulty` / `tags` | 選填 metadata |

### 各題型

| `type` | 專屬欄位 | `answer` 格式 |
| --- | --- | --- |
| `single_choice` | `options: [{ id, text }]` | `"B"` |
| `multiple_choice` | `options: [{ id, text }]` | `["A", "C"]`（數量即「請選 N 個」） |
| `dropdown_cloze` | `template`（用 `{{select_0}}` 挖空）、`templateType: "code" \| "text"`、`blanks: [{ id, options, answer }]` | 寫在各 `blanks[].answer` |
| `drag_drop_order` | `items: [{ id, text }]`（可含干擾項） | `["a", "b", "c"]`（正確順序，長度即需排列的步驟數） |
| `statement_matrix` | `statements: [{ id, text, answer: true/false }]`、選填 `columns: ["Yes", "No"]` | 寫在各 `statements[].answer` |
| `case_study` | `caseInfo: { title, sections: [{ title, content }] }`、`questions: [子題]` | 由各子題自行定義 |

完整範例見 [`public/data/mock_questions.json`](public/data/mock_questions.json)。

正式的 JSON Schema 在 [`public/data/questions.schema.json`](public/data/questions.schema.json)。題庫檔第一行加上 `"$schema": "./questions.schema.json"`，VS Code 就會即時檢查格式並提供自動完成。

## PDF 題庫轉換（scripts/parse_pdf.py）

```bash
pip install -r scripts/requirements.txt

# 1. 解析：標準選擇題直接寫入題庫，複雜題型寫入 *.drafts.json
python scripts/parse_pdf.py parse SC-200.pdf -o public/data/sc200.json --title "SC-200 題庫" --snapshots drafts_img/

# 2. 校正 public/data/sc200.drafts.json（手動或交給 LLM），完成的題目把 status 改成 "approved"

# 3. 合併：通過檢查的題目併入題庫，未通過的退回並附上 merge_errors
python scripts/parse_pdf.py merge public/data/sc200.json public/data/sc200.drafts.json
```

- **會被放進 draft 的情況**：HOTSPOT、DRAG DROP、Case Study、找不到選項或答案、答案字母不在選項中、題幹寫 "Choose two" 但只有一個答案。
- **每筆 draft 包含**：`reasons`（原因）、`suggested_type`（建議題型）、`raw_text`（原始文字）、`source.pages`（頁碼）、`snapshots`（頁面截圖），以及一個依建議題型預先填好的 `question` 骨架，需要補的欄位以 `TODO` / `null` 標示。
- **merge 前的檢查**：仍含 `TODO` 的題目會被擋下，另外也會檢查答案是否在選項中、挖空與 `blanks` 是否一致。
- **PDF 排版不同時**：先用 `--dump-text out.txt` 看擷取出來的文字，再調整腳本開頭的正規表示式。最後別忘了在 `public/data/index.json` 加入新的題庫。

## localStorage 資料

| Key | 內容 |
| --- | --- |
| `sc200.history` | 每題的作答次數、答對次數、最近一次作答 |
| `sc200.mistakes` | 錯題本（答錯自動加入，在「錯題重練」答對自動移出） |
| `sc200.starred` | 收藏標記 |
| `sc200.progress` | 各章節已完成的題目 |
| `sc200.lastBank` | 上次選擇的題庫 |
