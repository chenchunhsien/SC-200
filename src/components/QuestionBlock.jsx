import { AlertTriangle, BookOpen, CheckCircle2, ExternalLink, Eye, XCircle } from 'lucide-react'
import { gradeQuestion, isAnswered, TYPE_LABELS } from '../lib/grading'
import RichText, { CodeBlock } from './RichText'
import ChoiceQuestion from './questions/ChoiceQuestion'
import DropdownCloze from './questions/DropdownCloze'
import DragDropOrder from './questions/DragDropOrder'
import StatementMatrix from './questions/StatementMatrix'

const BODIES = {
  single_choice: ChoiceQuestion,
  multiple_choice: ChoiceQuestion,
  dropdown_cloze: DropdownCloze,
  drag_drop_order: DragDropOrder,
  statement_matrix: StatementMatrix,
}

/**
 * 單一（非題組）題目：題幹 + 作答元件 + 對答案按鈕 + 解析。
 * onCheck 為 null 時（考試模式）不顯示「對答案」按鈕。
 */
export default function QuestionBlock({ q, answer, onChange, revealed, onCheck }) {
  const Body = BODIES[q.type]
  const grade = revealed ? gradeQuestion(q, answer) : null
  const answered = isAnswered(q, answer)

  return (
    <div className="space-y-5">
      <div className="space-y-3">
        <span className="inline-block rounded-full bg-blue-100 px-2.5 py-0.5 text-xs font-semibold text-blue-700">
          {TYPE_LABELS[q.type] ?? q.type}
        </span>
        <RichText text={q.question} className="text-[1.02rem] text-slate-800" />
        {q.code && <CodeBlock code={q.code} />}
      </div>

      {Body ? (
        <Body q={q} answer={answer} onChange={onChange} revealed={revealed} grade={grade} />
      ) : (
        <p className="text-rose-600">不支援的題型：{q.type}</p>
      )}

      {onCheck && !revealed && (
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onCheck}
            className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 font-semibold text-white shadow-sm transition hover:bg-blue-700"
          >
            <Eye className="h-4 w-4" />
            對答案
          </button>
          {!answered && <span className="text-sm text-amber-600">尚未完成作答，仍可直接查看答案</span>}
        </div>
      )}

      {revealed && <Explanation q={q} grade={grade} />}
    </div>
  )
}

export function Explanation({ q, grade }) {
  return (
    <div className="space-y-3">
      {grade && (
        <div
          className={`flex items-center gap-2 rounded-lg px-4 py-3 font-semibold ${
            grade.correct ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
          }`}
        >
          {grade.correct ? <CheckCircle2 className="h-5 w-5" /> : <XCircle className="h-5 w-5" />}
          {grade.correct ? '答對了！' : '答錯了'}
          {grade.total > 1 && (
            <span className="ml-auto text-sm font-normal">
              得分 {grade.earned} / {grade.total}
            </span>
          )}
        </div>
      )}
      {q.reviewNote && (
        <div className="flex gap-2 rounded-lg border border-orange-300 bg-orange-50 p-4 text-sm text-orange-900">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            <span className="font-semibold">待確認：</span>
            {q.reviewNote}
          </div>
        </div>
      )}
      {(q.explanation || q.references?.length > 0) && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-4">
          <h4 className="mb-2 flex items-center gap-2 font-semibold text-amber-900">
            <BookOpen className="h-4 w-4" />
            解析
          </h4>
          <RichText text={q.explanation} className="text-sm text-slate-700" />
          {q.references?.length > 0 && (
            <ul className="mt-3 space-y-1 border-t border-amber-200 pt-3 text-sm">
              {q.references.map((r) => (
                <li key={r.url}>
                  <a
                    href={r.url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-blue-700 hover:underline"
                  >
                    <ExternalLink className="h-3.5 w-3.5" />
                    {r.title}
                  </a>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
