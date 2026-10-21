import { Fragment } from 'react'

// 支援 `inline code`、**粗體**、"- " 開頭的清單行與換行（空行分段）
function renderInline(text, keyPrefix) {
  return text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).map((part, i) => {
    const key = `${keyPrefix}-${i}`
    if (part.startsWith('`') && part.endsWith('`') && part.length > 1)
      return (
        <code key={key} className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[0.9em] text-pink-700">
          {part.slice(1, -1)}
        </code>
      )
    if (part.startsWith('**') && part.endsWith('**') && part.length > 3)
      return (
        <strong key={key} className="font-semibold text-slate-900">
          {part.slice(2, -2)}
        </strong>
      )
    return <Fragment key={key}>{part}</Fragment>
  })
}

// ``` 圍起來的區塊以 CodeBlock 呈現，其餘依段落處理
export default function RichText({ text, className = '' }) {
  if (!text) return null
  const chunks = text.split(/```[\w-]*\n?([\s\S]*?)```/)
  return (
    <div className={`space-y-3 leading-[1.75] ${className}`}>
      {chunks.map((chunk, i) =>
        i % 2 === 1 ? <CodeBlock key={i} code={chunk.replace(/\n$/, '')} /> : <Paragraphs key={i} text={chunk.trim()} />,
      )}
    </div>
  )
}

function Paragraphs({ text }) {
  if (!text) return null
  const paragraphs = text.split(/\n\s*\n/)
  return (
    <>
      {paragraphs.map((p, i) => (
        <p key={i}>
          {p.split('\n').map((line, j) => (
            <Fragment key={j}>
              {j > 0 && <br />}
              {line.startsWith('- ') ? (
                <>
                  <span className="mr-1.5 text-blue-500">•</span>
                  {renderInline(line.slice(2), `${i}-${j}`)}
                </>
              ) : (
                renderInline(line, `${i}-${j}`)
              )}
            </Fragment>
          ))}
        </p>
      ))}
    </>
  )
}

export function CodeBlock({ code, children }) {
  return (
    <pre className="overflow-x-auto rounded-lg bg-slate-900 p-4 font-mono text-sm leading-8 text-slate-100 shadow-inner">
      {children ?? code}
    </pre>
  )
}
