import { useState } from 'react'
import { ArrowDown, ArrowUp, ChevronRight, GripVertical, Plus, X } from 'lucide-react'

const DND_TYPE = 'application/x-sc200-item'

export default function DragDropOrder({ q, answer, onChange, revealed, grade }) {
  const order = answer || []
  const byId = Object.fromEntries(q.items.map((it) => [it.id, it]))
  const pool = q.items.filter((it) => !order.includes(it.id))
  const full = order.length >= q.answer.length
  const [dragOver, setDragOver] = useState(null) // 'pool' | 'answer' | index

  const insertAt = (id, index) => {
    const next = order.filter((x) => x !== id)
    if (!order.includes(id) && full) return // 答案區已滿
    next.splice(index ?? next.length, 0, id)
    onChange(next)
  }
  const remove = (id) => onChange(order.filter((x) => x !== id))
  const move = (i, delta) => {
    const j = i + delta
    if (j < 0 || j >= order.length) return
    const next = [...order]
    ;[next[i], next[j]] = [next[j], next[i]]
    onChange(next)
  }

  const dragProps = (id) =>
    revealed
      ? {}
      : {
          draggable: true,
          onDragStart: (e) => {
            e.dataTransfer.setData(DND_TYPE, id)
            e.dataTransfer.effectAllowed = 'move'
          },
          onDragEnd: () => setDragOver(null),
        }
  const dropProps = (target, handler) =>
    revealed
      ? {}
      : {
          onDragOver: (e) => {
            e.preventDefault()
            e.stopPropagation()
            setDragOver(target)
          },
          onDragLeave: () => setDragOver((t) => (t === target ? null : t)),
          onDrop: (e) => {
            e.preventDefault()
            e.stopPropagation()
            setDragOver(null)
            const id = e.dataTransfer.getData(DND_TYPE)
            if (id) handler(id)
          },
        }

  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-[1fr_auto_1fr]">
        {/* 待選區 */}
        <section
          {...dropProps('pool', remove)}
          className={`rounded-xl border-2 border-dashed p-3 transition ${
            dragOver === 'pool' ? 'border-blue-400 bg-blue-50' : 'border-slate-300 bg-slate-50'
          }`}
        >
          <h4 className="mb-2 text-sm font-semibold text-slate-600">Actions（待選區）</h4>
          <ul className="space-y-2">
            {pool.map((it) => (
              <li
                key={it.id}
                {...dragProps(it.id)}
                className={`flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 shadow-sm ${
                  revealed ? 'opacity-60' : 'cursor-grab active:cursor-grabbing'
                }`}
              >
                <GripVertical className="h-4 w-4 shrink-0 text-slate-500" />
                <span className="flex-1 text-base leading-[1.65]">{it.text}</span>
                {!revealed && (
                  <button
                    type="button"
                    disabled={full}
                    onClick={() => insertAt(it.id)}
                    title={full ? '答案區已滿' : '加入答案區'}
                    className="rounded-md p-1 text-blue-600 hover:bg-blue-50 disabled:text-slate-300 disabled:hover:bg-transparent"
                  >
                    <Plus className="h-4 w-4" />
                  </button>
                )}
              </li>
            ))}
            {pool.length === 0 && <li className="py-4 text-center text-sm text-slate-500">（已無可選項目）</li>}
          </ul>
        </section>

        <div className="hidden items-center justify-center md:flex">
          <ChevronRight className="h-8 w-8 text-slate-300" />
        </div>

        {/* 答案區 */}
        <section
          {...dropProps('answer', (id) => insertAt(id))}
          className={`rounded-xl border-2 border-dashed p-3 transition ${
            dragOver === 'answer' ? 'border-blue-400 bg-blue-50' : 'border-blue-200 bg-white'
          }`}
        >
          <h4 className="mb-2 text-sm font-semibold text-slate-600">
            Answer Area（依序排列 {q.answer.length} 個步驟）
          </h4>
          <ol className="space-y-2">
            {order.map((id, i) => {
              const ok = grade?.detail?.[i]
              const tone = revealed
                ? ok
                  ? 'border-emerald-400 bg-emerald-50'
                  : 'border-rose-400 bg-rose-50'
                : dragOver === i
                  ? 'border-blue-500 bg-blue-50'
                  : 'border-slate-200 bg-white'
              return (
                <li
                  key={id}
                  {...dragProps(id)}
                  {...dropProps(i, (dragged) => insertAt(dragged, i))}
                  className={`flex items-center gap-2 rounded-lg border-2 px-3 py-2 shadow-sm ${tone} ${
                    revealed ? '' : 'cursor-grab active:cursor-grabbing'
                  }`}
                >
                  <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-blue-600 text-xs font-bold text-white">
                    {i + 1}
                  </span>
                  <span className="flex-1 text-base leading-[1.65]">{byId[id]?.text}</span>
                  {!revealed && (
                    <span className="flex shrink-0 items-center">
                      <IconBtn onClick={() => move(i, -1)} disabled={i === 0} title="上移">
                        <ArrowUp className="h-4 w-4" />
                      </IconBtn>
                      <IconBtn onClick={() => move(i, 1)} disabled={i === order.length - 1} title="下移">
                        <ArrowDown className="h-4 w-4" />
                      </IconBtn>
                      <IconBtn onClick={() => remove(id)} title="移除" danger>
                        <X className="h-4 w-4" />
                      </IconBtn>
                    </span>
                  )}
                </li>
              )
            })}
            {Array.from({ length: Math.max(0, q.answer.length - order.length) }).map((_, i) => (
              <li
                key={`empty-${i}`}
                className="flex items-center gap-2 rounded-lg border-2 border-dashed border-slate-200 px-3 py-2 text-sm text-slate-500"
              >
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-slate-200 text-xs font-bold text-slate-600">
                  {order.length + i + 1}
                </span>
                拖曳或點擊 + 加入步驟
              </li>
            ))}
          </ol>
        </section>
      </div>

      {revealed && !grade?.correct && (
        <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-3">
          <h4 className="mb-1.5 text-sm font-semibold text-emerald-800">正確順序</h4>
          <ol className="list-inside list-decimal space-y-1 text-sm text-emerald-900">
            {q.answer.map((id) => (
              <li key={id}>{byId[id]?.text}</li>
            ))}
          </ol>
        </div>
      )}
    </div>
  )
}

function IconBtn({ children, danger, ...props }) {
  return (
    <button
      type="button"
      {...props}
      className={`rounded-md p-1 disabled:text-slate-300 disabled:hover:bg-transparent ${
        danger ? 'text-rose-500 hover:bg-rose-50' : 'text-slate-600 hover:bg-slate-100'
      }`}
    >
      {children}
    </button>
  )
}
