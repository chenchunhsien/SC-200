import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, BookMarked, Home as HomeIcon, Loader2, ShieldCheck } from 'lucide-react'
import { StoreProvider } from './lib/store'
import Home from './pages/Home'
import Quiz from './pages/Quiz'
import Result from './pages/Result'
import Notebook from './pages/Notebook'

const DATA_BASE = `${import.meta.env.BASE_URL}data/`
const LAST_BANK_KEY = 'sc200.lastBank'

async function fetchJson(file) {
  const res = await fetch(`${DATA_BASE}${file}`)
  if (!res.ok) throw new Error(`無法載入 ${file}（HTTP ${res.status}）`)
  return res.json()
}

export default function App() {
  return (
    <StoreProvider>
      <Shell />
    </StoreProvider>
  )
}

function Shell() {
  const [manifest, setManifest] = useState(null)
  const [bankId, setBankId] = useState(() => {
    try {
      return localStorage.getItem(LAST_BANK_KEY)
    } catch {
      return null
    }
  })
  const [bank, setBank] = useState(null)
  const [error, setError] = useState(null)
  const [view, setView] = useState({ name: 'home' })

  // 載入題庫清單
  useEffect(() => {
    fetchJson('index.json')
      .then((m) => {
        setManifest(m)
        setBankId((id) => (m.banks.some((b) => b.id === id) ? id : m.banks[0]?.id))
      })
      .catch((e) => setError(e.message))
  }, [])

  // 載入選定題庫
  useEffect(() => {
    if (!manifest || !bankId) return
    const meta = manifest.banks.find((b) => b.id === bankId)
    if (!meta) return
    let cancelled = false
    setBank(null)
    fetchJson(meta.file)
      .then((data) => {
        if (cancelled) return
        setBank({ ...data, id: meta.id })
        try {
          localStorage.setItem(LAST_BANK_KEY, meta.id)
        } catch {
          /* ignore */
        }
      })
      .catch((e) => !cancelled && setError(e.message))
    return () => {
      cancelled = true
    }
  }, [manifest, bankId])

  const go = useCallback((name, props = {}) => {
    setView({ name, ...props })
    window.scrollTo({ top: 0 })
  }, [])
  const startQuiz = useCallback((config) => go('quiz', { config, runId: Date.now() }), [go])
  const inQuiz = view.name === 'quiz'

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/90 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center gap-3 px-4 py-3">
          <button
            type="button"
            onClick={() => (!inQuiz || window.confirm('離開目前測驗？作答進度將不會保留。')) && go('home')}
            className="flex items-center gap-2 font-bold text-slate-900"
          >
            <ShieldCheck className="h-7 w-7 text-blue-600" />
            <span className="hidden sm:inline">SC-200 題庫自測平台</span>
            <span className="sm:hidden">SC-200</span>
          </button>
          <div className="ml-auto flex items-center gap-2">
            {manifest && manifest.banks.length > 1 && !inQuiz && (
              <select
                value={bankId ?? ''}
                onChange={(e) => setBankId(e.target.value)}
                className="max-w-44 rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm"
                aria-label="選擇題庫"
              >
                {manifest.banks.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.title}
                  </option>
                ))}
              </select>
            )}
            {!inQuiz && (
              <>
                <NavBtn active={view.name === 'home'} onClick={() => go('home')} icon={HomeIcon} label="首頁" />
                <NavBtn
                  active={view.name === 'notebook'}
                  onClick={() => go('notebook')}
                  icon={BookMarked}
                  label="錯題本 / 收藏"
                />
              </>
            )}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-6">
        {error ? (
          <div className="flex items-center gap-3 rounded-xl border border-rose-200 bg-rose-50 p-5 text-rose-800">
            <AlertTriangle className="h-6 w-6 shrink-0" />
            {error}
          </div>
        ) : !bank ? (
          <div className="flex items-center justify-center gap-2 py-24 text-slate-500">
            <Loader2 className="h-5 w-5 animate-spin" />
            載入題庫中…
          </div>
        ) : view.name === 'quiz' ? (
          <Quiz key={view.runId} bank={bank} config={view.config} onFinish={(result) => go('result', { result })} />
        ) : view.name === 'result' ? (
          <Result bank={bank} result={view.result} onRetry={startQuiz} onHome={() => go('home')} />
        ) : view.name === 'notebook' ? (
          <Notebook bank={bank} onStart={startQuiz} initialTab={view.tab} />
        ) : (
          <Home bank={bank} onStart={startQuiz} onOpenNotebook={(tab) => go('notebook', { tab })} />
        )}
      </main>
    </div>
  )
}

function NavBtn({ active, onClick, icon: Icon, label }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition ${
        active ? 'bg-blue-50 text-blue-700' : 'text-slate-600 hover:bg-slate-100'
      }`}
    >
      <Icon className="h-4 w-4" />
      <span className="hidden md:inline">{label}</span>
    </button>
  )
}
