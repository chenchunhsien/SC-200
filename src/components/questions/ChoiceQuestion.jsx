import { Check, X } from 'lucide-react'

// single_choice 與 multiple_choice 共用
export default function ChoiceQuestion({ q, answer, onChange, revealed }) {
  const multi = q.type === 'multiple_choice'
  const selected = multi ? answer || [] : answer ? [answer] : []
  const correctIds = multi ? q.answer : [q.answer]

  const toggle = (id) => {
    if (revealed) return
    if (!multi) return onChange(id)
    onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id].sort())
  }

  return (
    <div className="space-y-2.5">
      {multi && (
        <p className="text-sm text-slate-600">
          請選擇 {q.answer.length} 個答案（已選 {selected.length} 個）
        </p>
      )}
      {q.options.map((opt) => {
        const isSel = selected.includes(opt.id)
        const isCorrect = correctIds.includes(opt.id)
        let tone = isSel
          ? 'border-blue-500 bg-blue-50'
          : 'border-slate-200 bg-white hover:border-blue-300 hover:bg-slate-50'
        if (revealed) {
          if (isCorrect) tone = 'border-emerald-500 bg-emerald-50'
          else if (isSel) tone = 'border-rose-500 bg-rose-50'
          else tone = 'border-slate-200 bg-white opacity-70'
        }
        return (
          <button
            key={opt.id}
            type="button"
            onClick={() => toggle(opt.id)}
            disabled={revealed}
            className={`flex w-full items-start gap-3 rounded-lg border-2 px-4 py-3 text-left transition ${tone} ${
              revealed ? 'cursor-default' : 'cursor-pointer'
            }`}
          >
            <span
              className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center border-2 text-xs font-bold ${
                multi ? 'rounded-md' : 'rounded-full'
              } ${isSel ? 'border-blue-600 bg-blue-600 text-white' : 'border-slate-300 text-slate-600'}`}
            >
              {opt.id}
            </span>
            <span className="flex-1 text-base leading-[1.65]">{opt.text}</span>
            {revealed && isCorrect && <Check className="h-5 w-5 shrink-0 text-emerald-600" />}
            {revealed && isSel && !isCorrect && <X className="h-5 w-5 shrink-0 text-rose-600" />}
          </button>
        )
      })}
    </div>
  )
}
