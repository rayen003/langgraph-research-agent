import type { KgNode, KgEdge } from '../hooks/useKnowledgeGraph'

/**
 * KG view model — analyst-oriented presentation layer over the raw graph.
 *
 * The raw KG stores one node per field (30+ per DCF run). Rendering all of them
 * is cluttered and mirrors storage, not how an analyst reads a company. This
 * layer collapses the raw graph into a small set of HUB nodes:
 *
 *     ticker (company)
 *        ├── News         (1, synthetic)   → all news_item
 *        ├── Financials   (1, synthetic)   → synthesis + metrics + thesis + …
 *        └── DCF Run      (N, real)        → assumptions/outputs table + decks
 *
 * The canvas renders ONLY hubs. Detail (individual fields, news, metrics, run
 * tables) lives in side panels keyed by hub. No DB change — purely derived.
 */

export const HUB_NEWS = '__hub_news__'
export const HUB_FINANCIALS = '__hub_financials__'

// Raw node_types that roll up into the Financials hub, grouped into category
// sub-hubs. Each distinct category becomes a child node under Financials when
// the user expands it (two-tier drill-down → table).
const FINANCIALS_TYPES = new Set([
  'company_synthesis',
  'thesis',
  'market_metric_fund',
  'market_metric_price',
  'company_lifecycle',
  'filing',
  'driver',
  'risk',
  'theme',
  'user_belief',
  'document_fact',
  'key_fact',
  'snippet_fact',
  // doc-extraction typed nodes (mapped from fact_type by documents.py)
  'guidance',
  'risk_factor',
  'competitive_moat',
  'capital_allocation',
])

// Maps a raw node_type → its Financials category sub-hub key + label.
const FIN_CATEGORY: Record<string, { key: string; label: string }> = {
  company_synthesis: { key: 'synthesis', label: 'Synthesis' },
  thesis: { key: 'thesis', label: 'Thesis' },
  market_metric_fund: { key: 'metrics', label: 'Fundamentals' },
  market_metric_price: { key: 'metrics', label: 'Fundamentals' },
  company_lifecycle: { key: 'lifecycle', label: 'Lifecycle' },
  filing: { key: 'filings', label: 'Filings' },
  driver: { key: 'drivers', label: 'Drivers' },
  risk: { key: 'drivers', label: 'Drivers' },
  theme: { key: 'drivers', label: 'Drivers' },
  user_belief: { key: 'beliefs', label: 'Beliefs' },
  // doc-extraction typed nodes
  guidance:          { key: 'thesis',   label: 'Thesis' },
  risk_factor:       { key: 'drivers',  label: 'Drivers' },
  competitive_moat:  { key: 'drivers',  label: 'Drivers' },
  capital_allocation:{ key: 'metrics',  label: 'Fundamentals' },
}

/** Route a document_fact node into the right Financials sub-category based on its
 *  semantic field (fact_type). Revenue / earnings / margin → Fundamentals;
 *  guidance → Thesis; risk / moat → Drivers; everything else → Synthesis. */
function getDocFactCategory(field: string): { key: string; label: string } {
  const metrics = new Set([
    'revenue', 'base_revenue', 'earnings', 'net_income', 'operating_income',
    'gross_profit', 'eps', 'shares_outstanding', 'margin', 'free_cash_flow',
    'growth_rate', 'fcff_margin', 'debt_metric', 'net_debt',
    'valuation_metric', 'capital_allocation', 'ebitda_margin',
    'effective_tax_rate',
  ])
  const thesis = new Set(['guidance', 'forward_looking'])
  const drivers = new Set(['risk_factor', 'competitive_moat', 'wacc_signal'])
  const f = (field || '').toLowerCase().replace(/[^a-z0-9_]/g, '_')
  if (metrics.has(f)) return { key: 'metrics', label: 'Fundamentals' }
  if (thesis.has(f)) return { key: 'thesis', label: 'Thesis' }
  if (drivers.has(f)) return { key: 'drivers', label: 'Drivers' }
  return { key: 'synthesis', label: 'Synthesis' }
}

const NEWS_TYPES = new Set(['news_item'])

// Run-scoped leaf types that belong to a dcf_run hub.
const RUN_LEAF_TYPES = new Set(['run_assumption', 'run_output', 'run_scenario'])

// Deck types nest under their parent dcf_run hub (via HAS_DECK / HAS_SLIDE).
const DECK_TYPES = new Set(['deck_run', 'deck_slide'])

export interface KgViewModel {
  /** Hub nodes to render on the canvas (company + synthetic News/Financials + dcf_run). */
  hubNodes: KgNode[]
  /** Edges between hubs (company→hub, company→run, run→run handled as members). */
  hubEdges: KgEdge[]
  /** hubId → raw member nodes (for the detail panel). */
  membersByHub: Map<string, KgNode[]>
  /** raw node id → owning hubId (for query row-glow + highlight rollup). */
  hubForRaw: Map<string, string>
  /** parent hubId → its collapsible child sub-hubs (Financials → categories). */
  childHubs: Map<string, KgNode[]>
  /** child sub-hub id → parent hubId (to decide canvas visibility on expand). */
  parentOfHub: Map<string, string>
}

function syntheticHub(
  ticker: string,
  kind: string,
  nodeType: string,
  members: KgNode[],
): KgNode {
  // Most-recent member timestamp drives the hub's freshness.
  const updated = members.reduce((m, n) => Math.max(m, n.updated_at || 0), 0)
  return {
    id: `${ticker}::${kind}`,
    session_id: members[0]?.session_id ?? null,
    ticker,
    node_type: nodeType,
    field: kind,
    value: { member_count: members.length },
    confidence: 1,
    source: 'view_model',
    input_hash: null,
    run_id: null,
    created_at: updated,
    updated_at: updated,
  }
}

export function buildKgViewModel(nodes: KgNode[], edges: KgEdge[]): KgViewModel {
  const hubNodes: KgNode[] = []
  const hubEdges: KgEdge[] = []
  const membersByHub = new Map<string, KgNode[]>()
  const hubForRaw = new Map<string, string>()
  const childHubs = new Map<string, KgNode[]>()
  const parentOfHub = new Map<string, string>()

  const byId = new Map(nodes.map(n => [n.id, n]))

  // ── Index companies + dcf_runs (these stay as real hubs) ──────────────────
  const companyByTicker = new Map<string, KgNode>()
  const dcfRuns: KgNode[] = []
  for (const n of nodes) {
    if (n.node_type === 'company') companyByTicker.set(n.ticker, n)
    else if (n.node_type === 'dcf_run') dcfRuns.push(n)
  }

  // All tickers present in the graph (a ticker may have data but no company node).
  const tickers = new Set<string>()
  for (const n of nodes) if (n.ticker) tickers.add(n.ticker)

  // ── Bucket raw nodes per ticker ───────────────────────────────────────────
  const newsByTicker = new Map<string, KgNode[]>()
  const finByTicker = new Map<string, KgNode[]>()
  for (const n of nodes) {
    if (NEWS_TYPES.has(n.node_type)) {
      const arr = newsByTicker.get(n.ticker) || []
      arr.push(n); newsByTicker.set(n.ticker, arr)
    } else if (FINANCIALS_TYPES.has(n.node_type)) {
      const arr = finByTicker.get(n.ticker) || []
      arr.push(n); finByTicker.set(n.ticker, arr)
    }
  }

  // ── dcf_run → its run-scoped leaves (match run_id AND ticker) ─────────────
  const runMembers = new Map<string, KgNode[]>() // dcf_run.id → members
  for (const run of dcfRuns) {
    const members: KgNode[] = []
    for (const n of nodes) {
      if (RUN_LEAF_TYPES.has(n.node_type) && n.run_id === run.run_id && n.ticker === run.ticker) {
        members.push(n)
      }
    }
    runMembers.set(run.id, members)
  }

  // ── Decks: HAS_DECK (dcf_run→deck_run), HAS_SLIDE (deck_run→deck_slide) ────
  // Attach deck_run + its slides to the parent dcf_run hub's members.
  const deckRunToParent = new Map<string, string>() // deck_run.id → dcf_run.id
  for (const e of edges) {
    if (e.relation === 'HAS_DECK') deckRunToParent.set(e.tgt_id, e.src_id)
  }
  for (const e of edges) {
    if (e.relation === 'HAS_SLIDE') {
      // slide's parent deck_run → resolve to dcf_run
      const parentRun = deckRunToParent.get(e.src_id)
      if (parentRun) deckRunToParent.set(e.tgt_id, parentRun)
    }
  }
  for (const n of nodes) {
    if (!DECK_TYPES.has(n.node_type)) continue
    const parentRun = deckRunToParent.get(n.id)
    if (parentRun && runMembers.has(parentRun)) runMembers.get(parentRun)!.push(n)
  }

  // ── Emit hubs per ticker ──────────────────────────────────────────────────
  for (const ticker of tickers) {
    // Company / ticker hub — reuse real company node or synthesize one.
    let companyHub = companyByTicker.get(ticker)
    if (!companyHub) {
      companyHub = syntheticHub(ticker, '__ticker__', 'company', [])
    }
    hubNodes.push(companyHub)

    // News hub
    const news = newsByTicker.get(ticker) || []
    if (news.length) {
      const hub = syntheticHub(ticker, HUB_NEWS, 'news_hub', news)
      hubNodes.push(hub)
      membersByHub.set(hub.id, news)
      for (const m of news) hubForRaw.set(m.id, hub.id)
      hubEdges.push(mkEdge(companyHub.id, hub.id, 'HAS_NEWS'))
    }

    // Financials hub → splits into category sub-hubs (Drivers/Thesis/Metrics/…)
    // Always present so the Beliefs composer is reachable even before any
    // financial data exists for the ticker.
    const fin = finByTicker.get(ticker) || []
    {
      const hub = syntheticHub(ticker, HUB_FINANCIALS, 'financials_hub', fin)
      hubNodes.push(hub)
      membersByHub.set(hub.id, fin)
      hubEdges.push(mkEdge(companyHub.id, hub.id, 'HAS_FINANCIALS'))

      // Bucket financials members into category sub-hubs.
      const byCat = new Map<string, { label: string; members: KgNode[] }>()
      for (const m of fin) {
        // Field keys are period-scoped ("net_income::Q2 2026"); categorize by
        // the base fact_type. Prefer the explicit value.fact_type, else strip
        // the period suffix from the field.
        const baseFactType =
          (m.value && typeof m.value === 'object' && typeof (m.value as Record<string, unknown>).fact_type === 'string'
            ? (m.value as Record<string, unknown>).fact_type as string
            : m.field.split('::')[0])
        const cat = m.node_type === 'document_fact' || m.node_type === 'key_fact' || m.node_type === 'snippet_fact'
          ? getDocFactCategory(baseFactType)
          : (FIN_CATEGORY[m.node_type] || { key: 'other', label: 'Other' })
        const entry = byCat.get(cat.key) || { label: cat.label, members: [] }
        entry.members.push(m)
        byCat.set(cat.key, entry)
      }
      // Beliefs category always exists (analyst can state beliefs anytime).
      if (!byCat.has('beliefs')) byCat.set('beliefs', { label: 'Beliefs', members: [] })

      // Category sub-hubs are NO LONGER rendered on the canvas (they clustered
      // and overlapped). They live only as data: childHubs feeds the tabbed
      // KgFinancialsPanel opened when the Financials hub is clicked. Query
      // highlights on a category member roll up to the Financials hub itself.
      const children: KgNode[] = []
      for (const [key, { label, members }] of byCat) {
        const sub = syntheticHub(ticker, `fin_${key}`, 'fin_category', members)
        sub.value = { member_count: members.length, label }
        membersByHub.set(sub.id, members)
        for (const m of members) hubForRaw.set(m.id, hub.id) // glow Financials hub
        children.push(sub)
        parentOfHub.set(sub.id, hub.id)
      }
      // Sort children by descending member count for a stable tab order.
      children.sort((a, b) =>
        ((b.value as { member_count: number }).member_count) -
        ((a.value as { member_count: number }).member_count))
      childHubs.set(hub.id, children)
    }
  }

  // ── DCF run hubs (real dcf_run nodes) ─────────────────────────────────────
  for (const run of dcfRuns) {
    hubNodes.push(run)
    const members = runMembers.get(run.id) || []
    membersByHub.set(run.id, members)
    for (const m of members) hubForRaw.set(m.id, run.id)
    // dcf_run itself maps to its own hub (so a matched run node glows it).
    hubForRaw.set(run.id, run.id)

    // Edge company → dcf_run (reuse existing HAS_RUN if present, else synth).
    const company = companyByTicker.get(run.ticker)
    if (company) hubEdges.push(mkEdge(company.id, run.id, 'HAS_RUN'))
  }

  // company nodes map to themselves
  for (const c of companyByTicker.values()) hubForRaw.set(c.id, c.id)

  // Keep only edges whose endpoints are rendered hubs (defensive).
  const hubIds = new Set(hubNodes.map(n => n.id))
  const filteredEdges = hubEdges.filter(e => hubIds.has(e.src_id) && hubIds.has(e.tgt_id))

  // Dedupe edges by src+tgt+relation.
  const seen = new Set<string>()
  const dedupedEdges = filteredEdges.filter(e => {
    const k = `${e.src_id}->${e.tgt_id}:${e.relation}`
    if (seen.has(k)) return false
    seen.add(k)
    return true
  })

  void byId // reserved for future provenance lookups
  return { hubNodes, hubEdges: dedupedEdges, membersByHub, hubForRaw, childHubs, parentOfHub }
}

let _edgeSeq = 0
function mkEdge(src: string, tgt: string, relation: string): KgEdge {
  return {
    id: `vm_edge_${_edgeSeq++}`,
    session_id: null,
    src_id: src,
    tgt_id: tgt,
    relation,
    confidence: 1,
    source: 'view_model',
    created_at: 0,
  }
}
