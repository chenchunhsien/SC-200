import { shuffle } from './grading'
import { questionUnits } from './customBank'

/**
 * 隨機模擬測驗抽題：題數以子題計，情境題組整組抽取、不拆散。
 * 1. 將頂層題目均勻洗牌；題組與單題被抽中的機率相同，每個子題出現的機率因此一致
 * 2. 依序放入，放得下才放；放不下的題組先跳過，由後面的單題補滿
 * 3. 單題用完仍不足時，補入最小的跳過題組（允許微幅超過目標題數）
 * 回傳 { questions, total }；題庫不足目標題數時回傳全部題目
 */
export function drawMockExam(questions, target) {
  const picked = []
  const skipped = []
  let total = 0
  for (const q of shuffle(questions)) {
    if (total >= target) break
    const n = questionUnits(q)
    if (total + n <= target) {
      picked.push(q)
      total += n
    } else skipped.push(q)
  }
  skipped.sort((a, b) => questionUnits(a) - questionUnits(b))
  for (const q of skipped) {
    if (total >= target) break
    picked.push(q)
    total += questionUnits(q)
  }
  return { questions: picked, total }
}
