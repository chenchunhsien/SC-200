import { Check, X } from 'lucide-react'

export default function StatementMatrix({ q, answer, onChange, revealed, grade }) {
  const value = answer || {}
  const [yesLabel, noLabel] = q.columns || ['Yes', 'No']

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200">
      <table className="w-full table-fixed border-collapse bg-white text-left">
        <thead className="bg-slate-100 text-sm text-slate-600">
          <tr>
            <th className="px-4 py-2.5 font-semibold">Statements</th>
            <th className="w-16 px-1 py-2.5 text-center font-semibold">{yesLabel}</th>
            <th className="w-16 px-1 py-2.5 text-center font-semibold">{noLabel}</th>
            {revealed && <th className="w-20 px-1 py-2.5 text-center font-semibold">正解</th>}
          </tr>
        </thead>
        <tbody>
          {q.statements.map((s) => {
            const ok = grade?.detail?.[s.id]
            const rowTone = revealed ? (ok ? 'bg-emerald-50' : 'bg-rose-50') : 'hover:bg-slate-50'
            return (
              <tr key={s.id} className={`border-t border-slate-200 ${rowTone}`}>
                <td className="break-words px-4 py-3 leading-[1.65]">{s.text}</td>
                {[true, false].map((v) => (
                  <td key={String(v)} className="px-2 py-3 text-center">
                    <input
                      type="radio"
                      name={`${q.id}-${s.id}`}
                      checked={value[s.id] === v}
                      disabled={revealed}
                      onChange={() => onChange({ ...value, [s.id]: v })}
                      className="h-5 w-5 cursor-pointer accent-blue-600 disabled:cursor-default"
                      aria-label={`${s.text} — ${v ? yesLabel : noLabel}`}
                    />
                  </td>
                ))}
                {revealed && (
                  <td className="px-2 py-3 text-center">
                    <span
                      className={`inline-flex items-center gap-1 text-sm font-semibold ${
                        ok ? 'text-emerald-700' : 'text-rose-700'
                      }`}
                    >
                      {ok ? <Check className="h-4 w-4" /> : <X className="h-4 w-4" />}
                      {s.answer ? yesLabel : noLabel}
                    </span>
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
