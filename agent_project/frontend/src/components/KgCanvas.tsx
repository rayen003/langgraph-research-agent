import { useEffect, useRef, useState, useMemo, useCallback } from 'react'
import {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceCollide,
  forceRadial,
  forceX,
  forceY,
  type Simulation,
  type SimulationNodeDatum,
  type SimulationLinkDatum,
} from 'd3-force'
import { zoom, zoomIdentity, type ZoomBehavior } from 'd3-zoom'

import { select } from 'd3-selection'
import { edgeKey, type KgNode, type KgEdge } from '../hooks/useKnowledgeGraph'

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
  deck_run: '#0891b2',
  deck_slide: '#164e63',
  company_lifecycle: '#7c3aed',
  filing: '#78350f',
  news_item: '#1c1917',
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
  HAS_DECK: '#0891b2',
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
  /** Directed traversal edges ("srcId->tgtId") to draw as the answer route. */
  highlightEdgeSet?: Set<string>
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
      return { distance: 110, strength: 0.7 }
    case 'PRODUCES':
      return { distance: 45, strength: 0.9 }
    case 'LOCKED_ASSUMPTION':
      return { distance: 55, strength: 0.85 }
    case 'HAS_DECK':
      return { distance: 150, strength: 0.5 }
    case 'RELATES_TO':
      return { distance: 70, strength: 0.4 }
    default:
      return { distance: 80, strength: 0.4 }
  }
}

// ── Canvas ───────────────────────────────────────────────────────────────────

export function KgCanvas({ nodes, edges, highlightSet, highlightEdgeSet, onNodeClick, onNodeHover, companySummary }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const simRef = useRef<Simulation<SimNode, SimLink> | null>(null)
  const zoomRef = useRef<ZoomBehavior<HTMLCanvasElement, unknown> | null>(null)
  const nodePositionsRef = useRef<Map<string, { x: number; y: number; fx?: number | null; fy?: number | null }>>(new Map())
  const transformRef = useRef<{ k: number; x: number; y: number }>({ k: 1, x: 0, y: 0 })
  const rafRef = useRef<number | null>(null)
  const simHotRef = useRef(true)

  const [size, setSize] = useState<{ w: number; h: number }>({ w: 800, h: 600 })
  const [hoveredNode, setHoveredNode] = useState<string | null>(null)
  const hoveredNodeRef = useRef<string | null>(null)
  const [zoomScale, setZoomScale] = useState(1)
  const zoomScaleRef = useRef(1)

  const simNodesRef = useRef<SimNode[]>([])
  const simLinksRef = useRef<SimLink[]>([])
  const simInitializedRef = useRef(false)

  // ── Build sim nodes / links ────────────────────────────────────────────────
  useMemo(() => {
    const degree = new Map<string, number>()
    for (const e of edges) {
      degree.set(e.src_id, (degree.get(e.src_id) || 0) + 1)
      degree.set(e.tgt_id, (degree.get(e.tgt_id) || 0) + 1)
    }

    const companies = nodes.filter(n => n.node_type === 'company')
    const companyPin = new Map<string, { x: number; y: number }>()
    if (companies.length === 1) {
      companyPin.set(companies[0].id, { x: size.w / 2, y: size.h / 2 })
    } else if (companies.length > 1) {
      const spacing = Math.min(size.w / (companies.length + 1), 300)
      const startX = (size.w - spacing * (companies.length - 1)) / 2
      companies.forEach((c, i) => {
        companyPin.set(c.id, { x: startX + i * spacing, y: size.h / 2 })
      })
    }

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
      const angle = idx * 2.39996
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

      const init = prev ? { x: prev.x, y: prev.y } : initialPosFor(n, role, ringIdx)
      const fx = prev?.fx ?? null
      const fy = prev?.fy ?? null
      return {
        id: n.id,
        raw: n,
        role,
        degree: deg,
        radius,
        x: init.x,
        y: init.y,
        fx,
        fy,
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

  // ── Draw frame ───────────────────────────────────────────────────────────────
  const drawFrame = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const dpr = window.devicePixelRatio || 1
    const { k, x, y } = transformRef.current
    const currentZoom = zoomScaleRef.current
    const simNodes = simNodesRef.current
    const simLinks = simLinksRef.current
    const hiNodes = highlightSet
    const hovId = hoveredNodeRef.current

    ctx.clearRect(0, 0, canvas.width, canvas.height)

    ctx.save()
    ctx.setTransform(k * dpr, 0, 0, k * dpr, x * dpr, y * dpr)

    // ── Draw edges ──────────────────────────────────────────────────────────
    const hasHighlight = hiNodes.size > 0 || (highlightEdgeSet?.size ?? 0) > 0

    for (const l of simLinks) {
      const src = l.source as SimNode
      const tgt = l.target as SimNode
      const isPath = !!highlightEdgeSet?.has(edgeKey(src.id, tgt.id))
      const isHi = isPath || (hiNodes.has(src.id) && hiNodes.has(tgt.id))
      const opacity = hasHighlight && !isHi ? 0.12 : 0.55
      const stroke = isHi ? '#2dd4bf' : edgeColor(l.raw.relation)

      const x1 = src.x || 0, y1 = src.y || 0
      const x2 = tgt.x || 0, y2 = tgt.y || 0
      const dx = x2 - x1, dy = y2 - y1
      const dr = Math.sqrt(dx * dx + dy * dy)
      const curvature = 0.18
      const mx = (x1 + x2) / 2 - dy * curvature
      const my = (y1 + y2) / 2 + dx * curvature
      const shorten = (tgt.radius || 8) + 2
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
      // Tangent at end of quadratic: from control point to end point
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
    for (const n of simNodes) {
      const isHi = hiNodes.has(n.id)
      const isHover = hovId === n.id
      const dim = hiNodes.size > 0 && !isHi
      const color = colorForNode(n.raw)
      const r = n.radius
      const nx = n.x || 0, ny = n.y || 0

      ctx.save()
      ctx.globalAlpha = dim ? 0.3 : 1

      // Outer ring for highlighted / hovered / company nodes
      if (isHi || isHover || n.role === 'company') {
        const ringR = r + (n.role === 'company' ? 7 : 5)
        ctx.beginPath()
        ctx.arc(nx, ny, ringR, 0, Math.PI * 2)
        ctx.strokeStyle = isHi ? '#2dd4bf' : color
        ctx.lineWidth = n.role === 'company' ? 2 : 1.5
        ctx.globalAlpha = dim ? 0.3 : (n.role === 'company' ? 0.4 : 0.7)
        ctx.stroke()
        ctx.globalAlpha = dim ? 0.3 : 1
      }

      // Filled circle
      ctx.beginPath()
      ctx.arc(nx, ny, r, 0, Math.PI * 2)
      ctx.fillStyle = color
      ctx.globalAlpha = dim ? 0.3 : (n.role === 'company' ? 0.95 : 0.85)
      ctx.fill()
      ctx.globalAlpha = dim ? 0.3 : 1
      ctx.strokeStyle = isHi ? '#2dd4bf' : '#0a0a0a'
      ctx.lineWidth = isHi ? 2 : 1.5
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
      ctx.strokeStyle = '#0a0a0a'
      ctx.lineWidth = 2.5
      ctx.lineJoin = 'round'
      ctx.strokeText(label, nx, ny + r + 3)

      ctx.globalAlpha = dim ? 0.3 * labelOpacity : labelOpacity
      ctx.fillStyle = isHi ? '#5eead4' : (n.role === 'company' ? '#e4e4e7' : '#a1a1aa')
      ctx.fillText(label, nx, ny + r + 3)

      ctx.restore()
    }

    ctx.restore() // end world-space transform

    // ── Tooltip in screen space ─────────────────────────────────────────────
    if (hovId) {
      const n = simNodes.find(s => s.id === hovId)
      if (n) {
        const isCompany = n.role === 'company'
        const summary = isCompany ? companySummary?.get(n.raw.ticker) : undefined
        const lines = buildTooltipLines(n.raw, summary)

        // Project node center to screen
        const sx = (n.x || 0) * k + x
        const sy = (n.y || 0) * k + y

        const lineH = 14
        const padX = 8, padY = 8
        const maxLen = Math.max(...lines.map(l => l.length))
        const w = Math.min(280, Math.max(120, maxLen * 7 + padX * 2))
        const h = lines.length * lineH + padY * 2
        const bx = sx + n.radius * k + 14
        const by = sy - h / 2

        ctx.save()
        ctx.globalAlpha = 0.96
        ctx.fillStyle = '#11111a'
        ctx.strokeStyle = '#2a2a36'
        ctx.lineWidth = 1
        ctx.beginPath()
        ctx.roundRect(bx, by, w, h, 4)
        ctx.fill()
        ctx.stroke()

        lines.forEach((line, i) => {
          ctx.globalAlpha = 1
          ctx.font = `${i === 0 ? 600 : 400} ${i === 0 ? 11 : 10}px sans-serif`
          ctx.fillStyle = i === 0 ? '#e4e4e7' : '#a1a1aa'
          ctx.textAlign = 'left'
          ctx.textBaseline = 'top'
          ctx.fillText(line, bx + padX, by + padY + i * lineH)
        })
        ctx.restore()
      }
    }
  }, [highlightSet, highlightEdgeSet, companySummary])

  // ── rAF loop ─────────────────────────────────────────────────────────────
  useEffect(() => {
    let running = true
    function loop() {
      if (!running) return
      if (simHotRef.current) {
        drawFrame()
      }
      rafRef.current = requestAnimationFrame(loop)
    }
    rafRef.current = requestAnimationFrame(loop)
    return () => {
      running = false
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current)
    }
  }, [drawFrame])

  // ── Resize observer ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!canvasRef.current) return
    const el = canvasRef.current
    const ro = new ResizeObserver(entries => {
      for (const e of entries) {
        const { width, height } = e.contentRect
        const w = Math.max(200, width)
        const h = Math.max(200, height)
        setSize({ w, h })
        // Resize canvas backing store
        const dpr = window.devicePixelRatio || 1
        el.width = w * dpr
        el.height = h * dpr
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

    simHotRef.current = true

    const sim = forceSimulation<SimNode, SimLink>(simNodes)
      .force('link', forceLink<SimNode, SimLink>(simLinks)
        .id(d => d.id)
        .distance(d => d.distance)
        .strength(d => d.strength),
      )
      .force('charge', forceManyBody<SimNode>().strength(d => {
        if (d.degree === 0) return -20
        if (d.role === 'company') return -900
        if (d.role === 'run') return -350
        if (d.role === 'shared') return -200
        if (d.role === 'run_leaf') return -100
        return -150
      }))
      .force('collide', forceCollide<SimNode>().radius(d => d.radius + 4))
      .force('radial-leaves', forceRadial<SimNode>(
        d => d.role === 'run_leaf' ? 220 : d.role === 'run' ? 160 : 0,
        size.w / 2,
        size.h / 2,
      ).strength(d => d.role === 'run_leaf' ? 0.08 : d.role === 'run' ? 0.05 : 0))
      .force('gravity-x', forceX<SimNode>(d =>
        d.degree === 0 ? size.w * 0.08 : size.w / 2,
      ).strength(d =>
        d.role === 'company' ? 0.09 : d.degree === 0 ? 0.12 : 0.04))
      .force('gravity-y', forceY<SimNode>(size.h / 2).strength(d =>
        d.role === 'company' ? 0.09 : d.degree === 0 ? 0.03 : 0.04))
      .alpha(simInitializedRef.current ? 0.4 : 1.2)
      .alphaTarget(0.03)
      .alphaDecay(0.008)
      .velocityDecay(0.25)
      .on('tick', () => {
        for (const n of simNodes) {
          nodePositionsRef.current.set(n.id, {
            x: n.x || 0,
            y: n.y || 0,
            fx: n.fx,
            fy: n.fy,
          })
        }
        drawFrame()
      })

    simRef.current = sim
    simInitializedRef.current = true
    return () => { sim.stop() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes.length, edges.length, nodes.map(n => n.id).join(','), edges.map(e => e.id).join(','), size.w, size.h])

  // ── Zoom / pan ─────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!canvasRef.current) return
    const canvasSel = select(canvasRef.current)

    const z = zoom<HTMLCanvasElement, unknown>()
      .scaleExtent([0.1, 4])
      .on('zoom', ev => {
        const { k, x, y } = ev.transform
        transformRef.current = { k, x, y }
        zoomScaleRef.current = k
        setZoomScale(k)
        simHotRef.current = true
        drawFrame()
      })

    canvasSel.call(z)
    zoomRef.current = z
    canvasSel.call(z.transform, zoomIdentity)
    return () => { canvasSel.on('.zoom', null) }
  }, [drawFrame])

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
  const hitTest = useCallback((gx: number, gy: number): SimNode | null => {
    let closest: SimNode | null = null
    let minDist = Infinity
    for (const n of simNodesRef.current) {
      const dx = (n.x || 0) - gx
      const dy = (n.y || 0) - gy
      const d = Math.sqrt(dx * dx + dy * dy)
      if (d < n.radius + 4 && d < minDist) {
        minDist = d
        closest = n
      }
    }
    return closest
  }, [])

  // ── Mouse move for hover ─────────────────────────────────────────────────
  const handleCanvasMouseMove = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    // Skip during d3-zoom drag (buttons held = panning)
    if (e.buttons > 0 && !e.shiftKey) return
    const g = screenToGraph(e.clientX, e.clientY)
    if (!g) return
    const hit = hitTest(g.x, g.y)
    const newId = hit ? hit.id : null
    if (newId !== hoveredNodeRef.current) {
      hoveredNodeRef.current = newId
      setHoveredNode(newId)
      onNodeHover?.(hit ? hit.raw : null)
      simHotRef.current = true
      drawFrame()
    }
  }, [screenToGraph, hitTest, onNodeHover, drawFrame])

  // ── Click ────────────────────────────────────────────────────────────────
  const clickStartRef = useRef<{ x: number; y: number } | null>(null)

  const handleCanvasMouseDown = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    clickStartRef.current = { x: e.clientX, y: e.clientY }
    const g = screenToGraph(e.clientX, e.clientY)
    if (!g) return
    const hit = hitTest(g.x, g.y)
    if (!hit) return

    e.stopPropagation()

    const pinOnDrop = e.shiftKey
    const nodeStartX = hit.x ?? 0
    const nodeStartY = hit.y ?? 0
    let didMove = false

    simRef.current?.alphaTarget(0.5).restart()
    simHotRef.current = true

    function onMove(ev: MouseEvent) {
      const g2 = screenToGraph(ev.clientX, ev.clientY)
      if (!g2) return
      const startG = screenToGraph(e.clientX, e.clientY)
      if (!startG) return
      didMove = true
      hit!.fx = nodeStartX + (g2.x - startG.x)
      hit!.fy = nodeStartY + (g2.y - startG.y)
    }
    function onUp() {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      simRef.current?.alphaTarget(0.03)
      if (hit) {
        if (!didMove || !pinOnDrop) {
          hit.fx = null
          hit.fy = null
        }
        nodePositionsRef.current.set(hit.id, {
          x: hit.x ?? 0, y: hit.y ?? 0, fx: hit.fx, fy: hit.fy,
        })
      }
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [screenToGraph, hitTest])

  const handleCanvasClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const start = clickStartRef.current
    if (!start) return
    const dist = Math.sqrt((e.clientX - start.x) ** 2 + (e.clientY - start.y) ** 2)
    if (dist > 4) return // was a drag
    const g = screenToGraph(e.clientX, e.clientY)
    if (!g) return
    const hit = hitTest(g.x, g.y)
    if (hit) onNodeClick(hit.raw)
  }, [screenToGraph, hitTest, onNodeClick])

  const handleCanvasDblClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const g = screenToGraph(e.clientX, e.clientY)
    if (!g) return
    const hit = hitTest(g.x, g.y)
    if (!hit) return
    hit.fx = null
    hit.fy = null
    nodePositionsRef.current.set(hit.id, { x: hit.x ?? 0, y: hit.y ?? 0, fx: null, fy: null })
    simRef.current?.alpha(0.3).restart()
  }, [screenToGraph, hitTest])

  const handleCanvasMouseLeave = useCallback(() => {
    if (hoveredNodeRef.current !== null) {
      hoveredNodeRef.current = null
      setHoveredNode(null)
      onNodeHover?.(null)
      drawFrame()
    }
  }, [onNodeHover, drawFrame])

  return (
    <canvas
      ref={canvasRef}
      className="w-full h-full bg-[#0a0a0a]"
      style={{ display: 'block' }}
      onMouseMove={handleCanvasMouseMove}
      onMouseDown={handleCanvasMouseDown}
      onClick={handleCanvasClick}
      onDoubleClick={handleCanvasDblClick}
      onMouseLeave={handleCanvasMouseLeave}
    />
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
