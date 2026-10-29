// AI 判讀版題庫（sc200.ai.json）解析區使用的顯示工具：答案轉成可讀文字、狀態標籤

export const AI_STATUS_LABELS = {
  agree: 'AI 判讀與原題庫答案一致',
  differ: 'AI 判讀與原題庫答案不同',
  community: '社群共識題（未呼叫 AI）',
  fallback: 'AI 輸出格式無效',
  deferred: 'AI 服務暫時不可用，未取得判讀',
  invalid_spec: '答案規格異常',
  stale: '判讀依據已過期',
  basis_missing: '缺少判讀依據',
  unavailable: '沒有可用的 AI 判讀',
}

/** 依題型把作答格式的答案轉成可讀文字（附選項內容）；無法轉換時回傳 JSON 文字 */
export function formatAnswer(q, ans) {
  if (ans == null) return '—'
  const optText = (id) => {
    const o = (q.options || q.items || []).find((x) => x.id === id)
    return o ? `${id}. ${o.text}` : String(id)
  }
  switch (q.type) {
    case 'single_choice':
      return optText(ans)
    case 'multiple_choice':
      return [...ans].sort().map(optText).join('；')
    case 'drag_drop_order':
      return ans.map((id, i) => `${i + 1}) ${optText(id)}`).join(' → ')
    case 'dropdown_cloze':
      return (q.blanks || []).map((b, i) => `第 ${i + 1} 格：${ans[b.id] ?? '—'}`).join('；')
    case 'statement_matrix': {
      const [yes, no] = q.columns || ['Yes', 'No']
      return (q.statements || []).map((s, i) => `敘述 ${i + 1}：${ans[s.id] === true ? yes : ans[s.id] === false ? no : '—'}`).join('；')
    }
    default:
      return JSON.stringify(ans)
  }
}
