/** 題目在來源 PDF 的位置（題組、題號、頁碼），方便對照原始題庫 */
export default function SourceLabel({ source, className = 'mt-0.5' }) {
  const parts = [
    source.section,
    source.number && `QUESTION ${source.number}`,
    source.pages?.length > 0 && `PDF 第 ${source.pages.join('、')} 頁`,
  ].filter(Boolean)
  return <div className={`text-xs text-slate-400 ${className}`}>原題：{parts.join(' · ')}</div>
}
