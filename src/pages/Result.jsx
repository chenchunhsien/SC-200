import { useMemo, useState } from 'react'
import { Award, CheckCircle2, ChevronDown, Home, PartyPopper, Star, XCircle } from 'lucide-react'
import QuestionView from '../components/QuestionView'
import { previewText, summarizeResult, TYPE_LABELS } from '../lib/grading'
import { qKey, useStore } from '../lib/store'
import { numberQuestions, questionUnits } from '../lib/customBank'
import SourceLabel from '../components/SourceLabel'
import { formatTime } from './Quiz'

const PASS_SCORE = 700

export default function Result({ bank, result, onRetry, onHome }) {
  const { questions, answers, elapsed, title, mode, source, round = 0, origin = title } = result
  const { starred, toggleStar } = useStore()
  const [filter, setFilter] = useState('all')
  const [open, setOpen] = useState(null)

  const summary = useMemo(() => summarizeResult(questions, answers), [questions, answers])
  const { graded, earned, points, wrong, scaledScore: scaled } = summary
  const correctCount = graded.length - wrong.length
  // 題號以子題計，與首頁、作答頁一致
  const numbering = numberQuestions(questions)
  const passed = scaled >= PASS_SCORE

  // 錯題循環重測：只帶入答錯的題目（題組整組保留），一輪輪重測直到全部答對
  const inCycle = source === 'cycle'
  const cleared = inCycle && !wrong.length
  const wrongUnits = wrong.reduce((n, q) => n + questionUnits(q), 0)
  const startCycle = () =>
    onRetry({
      title: `${origin}・錯題循環重測 第 ${round + 1} 輪`,
      questions: wrong,
      mode,
      source: 'cycle',
      round: round + 1,
      origin,
    })

  const shown = graded.filter((g) =>
    filter === 'all' ? true : filter === 'wrong' ? !g.grade.correct : g.grade.correct,
  )

  return (
    <div className="space-y-6">
      {cleared && (
        <section className="flex flex-wrap items-center gap-4 rounded-2xl border-2 border-emerald-300 bg-emerald-50 p-6 text-emerald-900 shadow-sm">
          <PartyPopper className="h-12 w-12 shrink-0 text-emerald-600" />
          <div className="min-w-0 flex-1">
            <h2 className="text-xl font-bold">🎉 恭喜！本輪錯題已全數克服！</h2>
            <p className="mt-1 text-emerald-800">經過 {round} 輪錯題循環重測，「{origin}」的錯題已經全部答對。</p>
          </div>
          <button
            type="button"
            onClick={onHome}
            className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-5 py-2.5 font-semibold text-white hover:bg-emerald-700"
          >
            <Home className="h-4 w-4" />
            返回首頁
          </button>
        </section>
      )}
      <section
        className={`rounded-2xl p-6 text-white shadow-lg sm:p-8 ${
          passed ? 'bg-gradient-to-br from-emerald-600 to-teal-600' : 'bg-gradient-to-br from-rose-600 to-orange-500'
        }`}
      >
        <div className="flex flex-wrap items-center gap-6">
          <Award className="h-16 w-16 shrink-0 opacity-90" />
          <div className="flex-1">
            <p className="text-sm opacity-80">
              {title} · {mode === 'exam' ? '考試模式' : '練習模式'}
              {inCycle && (
                <span className="ml-2 rounded-full bg-white/20 px-2 py-0.5 text-xs font-semibold">第 {round} 輪</span>
              )}
            </p>
            <div className="mt-1 flex items-baseline gap-2">
              <span className="text-5xl font-extrabold">{scaled}</span>
              <span className="text-lg opacity-80">/ 1000</span>
              <span className="ml-2 rounded-full bg-white/20 px-3 py-0.5 text-sm font-semibold">
                {passed ? 'PASS' : 'FAIL'}（及格 {PASS_SCORE}）
              </span>
            </div>
          </div>
          <dl className="grid grid-cols-3 gap-4 text-center">
            <Metric label="答對題數" value={`${summary.correctCount} / ${summary.totalCount}`} />
            <Metric label="得分點" value={`${earned} / ${points}`} />
            <Metric label="用時" value={formatTime(elapsed)} />
          </dl>
        </div>
      </section>

      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          onClick={onHome}
          className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-4 py-2 font-medium text-slate-700 hover:bg-slate-50"
        >
          <Home className="h-4 w-4" />
          回首頁
        </button>
        {wrong.length > 0 && (
          <button
            type="button"
            onClick={startCycle}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 font-semibold text-white hover:bg-blue-700"
          >
            🔁 錯題循環重測（剩餘 {wrongUnits} 題）
          </button>
        )}
        <div className="ml-auto inline-flex rounded-lg bg-slate-200 p-1 text-sm">
          {[
            ['all', `全部 ${graded.length}`],
            ['wrong', `答錯 ${graded.length - correctCount}`],
            ['right', `答對 ${correctCount}`],
          ].map(([k, label]) => (
            <button
              key={k}
              type="button"
              onClick={() => setFilter(k)}
              className={`rounded-md px-3 py-1 font-medium ${filter === k ? 'bg-white shadow-sm' : 'text-slate-600'}`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <section className="space-y-3">
        {shown.map(({ q, grade }) => {
          const n = numbering.labels[questions.indexOf(q)]
          const isOpen = open === q.id
          const isStar = !!starred[qKey(bank.id, q.id)]
          return (
            <div key={q.id} className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
              <div className="flex items-center gap-3 px-4 py-3">
                <button
                  type="button"
                  onClick={() => setOpen(isOpen ? null : q.id)}
                  className="flex min-w-0 flex-1 items-center gap-3 text-left"
                >
                  {grade.correct ? (
                    <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-500" />
                  ) : (
                    <XCircle className="h-5 w-5 shrink-0 text-rose-500" />
                  )}
                  <span className="shrink-0 text-sm font-semibold text-slate-600">#{n}</span>
                  <span className="hidden shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600 sm:inline">
                    {TYPE_LABELS[q.type]}
                  </span>
                  <span className="truncate text-sm text-slate-700">{previewText(q)}</span>
                  {grade.total > 1 && (
                    <span className="ml-auto shrink-0 text-xs text-slate-600">
                      {grade.earned}/{grade.total}
                    </span>
                  )}
                  <ChevronDown className={`h-4 w-4 shrink-0 text-slate-500 transition ${isOpen ? 'rotate-180' : ''}`} />
                </button>
                <button
                  type="button"
                  onClick={() => toggleStar(bank.id, q.id)}
                  title={isStar ? '取消收藏' : '收藏'}
                  className="shrink-0 rounded p-1 hover:bg-slate-100"
                >
                  <Star className={`h-4 w-4 ${isStar ? 'fill-amber-400 text-amber-500' : 'text-slate-500'}`} />
                </button>
              </div>
              {isOpen && (
                <div className="border-t border-slate-100 p-5">
                  {q.source && (
                    <div className="mb-3">
                      <SourceLabel source={q.source} />
                    </div>
                  )}
                  <QuestionView q={q} answer={answers[q.id]} onChange={() => {}} isRevealed={() => true} onReveal={null} />
                </div>
              )}
            </div>
          )
        })}
        {!shown.length && <p className="py-10 text-center text-slate-500">沒有符合條件的題目</p>}
      </section>
    </div>
  )
}

function Metric({ label, value }) {
  return (
    <div>
      <dt className="text-xs opacity-80">{label}</dt>
      <dd className="mt-0.5 text-lg font-bold">{value}</dd>
    </div>
  )
}
