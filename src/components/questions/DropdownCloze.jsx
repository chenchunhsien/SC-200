import { Fragment } from 'react'
import { BLANK_PATTERN } from '../../lib/grading'
import { CodeBlock } from '../RichText'

export default function DropdownCloze({ q, answer, onChange, revealed, grade }) {
  const value = answer || {}
  const isCode = q.templateType !== 'text'
  const blanks = Object.fromEntries(q.blanks.map((b) => [b.id, b]))
  // split 搭配捕獲群組：奇數索引即為挖空 id
  const parts = q.template.split(BLANK_PATTERN)

  const renderSelect = (id, idx) => {
    const blank = blanks[id]
    if (!blank) return <span key={idx} className="text-rose-500">{`{{${id}}}`}</span>
    const ok = grade?.detail?.[id]
    let tone = isCode ? 'border-sky-400 bg-slate-800 text-sky-200' : 'border-blue-400 bg-blue-50 text-blue-900'
    if (revealed) tone = ok ? 'border-emerald-400 bg-emerald-50 text-emerald-800' : 'border-rose-400 bg-rose-50 text-rose-800'
    return (
      <span key={idx} className="inline-flex items-center gap-1 align-middle">
        <select
          value={value[id] ?? ''}
          disabled={revealed}
          onChange={(e) => onChange({ ...value, [id]: e.target.value })}
          className={`rounded-md border-2 px-2 py-0.5 font-mono text-sm font-semibold outline-none focus:ring-2 focus:ring-blue-400 ${tone}`}
        >
          <option value="" disabled>
            — 選擇 —
          </option>
          {blank.options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
        {revealed && !ok && (
          <span className="rounded bg-emerald-600 px-1.5 py-0.5 font-mono text-xs font-semibold text-white">
            {blank.answer}
          </span>
        )}
      </span>
    )
  }

  const content = parts.map((part, i) => (i % 2 === 1 ? renderSelect(part, i) : <Fragment key={i}>{part}</Fragment>))

  return isCode ? (
    <CodeBlock>{content}</CodeBlock>
  ) : (
    <div className="whitespace-pre-wrap rounded-lg border border-slate-200 bg-white p-4 leading-10">{content}</div>
  )
}
