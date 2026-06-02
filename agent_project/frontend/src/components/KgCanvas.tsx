import { useEffect, useRef, useState, useMemo, useCallback } from 'react'
import { zoom, zoomIdentity, type ZoomBehavior } from 'd3-zoom'
import { select } from 'd3-selection'
import { edgeKey, type KgNode, type KgEdge } from '../hooks/useKnowledgeGraph'

// ── Node colour scheme — "Slate Terminal" ────────────────────────────────────
// Cohesive cool palette, NOT a rainbow. Hubs differ by hue but all desaturated
// and in the same family so the canvas reads calm. Interaction states (newest,
// compare, query) are weights of the single accent blue, applied in the draw
// loop — not extra hues here.

const ACCENT = '#3b82f6'

/** Read CSS custom-property palette at draw time so canvas colours
 *  adapt to dark/light mode. Falls back to dark-mode hex if CSS
 *  variables aren't resolved (e.g. SSR). */
function canvasPalette() {
  const s = getComputedStyle(document.documentElement)
  const v = (name: string, fallback: string) => s.getPropertyValue(name).trim() || fallback
  return {
    // Backgrounds
    bg:          v('--color-bg',          '#08080a'),
    bgRaised:    v('--color-bg-raised',   '#0c0c10'),
    bgOverlay:   v('--color-bg-overlay',  '#101016'),
    // Surfaces
    surface:     v('--color-surface',     '#18181e'),
    surface2:    v('--color-surface-2',   '#1c1c24'),
    surface3:    v('--color-surface-3',   '#22222c'),
    // Borders
    border:      v('--color-border',      '#1e1e28'),
    borderHover: v('--color-border-hover','#282836'),
    borderAccent:v('--color-border-accent','#2a2a3c'),
    // Text
    ink:         v('--color-ink',         '#e4e4eb'),
    inkMuted:    v('--color-ink-muted',   '#8b8b99'),
    inkDim:      v('--color-ink-dim',     '#5a5a66'),
    // Accents
    accent:      v('--color-accent',      '#3b82f6'),
    accentMuted:  v('--color-accent-muted','#6366f1'),
    success:     v('--color-success',     '#10b981'),
    warn:        v('--color-warn',        '#f59e0b'),
  }
}


const NODE_COLORS: Record<string, string> = {
  // Hub model — the only types actually rendered:
  company: '#3b82f6',        // accent — the anchor
  news_hub: '#64748b',       // slate
  financials_hub: '#0d9488', // muted teal
  fin_category: '#5b6b8c',   // dim slate-blue
  dcf_run: '#6366f1',        // indigo (single, not violet+purple+magenta)
  // Legacy raw types (rarely rendered in hub model) — kept muted:
  thesis: '#6366f1',
  company_synthesis: '#5b6b8c',
  run_assumption: '#64748b',
  run_output: '#0d9488',
  run_scenario: '#6366f1',
  market_metric_fund: '#0d9488',
  market_metric_price: '#0d9488',
  driver: '#64748b',
  risk: '#64748b',
  theme: '#64748b',
  user_belief: '#10b981',
  deck_run: '#475569',
  deck_slide: '#334155',
  company_lifecycle: '#5b6b8c',
  filing: '#475569',
  news_item: '#334155',
}

/** Hub legend for the sidebar (only the types the hub model renders). */
export const HUB_LEGEND: { color: string; label: string }[] = [
  { color: '#3b82f6', label: 'Ticker' },
  { color: '#64748b', label: 'News' },
  { color: '#0d9488', label: 'Financials' },
  { color: '#6366f1', label: 'DCF Run' },
  { color: ACCENT, label: 'Latest / selected' },
]

export function colorForNode(node: KgNode): string {
  if (node.source === 'user_stated') return '#10b981'
  return NODE_COLORS[node.node_type] || '#64748b'
}

// Edges: muted hairlines by default; primary structural edges get a faint
// accent tint. No candy colors competing with the nodes.
const EDGE_COLORS: Record<string, string> = {
  HAS_RUN: '#3b82f6',
  HAS_NEWS: '#3f4654',
  HAS_FINANCIALS: '#3f4654',
  HAS_CATEGORY: '#363c47',
  HAS_METRIC: '#363c47',
  HAS_SYNTHESIS: '#363c47',
  HAS_THESIS: '#363c47',
  HAS_DRIVER: '#363c47',
  HAS_DECK: '#3f4654',
  HAS_SLIDE: '#363c47',
  PRODUCES: '#3f4654',
  LOCKED_ASSUMPTION: '#363c47',
  RELATES_TO: '#2e353f',
}

function edgeColor(relation: string): string {
  return EDGE_COLORS[relation] || '#2e353f'
}

// ── Node role classification ────────────────────────────────────────────────

const RUN_SCOPED_TYPES = new Set(['run_assumption', 'run_output', 'run_scenario'])
const SHARED_KNOWLEDGE_TYPES = new Set([
  'thesis',
  'company_synthesis',
  'market_metric_fund',
  'market_metric_price',
  'driver',
  'risk',
  'theme',
  'user_belief',
])

type NodeRole = 'company' | 'run' | 'run_leaf' | 'shared' | 'other'

const HUB_TYPES = new Set(['news_hub', 'financials_hub'])

function nodeRole(n: KgNode): NodeRole {
  if (n.node_type === 'company') return 'company'
  if (n.node_type === 'dcf_run' || HUB_TYPES.has(n.node_type)) return 'run'
  if (n.node_type === 'fin_category') return 'shared'
  if (RUN_SCOPED_TYPES.has(n.node_type)) return 'run_leaf'
  if (SHARED_KNOWLEDGE_TYPES.has(n.node_type)) return 'shared'
  return 'other'
}

// ── Layout types (STATIC — no physics) ────────────────────────────────────────
// Nodes are positioned ONCE by a deterministic radial-tree layout. After that,
// positions only change when the user drags a node. Dragging one node never
// moves another. Edges redraw from live node references each frame.

interface LayoutNode {
  id: string
  raw: KgNode
  role: NodeRole
  degree: number
  radius: number
  x: number
  y: number
}

interface LayoutLink {
  id: string
  raw: KgEdge
  source: LayoutNode
  target: LayoutNode
}

// ── Props ────────────────────────────────────────────────────────────────────

interface Props {
  nodes: KgNode[]
  edges: KgEdge[]
  highlightSet: Set<string>
  /** Directed traversal edges ("srcId->tgtId") to draw as the answer route. */
  highlightEdgeSet?: Set<string>
  onNodeClick: (n: KgNode) => void
  onNodeHover?: (n: KgNode | null) => void
  /** map ticker → { implied, spot } for tooltip enrichment on company nodes */
  companySummary?: Map<string, { implied?: number; spot?: number; delta?: number }>
  /** run keys (`ticker::run_id`) assembled into the open comparison — ring them. */
  compareKeys?: Set<string>
  /** when true, dragging a dcf_run node emits its key for the compare drop zone. */
  dragRunsToCompare?: boolean
}

// ── Deterministic radial-tree layout ──────────────────────────────────────────
// Builds a forest from directed edges (parent = src, child = tgt, first edge
// wins so each node has at most one parent → no cycles). Company nodes anchor a
// horizontal row; each company's subtree fans out radially. Disconnected nodes
// drop into a top-left grid. Returns id → {x, y}.

const RING_BY_DEPTH = [0, 185, 120, 85, 70]

function computeLayout(
  nodes: KgNode[],
  edges: KgEdge[],
  w: number,
  h: number,
): Map<string, { x: number; y: number }> {
  const pos = new Map<string, { x: number; y: number }>()
  const placed = new Set<string>()
  const nodeIds = new Set(nodes.map(n => n.id))

  // Build parent/children maps (directed: src → tgt). First incoming edge wins.
  const parentOf = new Map<string, string>()
  for (const e of edges) {
    if (!nodeIds.has(e.src_id) || !nodeIds.has(e.tgt_id)) continue
    if (e.src_id === e.tgt_id) continue
    if (!parentOf.has(e.tgt_id)) parentOf.set(e.tgt_id, e.src_id)
  }
  const childrenOf = new Map<string, string[]>()
  for (const [child, parent] of parentOf) {
    const arr = childrenOf.get(parent) || []
    arr.push(child)
    childrenOf.set(parent, arr)
  }

  // BFS radial placement of one subtree rooted at (rootId, rootX, rootY).
  function placeSubtree(rootId: string, rootX: number, rootY: number) {
    if (placed.has(rootId)) return
    pos.set(rootId, { x: rootX, y: rootY })
    placed.add(rootId)
    const queue: { id: string; depth: number; a0: number; a1: number }[] = [
      { id: rootId, depth: 0, a0: 0, a1: Math.PI * 2 },
    ]
    while (queue.length) {
      const { id, depth, a0, a1 } = queue.shift()!
      const kids = (childrenOf.get(id) || []).filter(k => !placed.has(k))
      if (!kids.length) continue
      const p = pos.get(id)!
      const span = a1 - a0
      const r = RING_BY_DEPTH[Math.min(depth + 1, RING_BY_DEPTH.length - 1)]
      kids.forEach((kid, i) => {
        const angle = a0 + ((i + 0.5) / kids.length) * span
        pos.set(kid, { x: p.x + r * Math.cos(angle), y: p.y + r * Math.sin(angle) })
        placed.add(kid)
        const kidSpan = span / kids.length
        queue.push({ id: kid, depth: depth + 1, a0: angle - kidSpan / 2, a1: angle + kidSpan / 2 })
      })
    }
  }

  // 1) Company anchors in a horizontal row at vertical center.
  const companies = nodes.filter(n => n.node_type === 'company')
  if (companies.length === 1) {
    placeSubtree(companies[0].id, w / 2, h / 2)
  } else if (companies.length > 1) {
    const spacing = Math.max(360, Math.min(w / (companies.length + 1), 520))
    const startX = w / 2 - (spacing * (companies.length - 1)) / 2
    companies.forEach((c, i) => placeSubtree(c.id, startX + i * spacing, h / 2))
  }

  // 2) Remaining roots (no parent, not yet placed) → small subtrees in a grid.
  let gi = 0
  const gridStep = 110
  const placeGrid = (id: string) => {
    const gx = 70 + (gi % 6) * gridStep
    const gy = 70 + Math.floor(gi / 6) * (gridStep * 0.75)
    placeSubtree(id, gx, gy)
    gi++
  }
  for (const n of nodes) {
    if (placed.has(n.id)) continue
    if (parentOf.has(n.id)) continue // its parent was filtered out — handle below
    placeGrid(n.id)
  }
  // 3) Leftovers whose parent is absent → grid singletons.
  for (const n of nodes) {
    if (placed.has(n.id)) continue
    const gx = 70 + (gi % 6) * gridStep
    const gy = 70 + Math.floor(gi / 6) * (gridStep * 0.75)
    pos.set(n.id, { x: gx, y: gy })
    placed.add(n.id)
    gi++
  }

  return pos
}

// ── Canvas ───────────────────────────────────────────────────────────────────

export function KgCanvas({ nodes, edges, highlightSet, highlightEdgeSet, onNodeClick, onNodeHover, companySummary, compareKeys, dragRunsToCompare }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const zoomRef = useRef<ZoomBehavior<HTMLCanvasElement, unknown> | null>(null)
  // Persistent user-facing positions (includes dragged nodes). Survives refresh.
  const nodePositionsRef = useRef<Map<string, { x: number; y: number }>>(new Map())
  // Computed (un-dragged) positions, for double-click reset.
  const computedPositionsRef = useRef<Map<string, { x: number; y: number }>>(new Map())
  const transformRef = useRef<{ k: number; x: number; y: number }>({ k: 1, x: 0, y: 0 })
  const drawReqRef = useRef<number | null>(null)

  const [size, setSize] = useState<{ w: number; h: number }>({ w: 800, h: 600 })
  const [hoveredNode, setHoveredNode] = useState<string | null>(null)
  const hoveredNodeRef = useRef<string | null>(null)
  const [zoomScale, setZoomScale] = useState(1)
  const zoomScaleRef = useRef(1)

  const layoutNodesRef = useRef<LayoutNode[]>([])
  const layoutLinksRef = useRef<LayoutLink[]>([])
  // nodeId → recency rank: 0 = newest per ticker, increasing = older.
  const runRecencyRef = useRef<Map<string, number>>(new Map())
  // Comparison-assembled run keys (ring on canvas). Ref so draw sees latest.
  const compareKeysRef = useRef<Set<string>>(compareKeys ?? new Set())
  compareKeysRef.current = compareKeys ?? new Set()

  // ── Build layout (deterministic + persistent) ────────────────────────────
  useMemo(() => {
    const degree = new Map<string, number>()
    for (const e of edges) {
      degree.set(e.src_id, (degree.get(e.src_id) || 0) + 1)
      degree.set(e.tgt_id, (degree.get(e.tgt_id) || 0) + 1)
    }

    const computed = computeLayout(nodes, edges, size.w, size.h)
    computedPositionsRef.current = computed

    const newNodes: LayoutNode[] = nodes.map(n => {
      const role = nodeRole(n)
      const deg = degree.get(n.id) || 0
      const baseRadius =
        role === 'company' ? 16 :
        role === 'run' ? 11 :
        role === 'shared' ? 8 :
        role === 'run_leaf' ? 6 : 7
      const radius = Math.min(20, baseRadius + Math.min(4, deg * 0.4))

      // Existing (possibly dragged) position wins; else freshly computed.
      const persisted = nodePositionsRef.current.get(n.id)
      const fresh = computed.get(n.id) || { x: size.w / 2, y: size.h / 2 }
      const p = persisted ?? fresh
      return { id: n.id, raw: n, role, degree: deg, radius, x: p.x, y: p.y }
    })

    // Refresh persistent map: keep dragged spots, seed new nodes with computed.
    const merged = new Map<string, { x: number; y: number }>()
    for (const ln of newNodes) merged.set(ln.id, { x: ln.x, y: ln.y })
    nodePositionsRef.current = merged

    const idMap = new Map(newNodes.map(n => [n.id, n]))
    const newLinks: LayoutLink[] = edges
      .filter(e => idMap.has(e.src_id) && idMap.has(e.tgt_id))
      .map(e => ({ id: e.id, raw: e, source: idMap.get(e.src_id)!, target: idMap.get(e.tgt_id)! }))

    // Recency rank per ticker: sort dcf_run nodes newest→oldest, assign 0,1,2…
    const runsByTicker = new Map<string, KgNode[]>()
    for (const n of nodes) {
      if (n.node_type !== 'dcf_run') continue
      const arr = runsByTicker.get(n.ticker) || []
      arr.push(n); runsByTicker.set(n.ticker, arr)
    }
    const recency = new Map<string, number>()
    for (const arr of runsByTicker.values()) {
      arr.sort((a, b) => b.updated_at - a.updated_at)
      arr.forEach((n, i) => recency.set(n.id, i))
    }
    runRecencyRef.current = recency

    layoutNodesRef.current = newNodes
    layoutLinksRef.current = newLinks
  }, [nodes, edges, size.w, size.h])

  // ── Draw frame ───────────────────────────────────────────────────────────────
  const drawFrame = useCallback(() => {
    const palette = canvasPalette()
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const dpr = window.devicePixelRatio || 1
    const { k, x, y } = transformRef.current
    const currentZoom = zoomScaleRef.current
    const layoutNodes = layoutNodesRef.current
    const layoutLinks = layoutLinksRef.current
    const hiNodes = highlightSet
    const hovId = hoveredNodeRef.current

    ctx.clearRect(0, 0, canvas.width, canvas.height)

    ctx.save()
    ctx.setTransform(k * dpr, 0, 0, k * dpr, x * dpr, y * dpr)

    // ── Draw edges ──────────────────────────────────────────────────────────
    const hasHighlight = hiNodes.size > 0 || (highlightEdgeSet?.size ?? 0) > 0

    for (const l of layoutLinks) {
      const src = l.source
      const tgt = l.target
      const isPath = !!highlightEdgeSet?.has(edgeKey(src.id, tgt.id))
      const isHi = isPath || (hiNodes.has(src.id) && hiNodes.has(tgt.id))
      const opacity = hasHighlight && !isHi ? 0.12 : 0.55
      const stroke = isHi ? '#3b82f6' : edgeColor(l.raw.relation)

      const x1 = src.x, y1 = src.y
      const x2 = tgt.x, y2 = tgt.y
      const dx = x2 - x1, dy = y2 - y1
      const dr = Math.sqrt(dx * dx + dy * dy)
      const curvature = 0.18
      const mx = (x1 + x2) / 2 - dy * curvature
      const my = (y1 + y2) / 2 + dx * curvature
      const shorten = tgt.radius + 2
      const ex = x2 - (dx / (dr || 1)) * shorten
      const ey = y2 - (dy / (dr || 1)) * shorten

      ctx.save()
      ctx.globalAlpha = opacity
      ctx.strokeStyle = stroke
      ctx.lineWidth = isPath ? 3 : isHi ? 2 : 1

      if (isPath) {
        ctx.setLineDash([6, 4])
      } else {
        ctx.setLineDash([])
      }

      ctx.beginPath()
      ctx.moveTo(x1, y1)
      ctx.quadraticCurveTo(mx, my, ex, ey)
      ctx.stroke()

      // Arrowhead at endpoint
      const arrowSize = 6
      const atx = ex - mx, aty = ey - my
      const alen = Math.sqrt(atx * atx + aty * aty) || 1
      const unx = atx / alen, uny = aty / alen
      ctx.setLineDash([])
      ctx.fillStyle = stroke
      ctx.beginPath()
      ctx.moveTo(ex, ey)
      ctx.lineTo(
        ex - arrowSize * unx + (arrowSize * 0.5) * (-uny),
        ey - arrowSize * uny + (arrowSize * 0.5) * unx,
      )
      ctx.lineTo(
        ex - arrowSize * unx - (arrowSize * 0.5) * (-uny),
        ey - arrowSize * uny - (arrowSize * 0.5) * unx,
      )
      ctx.closePath()
      ctx.fill()
      ctx.restore()
    }

    // ── Draw nodes ──────────────────────────────────────────────────────────
    const recency = runRecencyRef.current
    for (const n of layoutNodes) {
      const isHi = hiNodes.has(n.id)
      const isHover = hovId === n.id
      const dim = hiNodes.size > 0 && !isHi
      const color = colorForNode(n.raw)
      const r = n.radius
      const nx = n.x, ny = n.y

      // Recency context for dcf_run nodes.
      const rank = n.raw.node_type === 'dcf_run' ? (recency.get(n.id) ?? 0) : -1
      const isNewest = rank === 0 && n.raw.node_type === 'dcf_run'
      // Older runs fade slightly so newest stands out without hiding others.
      const recencyAlpha = rank > 0 ? Math.max(0.5, 1 - rank * 0.15) : 1
      // Assembled into the open comparison? (ticker::run_id)
      const inCompare = n.raw.node_type === 'dcf_run' && n.raw.run_id
        ? compareKeysRef.current.has(`${n.raw.ticker}::${n.raw.run_id}`)
        : false

      ctx.save()
      ctx.globalAlpha = dim ? 0.3 : recencyAlpha

      // Amber double-ring for runs assembled into the comparison.
      if (inCompare) {
        ctx.beginPath()
        ctx.arc(nx, ny, r + 7, 0, Math.PI * 2)
        ctx.strokeStyle = palette.warn
        ctx.lineWidth = 2
        ctx.globalAlpha = dim ? 0.3 : 0.95
        ctx.stroke()
        ctx.globalAlpha = dim ? 0.3 : recencyAlpha
      }

      // Outer ring for highlighted / hovered / company nodes / newest run
      if (isHi || isHover || n.role === 'company' || isNewest) {
        const ringR = r + (n.role === 'company' ? 7 : 5)
        ctx.beginPath()
        ctx.arc(nx, ny, ringR, 0, Math.PI * 2)
        ctx.strokeStyle = isHi ? palette.accent : isNewest ? palette.accent : color
        ctx.lineWidth = isNewest ? 1.5 : (n.role === 'company' ? 2 : 1.5)
        ctx.globalAlpha = dim ? 0.3 : (isNewest ? 0.9 : n.role === 'company' ? 0.4 : 0.7)
        ctx.stroke()
        ctx.globalAlpha = dim ? 0.3 : recencyAlpha
      }

      // Filled circle
      ctx.beginPath()
      ctx.arc(nx, ny, r, 0, Math.PI * 2)
      ctx.fillStyle = color
      ctx.globalAlpha = dim ? 0.3 : (n.role === 'company' ? 0.95 : 0.85) * recencyAlpha
      ctx.fill()
      ctx.globalAlpha = dim ? 0.3 : recencyAlpha
      ctx.strokeStyle = isHi ? palette.accent : (isHover ? palette.ink : palette.bg)
      ctx.lineWidth = isHi || isHover ? 2 : 1.5
      ctx.stroke()

      // Label
      const isAnchor = n.role === 'company' || n.role === 'run'
      const labelOpacity =
        isHi || isHover || isAnchor ? 1 :
        currentZoom > 1.2 ? 0.85 : 0.5

      // Skip leaf labels when zoomed out and not highlighted
      if (!isAnchor && !isHi && !isHover && currentZoom < 0.6) {
        ctx.restore()
        continue
      }

      const label = labelFor(n.raw)
      const fontSize = n.role === 'company' ? 13 : isAnchor ? 11 : 10
      ctx.font = `${n.role === 'company' ? 600 : 400} ${fontSize}px sans-serif`
      ctx.textAlign = 'center'
      ctx.textBaseline = 'top'

      // Stroke for legibility
      ctx.globalAlpha = dim ? 0.3 * labelOpacity * 0.85 : labelOpacity * 0.85
      ctx.strokeStyle = palette.bg
      ctx.lineWidth = 2.5
      ctx.lineJoin = 'round'
      ctx.strokeText(label, nx, ny + r + 3)

      ctx.globalAlpha = dim ? 0.3 * labelOpacity : labelOpacity
      ctx.fillStyle = isHi ? palette.accentMuted : (n.role === 'company' ? palette.ink : palette.inkMuted)
      ctx.fillText(label, nx, ny + r + 3)

      // "LATEST" dot badge on the newest dcf_run per ticker.
      if (isNewest && !dim) {
        const badgeX = nx + r * 0.7
        const badgeY = ny - r * 0.7
        ctx.globalAlpha = 0.95
        ctx.beginPath()
        ctx.arc(badgeX, badgeY, 3.5, 0, Math.PI * 2)
        ctx.fillStyle = palette.accent
        ctx.fill()
        ctx.strokeStyle = palette.bg
        ctx.lineWidth = 1
        ctx.stroke()
      }

      ctx.restore()
    }

    ctx.restore() // end world-space transform

    // ── Tooltip in screen space ─────────────────────────────────────────────
    if (hovId) {
      const n = layoutNodes.find(s => s.id === hovId)
      if (n) {
        const isCompany = n.role === 'company'
        const summary = isCompany ? companySummary?.get(n.raw.ticker) : undefined
        const lines = buildTooltipLines(n.raw, summary)

        const sx = n.x * k + x
        const sy = n.y * k + y

        const lineH = 14
        const padX = 8, padY = 8
        const maxLen = Math.max(...lines.map(l => l.length))
        const w = Math.min(280, Math.max(120, maxLen * 7 + padX * 2))
        const h = lines.length * lineH + padY * 2
        const bx = sx + n.radius * k + 14
        const by = sy - h / 2

        ctx.save()
        ctx.globalAlpha = 0.96
        ctx.fillStyle = palette.surface
        ctx.strokeStyle = palette.borderHover
        ctx.lineWidth = 1
        ctx.beginPath()
        ctx.roundRect(bx, by, w, h, 4)
        ctx.fill()
        ctx.stroke()

        lines.forEach((line, i) => {
          ctx.globalAlpha = 1
          ctx.font = `${i === 0 ? 600 : 400} ${i === 0 ? 11 : 10}px sans-serif`
          ctx.fillStyle = i === 0 ? palette.ink : palette.inkMuted
          ctx.textAlign = 'left'
          ctx.textBaseline = 'top'
          ctx.fillText(line, bx + padX, by + padY + i * lineH)
        })
        ctx.restore()
      }
    }
  }, [highlightSet, highlightEdgeSet, companySummary])

  // Coalesced single-frame redraw (static graph — only draw on demand).
  const scheduleDraw = useCallback(() => {
    if (drawReqRef.current != null) return
    drawReqRef.current = requestAnimationFrame(() => {
      drawReqRef.current = null
      drawFrame()
    })
  }, [drawFrame])

  // Redraw whenever inputs that affect the picture change.
  useEffect(() => {
    scheduleDraw()
  }, [scheduleDraw, size.w, size.h, highlightSet, highlightEdgeSet, hoveredNode, nodes, edges, compareKeys])

  // ── Resize observer ────────────────────────────────────────────────────────
  // Setting el.width / el.height (the pixel buffer) clears the canvas instantly,
  // causing a blank frame on every drag tick. Fix: redraw immediately on every
  // size tick to prevent the blank, but debounce the React state update (which
  // re-runs the expensive layout + recomputes node positions) so it only fires
  // once the resize gesture settles.
  useEffect(() => {
    if (!canvasRef.current) return
    const el = canvasRef.current
    let debounceTimer: ReturnType<typeof setTimeout> | null = null

    const ro = new ResizeObserver(entries => {
      for (const e of entries) {
        const { width, height } = e.contentRect
        const w = Math.max(200, width)
        const h = Math.max(200, height)
        const dpr = window.devicePixelRatio || 1

        // Resize the pixel buffer immediately — this clears the canvas, but we
        // re-draw right after so the blank is invisible (same-frame).
        el.width = w * dpr
        el.height = h * dpr
        scheduleDraw()

        // Debounce the React state / layout recompute: fires only after the
        // resize gesture has settled (~120 ms of no new entries).
        if (debounceTimer !== null) clearTimeout(debounceTimer)
        debounceTimer = setTimeout(() => {
          debounceTimer = null
          setSize({ w, h })
        }, 120)
      }
    })
    ro.observe(el)
    return () => {
      ro.disconnect()
      if (debounceTimer !== null) clearTimeout(debounceTimer)
    }
  }, [scheduleDraw])

  // ── Unproject screen → graph coords ─────────────────────────────────────
  const screenToGraph = useCallback((clientX: number, clientY: number): { x: number; y: number } | null => {
    const canvas = canvasRef.current
    if (!canvas) return null
    const rect = canvas.getBoundingClientRect()
    const { k, x, y } = transformRef.current
    return {
      x: (clientX - rect.left - x) / k,
      y: (clientY - rect.top - y) / k,
    }
  }, [])

  // ── Hit test: find node near graph coords ───────────────────────────────
  const hitTest = useCallback((gx: number, gy: number): LayoutNode | null => {
    let closest: LayoutNode | null = null
    let minDist = Infinity
    for (const n of layoutNodesRef.current) {
      const dx = n.x - gx
      const dy = n.y - gy
      const d = Math.sqrt(dx * dx + dy * dy)
      if (d < n.radius + 5 && d < minDist) {
        minDist = d
        closest = n
      }
    }
    return closest
  }, [])

  // ── Zoom / pan (pan only on empty space; node-drag handled separately) ─────
  useEffect(() => {
    if (!canvasRef.current) return
    const canvasSel = select(canvasRef.current)

    const z = zoom<HTMLCanvasElement, unknown>()
      .scaleExtent([0.1, 4])
      .filter(ev => {
        // Always allow wheel zoom + touch.
        if (ev.type === 'wheel') return true
        if (ev.type === 'dblclick') return false
        // For mousedown: only pan when NOT starting on a node (so node drag wins).
        if (ev.type === 'mousedown') {
          const me = ev as MouseEvent
          const g = screenToGraph(me.clientX, me.clientY)
          if (!g) return true
          return !hitTest(g.x, g.y)
        }
        return true
      })
      .on('zoom', ev => {
        const { k, x, y } = ev.transform
        transformRef.current = { k, x, y }
        zoomScaleRef.current = k
        setZoomScale(k)
        scheduleDraw()
      })

    canvasSel.call(z)
    zoomRef.current = z
    canvasSel.call(z.transform, zoomIdentity)
    return () => { canvasSel.on('.zoom', null) }
  }, [scheduleDraw, screenToGraph, hitTest])

  // ── Mouse move for hover ─────────────────────────────────────────────────
  const draggingRef = useRef(false)
  const handleCanvasMouseMove = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    if (draggingRef.current) return
    if (e.buttons > 0) return // panning
    const g = screenToGraph(e.clientX, e.clientY)
    if (!g) return
    const hit = hitTest(g.x, g.y)
    const newId = hit ? hit.id : null
    if (newId !== hoveredNodeRef.current) {
      hoveredNodeRef.current = newId
      setHoveredNode(newId)
      onNodeHover?.(hit ? hit.raw : null)
      scheduleDraw()
    }
  }, [screenToGraph, hitTest, onNodeHover, scheduleDraw])

  // ── Node drag (static: moves ONLY the grabbed node, stays where dropped) ───
  const clickStartRef = useRef<{ x: number; y: number } | null>(null)

  const handleCanvasMouseDown = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    clickStartRef.current = { x: e.clientX, y: e.clientY }
    const g = screenToGraph(e.clientX, e.clientY)
    if (!g) return
    const hit = hitTest(g.x, g.y)
    if (!hit) return // empty space → let d3-zoom pan

    e.stopPropagation()
    draggingRef.current = true

    // Grab offset so the node doesn't jump to the cursor center.
    const startG = g
    const nodeStartX = hit.x
    const nodeStartY = hit.y

    function onMove(ev: MouseEvent) {
      const g2 = screenToGraph(ev.clientX, ev.clientY)
      if (!g2) return
      hit!.x = nodeStartX + (g2.x - startG.x)
      hit!.y = nodeStartY + (g2.y - startG.y)
      nodePositionsRef.current.set(hit!.id, { x: hit!.x, y: hit!.y })
      scheduleDraw()
    }
    function onUp() {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      draggingRef.current = false
      // Position already persisted in nodePositionsRef — node stays put.
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [screenToGraph, hitTest, scheduleDraw])

  const handleCanvasClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const start = clickStartRef.current
    if (!start) return
    const dist = Math.sqrt((e.clientX - start.x) ** 2 + (e.clientY - start.y) ** 2)
    if (dist > 4) return // was a drag, not a click
    const g = screenToGraph(e.clientX, e.clientY)
    if (!g) return
    const hit = hitTest(g.x, g.y)
    if (hit) onNodeClick(hit.raw)
  }, [screenToGraph, hitTest, onNodeClick])

  // Double-click a node → snap it back to its computed layout position.
  const handleCanvasDblClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const g = screenToGraph(e.clientX, e.clientY)
    if (!g) return
    const hit = hitTest(g.x, g.y)
    if (!hit) return
    const computed = computedPositionsRef.current.get(hit.id)
    if (!computed) return
    hit.x = computed.x
    hit.y = computed.y
    nodePositionsRef.current.set(hit.id, { x: computed.x, y: computed.y })
    scheduleDraw()
  }, [screenToGraph, hitTest, scheduleDraw])

  const handleCanvasMouseLeave = useCallback(() => {
    if (draggingRef.current) return
    if (hoveredNodeRef.current !== null) {
      hoveredNodeRef.current = null
      setHoveredNode(null)
      onNodeHover?.(null)
      scheduleDraw()
    }
  }, [onNodeHover, scheduleDraw])

  // ── Native drag of a dcf_run node into the comparison drop zone ─────────────
  // Active only in compare mode. Writes `ticker::run_id` to dataTransfer; the
  // KgCompareRuns panel reads it on drop.
  const hoveredIsRun = (() => {
    if (!dragRunsToCompare || !hoveredNode) return false
    const n = layoutNodesRef.current.find(s => s.id === hoveredNode)
    return !!n && n.raw.node_type === 'dcf_run' && !!n.raw.run_id
  })()

  const handleDragStart = useCallback((e: React.DragEvent<HTMLCanvasElement>) => {
    const hovId = hoveredNodeRef.current
    if (!hovId) { e.preventDefault(); return }
    const n = layoutNodesRef.current.find(s => s.id === hovId)
    if (!n || n.raw.node_type !== 'dcf_run' || !n.raw.run_id) { e.preventDefault(); return }
    e.dataTransfer.setData('application/x-kg-run', `${n.raw.ticker}::${n.raw.run_id}`)
    e.dataTransfer.effectAllowed = 'copy'
  }, [])

  return (
    <canvas
      ref={canvasRef}
      className="w-full h-full bg-bg"
      style={{ display: 'block', cursor: hoveredIsRun ? 'grab' : hoveredNode ? 'grab' : 'default' }}
      draggable={hoveredIsRun}
      onDragStart={handleDragStart}
      onMouseMove={handleCanvasMouseMove}
      onMouseDown={handleCanvasMouseDown}
      onClick={handleCanvasClick}
      onDoubleClick={handleCanvasDblClick}
      onMouseLeave={handleCanvasMouseLeave}
    />
  )
}

function fmtShortDate(ts: number): string {
  const d = new Date(ts * 1000)
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function labelFor(n: KgNode): string {
  if (n.node_type === 'company') return n.ticker
  if (n.node_type === 'news_hub') return 'News'
  if (n.node_type === 'financials_hub') return 'Financials'
  if (n.node_type === 'fin_category') {
    const o = (n.value && typeof n.value === 'object') ? n.value as Record<string, unknown> : {}
    const label = String(o.label ?? n.field)
    const count = o.member_count != null ? ` (${o.member_count})` : ''
    return `${label}${count}`
  }
  if (n.node_type === 'dcf_run') {
    const o = (n.value && typeof n.value === 'object') ? n.value as Record<string, unknown> : {}
    const horizon = o.horizon_years ? `${o.horizon_years}y` : ''
    const date = n.updated_at ? fmtShortDate(n.updated_at) : ''
    return `DCF${horizon ? ' ' + horizon : ''}${date ? ' · ' + date : ''}`
  }
  return n.field.length > 24 ? n.field.slice(0, 24) + '…' : n.field
}

function buildTooltipLines(
  n: KgNode,
  summary?: { implied?: number; spot?: number; delta?: number },
): string[] {
  const lines: string[] = []
  if (n.node_type === 'company') {
    lines.push(n.ticker)
    if (summary) {
      if (summary.implied !== undefined) lines.push(`implied: $${fmtMoney(summary.implied)}`)
      if (summary.spot !== undefined) lines.push(`spot: $${fmtMoney(summary.spot)}`)
      if (summary.delta !== undefined) {
        const pct = (summary.delta * 100).toFixed(1)
        const sign = summary.delta >= 0 ? '+' : ''
        lines.push(`Δ ${sign}${pct}%`)
      }
    } else {
      lines.push('no DCF run yet')
    }
    return lines
  }
  if (n.node_type === 'dcf_run') {
    const o = (n.value && typeof n.value === 'object') ? n.value as Record<string, unknown> : {}
    const horizon = o.horizon_years ? `${o.horizon_years}y horizon` : ''
    const implied = typeof o.implied_share_price === 'number' ? `$${fmtMoney(o.implied_share_price)}` : null
    const trigger = o.trigger ? String(o.trigger) : null
    const parent = o.parent_run_id ? `from ${String(o.parent_run_id).slice(0, 20)}…` : null
    const conf = o.confidence_label ? String(o.confidence_label) : null
    const validity = o.model_validity === 'invalid' ? '⚠ invalid' : null
    const age = n.updated_at
      ? (() => {
          const s = Math.max(0, Date.now() / 1000 - n.updated_at)
          if (s < 3600) return `${Math.round(s / 60)}m ago`
          if (s < 86400) return `${Math.round(s / 3600)}h ago`
          return `${Math.round(s / 86400)}d ago`
        })()
      : null
    lines.push(`${n.ticker} DCF${horizon ? ' · ' + horizon : ''}`)
    if (implied) lines.push(`implied: ${implied}`)
    if (conf) lines.push(`confidence: ${conf}`)
    if (trigger && trigger !== 'initial') lines.push(`trigger: ${trigger}`)
    if (parent) lines.push(parent)
    if (validity) lines.push(validity)
    if (age) lines.push(age)
    return lines
  }
  lines.push(n.node_type)
  lines.push(n.field)
  return lines
}

function fmtMoney(v: number): string {
  if (Math.abs(v) >= 1000) return v.toFixed(0)
  return v.toFixed(2)
}
