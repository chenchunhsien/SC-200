import { BLANK_PATTERN, TYPE_LABELS } from './grading'

// 與 public/data/questions.schema.json 對應的執行期檢查，確保匯入的題庫能被正確作答與計分
const isStr = (v) => typeof v === 'string' && v.length > 0
const LETTER = /^[A-Z]$/

function checkQuestion(q, path, errs, { sub = false } = {}) {
  if (!q || typeof q !== 'object') return errs.push(`${path}：不是物件`)
  const where = `${path}（${q.id ?? '無 id'}）`
  if (!isStr(q.id)) errs.push(`${path}：缺少 id`)
  if (typeof q.question !== 'string') errs.push(`${where}：缺少 question`)
  if (!TYPE_LABELS[q.type] || (sub && q.type === 'case_study')) return errs.push(`${where}：不支援的題型 ${q.type}`)

  switch (q.type) {
    case 'single_choice':
    case 'multiple_choice': {
      const ids = Array.isArray(q.options) ? q.options.map((o) => o?.id) : []
      if (ids.length < 2 || !ids.every((id) => LETTER.test(id ?? ''))) errs.push(`${where}：options 至少 2 個且 id 為 A–Z`)
      const ans = q.type === 'single_choice' ? [q.answer] : q.answer
      if (q.type === 'single_choice' ? !isStr(q.answer) : !Array.isArray(q.answer) || !q.answer.length)
        errs.push(`${where}：answer 格式錯誤`)
      else if (!ans.every((a) => ids.includes(a))) errs.push(`${where}：answer 不在 options 中`)
      break
    }
    case 'dropdown_cloze': {
      if (!isStr(q.template) || !Array.isArray(q.blanks)) {
        errs.push(`${where}：缺少 template 或 blanks`)
        break
      }
      const holes = [...q.template.matchAll(BLANK_PATTERN)].map((m) => m[1])
      const ids = q.blanks.map((b) => b?.id)
      if (holes.sort().join() !== [...ids].sort().join()) errs.push(`${where}：template 挖空與 blanks 不一致`)
      q.blanks.forEach((b) => {
        if (!Array.isArray(b?.options) || !b.options.includes(b.answer)) errs.push(`${where}：blank ${b?.id} 的 answer 不在 options 中`)
      })
      break
    }
    case 'drag_drop_order': {
      const ids = Array.isArray(q.items) ? q.items.map((i) => i?.id) : []
      if (ids.length < 2) errs.push(`${where}：items 至少 2 個`)
      if (!Array.isArray(q.answer) || !q.answer.length || !q.answer.every((a) => ids.includes(a)))
        errs.push(`${where}：answer 必須是 items 的 id`)
      break
    }
    case 'statement_matrix':
      if (!Array.isArray(q.statements) || !q.statements.length) errs.push(`${where}：缺少 statements`)
      else if (!q.statements.every((s) => isStr(s?.id) && typeof s.answer === 'boolean'))
        errs.push(`${where}：statements 的 answer 必須是 true / false`)
      break
    case 'case_study':
      if (!Array.isArray(q.caseInfo?.sections) || !q.caseInfo.sections.length) errs.push(`${where}：缺少 caseInfo.sections`)
      if (!Array.isArray(q.questions) || !q.questions.length) errs.push(`${where}：缺少子題`)
      else q.questions.forEach((s, i) => checkQuestion(s, `${path} 子題 ${i + 1}`, errs, { sub: true }))
      break
  }
}

/** 回傳錯誤訊息陣列；空陣列代表格式正確 */
export function validateBank(bank) {
  const errs = []
  if (!bank || typeof bank !== 'object' || Array.isArray(bank)) return ['檔案內容不是題庫物件']
  if (!Array.isArray(bank.questions) || !bank.questions.length) return ['缺少 questions 陣列或沒有任何題目']
  if (!Array.isArray(bank.chapters)) errs.push('缺少 chapters 陣列')
  const chapterIds = new Set((bank.chapters ?? []).map((c) => c?.id))
  const seen = new Set()
  bank.questions.forEach((q, i) => {
    checkQuestion(q, `第 ${i + 1} 題`, errs)
    if (q?.id && seen.has(q.id)) errs.push(`第 ${i + 1} 題：id ${q.id} 重複`)
    seen.add(q?.id)
    if (q?.chapter && !chapterIds.has(q.chapter)) errs.push(`第 ${i + 1} 題：chapter ${q.chapter} 不存在於 chapters`)
  })
  return errs
}
