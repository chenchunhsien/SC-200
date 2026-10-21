import { useMemo, useState } from 'react'
import {
  BookOpen,
  CheckCircle2,
  ClipboardList,
  GraduationCap,
  Layers,
  Play,
  RotateCcw,
  Shuffle,
  Star,
  Target,
  Timer,
  XCircle,
} from 'lucide-react'
import { qKey, useStore } from '../lib/store'
import { shuffle } from '../lib/grading'

export default function Home({ bank, onStart, onOpenNotebook }) {
  const { history, mistakes, starred, progress, resetBank } = useStore()
  const [mode, setMode] = useState('practice')
  const [shuffled, setShuffled] = useState(false)

  const questions = bank.questions
  const stats = useMemo(() => {
    const keys = questions.map((q) => qKey(bank.id, q.id))
    const tried = keys.filter((k) => history[k])
    const right = tried.filter((k) => history[k].lastCorrect)
    return {
      total: questions.length,
      tried: tried.length,
      accuracy: tried.length ? Math.round((right.length / tried.length) * 100) : null,
      mistakes: keys.filter((k) => mistakes[k]).length,
      starred: keys.filter((k) => starred[k]).length,
    }
  }, [questions, bank.id, history, mistakes, starred])

  const start = (title, list, source) => {
    if (!list.length) return
    onStart({ title, questions: shuffled ? shuffle(list) : list, mode, source })
  }

  const bankProgress = progress[bank.id] || {}

  return (
    <div className="space-y-8">
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
        <p className="flex-1 text-sm text-slate-500">
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

      <section>
        <h2 className="mb-3 text-lg font-bold text-slate-900">章節練習</h2>
        <div className="grid gap-3 md:grid-cols-2">
          {bank.chapters.map((ch, i) => {
            const list = questions.filter((q) => q.chapter === ch.id)
            const done = (bankProgress[ch.id] || []).filter((id) => list.some((q) => q.id === id)).length
            const pct = list.length ? Math.round((done / list.length) * 100) : 0
            return (
              <div key={ch.id} className="flex flex-col rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
                <div className="flex items-start gap-3">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-blue-100 text-sm font-bold text-blue-700">
                    {i + 1}
                  </span>
                  <div className="min-w-0 flex-1">
                    <h3 className="font-semibold text-slate-900">{ch.title}</h3>
                    {ch.description && <p className="mt-0.5 text-sm text-slate-500">{ch.description}</p>}
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
                  <span className="w-24 text-right text-xs text-slate-500">
                    {done} / {list.length} · {pct}%
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

      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => {
            if (window.confirm('確定要清除此題庫的作答紀錄、錯題本、收藏與章節進度嗎？此動作無法復原。'))
              resetBank(bank.id)
          }}
          className="inline-flex items-center gap-1.5 text-sm text-slate-400 hover:text-rose-600"
        >
          <RotateCcw className="h-4 w-4" />
          清除此題庫紀錄
        </button>
      </div>
    </div>
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
        active ? 'bg-white text-blue-700 shadow-sm' : 'text-slate-500 hover:text-slate-800'
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
        <span className="block text-sm text-slate-500">{desc}</span>
      </span>
    </button>
  )
}
