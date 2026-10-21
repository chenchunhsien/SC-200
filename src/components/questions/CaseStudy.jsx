import { useState } from 'react'
import { Building2, CheckCircle2, ChevronLeft, ChevronRight, XCircle } from 'lucide-react'
import { gradeQuestion, isAnswered } from '../../lib/grading'
import QuestionBlock, { Explanation } from '../QuestionBlock'
import RichText from '../RichText'

/**
 * 情境題組：左欄背景資訊（可切換分頁），右欄子題目。
 * answer 形如 { [subId]: 子題作答 }；revealed(subId) / onReveal(subId) 由外層管理。
 */
export default function CaseStudy({ q, answer, onChange, isRevealed, onReveal }) {
  const [tab, setTab] = useState(0)
  const [subIdx, setSubIdx] = useState(0)
  const value = answer || {}
  const sections = q.caseInfo?.sections ?? []
  const sub = q.questions[subIdx]
  const allRevealed = q.questions.every((s) => isRevealed(s.id))

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1.5 rounded-full bg-violet-100 px-2.5 py-0.5 text-xs font-semibold text-violet-700">
          <Building2 className="h-3.5 w-3.5" />
          情境題組 · Case Study
        </span>
        <h2 className="text-lg font-bold text-slate-900">{q.caseInfo?.title}</h2>
      </div>
      {q.question && <RichText text={q.question} className="text-sm text-slate-600" />}

      <div className="grid gap-4 lg:grid-cols-2">
        {/* 左欄：背景資訊 */}
        <aside className="flex max-h-[75vh] flex-col overflow-hidden rounded-xl border border-slate-200 bg-white lg:sticky lg:top-20">
          <nav className="flex shrink-0 overflow-x-auto border-b border-slate-200 bg-slate-50">
            {sections.map((s, i) => (
              <button
                key={s.title}
                type="button"
                onClick={() => setTab(i)}
                className={`whitespace-nowrap border-b-2 px-4 py-2.5 text-sm font-medium transition ${
                  tab === i
                    ? 'border-violet-600 bg-white text-violet-700'
                    : 'border-transparent text-slate-500 hover:text-slate-800'
                }`}
              >
                {s.title}
              </button>
            ))}
          </nav>
          <div className="overflow-y-auto p-5">
            <RichText text={sections[tab]?.content} className="text-sm text-slate-700" />
          </div>
        </aside>

        {/* 右欄：子題 */}
        <section className="rounded-xl border border-slate-200 bg-white p-5">
          <div className="mb-4 flex flex-wrap gap-2">
            {q.questions.map((s, i) => {
              const rev = isRevealed(s.id)
              const ok = rev && gradeQuestion(s, value[s.id]).correct
              const done = isAnswered(s, value[s.id])
              return (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => setSubIdx(i)}
                  className={`inline-flex items-center gap-1 rounded-lg border-2 px-3 py-1 text-sm font-semibold transition ${
                    i === subIdx ? 'border-violet-600' : 'border-transparent'
                  } ${
                    rev
                      ? ok
                        ? 'bg-emerald-100 text-emerald-800'
                        : 'bg-rose-100 text-rose-800'
                      : done
                        ? 'bg-blue-100 text-blue-800'
                        : 'bg-slate-100 text-slate-600'
                  }`}
                >
                  {rev && (ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />)}
                  子題 {i + 1}
                </button>
              )
            })}
          </div>

          <QuestionBlock
            key={sub.id}
            q={sub}
            answer={value[sub.id]}
            onChange={(v) => onChange({ ...value, [sub.id]: v })}
            revealed={isRevealed(sub.id)}
            onCheck={onReveal ? () => onReveal(sub.id) : null}
          />

          <div className="mt-6 flex justify-between border-t border-slate-100 pt-4">
            <button
              type="button"
              disabled={subIdx === 0}
              onClick={() => setSubIdx((i) => i - 1)}
              className="inline-flex items-center gap-1 text-sm font-medium text-violet-700 disabled:text-slate-300"
            >
              <ChevronLeft className="h-4 w-4" />
              上一子題
            </button>
            <button
              type="button"
              disabled={subIdx === q.questions.length - 1}
              onClick={() => setSubIdx((i) => i + 1)}
              className="inline-flex items-center gap-1 text-sm font-medium text-violet-700 disabled:text-slate-300"
            >
              下一子題
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        </section>
      </div>

      {allRevealed && q.explanation && <Explanation q={q} grade={gradeQuestion(q, value)} />}
    </div>
  )
}
