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

## localStorage 資料

| Key | 內容 |
| --- | --- |
| `sc200.history` | 每題的作答次數、答對次數、最近一次作答 |
| `sc200.mistakes` | 錯題本（答錯自動加入，在「錯題重練」答對自動移出） |
| `sc200.starred` | 收藏標記 |
| `sc200.progress` | 各章節已完成的題目 |
| `sc200.lastBank` | 上次選擇的題庫 |
