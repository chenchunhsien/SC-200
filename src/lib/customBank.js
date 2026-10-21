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

// ── Google 雲端硬碟 ──────────────────────────────────────────
const DRIVE_LINK_KEY = 'sc200.driveLink' // 最後一次成功載入的雲端連結

/** 從共用連結（/file/d/ID/view、?id=ID）或純檔案 ID 取出 FILE_ID；無法辨識時回傳 null */
export function parseDriveFileId(input) {
  const s = input.trim()
  const m = s.match(/\/file\/d\/([\w-]{10,})/) || s.match(/[?&]id=([\w-]{10,})/) || s.match(/^([\w-]{20,})$/)
  return m?.[1] ?? null
}

/**
 * 從公開共用的 Google 雲端硬碟檔案下載題庫 JSON。
 * drive.usercontent.google.com 允許跨來源讀取；檔案未公開時 Google 會導向登入頁，瀏覽器會擋下並丟出 TypeError。
 */
export async function fetchDriveBank(link) {
  const id = parseDriveFileId(link)
  if (!id) throw new Error('無法從連結取得檔案 ID，請貼上 https://drive.google.com/file/d/…/view 格式的共用連結')
  let res
  try {
    // confirm=t 略過大型檔案的病毒掃描確認頁，直接取得檔案內容
    res = await fetch(
      `https://drive.usercontent.google.com/download?id=${encodeURIComponent(id)}&export=download&confirm=t`,
    )
  } catch {
    throw new Error(
      '無法讀取雲端檔案。請確認共用設定為「知道連結的任何人」都能檢視；若網路環境封鎖 Google 雲端硬碟，請改用「上傳本機 JSON」。',
    )
  }
  if (!res.ok) {
    throw new Error(
      res.status === 404
        ? '找不到這個雲端檔案，請確認連結是否正確'
        : res.status >= 500
          ? `Google 雲端硬碟暫時無法回應（HTTP ${res.status}），請稍後再按一次「載入題庫」`
          : `Google 雲端硬碟拒絕存取（HTTP ${res.status}），請確認連結指向題庫 JSON 檔且已公開共用`,
    )
  }
  const text = await res.text()
  try {
    return JSON.parse(text)
  } catch {
    throw new Error('下載的內容不是 JSON：檔案可能尚未公開共用（取得的是登入頁），或連結指向的不是題庫 JSON 檔')
  }
}

export function loadDriveLink() {
  try {
    return localStorage.getItem(DRIVE_LINK_KEY) || ''
  } catch {
    return ''
  }
}

export function saveDriveLink(link) {
  try {
    localStorage.setItem(DRIVE_LINK_KEY, link)
  } catch {
    /* ignore */
  }
}

/** 單一題目的題數（情境題組以子題計） */
export const questionUnits = (q) => (q.type === 'case_study' ? q.questions.length : 1)

/** 題數（情境題組以子題計） */
export function countQuestions(bank) {
  return bank.questions.reduce((n, q) => n + questionUnits(q), 0)
}

/** 依序為每題編題號（情境題組佔多個題號，例如 "12–15"），回傳 { labels, starts, total } */
export function numberQuestions(questions) {
  let total = 0
  const starts = []
  const labels = questions.map((q) => {
    const n = questionUnits(q)
    starts.push(total + 1)
    total += n
    return n > 1 ? `${total - n + 1}–${total}` : `${total}`
  })
  return { labels, starts, total }
}

/** 依作答紀錄計算某題答對的題數；舊紀錄沒有子題明細時，以整組對錯推算 */
export function correctUnits(q, h) {
  if (q.type !== 'case_study') return h.lastCorrect ? 1 : 0
  if (!h.subCorrect) return h.lastCorrect ? q.questions.length : 0
  return q.questions.filter((s) => h.subCorrect[s.id]).length
}
