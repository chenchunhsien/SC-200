import { shuffle } from './grading'
import { questionUnits } from './customBank'
import { bindSeries, seriesBlocks } from './series'

const blockUnits = (block) => block.reduce((n, q) => n + questionUnits(q), 0)

/**
 * 隨機模擬測驗抽題：題數以子題計，情境題組整組抽取、不拆散；系列題（series）整組抽取並連續出現。
 * 1. 將頂層題目（系列題視為一個區塊）均勻洗牌
 * 2. 依序放入，放得下才放；放不下的題組／系列先跳過，由後面的單題補滿
 * 3. 單題用完仍不足時，補入最小的跳過區塊（允許微幅超過目標題數）
 * 回傳 { questions, total }；題庫不足目標題數時回傳全部題目
 */
export function drawMockExam(questions, target) {
  const picked = []
  const skipped = []
  let total = 0
  for (const block of shuffle(seriesBlocks(questions))) {
    if (total >= target) break
    const n = blockUnits(block)
    if (total + n <= target) {
      picked.push(...block)
      total += n
    } else skipped.push(block)
  }
  skipped.sort((a, b) => blockUnits(a) - blockUnits(b))
  for (const block of skipped) {
    if (total >= target) break
    picked.push(...block)
    total += blockUnits(block)
  }
  return { questions: picked, total }
}

/**
 * 解析指定題號字串，例如 "1-30, 45, 60-70"（題號以子題計，依題庫順序從 1 開始）。
 * 分隔支援半形 / 全形逗號、頓號與空白；範圍支援 -、–、~，起訖顛倒時自動對調。
 * 回傳 { numbers: Set, outOfRange }；格式錯誤時回傳 { error }
 */
export function parseNumberRanges(text, max) {
  const parts = text
    .replace(/\s*[-–~～]\s*/g, '-')
    .split(/[,，、;\s]+/)
    .filter(Boolean)
  if (!parts.length) return { error: '請輸入題號，例如 1-30, 45, 60-70' }
  const numbers = new Set()
  let outOfRange = false
  for (const part of parts) {
    const m = part.match(/^(\d+)(?:-(\d+))?$/)
    if (!m) return { error: `無法辨識「${part}」，請使用 1-30, 45 這類格式` }
    const a = Number(m[1])
    const b = m[2] ? Number(m[2]) : a
    for (let n = Math.min(a, b); n <= Math.max(a, b); n++) {
      if (n >= 1 && n <= max) numbers.add(n)
      else outOfRange = true
    }
  }
  return { numbers, outOfRange }
}

/**
 * 依題號選題，維持題庫順序；情境題組只要有任一子題在範圍內就整組納入，
 * 系列題只要有任一題在範圍內就整個系列納入，並連續排列。
 * 回傳 { questions, total, added }，added 為因題組或系列完整性而額外補入的子題數
 */
export function pickByNumbers(questions, numbers) {
  const picked = []
  let next = 1
  let inRange = 0
  for (const q of questions) {
    const n = questionUnits(q)
    let hit = false
    for (let i = next; i < next + n && !hit; i++) hit = numbers.has(i)
    if (hit) {
      picked.push(q)
      for (let i = next; i < next + n; i++) if (numbers.has(i)) inRange++
    }
    next += n
  }
  const list = bindSeries(picked, questions, { expand: true })
  const total = list.reduce((n, q) => n + questionUnits(q), 0)
  return { questions: list, total, added: total - inRange }
}
