// 使用者自行匯入的題庫只存在本機瀏覽器的 IndexedDB，不會上傳或部署
const DB_NAME = 'sc200'
const STORE = 'banks'
const KEY = 'custom_question_bank'

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1)
    req.onupgradeneeded = () => req.result.createObjectStore(STORE)
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

async function run(mode, fn) {
  const db = await openDb()
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode)
      const req = fn(tx.objectStore(STORE))
      tx.oncomplete = () => resolve(req.result)
      tx.onerror = () => reject(tx.error)
      tx.onabort = () => reject(tx.error)
    })
  } finally {
    db.close()
  }
}

/** 回傳 { data, fileName, savedAt } 或 null；IndexedDB 不可用時也回傳 null */
export async function loadCustomBank() {
  try {
    return (await run('readonly', (s) => s.get(KEY))) ?? null
  } catch {
    return null
  }
}

export function saveCustomBank(data, fileName) {
  const record = { data, fileName, savedAt: Date.now() }
  return run('readwrite', (s) => s.put(record, KEY)).then(() => record)
}

export function clearCustomBank() {
  return run('readwrite', (s) => s.delete(KEY))
}

/** 單一題目的題數（情境題組以子題計） */
export const questionUnits = (q) => (q.type === 'case_study' ? q.questions.length : 1)

/** 題數（情境題組以子題計） */
export function countQuestions(bank) {
  return bank.questions.reduce((n, q) => n + questionUnits(q), 0)
}

/** 依作答紀錄計算某題答對的題數；舊紀錄沒有子題明細時，以整組對錯推算 */
export function correctUnits(q, h) {
  if (q.type !== 'case_study') return h.lastCorrect ? 1 : 0
  if (!h.subCorrect) return h.lastCorrect ? q.questions.length : 0
  return q.questions.filter((s) => h.subCorrect[s.id]).length
}
