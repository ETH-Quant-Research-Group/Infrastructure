import { Link, Navigate, useParams } from 'react-router-dom'
import { useTheme, th } from '../theme'
import { TOPICS } from '../data/researchMock'

export default function ResearchTopic() {
  const { topic: topicSlug } = useParams()
  const isDark = useTheme()
  const c = th(isDark)
  const topic = TOPICS.find(t => t.slug === topicSlug)

  if (!topic) return <Navigate to="/research" replace />

  return (
    <div className="flex flex-col gap-8 max-w-4xl">
      <div className="flex flex-col gap-3">
        <Link to="/research" className={`${c.t4} text-xs font-medium uppercase tracking-wider no-underline hover:${c.t2} w-fit`}>
          ← Themes
        </Link>
        <h2 className={`font-serif ${c.t1} font-medium text-3xl md:text-[40px] leading-[1.1]`}>{topic.title}</h2>
        <p className={`${c.t3} text-sm max-w-xl`}>{topic.description}</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {topic.articles.map(article => (
          <Link
            key={article.slug}
            to={`/research/${topic.slug}/${article.slug}`}
            className={`${c.card} border ${c.b1} hover:${c.b3} rounded-xl p-5 flex flex-col gap-2 no-underline transition-colors`}
          >
            <p className={`${c.t4} text-[11px] font-mono uppercase tracking-wider`}>{article.date}</p>
            <p className={`font-serif ${c.t1} font-medium text-lg leading-snug`}>{article.title}</p>
            <p className={`${c.t3} text-sm leading-snug`}>{article.summary}</p>
          </Link>
        ))}
      </div>
    </div>
  )
}
