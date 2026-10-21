// 每種題型的作答格式：
//   single_choice      -> "B"
//   multiple_choice    -> ["A", "C"]
//   dropdown_cloze     -> { select_0: "join", select_1: "SHA256" }
//   drag_drop_order    -> ["step1", "step3", "step2"]
//   statement_matrix   -> { s1: true, s2: false }
//   case_study         -> { [subQuestionId]: <子題作答> }

export const TYPE_LABELS = {
  single_choice: '單選題',
  multiple_choice: '複選題',
  dropdown_cloze: '下拉挖空題',
  drag_drop_order: '步驟排序題',
  statement_matrix: 'Yes / No 判斷題',
  case_study: '情境題組',
}

export const BLANK_PATTERN = /\{\{(\w+)\}\}/g

const sameSet = (a = [], b = []) =>
  a.length === b.length && a.every((x) => b.includes(x))

/** 回傳 { correct, earned, total, detail } */
export function gradeQuestion(q, ans) {
  switch (q.type) {
    case 'single_choice': {
      const ok = ans === q.answer
      return { correct: ok, earned: ok ? 1 : 0, total: 1 }
    }
    case 'multiple_choice': {
      const ok = sameSet(ans || [], q.answer)
      return { correct: ok, earned: ok ? 1 : 0, total: 1 }
    }
    case 'dropdown_cloze': {
      const detail = {}
      let earned = 0
      for (const b of q.blanks) {
        detail[b.id] = (ans || {})[b.id] === b.answer
        if (detail[b.id]) earned++
      }
      return { correct: earned === q.blanks.length, earned, total: q.blanks.length, detail }
    }
    case 'drag_drop_order': {
      const given = ans || []
      const detail = q.answer.map((id, i) => given[i] === id)
      const ok = given.length === q.answer.length && detail.every(Boolean)
      return { correct: ok, earned: ok ? 1 : 0, total: 1, detail }
    }
    case 'statement_matrix': {
      const detail = {}
      let earned = 0
      for (const s of q.statements) {
        detail[s.id] = (ans || {})[s.id] === s.answer
        if (detail[s.id]) earned++
      }
      return { correct: earned === q.statements.length, earned, total: q.statements.length, detail }
    }
    case 'case_study': {
      const detail = {}
      let earned = 0
      let total = 0
      for (const sub of q.questions) {
        const g = gradeQuestion(sub, (ans || {})[sub.id])
        detail[sub.id] = g
        earned += g.earned
        total += g.total
      }
      return { correct: earned === total, earned, total, detail }
    }
    default:
      return { correct: false, earned: 0, total: 1 }
  }
}

/** 依評分結果計算答對的題數（情境題組以子題計） */
export function correctUnitsOf(q, grade) {
  if (q.type !== 'case_study') return grade.correct ? 1 : 0
  return q.questions.filter((s) => grade.detail[s.id]?.correct).length
}

/** 是否已完整作答 */
export function isAnswered(q, ans) {
  if (ans == null) return false
  switch (q.type) {
    case 'single_choice':
      return !!ans
    case 'multiple_choice':
      return ans.length > 0
    case 'dropdown_cloze':
      return q.blanks.every((b) => !!ans[b.id])
    case 'drag_drop_order':
      return ans.length === q.answer.length
    case 'statement_matrix':
      return q.statements.every((s) => typeof ans[s.id] === 'boolean')
    case 'case_study':
      return q.questions.every((sub) => isAnswered(sub, ans[sub.id]))
    default:
      return false
  }
}

/** 是否作答了一部分（題目導覽列顯示用） */
export function isTouched(ans) {
  if (ans == null) return false
  if (Array.isArray(ans)) return ans.length > 0
  if (typeof ans === 'object') return Object.keys(ans).length > 0
  return !!ans
}

export function shuffle(arr) {
  const a = [...arr]
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    ;[a[i], a[j]] = [a[j], a[i]]
  }
  return a
}

/** 取題幹的純文字預覽 */
export function previewText(q, max = 110) {
  const raw = (q.type === 'case_study' ? `${q.caseInfo?.title ?? ''} — ${q.question ?? ''}` : q.question) || ''
  const text = raw.replace(BLANK_PATTERN, '____').replace(/[`*]/g, '').replace(/\s+/g, ' ').trim()
  return text.length > max ? text.slice(0, max) + '…' : text
}
