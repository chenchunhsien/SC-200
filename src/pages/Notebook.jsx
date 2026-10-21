import { useState } from 'react'
import { Play, Star, Trash2, XCircle } from 'lucide-react'
import { previewText, TYPE_LABELS } from '../lib/grading'
import { qKey, useStore } from '../lib/store'

export default function Notebook({ bank, onStart, initialTab = 'mistakes' }) {
  const { mistakes, starred, history, removeMistake, toggleStar } = useStore()
  const [tab, setTab] = useState(initialTab)
  const chapterTitle = Object.fromEntries(bank.chapters.map((c) => [c.id, c.title]))

  const isMistakes = tab === 'mistakes'
  const source = isMistakes ? mistakes : starred
  const list = bank.questions.filter((q) => source[qKey(bank.id, q.id)])

  const practice = (questions, title) =>
    onStart({ title, questions, mode: 'practice', source: isMistakes ? 'mistakes' : 'starred' })

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <div className="inline-flex rounded-lg bg-slate-200 p-1">
          <TabBtn active={isMistakes} onClick={() => setTab('mistakes')} icon={XCircle}>
            錯題本（{bank.questions.filter((q) => mistakes[qKey(bank.id, q.id)]).length}）
          </TabBtn>
          <TabBtn active={!isMistakes} onClick={() => setTab('starred')} icon={Star}>
            收藏（{bank.questions.filter((q) => starred[qKey(bank.id, q.id)]).length}）
          </TabBtn>
        </div>
        <button
          type="button"
          disabled={!list.length}
          onClick={() => practice(list, isMistakes ? '錯題重練' : '收藏題目')}
          className="ml-auto inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 disabled:bg-slate-300"
        >
          <Play className="h-4 w-4" />
          全部練習
        </button>
      </div>

      {isMistakes && (
        <p className="text-sm text-slate-600">答錯的題目會自動加入錯題本；在「錯題重練」中答對即自動移出。</p>
      )}

      <ul className="space-y-3">
        {list.map((q) => {
          const key = qKey(bank.id, q.id)
          const h = history[key]
          return (
            <li key={q.id} className="flex items-center gap-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
              <div className="min-w-0 flex-1">
                <div className="mb-1 flex flex-wrap items-center gap-2 text-xs">
                  <span className="rounded bg-blue-100 px-1.5 py-0.5 font-semibold text-blue-700">
                    {TYPE_LABELS[q.type]}
                  </span>
                  <span className="text-slate-600">{chapterTitle[q.chapter]}</span>
                  {isMistakes && (
                    <span className="text-rose-600">錯 {mistakes[key].wrongCount} 次</span>
                  )}
                  {h && (
                    <span className="text-slate-500">
                      作答 {h.attempts} 次 · 答對 {h.correctCount} 次
                    </span>
                  )}
                </div>
                <p className="truncate text-sm text-slate-800">{previewText(q, 160)}</p>
              </div>
              <button
                type="button"
                onClick={() => practice([q], `單題練習 · ${q.id}`)}
                title="練習此題"
                className="shrink-0 rounded-lg p-2 text-blue-600 hover:bg-blue-50"
              >
                <Play className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={() => (isMistakes ? removeMistake(bank.id, q.id) : toggleStar(bank.id, q.id))}
                title={isMistakes ? '移出錯題本' : '取消收藏'}
                className="shrink-0 rounded-lg p-2 text-slate-500 hover:bg-rose-50 hover:text-rose-600"
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </li>
          )
        })}
        {!list.length && (
          <li className="rounded-xl border-2 border-dashed border-slate-200 py-16 text-center text-slate-500">
            {isMistakes ? '目前沒有錯題，繼續保持！' : '尚未收藏任何題目，作答時點擊「收藏」即可加入。'}
          </li>
        )}
      </ul>
    </div>
  )
}

function TabBtn({ active, onClick, icon: Icon, children }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-semibold transition ${
        active ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600'
      }`}
    >
      <Icon className="h-4 w-4" />
      {children}
    </button>
  )
}
