import QuestionBlock from './QuestionBlock'
import CaseStudy from './questions/CaseStudy'

/**
 * 頂層題目分派：一般題型交給 QuestionBlock，case_study 交給 CaseStudy。
 * isRevealed(key) / onReveal(key) 的 key：一般題為 q.id，題組子題為 `${q.id}/${sub.id}`。
 * onReveal 為 null 時（考試模式 / 檢討模式）不顯示「對答案」按鈕。
 */
export default function QuestionView({ q, answer, onChange, isRevealed, onReveal }) {
  if (q.type === 'case_study') {
    return (
      <CaseStudy
        key={q.id}
        q={q}
        answer={answer}
        onChange={onChange}
        isRevealed={(subId) => isRevealed(`${q.id}/${subId}`)}
        onReveal={onReveal ? (subId) => onReveal(`${q.id}/${subId}`) : null}
      />
    )
  }
  return (
    <QuestionBlock
      key={q.id}
      q={q}
      answer={answer}
      onChange={onChange}
      revealed={isRevealed(q.id)}
      onCheck={onReveal ? () => onReveal(q.id) : null}
    />
  )
}
