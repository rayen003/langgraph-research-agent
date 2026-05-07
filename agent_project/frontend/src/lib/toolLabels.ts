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
}

const WORKFLOW_LABEL: Record<string, string> = {
  dcf: 'DCF',
}

const WORKFLOW_STEP_LABEL: Record<string, Record<string, string>> = {
  dcf: {
    start: 'Starting workflow',
    normalize_input: 'Resolving ticker & horizon',
    hydrate_fundamentals: 'Fetching canonical fundamentals',
    build_assumptions: 'Building assumptions',
    assumption_review: 'Reviewing assumptions',
    collect_market_data: 'Fetching market data',
    project_cashflows: 'Projecting cash flows',
    compute_valuation: 'Computing valuation',
    sensitivity: 'Running sensitivity table',
    finalize: 'Finalizing result',
  },
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
