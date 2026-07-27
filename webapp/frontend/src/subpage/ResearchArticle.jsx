import { useMemo, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useTheme, th } from '../theme'
import { COMPUTE, findArticle, findLooseArticle } from '../data/researchMock'

function InteractiveBlock({ block }) {
  const isDark = useTheme()
  const c = th(isDark)
  const [values, setValues] = useState(() =>
    Object.fromEntries(block.params.map(p => [p.key, p.default]))
  )

  const data = useMemo(() => COMPUTE[block.id]?.(values) ?? [], [block.id, values])

  return (
    <div className={`${c.card} border ${c.b1} rounded-xl p-5 md:p-6 flex flex-col gap-6`}>
      <p className={`${c.t3} text-sm`}>{block.description}</p>

      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-x-6 gap-y-4">
        {block.params.map(p => (
          <label key={p.key} className="flex flex-col gap-1.5">
            <span className={`${c.t4} text-[11px] font-medium uppercase tracking-wider flex justify-between`}>
              <span>{p.label}</span>
              <span className={`${c.t2} font-mono`}>{values[p.key]}</span>
            </span>
            <input
              type="range"
              min={p.min}
              max={p.max}
              step={p.step}
              value={values[p.key]}
              onChange={e => setValues(v => ({ ...v, [p.key]: parseFloat(e.target.value) }))}
              className="w-full accent-current"
            />
          </label>
        ))}
      </div>

      <div className="h-64 -ml-2">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={c.chartGrid} vertical={false} />
            <XAxis
              dataKey="x"
              tick={{ fill: c.chartText, fontSize: 11 }}
              stroke={c.chartBorder}
              label={{ value: block.chart.xLabel, position: 'insideBottom', offset: -4, fill: c.chartText, fontSize: 11 }}
            />
            <YAxis
              tick={{ fill: c.chartText, fontSize: 11 }}
              stroke={c.chartBorder}
              label={{ value: block.chart.yLabel, angle: -90, position: 'insideLeft', fill: c.chartText, fontSize: 11 }}
              width={70}
            />
            <Tooltip
              contentStyle={{ background: c.chartBg, border: `1px solid ${c.chartBorder}`, borderRadius: 8, fontSize: 12 }}
              labelStyle={{ color: c.chartText }}
            />
            <Line type="monotone" dataKey="y" stroke="#7b8cde" strokeWidth={2} dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

function Block({ block }) {
  const isDark = useTheme()
  const c = th(isDark)
  switch (block.type) {
    case 'heading':
      return <h3 className={`font-serif ${c.t1} font-medium text-2xl mt-2`}>{block.content}</h3>
    case 'text':
      return <p className={`${c.t2} text-[15px] leading-[1.75]`}>{block.content}</p>
    case 'interactive':
      return <InteractiveBlock block={block} />
    default:
      return null
  }
}

function ArticleBody({ article, backTo, backLabel }) {
  const isDark = useTheme()
  const c = th(isDark)

  return (
    <div className="flex flex-col gap-8 max-w-3xl">
      <div className="flex flex-col gap-3">
        <Link to={backTo} className={`${c.t4} text-xs font-medium uppercase tracking-wider no-underline hover:${c.t2} w-fit`}>
          ← {backLabel}
        </Link>
        <h1 className={`font-serif ${c.t1} font-medium text-3xl md:text-[40px] leading-[1.1]`}>{article.title}</h1>
        <p className={`${c.t4} text-xs font-mono`}>{article.date}</p>
      </div>

      <div className="flex flex-col gap-6">
        {article.blocks.map((block, i) => (
          <Block key={i} block={block} />
        ))}
      </div>
    </div>
  )
}

export default function ResearchArticle() {
  const { topic: topicSlug, article: articleSlug } = useParams()
  const found = findArticle(topicSlug, articleSlug)

  if (!found) return <Navigate to="/research" replace />
  const { topic, article } = found

  return <ArticleBody article={article} backTo={`/research/${topic.slug}`} backLabel={topic.title} />
}

export function ResearchLooseArticle() {
  const { article: articleSlug } = useParams()
  const article = findLooseArticle(articleSlug)

  if (!article) return <Navigate to="/research" replace />

  return <ArticleBody article={article} backTo="/research" backLabel="Latest" />
}
