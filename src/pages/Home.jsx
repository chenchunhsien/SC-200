import { useMemo, useState } from 'react'
import {
  BookOpen,
  CheckCircle2,
  ClipboardList,
  Database,
  Dices,
  HardDrive,
  Trash2,
  Upload,
  GraduationCap,
  Layers,
  ListOrdered,
  Play,
  RotateCcw,
  Shuffle,
  Star,
  Target,
  Timer,
  XCircle,
} from 'lucide-react'
import { qKey, useStore } from '../lib/store'
import { formatTime } from './Quiz'
import { shuffle } from '../lib/grading'
import { correctUnits, countQuestions, questionUnits } from '../lib/customBank'
import { drawMockExam, parseNumberRanges, pickByNumbers } from '../lib/mockExam'

const MOCK_SIZES = [50, 100]

export default function Home({ bank, sourceInfo, onStart, onOpenNotebook, onImport, onClearCustom }) {
  const { history, mistakes, starred, progress, examHistory, resetBank, clearExamHistory } = useStore()
  const [mode, setMode] = useState('practice')
  const [shuffled, setShuffled] = useState(false)
  const [customCount, setCustomCount] = useState('10')
  const [rangeText, setRangeText] = useState('')

  const questions = bank.questions
  const stats = useMemo(() => {
    const keys = questions.map((q) => qKey(bank.id, q.id))
    // 與題目總數一致，情境題組以子題計
    let tried = 0
    let right = 0
    questions.forEach((q, i) => {
      const h = history[keys[i]]
      if (!h) return
      tried += questionUnits(q)
      right += correctUnits(q, h)
    })
    return {
      total: countQuestions(bank),
      tried,
      accuracy: tried ? Math.round((right / tried) * 100) : null,
      mistakes: keys.filter((k) => mistakes[k]).length,
      starred: keys.filter((k) => starred[k]).length,
    }
  }, [questions, bank, history, mistakes, starred])

  const start = (title, list, source) => {
    if (!list.length) return
    onStart({ title, questions: shuffled ? shuffle(list) : list, mode, source })
  }

  // 隨機模擬測驗固定使用考試模式，不受練習 / 考試切換與隨機題序影響
  const startMock = (size) => {
    const { questions: list, total } = drawMockExam(questions, size)
    onStart({ title: `隨機模擬測驗（${total} 題）`, questions: list, mode: 'exam', source: 'mock' })
  }
  const countValue = Number(customCount)
  const countValid = Number.isInteger(countValue) && countValue >= 1

  // 指定題號：輸入時即時解析並預覽題數
  const range = useMemo(() => {
    if (!rangeText.trim()) return null
    const parsed = parseNumberRanges(rangeText, stats.total)
    return parsed.error ? parsed : { ...parsed, ...pickByNumbers(questions, parsed.numbers) }
  }, [rangeText, stats.total, questions])
  const rangeHint = !range
    ? null
    : range.error
      ? range.error
      : !range.questions.length
        ? `找不到符合的題目，題號範圍為 1–${stats.total}`
        : [
            `共 ${range.total} 題`,
            range.added > 0 && `為保持題組完整，額外納入 ${range.added} 個子題`,
            range.outOfRange && `超出 1–${stats.total} 的題號已略過`,
          ]
            .filter(Boolean)
            .join('；')
  const startRange = (e) => {
    e.preventDefault()
    if (range?.questions?.length) start(`指定題號：${rangeText.trim()}`, range.questions, 'range')
  }

  const bankProgress = progress[bank.id] || {}

  return (
    <div className="space-y-8">
      {sourceInfo && <SourceBanner info={sourceInfo} onImport={onImport} onClear={onClearCustom} />}
      <section className="rounded-2xl bg-gradient-to-br from-blue-700 via-blue-600 to-indigo-600 p-6 text-white shadow-lg sm:p-8">
        <p className="text-sm font-medium text-blue-100">Microsoft Security Operations Analyst</p>
        <h1 className="mt-1 text-2xl font-bold sm:text-3xl">{bank.title}</h1>
        {bank.description && <p className="mt-2 max-w-3xl text-blue-100">{bank.description}</p>}
        <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-5">
          <Stat icon={Layers} label="題目總數" value={stats.total} />
          <Stat icon={ClipboardList} label="已作答" value={stats.tried} />
          <Stat icon={Target} label="最近正確率" value={stats.accuracy == null ? '—' : `${stats.accuracy}%`} />
          <Stat icon={XCircle} label="錯題本" value={stats.mistakes} onClick={() => onOpenNotebook('mistakes')} />
          <Stat icon={Star} label="收藏" value={stats.starred} onClick={() => onOpenNotebook('starred')} />
        </div>
      </section>

      <section className="flex flex-col gap-4 rounded-xl border border-slate-200 bg-white p-4 sm:flex-row sm:items-center">
        <div className="inline-flex rounded-lg bg-slate-100 p-1">
          <ModeBtn active={mode === 'practice'} onClick={() => setMode('practice')} icon={GraduationCap}>
            練習模式
          </ModeBtn>
          <ModeBtn active={mode === 'exam'} onClick={() => setMode('exam')} icon={Timer}>
            考試模式
          </ModeBtn>
        </div>
        <p className="flex-1 text-sm text-slate-600">
          {mode === 'practice' ? '每題可即時對答案並查看解析。' : '計時作答，交卷後才顯示成績與解析。'}
        </p>
        <label className="inline-flex cursor-pointer items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={shuffled}
            onChange={(e) => setShuffled(e.target.checked)}
            className="h-4 w-4 accent-blue-600"
          />
          <Shuffle className="h-4 w-4" />
          隨機題序
        </label>
      </section>

      <section className="grid gap-4 sm:grid-cols-3">
        <ActionCard
          icon={BookOpen}
          title="全部題目"
          desc={`${stats.total} 題，涵蓋所有章節`}
          onClick={() => start('全部題目', questions, 'all')}
          tone="blue"
        />
        <ActionCard
          icon={XCircle}
          title="錯題重練"
          desc={stats.mistakes ? `${stats.mistakes} 題，答對後自動移出錯題本` : '目前沒有錯題'}
          disabled={!stats.mistakes}
          onClick={() =>
            start(
              '錯題重練',
              questions.filter((q) => mistakes[qKey(bank.id, q.id)]),
              'mistakes',
            )
          }
          tone="rose"
        />
        <ActionCard
          icon={Star}
          title="收藏題目"
          desc={stats.starred ? `${stats.starred} 題` : '尚未收藏任何題目'}
          disabled={!stats.starred}
          onClick={() =>
            start(
              '收藏題目',
              questions.filter((q) => starred[qKey(bank.id, q.id)]),
              'starred',
            )
          }
          tone="amber"
        />
      </section>

      <section className="space-y-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="flex items-start gap-4">
          <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-violet-100 text-violet-700">
            <Dices className="h-5 w-5" />
          </span>
          <div className="min-w-0 flex-1">
            <h2 className="font-semibold text-slate-900">隨機模擬測驗</h2>
            <p className="text-sm text-slate-600">
              從所有章節隨機抽題，以考試模式計時作答，交卷後顯示成績與解析。情境題組整組出題，題數以子題計。
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:pl-15">
          {MOCK_SIZES.map((size) => (
            <button
              key={size}
              type="button"
              onClick={() => startMock(size)}
              title={stats.total < size ? `題庫只有 ${stats.total} 題，將使用全部題目` : undefined}
              className="inline-flex items-center gap-1.5 rounded-lg bg-violet-600 px-4 py-2 text-sm font-semibold text-white hover:bg-violet-700"
            >
              <Play className="h-4 w-4" />
              隨機 {size} 題
            </button>
          ))}
          <form
            onSubmit={(e) => {
              e.preventDefault()
              if (countValid) startMock(countValue)
            }}
            className="flex items-center gap-2 border-slate-200 sm:ml-2 sm:border-l sm:pl-4"
          >
            <label htmlFor="mock-count" className="text-sm text-slate-700">
              自訂
            </label>
            <input
              id="mock-count"
              type="number"
              min="1"
              max={stats.total}
              value={customCount}
              onChange={(e) => setCustomCount(e.target.value)}
              className="w-20 rounded-lg border border-slate-300 px-2.5 py-1.5 text-sm"
            />
            <span className="text-sm text-slate-700">題</span>
            <button
              type="submit"
              disabled={!countValid}
              title={countValid && countValue > stats.total ? `題庫只有 ${stats.total} 題，將使用全部題目` : undefined}
              className="inline-flex items-center gap-1.5 rounded-lg border border-violet-300 bg-violet-50 px-3 py-1.5 text-sm font-semibold text-violet-700 hover:bg-violet-100 disabled:border-slate-200 disabled:bg-slate-50 disabled:text-slate-300"
            >
              <Play className="h-4 w-4" />
              開始
            </button>
          </form>
        </div>
      </section>

      <section className="space-y-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="flex items-start gap-4">
          <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-sky-100 text-sky-700">
            <ListOrdered className="h-5 w-5" />
          </span>
          <div className="min-w-0 flex-1">
            <h2 className="font-semibold text-slate-900">指定題號練習</h2>
            <p className="text-sm text-slate-600">
              輸入單題或範圍（1–{stats.total}），例如 1-30, 45, 60-70。題號依題庫順序、以子題計，與「全部題目」的題號相同；
              範圍涵蓋情境題組的任一子題時會整組納入。套用上方的練習 / 考試模式與隨機題序設定。
            </p>
          </div>
        </div>
        <form onSubmit={startRange} className="space-y-2 sm:pl-15">
          <div className="flex flex-wrap gap-2">
            <input
              type="text"
              value={rangeText}
              onChange={(e) => setRangeText(e.target.value)}
              placeholder="例如 1-30, 45, 60-70"
              aria-label="指定題號"
              className="min-w-0 flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
            <button
              type="submit"
              disabled={!range?.questions?.length}
              className="inline-flex items-center gap-1.5 rounded-lg bg-sky-600 px-4 py-2 text-sm font-semibold text-white hover:bg-sky-700 disabled:bg-slate-300"
            >
              <Play className="h-4 w-4" />
              開始練習
            </button>
          </div>
          {rangeHint && (
            <p className={`text-sm ${range?.questions?.length ? 'text-slate-600' : 'text-rose-600'}`}>{rangeHint}</p>
          )}
        </form>
      </section>

      <section>
        <h2 className="mb-3 text-lg font-bold text-slate-900">章節練習</h2>
        <div className="grid gap-3 md:grid-cols-2">
          {bank.chapters.map((ch, i) => {
            const list = questions.filter((q) => q.chapter === ch.id)
            const doneIds = new Set(bankProgress[ch.id] || [])
            const size = list.reduce((n, q) => n + questionUnits(q), 0)
            const done = list.reduce((n, q) => n + (doneIds.has(q.id) ? questionUnits(q) : 0), 0)
            const pct = size ? Math.round((done / size) * 100) : 0
            return (
              <div key={ch.id} className="flex flex-col rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
                <div className="flex items-start gap-3">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-blue-100 text-sm font-bold text-blue-700">
                    {i + 1}
                  </span>
                  <div className="min-w-0 flex-1">
                    <h3 className="font-semibold text-slate-900">{ch.title}</h3>
                    {ch.description && <p className="mt-0.5 text-sm text-slate-600">{ch.description}</p>}
                  </div>
                  {pct === 100 && <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-500" />}
                </div>
                <div className="mt-4 flex items-center gap-3">
                  <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
                    <div
                      className={`h-full rounded-full transition-all ${pct === 100 ? 'bg-emerald-500' : 'bg-blue-500'}`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className="w-24 text-right text-xs text-slate-600">
                    {done} / {size} · {pct}%
                  </span>
                  <button
                    type="button"
                    disabled={!list.length}
                    onClick={() => start(ch.title, list, 'chapter')}
                    className="inline-flex items-center gap-1 rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-blue-700 disabled:bg-slate-300"
                  >
                    <Play className="h-3.5 w-3.5" />
                    開始
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      </section>

      <ExamHistory
        records={examHistory.filter((r) => r.bankId === bank.id)}
        onClear={() => {
          if (window.confirm('確定要清空此題庫的歷次測驗紀錄嗎？作答統計與錯題本不受影響。')) clearExamHistory(bank.id)
        }}
      />

      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => {
            if (window.confirm('確定要清除此題庫的作答紀錄、錯題本、收藏與章節進度嗎？此動作無法復原。'))
              resetBank(bank.id)
          }}
          className="inline-flex items-center gap-1.5 text-sm text-slate-500 hover:text-rose-600"
        >
          <RotateCcw className="h-4 w-4" />
          清除此題庫紀錄
        </button>
      </div>
    </div>
  )
}

function SourceBanner({ info, onImport, onClear }) {
  return (
    <section
      className={`flex flex-wrap items-center gap-3 rounded-xl border px-4 py-3 text-sm ${
        info.isCustom ? 'border-violet-200 bg-violet-50 text-violet-900' : 'border-slate-200 bg-white text-slate-700'
      }`}
    >
      {info.isCustom ? <HardDrive className="h-5 w-5 shrink-0" /> : <Database className="h-5 w-5 shrink-0" />}
      <div className="min-w-0 flex-1">
        <span className="font-semibold">
          目前使用：{info.isCustom ? `自訂題庫（共 ${info.count} 題）` : `預設範例（共 ${info.count} 題）`}
        </span>
        <span className="ml-2 opacity-75">
          {info.isCustom
            ? `${info.fileName ?? ''}${info.savedAt ? ` · 匯入於 ${new Date(info.savedAt).toLocaleString()}` : ''} · 僅存在此瀏覽器`
            : '可匯入你自己的題庫 JSON，題目只會存在此瀏覽器、不會上傳'}
        </span>
      </div>
      <button
        type="button"
        onClick={onImport}
        className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 font-semibold text-white hover:bg-blue-700"
      >
        <Upload className="h-4 w-4" />
        {info.isCustom ? '重新匯入' : '匯入個人題庫 (Upload JSON)'}
      </button>
      {info.isCustom && (
        <button
          type="button"
          onClick={onClear}
          className="inline-flex items-center gap-1.5 rounded-lg border border-rose-200 bg-white px-3 py-1.5 font-medium text-rose-600 hover:bg-rose-50"
        >
          <Trash2 className="h-4 w-4" />
          清除本機題庫
        </button>
      )}
    </section>
  )
}

const HISTORY_PREVIEW = 10
const PASS_PERCENT = 70

/** 歷次測驗紀錄，新的在前；預設只顯示最近 HISTORY_PREVIEW 筆 */
function ExamHistory({ records, onClear }) {
  const [expanded, setExpanded] = useState(false)
  const shown = expanded ? records : records.slice(0, HISTORY_PREVIEW)

  return (
    <section>
      <div className="mb-3 flex items-center gap-3">
        <h2 className="text-lg font-bold text-slate-900">📊 歷次測驗歷史紀錄</h2>
        {records.length > 0 && <span className="text-sm text-slate-600">共 {records.length} 筆</span>}
        {records.length > 0 && (
          <button
            type="button"
            onClick={onClear}
            className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-600 hover:border-rose-200 hover:text-rose-600"
          >
            <Trash2 className="h-4 w-4" />
            清空歷史紀錄
          </button>
        )}
      </div>
      {records.length === 0 ? (
        <p className="rounded-xl border border-dashed border-slate-300 bg-white px-4 py-8 text-center text-sm text-slate-500">
          尚無測驗紀錄，交卷後會自動記錄在這裡。
        </p>
      ) : (
        <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <ul className="divide-y divide-slate-100">
            {shown.map((r) => {
              const passed = r.scorePercent >= PASS_PERCENT
              return (
                <li key={r.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3">
                  <span className="w-36 shrink-0 text-sm text-slate-600">
                    {new Date(r.timestamp).toLocaleString(undefined, {
                      month: '2-digit',
                      day: '2-digit',
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </span>
                  <span className="min-w-0 flex-1 basis-48 truncate font-medium text-slate-900" title={r.modeName}>
                    {r.modeName}
                  </span>
                  <span
                    className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-semibold ${
                      r.mode === 'exam' ? 'bg-indigo-100 text-indigo-700' : 'bg-slate-100 text-slate-700'
                    }`}
                  >
                    {r.mode === 'exam' ? '考試' : '練習'}
                  </span>
                  <span className="w-20 shrink-0 text-right text-sm text-slate-700">
                    {r.correctCount} / {r.totalCount}
                  </span>
                  <span
                    className={`w-14 shrink-0 text-right font-bold ${passed ? 'text-emerald-600' : 'text-rose-600'}`}
                  >
                    {r.scorePercent}%
                  </span>
                  <span className="inline-flex w-20 shrink-0 items-center justify-end gap-1 font-mono text-sm text-slate-600">
                    <Timer className="h-3.5 w-3.5" />
                    {formatTime(r.duration)}
                  </span>
                </li>
              )
            })}
          </ul>
          {records.length > HISTORY_PREVIEW && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="w-full border-t border-slate-100 py-2 text-sm font-medium text-blue-700 hover:bg-slate-50"
            >
              {expanded ? '收合' : `顯示全部 ${records.length} 筆`}
            </button>
          )}
        </div>
      )}
    </section>
  )
}

function Stat({ icon: Icon, label, value, onClick }) {
  const Tag = onClick ? 'button' : 'div'
  return (
    <Tag
      type={onClick ? 'button' : undefined}
      onClick={onClick}
      className={`rounded-xl bg-white/10 p-3 text-left ring-1 ring-white/20 ${onClick ? 'transition hover:bg-white/20' : ''}`}
    >
      <div className="flex items-center gap-1.5 text-xs text-blue-100">
        <Icon className="h-3.5 w-3.5" />
        {label}
      </div>
      <div className="mt-1 text-2xl font-bold">{value}</div>
    </Tag>
  )
}

function ModeBtn({ active, onClick, icon: Icon, children }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-semibold transition ${
        active ? 'bg-white text-blue-700 shadow-sm' : 'text-slate-600 hover:text-slate-800'
      }`}
    >
      <Icon className="h-4 w-4" />
      {children}
    </button>
  )
}

const TONES = {
  blue: 'bg-blue-100 text-blue-700',
  rose: 'bg-rose-100 text-rose-700',
  amber: 'bg-amber-100 text-amber-700',
}

function ActionCard({ icon: Icon, title, desc, onClick, disabled, tone }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="group flex items-center gap-4 rounded-xl border border-slate-200 bg-white p-4 text-left shadow-sm transition hover:-translate-y-0.5 hover:border-blue-300 hover:shadow-md disabled:translate-y-0 disabled:opacity-50 disabled:hover:border-slate-200 disabled:hover:shadow-sm"
    >
      <span className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-xl ${TONES[tone]}`}>
        <Icon className="h-5 w-5" />
      </span>
      <span>
        <span className="block font-semibold text-slate-900">{title}</span>
        <span className="block text-sm text-slate-600">{desc}</span>
      </span>
    </button>
  )
}
