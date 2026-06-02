/**
 * Central source of truth for human-friendly tool and workflow-step labels.
 *
 * All UI surfaces (chat bubble activity, research timeline, execution sidebar)
 * read from here so we stay consistent and never expose raw snake_case ids
 * or noisy JSON to end users.
 */

export type ToolGroup = 'tool' | 'workflow' | 'unknown'

export interface ToolDisplay {
  /** Short verb phrase shown to the user, e.g. "Searching web". */
  label: string
  /** Optional one-line description used as fallback or tooltip. */
  description?: string
  group: ToolGroup
  /** When group === 'workflow', the workflow id (e.g. "dcf"). */
  workflow?: string
  /** When group === 'workflow', the canonical step id (e.g. "build_assumptions"). */
  workflowStep?: string
}

const TOOL_DISPLAY: Record<string, ToolDisplay> = {
  search_web: {
    label: 'Searching the web',
    description: 'Querying external sources via Exa',
    group: 'tool',
  },
  search_documents: {
    label: 'Searching uploaded documents',
    description: 'Hybrid retrieval over session documents',
    group: 'tool',
  },
  calculator: {
    label: 'Calculating',
    description: 'Safe numeric expression evaluation',
    group: 'tool',
  },
  execute_python: {
    label: 'Running Python',
    description: 'Sandboxed Python execution',
    group: 'tool',
  },
  retrieve_context: {
    label: 'Reading prior step output',
    group: 'tool',
  },
  retrieve_tool_result: {
    label: 'Retrieving stored result',
    group: 'tool',
  },
  run_dcf_workflow: {
    label: 'Running DCF workflow',
    description: 'Deterministic discounted cash flow valuation',
    group: 'tool',
  },
  run_deck_workflow: {
    label: 'Building slide deck',
    description: 'Generate a PPTX from typed source inputs',
    group: 'tool',
  },
  fetch_sec_filing: {
    label: 'Reading SEC filing',
    description: 'Fetching 10-K/10-Q from EDGAR',
    group: 'tool',
  },
}

const WORKFLOW_LABEL: Record<string, string> = {
  dcf: 'DCF',
  deck: 'Deck',
  rag: 'Documents',
}

const WORKFLOW_STEP_LABEL: Record<string, Record<string, string>> = {
  dcf: {
    normalize_input: 'Resolving ticker & horizon',
    cache_check: 'Checking knowledge graph',
    kg_backwrite: 'Persisting to knowledge graph',
    assemble_evidence: 'Assembling evidence',
    semantic_synthesis: 'Synthesizing company profile',
    formulate_thesis: 'Formulating investment thesis',
    propose_assumptions: 'Proposing assumptions',
    assumption_review: 'Reviewing assumptions',
    collect_market_data: 'Fetching market data',
    project_cashflows: 'Projecting cash flows',
    compute_valuation: 'Computing valuation',
    compute_implied_wacc: 'Market-implied WACC check',
    compute_market_signals: 'Market-implied expectations',
    sensitivity: 'Running sensitivity table',
    analyze_result: 'Analyzing valuation quality',
    refine_assumptions: 'Refining assumptions',
    scenario_generator: 'Generating scenarios',
    scenario_runner: 'Running scenario valuations',
    review_subgraph: 'Adversarial review',
    review_deep_dive: 'Deep-dive findings',
    synthesize_adjustments: 'Synthesizing adjustments',
    detect_divergences: 'Detecting model–market divergences',
    analysis: 'Analyzing divergences with evidence',
    convergence_gate: 'Convergence gate',
    assumption_journey: 'Assumption journey',
    finalize: 'Finalizing result',
  },
  deck: {
    validate_sources: 'Validating sources',
    normalize_all: 'Normalizing blocks',
    generate_outline: 'Drafting outline',
    outline_review: 'Reviewing outline',
    per_slide_generate: 'Generating slides',
    assemble_pptx: 'Assembling PPTX',
    finalize_deck: 'Finalizing deck',
    adapter_failure: 'Adapter failure',
  },
  rag: {
    retrieve: 'Searching documents',
    embed_query: 'Matching by meaning',
    keyword_rank: 'Matching by keywords',
    fuse: 'Ranking best passages',
  },
}

/**
 * Past-tense action phrases for the collapsed inline chat summary, e.g.
 * "Searched web", "Ran Python", "Read filing". Aggregated + counted by
 * `summarizeToolActions` to produce ChatGPT-style lines like
 * "Searched web ×2 · Calculated".
 */
const TOOL_ACTION_PHRASE: Record<string, string> = {
  search_web: 'Searched web',
  search_documents: 'Searched documents',
  calculator: 'Calculated',
  execute_python: 'Ran Python',
  retrieve_context: 'Read prior step',
  retrieve_tool_result: 'Retrieved result',
  run_dcf_workflow: 'Ran DCF',
  run_deck_workflow: 'Built deck',
  fetch_sec_filing: 'Read filing',
}

export function getToolActionPhrase(toolName: string | undefined): string {
  const safe = String(toolName || 'unknown')
  if (safe.startsWith('workflow:')) {
    const [, workflow = 'workflow'] = safe.split(':')
    return `Ran ${WORKFLOW_LABEL[workflow] ?? workflow.toUpperCase()}`
  }
  return TOOL_ACTION_PHRASE[safe] ?? prettyId(safe)
}

/**
 * Collapse a list of tool names into a counted summary string, preserving
 * first-seen order: ["search_web","search_web","calculator"] →
 * "Searched web ×2 · Calculated".
 */
export function summarizeToolActions(toolNames: string[]): string {
  const order: string[] = []
  const counts = new Map<string, number>()
  for (const name of toolNames) {
    const phrase = getToolActionPhrase(name)
    if (!counts.has(phrase)) order.push(phrase)
    counts.set(phrase, (counts.get(phrase) ?? 0) + 1)
  }
  return order
    .map(phrase => {
      const n = counts.get(phrase) ?? 1
      return n > 1 ? `${phrase} ×${n}` : phrase
    })
    .join(' · ')
}

function prettyId(raw: string | undefined): string {
  return String(raw || 'unknown')
    .split('_')
    .filter(Boolean)
    .map(s => s.charAt(0).toUpperCase() + s.slice(1))
    .join(' ')
}

export function getToolDisplay(toolName: string | undefined): ToolDisplay {
  const safeName = String(toolName || 'unknown')
  if (safeName.startsWith('workflow:')) {
    const [, workflow = 'workflow', step = 'step'] = safeName.split(':')
    const workflowLabel = WORKFLOW_LABEL[workflow] ?? workflow.toUpperCase()
    const stepLabel = (WORKFLOW_STEP_LABEL[workflow] ?? {})[step] ?? prettyId(step)
    return {
      label: `${workflowLabel}: ${stepLabel}`,
      group: 'workflow',
      workflow,
      workflowStep: step,
    }
  }
  const known = TOOL_DISPLAY[safeName]
  if (known) return known
  return { label: prettyId(safeName), group: 'unknown' }
}

/**
 * Strip filesystem paths and condense long JSON-ish payloads so summaries are
 * readable at a glance. Returns a string ≤ maxLen characters.
 */
export function cleanToolSummary(raw: string | undefined, maxLen = 160): string {
  if (!raw) return ''
  let s = String(raw).trim()
  if (!s) return ''

  // Replace absolute filesystem paths with just the file name to reduce noise.
  s = s.replace(/(\s|^|["'(,])((?:\/[A-Za-z0-9._-]+)+)(\.[A-Za-z0-9]+)/g, (_m, lead, _full, ext) => {
    const file = (_full as string).split('/').pop() ?? ''
    return `${lead}${file}${ext}`
  })

  // Collapse repeated whitespace.
  s = s.replace(/\s+/g, ' ')

  // If the payload looks like JSON, summarize keys instead of dumping it raw.
  if ((s.startsWith('{') || s.startsWith('[')) && s.length > maxLen) {
    try {
      const parsed = JSON.parse(s) as unknown
      if (Array.isArray(parsed)) {
        s = `array(${parsed.length})`
      } else if (parsed && typeof parsed === 'object') {
        const keys = Object.keys(parsed as Record<string, unknown>)
        const head = keys.slice(0, 4).join(', ')
        s = `{${head}${keys.length > 4 ? ', …' : ''}}`
      }
    } catch {
      // fall through to truncation
    }
  }

  if (s.length > maxLen) s = s.slice(0, maxLen).trimEnd() + '…'
  return s
}

/** Best-effort one-line preview from raw JSON args. Used by live tool rows. */
export function describeToolArgs(args: Record<string, unknown> | undefined): string {
  if (!args || typeof args !== 'object') return ''
  const candidates = [
    'query',
    'expression',
    'ticker',
    'step_id',
    'tool_result_id',
    'symbol',
    'topic',
  ]
  for (const key of candidates) {
    const v = (args as Record<string, unknown>)[key]
    if (typeof v === 'string' && v.trim()) {
      return v.trim().length > 60 ? v.trim().slice(0, 60) + '…' : v.trim()
    }
  }
  return ''
}
