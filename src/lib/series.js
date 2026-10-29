import { shuffle } from './grading'

// 系列題：同一情境、各題提出不同 Solution 的題目，以題目的 series 欄位標記。
// 出題時同一系列綁在一起、連續出現；沒有 series 欄位的題庫行為不變。

/** 依系列把題目分成區塊（同系列的題目放在第一題的位置，依原順序；其他題目各自一個區塊） */
export function seriesBlocks(list) {
  const blocks = []
  const bySeries = new Map()
  for (const q of list) {
    if (!q.series) {
      blocks.push([q])
    } else if (bySeries.has(q.series)) {
      bySeries.get(q.series).push(q)
    } else {
      const block = [q]
      bySeries.set(q.series, block)
      blocks.push(block)
    }
  }
  return blocks
}

/**
 * 讓同系列題目相鄰；expand 為 true 時，補入 all（整份題庫）中同系列但不在 list 的題目（依題庫順序）。
 * 回傳新陣列，不修改輸入。
 */
export function bindSeries(list, all, { expand = false } = {}) {
  if (!expand) return seriesBlocks(list).flat()
  const wanted = new Set(list.filter((q) => q.series).map((q) => q.series))
  const have = new Set(list.map((q) => q.id))
  const extra = all.filter((q) => q.series && wanted.has(q.series) && !have.has(q.id))
  if (!extra.length) return seriesBlocks(list).flat()
  // 補入的題目接在後面，區塊仍位於 list 中第一個出現的位置；系列內依題庫順序排列
  const order = new Map(all.map((q, i) => [q.id, i]))
  return seriesBlocks([...list, ...extra])
    .map((b) => (b[0].series ? [...b].sort((x, y) => order.get(x.id) - order.get(y.id)) : b))
    .flat()
}

/** 以區塊為單位洗牌：同系列題目維持相鄰 */
export function shuffleKeepingSeries(list) {
  return shuffle(seriesBlocks(list)).flat()
}
