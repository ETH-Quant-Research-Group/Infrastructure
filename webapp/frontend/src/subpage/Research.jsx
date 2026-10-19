import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTheme, th } from '../theme'
import { TOPICS, allArticlesFeed } from '../data/researchMock'

export default function Research() {
  const isDark = useTheme()
  const c = th(isDark)
  const feed = useMemo(() => allArticlesFeed(), [])
  const [filter, setFilter] = useState('all')

  const filtered = filter === 'all' ? feed : feed.filter(({ topic }) => topic?.slug === filter)

  return (
    <div className="flex flex-col gap-8 max-w-3xl">
      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-2.5">
          <span className={`inline-block w-6 h-px ${isDark ? 'bg-zinc-700' : 'bg-zinc-300'}`} />
          <span className={`text-xs font-medium uppercase tracking-[0.25em] ${c.t4}`}>Research</span>
        </div>
        <h2 className={`font-serif ${c.t1} font-medium text-3xl md:text-[40px] leading-[1.1]`}>Topics</h2>
        <p className={`${c.t3} text-sm max-w-xl`}>Ordered to build from first principles to applied strategy. Filter by chapter, or browse everything in order.</p>
      </div>

      <div className="flex items-center gap-5 flex-wrap">
        <button
          onClick={() => setFilter('all')}
          className={`text-xs font-medium uppercase tracking-[0.15em] pb-2 border-b-2 transition-colors border-0 bg-transparent cursor-pointer ${
            filter === 'all' ? `${c.t1} border-current` : `${c.t4} border-transparent hover:${isDark ? 'text-zinc-300' : 'text-zinc-600'}`
          }`}
        >
          All
        </button>
        {TOPICS.map(topic => (
          <button
            key={topic.slug}
            onClick={() => setFilter(topic.slug)}
            className={`text-xs font-medium uppercase tracking-[0.15em] pb-2 border-b-2 transition-colors border-0 bg-transparent cursor-pointer ${
              filter === topic.slug ? `${c.t1} border-current` : `${c.t4} border-transparent hover:${isDark ? 'text-zinc-300' : 'text-zinc-600'}`
            }`}
          >
            {topic.title}
          </button>
        ))}
      </div>

      <div className={`flex flex-col border-t ${c.b1}`}>
        {filtered.map(({ article, topic }) => (
          <Link
            key={article.slug}
            to={topic ? `/research/${topic.slug}/${article.slug}` : `/research/loose/${article.slug}`}
            className={`border-b ${c.b1} py-5 flex flex-col gap-1.5 no-underline group`}
          >
            <div className="flex items-baseline justify-between gap-4">
              <span className={`font-serif text-lg md:text-xl font-medium ${c.t1} transition-colors group-hover:${c.t2}`}>{article.title}</span>
              <span className={`${c.t4} text-[11px] font-mono uppercase tracking-wider shrink-0`}>{article.date}</span>
            </div>
            <p className={`${c.t3} text-sm leading-relaxed`}>{article.summary}</p>
            <span className={`${c.t4} text-[11px] uppercase tracking-wider`}>{topic ? topic.title : 'Unfiled'}</span>
          </Link>
        ))}
      </div>
    </div>
  )
}
