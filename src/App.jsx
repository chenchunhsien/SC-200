import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, BookMarked, CheckCircle2, Home as HomeIcon, Loader2, ShieldCheck, Upload, X } from 'lucide-react'
import { StoreProvider } from './lib/store'
import { clearCustomBank, countQuestions, loadCustomBank, saveCustomBank } from './lib/customBank'
import { validateBank } from './lib/validateBank'
import Home from './pages/Home'
import Quiz from './pages/Quiz'
import Result from './pages/Result'
import Notebook from './pages/Notebook'

const DATA_BASE = `${import.meta.env.BASE_URL}data/`
const SOURCE_KEY = 'sc200.bankSource' // 使用者最後選擇的題庫來源：'custom' 或範例題庫 id
const CUSTOM = 'custom'

async function fetchJson(file) {
  const res = await fetch(`${DATA_BASE}${file}`)
  if (!res.ok) throw new Error(`無法載入 ${file}（HTTP ${res.status}）`)
  return res.json()
}

const readPref = () => {
  try {
    return localStorage.getItem(SOURCE_KEY)
  } catch {
    return null
  }
}
const writePref = (v) => {
  try {
    localStorage.setItem(SOURCE_KEY, v)
  } catch {
    /* ignore */
  }
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
  const [custom, setCustom] = useState(undefined) // undefined = 讀取中；null = 沒有自訂題庫
  const [source, setSource] = useState(null)
  const [bank, setBank] = useState(null)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null) // { tone: 'ok' | 'error', title, lines? }
  const [view, setView] = useState({ name: 'home' })
  const fileInput = useRef(null)

  // 同時載入範例題庫清單與本機自訂題庫；有自訂題庫時預設優先使用
  useEffect(() => {
    Promise.all([fetchJson('index.json'), loadCustomBank()])
      .then(([m, c]) => {
        setManifest(m)
        setCustom(c)
        const pref = readPref()
        const valid = (id) => (id === CUSTOM ? !!c : m.banks.some((b) => b.id === id))
        setSource(valid(pref) ? pref : c ? CUSTOM : m.banks[0]?.id)
      })
      .catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    if (!source || custom === undefined) return
    if (source === CUSTOM) {
      if (custom) setBank({ ...custom.data, id: custom.data.id || CUSTOM })
      return
    }
    const meta = manifest.banks.find((b) => b.id === source)
    if (!meta) return
    let cancelled = false
    setBank(null)
    fetchJson(meta.file)
      .then((data) => !cancelled && setBank({ ...data, id: meta.id }))
      .catch((e) => !cancelled && setError(e.message))
    return () => {
      cancelled = true
    }
  }, [source, custom, manifest])

  const go = useCallback((name, props = {}) => {
    setView({ name, ...props })
    window.scrollTo({ top: 0 })
  }, [])
  const startQuiz = useCallback((config) => go('quiz', { config, runId: Date.now() }), [go])

  const switchSource = (id) => {
    setSource(id)
    writePref(id)
    go('home')
  }

  const onFile = async (e) => {
    const file = e.target.files?.[0]
    e.target.value = '' // 允許重新選擇同一個檔案
    if (!file) return
    let data
    try {
      data = JSON.parse(await file.text())
    } catch {
      return setNotice({ tone: 'error', title: `「${file.name}」不是有效的 JSON 檔案` })
    }
    const errs = validateBank(data)
    if (errs.length) {
      return setNotice({
        tone: 'error',
        title: `「${file.name}」格式不符，共 ${errs.length} 個問題，未匯入`,
        lines: errs.slice(0, 8).concat(errs.length > 8 ? [`…其餘 ${errs.length - 8} 個問題省略`] : []),
      })
    }
    try {
      setCustom(await saveCustomBank(data, file.name))
    } catch (err) {
      return setNotice({ tone: 'error', title: `無法儲存到瀏覽器（${err?.message ?? '儲存空間不可用'}）` })
    }
    switchSource(CUSTOM)
    setNotice({ tone: 'ok', title: `已匯入「${data.title || file.name}」，共 ${countQuestions(data)} 題` })
  }

  const onClearCustom = async () => {
    if (!window.confirm('確定要從這個瀏覽器清除已匯入的題庫嗎？作答紀錄會保留，重新匯入同一份題庫即可接續。')) return
    await clearCustomBank().catch(() => {})
    setCustom(null)
    switchSource(manifest.banks[0]?.id)
    setNotice({ tone: 'ok', title: '已清除本機題庫，改用預設範例' })
  }

  const inQuiz = view.name === 'quiz'
  // 作答與檢討頁需要較寬的版面（情境題組為雙欄）
  const pageWidth = inQuiz || view.name === 'result' ? 'max-w-[1600px]' : 'max-w-7xl'
  const sourceInfo = bank && {
    isCustom: source === CUSTOM,
    title: bank.title,
    count: countQuestions(bank),
    fileName: custom?.fileName,
    savedAt: custom?.savedAt,
  }

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/90 backdrop-blur">
        <div className={`mx-auto flex ${pageWidth} items-center gap-3 px-4 py-3`}>
          <button
            type="button"
            onClick={() => (!inQuiz || window.confirm('離開目前測驗？作答進度將不會保留。')) && go('home')}
            className="flex items-center gap-2 font-bold text-slate-900"
          >
            <ShieldCheck className="h-7 w-7 text-blue-600" />
            <span className="hidden sm:inline">SC-200 題庫自測平台</span>
            <span className="sm:hidden">SC-200</span>
          </button>
          {!inQuiz && (
            <div className="ml-auto flex items-center gap-2">
              {manifest && (
                <select
                  value={source ?? ''}
                  onChange={(e) => switchSource(e.target.value)}
                  className="max-w-40 rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm"
                  aria-label="切換題庫"
                >
                  {manifest.banks.map((b) => (
                    <option key={b.id} value={b.id}>
                      預設範例：{b.title}
                    </option>
                  ))}
                  {custom && <option value={CUSTOM}>我的題庫：{custom.data.title || custom.fileName}</option>}
                </select>
              )}
              <button
                type="button"
                onClick={() => fileInput.current?.click()}
                className="inline-flex items-center gap-1.5 rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-sm font-medium text-blue-700 transition hover:bg-blue-100"
                title="匯入個人題庫 (Upload JSON)"
              >
                <Upload className="h-4 w-4" />
                <span className="hidden lg:inline">匯入個人題庫</span>
              </button>
              <input ref={fileInput} type="file" accept=".json,application/json" className="hidden" onChange={onFile} />
              <NavBtn active={view.name === 'home'} onClick={() => go('home')} icon={HomeIcon} label="首頁" />
              <NavBtn
                active={view.name === 'notebook'}
                onClick={() => go('notebook')}
                icon={BookMarked}
                label="錯題本 / 收藏"
              />
            </div>
          )}
        </div>
      </header>

      <main className={`mx-auto ${pageWidth} px-4 py-6`}>
        {notice && !inQuiz && <Notice notice={notice} onClose={() => setNotice(null)} />}
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
          <Home
            bank={bank}
            sourceInfo={sourceInfo}
            onStart={startQuiz}
            onOpenNotebook={(tab) => go('notebook', { tab })}
            onImport={() => fileInput.current?.click()}
            onClearCustom={onClearCustom}
          />
        )}
      </main>
    </div>
  )
}

function Notice({ notice, onClose }) {
  const ok = notice.tone === 'ok'
  return (
    <div
      role={ok ? 'status' : 'alert'}
      className={`mb-5 flex gap-3 rounded-xl border p-4 text-sm ${
        ok ? 'border-emerald-200 bg-emerald-50 text-emerald-900' : 'border-rose-200 bg-rose-50 text-rose-900'
      }`}
    >
      {ok ? <CheckCircle2 className="h-5 w-5 shrink-0" /> : <AlertTriangle className="h-5 w-5 shrink-0" />}
      <div className="min-w-0 flex-1">
        <p className="font-semibold">{notice.title}</p>
        {notice.lines && (
          <ul className="mt-1.5 list-inside list-disc space-y-0.5 break-words">
            {notice.lines.map((l) => (
              <li key={l}>{l}</li>
            ))}
          </ul>
        )}
      </div>
      <button type="button" onClick={onClose} className="shrink-0 self-start rounded p-0.5 hover:bg-black/5" aria-label="關閉">
        <X className="h-4 w-4" />
      </button>
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
