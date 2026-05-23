import { useEffect, useRef, useState, useMemo, useCallback } from 'react'
import {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceCenter,
  forceCollide,
  forceRadial,
  type Simulation,
  type SimulationNodeDatum,
  type SimulationLinkDatum,
} from 'd3-force'
import { zoom, zoomIdentity, zoomTransform, type ZoomBehavior } from 'd3-zoom'
import { select } from 'd3-selection'
import type { KgNode, KgEdge } from '../hooks/useKnowledgeGraph'

// ── Node colour scheme ───────────────────────────────────────────────────────

const NODE_COLORS: Record<string, string> = {
  company: '#3b82f6',
  dcf_run: '#8b5cf6',
  thesis: '#6366f1',
  company_synthesis: '#6366f1',
  run_assumption: '#f59e0b',
  run_output: '#10b981',
  run_scenario: '#a855f7',
  market_metric_fund: '#06b6d4',
  market_metric_price: '#06b6d4',
  driver: '#f43f5e',
  risk: '#f43f5e',
  theme: '#f43f5e',
  user_belief: '#22c55e',
}

export function colorForNode(node: KgNode): string {
  if (node.source === 'user_stated') return '#22c55e'
  return NODE_COLORS[node.node_type] || '#71717a'
}

const EDGE_COLORS: Record<string, string> = {
  HAS_RUN: '#3b82f6',
  HAS_METRIC: '#06b6d4',
  HAS_SYNTHESIS: '#6366f1',
  HAS_THESIS: '#6366f1',
  HAS_DRIVER: '#f43f5e',
  PRODUCES: '#8b5cf6',
  LOCKED_ASSUMPTION: '#10b981',
  RELATES_TO: '#52525b',
}

function edgeColor(relation: string): string {
  return EDGE_COLORS[relation] || '#3f3f46'
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

function nodeRole(n: KgNode): NodeRole {
  if (n.node_type === 'company') return 'company'
  if (n.node_type === 'dcf_run') return 'run'
  if (RUN_SCOPED_TYPES.has(n.node_type)) return 'run_leaf'
  if (SHARED_KNOWLEDGE_TYPES.has(n.node_type)) return 'shared'
  return 'other'
}

// ── Simulation types ─────────────────────────────────────────────────────────

interface SimNode extends SimulationNodeDatum {
  id: string
  raw: KgNode
  role: NodeRole
  degree: number
  radius: number
}

interface SimLink extends SimulationLinkDatum<SimNode> {
  id: string
  raw: KgEdge
  distance: number
  strength: number
}

// ── Props ────────────────────────────────────────────────────────────────────

interface Props {
  nodes: KgNode[]
  edges: KgEdge[]
  highlightSet: Set<string>
  onNodeClick: (n: KgNode) => void
  onNodeHover?: (n: KgNode | null) => void
  /** map ticker → { implied, spot } for tooltip enrichment on company nodes */
  companySummary?: Map<string, { implied?: number; spot?: number; delta?: number }>
}

// ── Per-link distance/strength by relation ───────────────────────────────────

function linkParamsFor(edge: KgEdge): { distance: number; strength: number } {
  switch (edge.relation) {
    case 'HAS_RUN':
      return { distance: 180, strength: 0.55 }
    case 'HAS_METRIC':
    case 'HAS_SYNTHESIS':
    case 'HAS_THESIS':
    case 'HAS_DRIVER':
      // Shared knowledge orbits closer to the company anchor than runs do
      return { distance: 110, strength: 0.7 }
    case 'PRODUCES':
      return { distance: 45, strength: 0.9 }
    case 'LOCKED_ASSUMPTION':
      return { distance: 55, strength: 0.85 }
    case 'RELATES_TO':
      return { distance: 70, strength: 0.4 }
    default:
      return { distance: 80, strength: 0.4 }
  }
}

// ── Canvas ───────────────────────────────────────────────────────────────────

export function KgCanvas({ nodes, edges, highlightSet, onNodeClick, onNodeHover, companySummary }: Props) {
  const svgRef = useRef<SVGSVGElement | null>(null)
  const gRef = useRef<SVGGElement | null>(null)
  const simRef = useRef<Simulation<SimNode, SimLink> | null>(null)
  const zoomRef = useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null)
  const nodePositionsRef = useRef<Map<string, { x: number; y: number; fx?: number | null; fy?: number | null }>>(new Map())

  const [size, setSize] = useState<{ w: number; h: number }>({ w: 800, h: 600 })
  const [hoveredNode, setHoveredNode] = useState<string | null>(null)
  const [renderTick, setRenderTick] = useState(0)

  const simNodesRef = useRef<SimNode[]>([])
  const simLinksRef = useRef<SimLink[]>([])

  // ── Build sim nodes / links ────────────────────────────────────────────────
  useMemo(() => {
    const degree = new Map<string, number>()
    for (const e of edges) {
      degree.set(e.src_id, (degree.get(e.src_id) || 0) + 1)
      degree.set(e.tgt_id, (degree.get(e.tgt_id) || 0) + 1)
    }

    // ── Company anchors: pin at canvas center (or grid if multiple) ─────────
    const companies = nodes.filter(n => n.node_type === 'company')
    const companyPin = new Map<string, { x: number; y: number }>()
    if (companies.length === 1) {
      companyPin.set(companies[0].id, { x: size.w / 2, y: size.h / 2 })
    } else if (companies.length > 1) {
      // Arrange companies in a horizontal row, evenly spaced
      const spacing = Math.min(size.w / (companies.length + 1), 300)
      const startX = (size.w - spacing * (companies.length - 1)) / 2
      companies.forEach((c, i) => {
        companyPin.set(c.id, { x: startX + i * spacing, y: size.h / 2 })
      })
    }

    // ── Concentric initial layout: company center, shared inner ring, runs middle ring,
    //    leaves outer ring. Prevents the "all clumped at center" first-render mess.
    const cx = size.w / 2
    const cy = size.h / 2
    function initialPosFor(n: KgNode, role: NodeRole, ringIdx: Map<NodeRole, number>): { x: number; y: number } {
      if (role === 'company') {
        const pin = companyPin.get(n.id)
        return pin ?? { x: cx, y: cy }
      }
      const ringRadius =
        role === 'shared' ? 130 :
        role === 'run' ? 200 :
        role === 'run_leaf' ? 290 :
        160
      const idx = ringIdx.get(role) ?? 0
      ringIdx.set(role, idx + 1)
      // Use golden angle for even angular spread regardless of count
      const angle = idx * 2.39996  // golden angle in radians
      return {
        x: cx + ringRadius * Math.cos(angle),
        y: cy + ringRadius * Math.sin(angle),
      }
    }
    const ringIdx = new Map<NodeRole, number>()

    const newSimNodes: SimNode[] = nodes.map(n => {
      const role = nodeRole(n)
      const prev = nodePositionsRef.current.get(n.id)
      const deg = degree.get(n.id) || 0
      const baseRadius =
        role === 'company' ? 16 :
        role === 'run' ? 11 :
        role === 'shared' ? 8 :
        role === 'run_leaf' ? 6 : 7
      const radius = Math.min(20, baseRadius + Math.min(4, deg * 0.4))

      const pin = companyPin.get(n.id)
      const init = prev ? { x: prev.x, y: prev.y } : initialPosFor(n, role, ringIdx)
      return {
        id: n.id,
        raw: n,
        role,
        degree: deg,
        radius,
        x: init.x,
        y: init.y,
        // Pin companies in place; preserve user-pinned positions for others
        fx: pin ? pin.x : (prev?.fx ?? null),
        fy: pin ? pin.y : (prev?.fy ?? null),
      }
    })

    const idMap = new Map(newSimNodes.map(n => [n.id, n]))
    const newSimLinks: SimLink[] = edges
      .filter(e => idMap.has(e.src_id) && idMap.has(e.tgt_id))
      .map(e => {
        const { distance, strength } = linkParamsFor(e)
        return {
          id: e.id,
          source: idMap.get(e.src_id)!,
          target: idMap.get(e.tgt_id)!,
          raw: e,
          distance,
          strength,
        }
      })

    simNodesRef.current = newSimNodes
    simLinksRef.current = newSimLinks
  }, [nodes, edges, size.w, size.h])

  // ── Resize observer ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!svgRef.current) return
    const el = svgRef.current
    const ro = new ResizeObserver(entries => {
      for (const e of entries) {
        const { width, height } = e.contentRect
        setSize({ w: Math.max(200, width), h: Math.max(200, height) })
      }
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // ── Force simulation lifecycle ─────────────────────────────────────────────
  useEffect(() => {
    if (simRef.current) simRef.current.stop()
    const simNodes = simNodesRef.current
    const simLinks = simLinksRef.current

    const sim = forceSimulation<SimNode, SimLink>(simNodes)
      .force('link', forceLink<SimNode, SimLink>(simLinks)
        .id(d => d.id)
        .distance(d => d.distance)
        .strength(d => d.strength),
      )
      // Stronger repulsion for companies, weaker for leaves
      .force('charge', forceManyBody<SimNode>().strength(d => {
        if (d.role === 'company') return -600
        if (d.role === 'run') return -250
        if (d.role === 'shared') return -180
        if (d.role === 'run_leaf') return -90
        return -150
      }))
      .force('center', forceCenter(size.w / 2, size.h / 2).strength(0.02))
      .force('collide', forceCollide<SimNode>().radius(d => d.radius + 4))
      // Push run-leaves slightly outward from center so they cluster around their parent run
      .force('radial-leaves', forceRadial<SimNode>(
        d => d.role === 'run_leaf' ? 220 : d.role === 'run' ? 160 : 0,
        size.w / 2,
        size.h / 2,
      ).strength(d => d.role === 'run_leaf' ? 0.08 : d.role === 'run' ? 0.05 : 0))
      .alpha(1.2)
      .alphaDecay(0.018)
      .velocityDecay(0.45)
      .on('tick', () => {
        for (const n of simNodes) {
          nodePositionsRef.current.set(n.id, {
            x: n.x || 0,
            y: n.y || 0,
            fx: n.fx,
            fy: n.fy,
          })
        }
        setRenderTick(t => t + 1)
      })

    simRef.current = sim
    return () => { sim.stop() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes.length, edges.length, nodes.map(n => n.id).join(','), edges.map(e => e.id).join(',')])

  // ── Zoom / pan ─────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!svgRef.current || !gRef.current) return
    const svgSel = select(svgRef.current)
    const gSel = select(gRef.current)

    const z = zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.1, 4])
      .on('zoom', ev => {
        gSel.attr('transform', ev.transform.toString())
      })

    svgSel.call(z)
    zoomRef.current = z
    svgSel.call(z.transform, zoomIdentity)
    return () => { svgSel.on('.zoom', null) }
  }, [])

  // ── React-managed drag (avoids d3-drag data-binding issues) ───────────────
  // Returns a screen→graph coord mapper that accounts for current zoom transform.
  const screenToGraph = useCallback((clientX: number, clientY: number): { x: number; y: number } | null => {
    const svg = svgRef.current
    if (!svg) return null
    const ctm = svg.getScreenCTM()
    if (!ctm) return null
    const pt = svg.createSVGPoint()
    pt.x = clientX
    pt.y = clientY
    const sp = pt.matrixTransform(ctm.inverse())
    const t = zoomTransform(svg)
    return { x: (sp.x - t.x) / t.k, y: (sp.y - t.y) / t.k }
  }, [])

  const handleNodeMouseDown = useCallback((e: React.MouseEvent, simNodeId: string) => {
    e.stopPropagation()
    e.preventDefault()
    const n = simNodesRef.current.find(s => s.id === simNodeId)
    if (!n) return
    if (n.role === 'company') return  // companies stay pinned by design

    // Record starting state
    const startGraph = screenToGraph(e.clientX, e.clientY)
    if (!startGraph) return
    const nodeStartX = n.x ?? 0
    const nodeStartY = n.y ?? 0
    let didMove = false

    simRef.current?.alphaTarget(0.3).restart()

    function onMove(ev: MouseEvent) {
      const g = screenToGraph(ev.clientX, ev.clientY)
      if (!g) return
      didMove = true
      n!.fx = nodeStartX + (g.x - startGraph!.x)
      n!.fy = nodeStartY + (g.y - startGraph!.y)
    }
    function onUp() {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      simRef.current?.alphaTarget(0)
      if (didMove && n) {
        // Pin where dropped
        nodePositionsRef.current.set(n.id, {
          x: n.fx ?? n.x ?? 0,
          y: n.fy ?? n.y ?? 0,
          fx: n.fx,
          fy: n.fy,
        })
      }
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [screenToGraph])

  // Unpin: double-click a node to release it back to the simulation
  const handleNodeDoubleClick = useCallback((e: React.MouseEvent, simNodeId: string) => {
    e.stopPropagation()
    const n = simNodesRef.current.find(s => s.id === simNodeId)
    if (!n || n.role === 'company') return
    n.fx = null
    n.fy = null
    nodePositionsRef.current.set(n.id, { x: n.x ?? 0, y: n.y ?? 0, fx: null, fy: null })
    simRef.current?.alpha(0.3).restart()
  }, [])

  // ── Render ─────────────────────────────────────────────────────────────────
  const simNodes = simNodesRef.current
  const simLinks = simLinksRef.current
  const hiNodes = highlightSet

  function handleNodeMouseEnter(n: SimNode) {
    setHoveredNode(n.id)
    onNodeHover?.(n.raw)
  }
  function handleNodeMouseLeave() {
    setHoveredNode(null)
    onNodeHover?.(null)
  }

  return (
    <svg ref={svgRef} className="w-full h-full bg-[#0a0a0a]">
      <defs>
        <marker id="arrow-default" viewBox="0 -5 10 10" refX="14" refY="0" markerWidth="5" markerHeight="5" orient="auto">
          <path d="M0,-5L10,0L0,5" fill="#52525b" />
        </marker>
        <marker id="arrow-highlight" viewBox="0 -5 10 10" refX="14" refY="0" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M0,-5L10,0L0,5" fill="#2dd4bf" />
        </marker>
      </defs>

      <g ref={gRef}>
        {/* Edges — curved bezier paths for less overlap */}
        <g className="kg-edges" fill="none">
          {simLinks.map(l => {
            const src = l.source as SimNode
            const tgt = l.target as SimNode
            const isHi = hiNodes.has(src.id) && hiNodes.has(tgt.id)
            const stroke = isHi ? '#2dd4bf' : edgeColor(l.raw.relation)
            const opacity = hiNodes.size > 0 && !isHi ? 0.15 : 0.55
            const x1 = src.x || 0, y1 = src.y || 0
            const x2 = tgt.x || 0, y2 = tgt.y || 0
            // Quadratic curve with control point perpendicular to midpoint
            const dx = x2 - x1, dy = y2 - y1
            const dr = Math.sqrt(dx * dx + dy * dy)
            const curvature = 0.18
            const mx = (x1 + x2) / 2 - dy * curvature
            const my = (y1 + y2) / 2 + dx * curvature
            // Shorten end of path so arrow doesn't overlap target node circle
            const shorten = (tgt.radius || 8) + 2
            const ex = x2 - (dx / (dr || 1)) * shorten
            const ey = y2 - (dy / (dr || 1)) * shorten
            const d = `M ${x1},${y1} Q ${mx},${my} ${ex},${ey}`
            return (
              <path
                key={l.id}
                d={d}
                stroke={stroke}
                strokeWidth={isHi ? 2 : 1}
                opacity={opacity}
                markerEnd={isHi ? 'url(#arrow-highlight)' : 'url(#arrow-default)'}
              />
            )
          })}
        </g>

        {/* Nodes */}
        <g className="kg-nodes">
          {simNodes.map(n => {
            const isHi = hiNodes.has(n.id)
            const isHover = hoveredNode === n.id
            const dim = hiNodes.size > 0 && !isHi
            const color = colorForNode(n.raw)
            const radius = n.radius
            return (
              <g
                key={n.id}
                className="kg-node"
                data-id={n.id}
                transform={`translate(${n.x || 0},${n.y || 0})`}
                style={{ cursor: n.role === 'company' ? 'pointer' : 'grab', opacity: dim ? 0.3 : 1 }}
                onMouseEnter={() => handleNodeMouseEnter(n)}
                onMouseLeave={handleNodeMouseLeave}
                onMouseDown={(e) => handleNodeMouseDown(e, n.id)}
                onDoubleClick={(e) => handleNodeDoubleClick(e, n.id)}
                onClick={(e) => { e.stopPropagation(); onNodeClick(n.raw) }}
              >
                {(isHi || isHover || n.role === 'company') && (
                  <circle
                    r={radius + (n.role === 'company' ? 7 : 5)}
                    fill="none"
                    stroke={isHi ? '#2dd4bf' : color}
                    strokeWidth={n.role === 'company' ? 2 : 1.5}
                    opacity={n.role === 'company' ? 0.4 : 0.7}
                  />
                )}
                <circle
                  r={radius}
                  fill={color}
                  fillOpacity={n.role === 'company' ? 0.95 : 0.85}
                  stroke={isHi ? '#2dd4bf' : '#0a0a0a'}
                  strokeWidth={isHi ? 2 : 1.5}
                />
                <text
                  y={radius + 12}
                  textAnchor="middle"
                  fontSize={n.role === 'company' ? 13 : 11}
                  fontWeight={n.role === 'company' ? 600 : 400}
                  fill={isHi ? '#5eead4' : (n.role === 'company' ? '#e4e4e7' : '#a1a1aa')}
                  pointerEvents="none"
                  style={{ userSelect: 'none' }}
                >
                  {labelFor(n.raw)}
                </text>
              </g>
            )
          })}
        </g>
      </g>

      {/* Hover tooltip — company gets enriched with implied/spot/delta */}
      {hoveredNode && (() => {
        const n = simNodes.find(s => s.id === hoveredNode)
        if (!n) return null
        const isCompany = n.role === 'company'
        const summary = isCompany ? companySummary?.get(n.raw.ticker) : undefined
        const lines = buildTooltipLines(n.raw, summary)
        const lineH = 14
        const padX = 8, padY = 8
        const w = Math.min(280, Math.max(120, Math.max(...lines.map(l => l.length)) * 7 + padX * 2))
        const h = lines.length * lineH + padY * 2

        // Position to right; clamp if near right edge
        const baseX = (n.x || 0) + n.radius + 14
        const baseY = (n.y || 0) - h / 2
        return (
          <g transform={`translate(${baseX}, ${baseY})`} pointerEvents="none">
            <rect
              x={0} y={0}
              width={w}
              height={h}
              rx={4}
              fill="#11111a"
              stroke="#2a2a36"
              opacity={0.96}
            />
            {lines.map((line, i) => (
              <text
                key={i}
                x={padX}
                y={padY + (i + 1) * lineH - 4}
                fontSize={i === 0 ? 11 : 10}
                fontWeight={i === 0 ? 600 : 400}
                fill={i === 0 ? '#e4e4e7' : '#a1a1aa'}
              >
                {line}
              </text>
            ))}
          </g>
        )
      })()}
    </svg>
  )
}

function labelFor(n: KgNode): string {
  if (n.node_type === 'company') return n.ticker
  if (n.node_type === 'dcf_run') {
    const horizon = (n.value && typeof n.value === 'object' && 'horizon_years' in n.value)
      ? `${(n.value as Record<string, unknown>).horizon_years}y`
      : ''
    return `${n.ticker} dcf${horizon ? ' ' + horizon : ''}`
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
  lines.push(n.node_type)
  lines.push(n.field)
  return lines
}

function fmtMoney(v: number): string {
  if (Math.abs(v) >= 1000) return v.toFixed(0)
  return v.toFixed(2)
}
