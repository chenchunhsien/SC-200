import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

const KEYS = {
  history: 'sc200.history', // { [key]: { attempts, correctCount, lastCorrect, lastAnswer, lastAt } }
  mistakes: 'sc200.mistakes', // { [key]: { bankId, qid, wrongCount, addedAt } }
  starred: 'sc200.starred', // { [key]: { bankId, qid, at } }
  progress: 'sc200.progress', // { [bankId]: { [chapterId]: [qid, ...] } }
}

export const qKey = (bankId, qid) => `${bankId}::${qid}`

function read(key) {
  try {
    return JSON.parse(localStorage.getItem(key)) ?? {}
  } catch {
    return {}
  }
}

function usePersisted(key) {
  const [value, setValue] = useState(() => read(key))
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value))
    } catch {
      /* 儲存空間不可用時僅保留於記憶體 */
    }
  }, [key, value])
  return [value, setValue]
}

const StoreContext = createContext(null)

export function StoreProvider({ children }) {
  const [history, setHistory] = usePersisted(KEYS.history)
  const [mistakes, setMistakes] = usePersisted(KEYS.mistakes)
  const [starred, setStarred] = usePersisted(KEYS.starred)
  const [progress, setProgress] = usePersisted(KEYS.progress)

  const recordAttempt = useCallback(
    (bankId, q, answer, grade, { clearMistakeOnCorrect = false } = {}) => {
      const key = qKey(bankId, q.id)
      const now = Date.now()
      setHistory((h) => {
        const prev = h[key] || { attempts: 0, correctCount: 0 }
        return {
          ...h,
          [key]: {
            attempts: prev.attempts + 1,
            correctCount: prev.correctCount + (grade.correct ? 1 : 0),
            lastCorrect: grade.correct,
            lastAnswer: answer ?? null,
            lastAt: now,
          },
        }
      })
      setMistakes((m) => {
        if (!grade.correct) {
          const prev = m[key]
          return { ...m, [key]: { bankId, qid: q.id, wrongCount: (prev?.wrongCount ?? 0) + 1, addedAt: prev?.addedAt ?? now } }
        }
        if (clearMistakeOnCorrect && m[key]) {
          const { [key]: _, ...rest } = m
          return rest
        }
        return m
      })
      if (q.chapter) {
        setProgress((p) => {
          const bank = p[bankId] || {}
          const done = bank[q.chapter] || []
          if (done.includes(q.id)) return p
          return { ...p, [bankId]: { ...bank, [q.chapter]: [...done, q.id] } }
        })
      }
    },
    [setHistory, setMistakes, setProgress],
  )

  const toggleStar = useCallback(
    (bankId, qid) =>
      setStarred((s) => {
        const key = qKey(bankId, qid)
        if (s[key]) {
          const { [key]: _, ...rest } = s
          return rest
        }
        return { ...s, [key]: { bankId, qid, at: Date.now() } }
      }),
    [setStarred],
  )

  const removeMistake = useCallback(
    (bankId, qid) =>
      setMistakes((m) => {
        const { [qKey(bankId, qid)]: _, ...rest } = m
        return rest
      }),
    [setMistakes],
  )

  const resetBank = useCallback(
    (bankId) => {
      const prefix = `${bankId}::`
      const strip = (obj) => Object.fromEntries(Object.entries(obj).filter(([k]) => !k.startsWith(prefix)))
      setHistory(strip)
      setMistakes(strip)
      setStarred(strip)
      setProgress(({ [bankId]: _, ...rest }) => rest)
    },
    [setHistory, setMistakes, setStarred, setProgress],
  )

  const value = useMemo(
    () => ({ history, mistakes, starred, progress, recordAttempt, toggleStar, removeMistake, resetBank }),
    [history, mistakes, starred, progress, recordAttempt, toggleStar, removeMistake, resetBank],
  )
  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>
}

export const useStore = () => useContext(StoreContext)
