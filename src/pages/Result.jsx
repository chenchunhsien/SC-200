import { useMemo, useState } from 'react'
import { Award, CheckCircle2, ChevronDown, Home, RotateCcw, Star, XCircle } from 'lucide-react'
import QuestionView from '../components/QuestionView'
import { gradeQuestion, previewText, TYPE_LABELS } from '../lib/grading'
import { qKey, useStore } from '../lib/store'
import SourceLabel from '../components/SourceLabel'
import { formatTime } from './Quiz'

const PASS_SCORE = 700

export default function Result({ bank, result, onRetry, onHome }) {
  const { questions, answers, elapsed, title, mode } = result
  const { starred, toggleStar } = useStore()
  const [filter, setFilter] = useState('all')
  const [open, setOpen] = useState(null)

  const graded = useMemo(
    () => questions.map((q) => ({ q, grade: gradeQuestion(q, answers[q.id]) })),
    [questions, answers],
  )
  const earned = graded.reduce((s, g) => s + g.grade.earned, 0)
  const total = graded.reduce((s, g) => s + g.grade.total, 0)
  const correctCount = graded.filter((g) => g.grade.correct).length
  const scaled = total ? Math.round((earned / total) * 1000) : 0
  const passed = scaled >= PASS_SCORE
  const wrong = graded.filter((g) => !g.grade.correct).map((g) => g.q)

  const shown = graded.filter((g) =>
    filter === 'all' ? true : filter === 'wrong' ? !g.grade.correct : g.grade.correct,
  )

  return (
    <div className="space-y-6">
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
            <Metric label="答對題數" value={`${correctCount} / ${questions.length}`} />
            <Metric label="得分點" value={`${earned} / ${total}`} />
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
        <button
          type="button"
          disabled={!wrong.length}
          onClick={() => onRetry({ title: `${title}－錯題重做`, questions: wrong, mode: 'practice', source: 'retry' })}
          className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 font-semibold text-white hover:bg-blue-700 disabled:bg-slate-300"
        >
          <RotateCcw className="h-4 w-4" />
          重做答錯的 {wrong.length} 題
        </button>
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
          const n = questions.indexOf(q) + 1
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
                  <span className="shrink-0 text-sm font-semibold text-slate-500">#{n}</span>
                  <span className="hidden shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600 sm:inline">
                    {TYPE_LABELS[q.type]}
                  </span>
                  <span className="truncate text-sm text-slate-700">{previewText(q)}</span>
                  {grade.total > 1 && (
                    <span className="ml-auto shrink-0 text-xs text-slate-500">
                      {grade.earned}/{grade.total}
                    </span>
                  )}
                  <ChevronDown className={`h-4 w-4 shrink-0 text-slate-400 transition ${isOpen ? 'rotate-180' : ''}`} />
                </button>
                <button
                  type="button"
                  onClick={() => toggleStar(bank.id, q.id)}
                  title={isStar ? '取消收藏' : '收藏'}
                  className="shrink-0 rounded p-1 hover:bg-slate-100"
                >
                  <Star className={`h-4 w-4 ${isStar ? 'fill-amber-400 text-amber-500' : 'text-slate-400'}`} />
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
        {!shown.length && <p className="py-10 text-center text-slate-400">沒有符合條件的題目</p>}
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
