import { useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, Clock, Flag, Send, Star } from 'lucide-react'
import QuestionView from '../components/QuestionView'
import SourceLabel from '../components/SourceLabel'
import { gradeQuestion, isAnswered, isTouched } from '../lib/grading'
import { qKey, useStore } from '../lib/store'
import { numberQuestions, questionUnits } from '../lib/customBank'

export const formatTime = (sec) => {
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

export default function Quiz({ bank, config, onFinish }) {
  const { questions, mode, title, source } = config
  const practice = mode === 'practice'
  const { starred, toggleStar, recordAttempt } = useStore()
  const [idx, setIdx] = useState(0)
  const [answers, setAnswers] = useState({})
  const [revealed, setRevealed] = useState({})
  const [elapsed, setElapsed] = useState(0)
  const recorded = useRef(new Set())
  const chapterTitle = Object.fromEntries(bank.chapters.map((c) => [c.id, c.title]))
  // 題號與題數以子題計，情境題組顯示為範圍（例如 12–15）
  const numbering = numberQuestions(questions)

  useEffect(() => {
    const t = setInterval(() => setElapsed((e) => e + 1), 1000)
    return () => clearInterval(t)
  }, [])

  const q = questions[idx]
  const recordOpts = { clearMistakeOnCorrect: source === 'mistakes' }

  const record = (question, answer) => {
    if (recorded.current.has(question.id)) return
    recorded.current.add(question.id)
    recordAttempt(bank.id, question, answer, gradeQuestion(question, answer), recordOpts)
  }

  const isRevealed = (key) => !!revealed[key]
  const onReveal = (key) => {
    const next = { ...revealed, [key]: true }
    setRevealed(next)
    // 一般題直接記錄；題組在所有子題都對完答案後才記錄
    const fullyRevealed =
      q.type === 'case_study' ? q.questions.every((s) => next[`${q.id}/${s.id}`]) : next[q.id]
    if (fullyRevealed) record(q, answers[q.id])
  }

  const questionDone = (question) =>
    question.type === 'case_study'
      ? question.questions.every((s) => revealed[`${question.id}/${s.id}`])
      : !!revealed[question.id]

  const answeredCount = questions.reduce((n, x) => n + (isAnswered(x, answers[x.id]) ? questionUnits(x) : 0), 0)

  const submit = () => {
    const unanswered = numbering.total - answeredCount
    const msg = unanswered ? `尚有 ${unanswered} 題未完成作答，確定要交卷嗎？` : '確定要交卷嗎？'
    if (!window.confirm(msg)) return
    questions.forEach((x) => record(x, answers[x.id]))
    onFinish({ title, mode, questions, answers, elapsed, source })
  }

  const statusTone = (x, i) => {
    const ans = answers[x.id]
    let tone = 'bg-white text-slate-600 border-slate-200'
    if (practice && questionDone(x))
      tone = gradeQuestion(x, ans).correct
        ? 'bg-emerald-500 text-white border-emerald-500'
        : 'bg-rose-500 text-white border-rose-500'
    else if (isAnswered(x, ans)) tone = 'bg-blue-500 text-white border-blue-500'
    else if (isTouched(ans)) tone = 'bg-blue-100 text-blue-700 border-blue-200'
    return `${tone} ${i === idx ? 'ring-2 ring-offset-2 ring-blue-600' : ''}`
  }

  const isStar = !!starred[qKey(bank.id, q.id)]

  return (
    <div className="space-y-4">
      {/* 頂部資訊列 */}
      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
        <div className="min-w-0 flex-1">
          <h1 className="truncate font-bold text-slate-900">{title}</h1>
          <p className="text-xs text-slate-600">
            {practice ? '練習模式' : '考試模式'} · 已作答 {answeredCount} / {numbering.total}
          </p>
        </div>
        <span className="inline-flex items-center gap-1.5 rounded-lg bg-slate-100 px-3 py-1.5 font-mono text-sm text-slate-700">
          <Clock className="h-4 w-4" />
          {formatTime(elapsed)}
        </span>
        <button
          type="button"
          onClick={submit}
          className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-4 py-1.5 text-sm font-semibold text-white shadow-sm hover:bg-emerald-700"
        >
          <Send className="h-4 w-4" />
          交卷
        </button>
      </div>

      <div className="grid gap-4 lg:grid-cols-[1fr_240px]">
        {/* 題目區 */}
        <article className="min-w-0 rounded-xl border border-slate-200 bg-white p-5 shadow-sm sm:p-6">
          <header className="mb-5 flex items-start gap-3 border-b border-slate-100 pb-4">
            <div className="flex-1">
              <div className="text-sm font-semibold text-blue-700">
                第 {numbering.labels[idx]} / {numbering.total} 題
              </div>
              {q.chapter && <div className="text-xs text-slate-600">{chapterTitle[q.chapter] ?? q.chapter}</div>}
              {q.source && <SourceLabel source={q.source} />}
            </div>
            <button
              type="button"
              onClick={() => toggleStar(bank.id, q.id)}
              className={`inline-flex items-center gap-1 rounded-lg border px-2.5 py-1 text-sm transition ${
                isStar
                  ? 'border-amber-300 bg-amber-50 text-amber-700'
                  : 'border-slate-200 text-slate-600 hover:bg-slate-50'
              }`}
            >
              <Star className={`h-4 w-4 ${isStar ? 'fill-amber-400 text-amber-500' : ''}`} />
              {isStar ? '已收藏' : '收藏'}
            </button>
          </header>

          <QuestionView
            q={q}
            answer={answers[q.id]}
            onChange={(v) => setAnswers((a) => ({ ...a, [q.id]: v }))}
            isRevealed={isRevealed}
            onReveal={practice ? onReveal : null}
          />

          <footer className="mt-8 flex items-center justify-between border-t border-slate-100 pt-4">
            <button
              type="button"
              disabled={idx === 0}
              onClick={() => setIdx((i) => i - 1)}
              className="inline-flex items-center gap-1 rounded-lg px-3 py-2 font-medium text-slate-700 hover:bg-slate-100 disabled:text-slate-300 disabled:hover:bg-transparent"
            >
              <ChevronLeft className="h-5 w-5" />
              上一題
            </button>
            {idx < questions.length - 1 ? (
              <button
                type="button"
                onClick={() => setIdx((i) => i + 1)}
                className="inline-flex items-center gap-1 rounded-lg bg-blue-600 px-4 py-2 font-semibold text-white hover:bg-blue-700"
              >
                下一題
                <ChevronRight className="h-5 w-5" />
              </button>
            ) : (
              <button
                type="button"
                onClick={submit}
                className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-4 py-2 font-semibold text-white hover:bg-emerald-700"
              >
                <Flag className="h-4 w-4" />
                完成並交卷
              </button>
            )}
          </footer>
        </article>

        {/* 題目導覽 */}
        <aside className="order-first h-fit rounded-xl border border-slate-200 bg-white p-4 shadow-sm lg:sticky lg:top-20 lg:order-none">
          <h2 className="mb-3 text-sm font-semibold text-slate-700">題目導覽</h2>
          <div className="grid grid-cols-8 gap-2 sm:grid-cols-10 lg:grid-cols-5">
            {questions.map((x, i) => (
              <button
                key={x.id}
                type="button"
                onClick={() => setIdx(i)}
                title={`第 ${numbering.labels[i]} 題`}
                className={`relative flex aspect-square items-center justify-center rounded-lg border text-sm font-semibold transition ${statusTone(x, i)}`}
              >
                {numbering.starts[i]}
                {questionUnits(x) > 1 && <span className="text-xs font-normal opacity-75">+</span>}
                {starred[qKey(bank.id, x.id)] && (
                  <Star className="absolute -right-1 -top-1 h-3 w-3 fill-amber-400 text-amber-500" />
                )}
              </button>
            ))}
          </div>
          <ul className="mt-4 space-y-1.5 text-xs text-slate-600">
            <Legend className="bg-blue-500">已作答</Legend>
            {practice && <Legend className="bg-emerald-500">答對</Legend>}
            {practice && <Legend className="bg-rose-500">答錯</Legend>}
            <Legend className="border border-slate-300 bg-white">未作答</Legend>
          </ul>
        </aside>
      </div>
    </div>
  )
}

function Legend({ className, children }) {
  return (
    <li className="flex items-center gap-2">
      <span className={`h-3 w-3 rounded ${className}`} />
      {children}
    </li>
  )
}
