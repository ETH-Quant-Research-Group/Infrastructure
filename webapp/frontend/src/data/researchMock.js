// Stand-in for a future build step that walks the ETH-Quant-Research-Group/
// Research-Blog repo and compiles each notebook into this shape:
//   - each top-level folder in that repo is one TOPICS entry (a chapter) —
//     folder name "NN-topic-slug" (e.g. "01-derivatives-foundations"); the
//     leading number is stripped for the compiled `slug`/URL and kept only
//     to set the chapter's position, since plain git folders have no
//     inherent order otherwise (only alphabetical).
//   - each notebook inside a topic folder is one article, in the order the
//     notebook files themselves are numbered/named within that folder.
// Cell tags map like:
//   "title"     -> the article's `title` (first one wins, one per notebook)
//   "subtitle"  -> the article's `summary`
//   "text"      -> { type: 'text', content }
//   "heading"   -> { type: 'heading', content }
//   "param"     -> the sliders/inputs feeding an "interactive" block
//   "chart"     -> the chart an "interactive" block renders
// See ~/Documents/Projects/Research-Blog/01-introduction-to-financial-markets/
// test.ipynb for a worked example of all six tags in one notebook.
// `compute` below stands in for whatever the real recompute strategy ends up
// being (precomputed grid lookup vs. a live backend call) — for now it just
// runs client-side so the article/interactivity UI can be evaluated on its
// own before that decision is made.

export const TOPICS = [
  {
    slug: 'derivatives-foundations',
    title: 'Derivatives Foundations',
    description: 'Start here — the vocabulary and mechanics everything else in this research builds on.',
    articles: [
      {
        slug: 'intro-to-derivatives',
        title: 'Intro to Derivatives',
        date: '2026-05-10',
        summary: 'What a derivative actually is, why it exists, and the handful of contract types (forwards, futures, perpetuals, options) everything else here assumes you know.',
        blocks: [
          {
            type: 'text',
            content: 'A derivative is a contract whose value is derived from something else — an underlying asset, rate, or index — rather than being that asset itself. You can get exposure to Bitcoin\'s price without holding Bitcoin, or to an interest rate without holding a bond.',
          },
          {
            type: 'heading',
            content: 'The four shapes that cover most of it',
          },
          {
            type: 'text',
            content: 'Forwards and futures both obligate two parties to transact at a set price on a future date — the difference is just standardization and daily settlement (futures) versus a private, one-off agreement (forwards). Perpetuals are futures with no expiry, kept tethered to spot via periodic funding payments instead. Options grant the right, not the obligation, to transact — which is what makes them worth a premium.',
          },
          {
            type: 'text',
            content: 'Everything in the Funding Rate Arbitrage and Market Microstructure chapters assumes this vocabulary — perpetual, basis, funding, participation rate — as given. Start here if any of that was unfamiliar.',
          },
        ],
      },
      {
        slug: 'spot-futures-perpetuals',
        title: 'Spot vs. Futures vs. Perpetuals',
        date: '2026-05-17',
        summary: 'Why the same asset can trade at three different prices at once, and what actually links them back together.',
        blocks: [
          {
            type: 'text',
            content: 'Spot is the asset itself, settled now. A futures contract trades at a premium or discount to spot depending on time-to-expiry, funding cost, and expectations — this gap is the "basis," and it mechanically converges to zero at expiry since the contract settles into the underlying.',
          },
          {
            type: 'text',
            content: 'Perpetuals have no expiry, so there\'s no settlement date to force convergence — instead, the funding rate (covered in depth in the next chapter) does that job continuously, paid between longs and shorts every few hours based on how far the perpetual has drifted from spot.',
          },
        ],
      },
    ],
  },
  {
    slug: 'funding-rate-arbitrage',
    title: 'Funding Rate Arbitrage',
    description: 'Perpetual-spot basis, funding carry, and decay dynamics.',
    articles: [
      {
        slug: 'perpetual-basis-decay',
        title: 'Modeling Perpetual Basis Decay',
        date: '2026-06-01',
        summary: 'How the basis between perpetual and spot closes over a funding horizon, and what that implies for position sizing.',
        blocks: [
          {
            type: 'text',
            content: 'Perpetual futures track spot via a funding payment exchanged between longs and shorts, typically every 8 hours. When funding is persistently positive, shorts are paid to hold the position, which pulls the perpetual price back toward spot over time — the "basis" decays.',
          },
          {
            type: 'heading',
            content: 'A simple decay model',
          },
          {
            type: 'text',
            content: 'Treating the basis as a mean-reverting process driven by the funding rate gives a rough first approximation: the basis shrinks geometrically each settlement, scaled by the annualized funding rate. It is not a precise forecast, but it is a useful sanity check before sizing a carry trade.',
          },
          {
            type: 'interactive',
            id: 'decay-sim',
            description: 'Adjust the funding rate and horizon to see the modeled basis decay.',
            params: [
              { key: 'fundingRate', label: 'Funding rate (bps / 8h)', type: 'slider', min: -5, max: 5, step: 0.1, default: 1.5 },
              { key: 'horizonDays', label: 'Horizon (days)', type: 'slider', min: 1, max: 90, step: 1, default: 30 },
              { key: 'startBasis', label: 'Starting basis (bps)', type: 'slider', min: 0, max: 200, step: 5, default: 80 },
            ],
            chart: { xLabel: 'Day', yLabel: 'Basis (bps)' },
          },
          {
            type: 'text',
            content: 'Note how a higher funding rate accelerates the pull toward zero — this is exactly the carry a delta-neutral funding-arb strategy is harvesting, at the cost of that basis (and the P&L it represents) shrinking as the trade matures.',
          },
        ],
      },
      {
        slug: 'annualized-carry-thresholds',
        title: 'Annualized Carry: What Counts as "Worth It"',
        date: '2026-06-14',
        summary: 'Back-of-envelope thresholds for when funding carry clears exchange fees, slippage, and hedge slippage.',
        blocks: [
          {
            type: 'text',
            content: 'Funding rates are usually quoted per 8-hour settlement. Annualizing them (rate × 3 settlements/day × 365) makes it possible to compare the carry against a familiar benchmark — a savings rate, a bond yield, or simply "is this worth the operational risk."',
          },
          {
            type: 'text',
            content: 'In practice, round-trip taker fees plus rebalancing slippage on a delta-neutral hedge tend to eat 15-40 annualized percentage points depending on venue and size. Anything below that band is not obviously worth running.',
          },
        ],
      },
    ],
  },
  {
    slug: 'market-microstructure',
    title: 'Market Microstructure',
    description: 'Order book dynamics, execution costs, and liquidity.',
    articles: [
      {
        slug: 'impact-vs-participation',
        title: 'Price Impact vs. Participation Rate',
        date: '2026-07-02',
        summary: 'A square-root impact model and how participation rate trades off execution speed against slippage.',
        blocks: [
          {
            type: 'text',
            content: 'Square-root impact models are a common first approximation for how much a market order moves the price: impact scales with the square root of the fraction of average daily volume you trade, not linearly. Trading twice as fast costs more than twice as much.',
          },
          {
            type: 'interactive',
            id: 'impact-sim',
            description: 'Adjust participation rate and daily volatility to see modeled impact across an execution window.',
            params: [
              { key: 'participation', label: 'Participation rate (% of ADV)', type: 'slider', min: 1, max: 50, step: 1, default: 10 },
              { key: 'volatility', label: 'Daily volatility (%)', type: 'slider', min: 0.5, max: 10, step: 0.5, default: 3 },
            ],
            chart: { xLabel: 'Minutes into execution', yLabel: 'Cumulative impact (bps)' },
          },
        ],
      },
    ],
  },
]

// Research that doesn't (yet) belong to a chapter — still shows up in the
// flat "Latest" feed below, just without a chapter to file under.
export const LOOSE_ARTICLES = [
  {
    slug: 'orderflow-clustering-weekend-notebook',
    title: 'Clustering Order Flow Patterns (Weekend Notebook)',
    date: '2026-07-20',
    summary: 'A quick unsupervised pass over tick-level order flow shapes. Interesting, but not clearly a Microstructure piece or its own new chapter yet.',
    blocks: [
      {
        type: 'text',
        content: 'Ran k-means over rolling 30-second windows of trade-sign imbalance and size, mostly to see if the clusters lined up with anything intuitive (sweep vs. iceberg vs. passive fill). They roughly do, but the notebook stops short of anything actionable — filed here rather than forced into a chapter.',
      },
    ],
  },
]

export function findArticle(topicSlug, articleSlug) {
  const topic = TOPICS.find(t => t.slug === topicSlug)
  if (!topic) return null
  const article = topic.articles.find(a => a.slug === articleSlug)
  if (!article) return null
  return { topic, article }
}

export function findLooseArticle(articleSlug) {
  return LOOSE_ARTICLES.find(a => a.slug === articleSlug) ?? null
}

// Every article, in reading order: chapters in their defined (foundational
// -> advanced) order, articles within a chapter in their defined build-up
// order, then unfiled pieces last (those have no curriculum position, so
// newest-first is the only sensible order for them).
export function allArticlesFeed() {
  const chaptered = TOPICS.flatMap(topic => topic.articles.map(article => ({ article, topic })))
  const loose = [...LOOSE_ARTICLES]
    .sort((a, b) => new Date(b.date) - new Date(a.date))
    .map(article => ({ article, topic: null }))
  return [...chaptered, ...loose]
}

// Client-side stand-ins for the eventual notebook-derived compute step.
// Keyed by interactive block id.
export const COMPUTE = {
  'decay-sim': ({ fundingRate, horizonDays, startBasis }) => {
    // Positive funding pulls the basis toward zero each settlement;
    // negative funding widens it instead. Not a forecast — just a shape.
    const dailyRate = Math.abs(fundingRate) * 3 * 0.0001 * 8
    return Array.from({ length: horizonDays + 1 }, (_, day) => {
      const y = fundingRate >= 0
        ? startBasis * Math.pow(1 - dailyRate, day)
        : startBasis * Math.pow(1 + dailyRate, day)
      return { x: day, y: Math.max(0, y) }
    })
  },
  'impact-sim': ({ participation, volatility }) => {
    const minutes = 120
    const step = 5
    const points = []
    for (let t = 0; t <= minutes; t += step) {
      const fractionDone = t / minutes
      const impact = 10 * volatility * Math.sqrt((participation / 100) * fractionDone)
      points.push({ x: t, y: impact })
    }
    return points
  },
}
