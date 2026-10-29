import { AlertTriangle, BookOpen, Bot, CheckCircle2, ExternalLink, Eye, Info, XCircle } from 'lucide-react'
import { gradeQuestion, isAnswered, TYPE_LABELS } from '../lib/grading'
import { AI_STATUS_LABELS, formatAnswer } from '../lib/aiAnswer'
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
        <RichText text={q.question} className="text-lg text-slate-900" />
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
      {q.ai && <AiPanel q={q} ai={q.ai} />}
      {(q.explanation || q.references?.length > 0) && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-4">
          <h4 className="mb-2 flex items-center gap-2 font-semibold text-amber-900">
            <BookOpen className="h-4 w-4" />
            解析
          </h4>
          <RichText text={q.explanation} className="text-base text-slate-800" />
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

/** AI 判讀版題庫（sc200.ai.json）的判讀資訊：只在作答後、題目帶有 ai 物件時顯示 */
function AiPanel({ q, ai }) {
  const fromAi = ai.practiceSource === 'ai'
  return (
    <div className="space-y-3 rounded-lg border border-indigo-200 bg-indigo-50/60 p-4 text-sm text-slate-800">
      <div className="flex flex-wrap items-center gap-2">
        <h4 className="flex items-center gap-2 font-semibold text-indigo-900">
          <Bot className="h-4 w-4" />
          AI 判讀
        </h4>
        <span className="rounded-full bg-indigo-100 px-2 py-0.5 text-xs font-medium text-indigo-800">
          {AI_STATUS_LABELS[ai.answerStatus] ?? ai.answerStatus}
        </span>
        <span className="rounded-full bg-slate-200 px-2 py-0.5 text-xs text-slate-700">AI 判讀，未經獨立驗證</span>
        {ai.evidenceLevel && <span className="text-xs text-slate-600">官方證據等級：{ai.evidenceLevel}（不是正確率）</span>}
        <span className="text-xs text-slate-600">人工複核：{ai.reviewStatus}</span>
      </div>

      {!fromAi && (
        <p className="flex gap-2 rounded-md bg-white/70 px-3 py-2 text-slate-700">
          <Info className="mt-0.5 h-4 w-4 shrink-0" />
          {ai.note || '目前以原題庫答案評分'}
        </p>
      )}
      {ai.answerStatus === 'differ' && (
        <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-amber-900">
          <p className="font-semibold">AI 判讀與原題庫答案不同，本題以 AI 答案評分</p>
          <p className="mt-1">原題庫答案（供參考）：{formatAnswer(q, q.originalAnswer)}</p>
        </div>
      )}
      {ai.conflicts?.length > 0 && (
        <div className="flex gap-2 rounded-md border border-rose-300 bg-rose-50 px-3 py-2 text-rose-900">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            <p className="font-semibold">官方證據與 AI 判讀存在衝突</p>
            <ul className="mt-1 list-inside list-disc">
              {ai.conflicts.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {ai.community && (
        <p className="text-slate-700">
          ExamTopics 社群共識答案：{formatAnswer(q, ai.community.answer)}
          {ai.community.agreesWithOriginal ? '（與原題庫答案相同）' : '（與原題庫答案不同）'}
        </p>
      )}

      {ai.explanation && <RichText text={ai.explanation} className="text-base text-slate-800" />}

      {ai.references?.length > 0 && (
        <div className="border-t border-indigo-200 pt-3">
          <p className="mb-1 font-semibold text-indigo-900">AI 引用的官方文件</p>
          <ul className="space-y-2">
            {ai.references.map((r) => (
              <li key={r.url}>
                <a href={r.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-blue-700 hover:underline">
                  <ExternalLink className="h-3.5 w-3.5" />
                  {r.title}
                </a>
                {r.quotes.map((x, i) => (
                  <blockquote key={i} className="mt-1 border-l-2 border-indigo-300 pl-2 text-slate-600">
                    {x.quote}
                  </blockquote>
                ))}
              </li>
            ))}
          </ul>
        </div>
      )}
      {ai.unreferencedSources?.length > 0 && (
        <div className="border-t border-indigo-200 pt-3 text-slate-600">
          <p className="mb-1">檢索到、未引用（不代表支持答案）</p>
          <ul className="space-y-1">
            {ai.unreferencedSources.map((r) => (
              <li key={r.url}>
                <a href={r.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:underline">
                  <ExternalLink className="h-3.5 w-3.5" />
                  {r.title}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
