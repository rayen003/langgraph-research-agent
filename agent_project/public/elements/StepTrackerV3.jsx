import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  Eye,
  EyeOff,
  Loader2,
  Sparkles,
} from "lucide-react";

const TOOL_ICON = {
  search_web: "\u{1F50D}",
  calculator: "\u{1F9EE}",
  retrieve_context: "\u{1F4CB}",
  retrieve_tool_result: "\u{1F4C4}",
  execute_python: "\u{1F40D}",
};

const TOOL_LABEL = {
  search_web: "Searching",
  calculator: "Calculating",
  retrieve_context: "Using context",
  retrieve_tool_result: "Reviewing result",
  execute_python: "Running Python",
};

function truncate(text, max) {
  if (!text || text.length <= max) return text || "";
  return text.slice(0, max) + "…";
}

function parseArgsHint(toolName, raw) {
  if (!raw) return "";
  try {
    const args = JSON.parse(raw);
    if (toolName === "search_web" && args.query) return args.query;
    if (toolName === "retrieve_context" && args.step_id) return `Retrieved ${args.step_id}`;
    if (toolName === "retrieve_tool_result") return "Load saved result";
    if (toolName === "calculator" && args.expression) return args.expression;
    if (toolName === "execute_python") return "Run analysis script";
    return "";
  } catch {
    return "";
  }
}

function cleanSummary(summary) {
  if (!summary) return "";
  const s = summary.trim();
  const countMatch = s.match(/(\d+)\s*results?\s*found/i);
  if (countMatch) return `${countMatch[1]} results`;
  if (s.startsWith("Web search completed")) return "Search completed";
  if (s.startsWith("Retrieved:")) return "Loaded";
  if (s.startsWith("Python execution succeeded")) return "Script completed";
  if (s.startsWith("Python execution failed")) return "Script failed";
  return truncate(s, 64);
}

function ToolPill({ tc }) {
  const [expanded, setExpanded] = useState(false);
  const icon = TOOL_ICON[tc.tool_name] || "\u{1F527}";
  const label = TOOL_LABEL[tc.tool_name] || tc.tool_name;
  const hint = parseArgsHint(tc.tool_name, tc.args_preview);
  const short = cleanSummary(tc.summary);
  const stateClass = tc.status === "running" ? "running" : tc.status === "error" ? "error" : "done";

  return (
    <div>
      <button
        onClick={() => tc.summary && setExpanded(!expanded)}
        className={`perplexity-tool-pill ${stateClass}`}
      >
        <span>{icon}</span>
        <span>{label}</span>
        {hint && <span className="opacity-70">→ {truncate(hint, 42)}</span>}
      </button>
      {short && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="perplexity-tool-pill-summary"
        >
          <span>{"└─"}</span>
          <span>{tc.status === "error" ? "⚠️" : "✓"} {short}</span>
        </button>
      )}
      {expanded && tc.summary && (
        <div className="perplexity-tool-pill-detail">
          {tc.summary}
        </div>
      )}
    </div>
  );
}

function StepRow({ step, index }) {
  const [open, setOpen] = useState(false);
  const isRunning = step.status === "running";
  const isCompleted = step.status === "completed";
  const isFailed = step.status === "failed";
  const isPending = step.status === "pending";
  const hasTools = step.tool_calls && step.tool_calls.length > 0;

  useEffect(() => {
    if (isRunning) setOpen(true);
  }, [isRunning]);

  let subLabel = "";
  if (isRunning) subLabel = "Processing";
  else if (isCompleted) subLabel = "Completed";
  else if (isFailed) subLabel = "Needs attention";
  else subLabel = "Pending";

  let nodeClass = "pending";
  if (isRunning) nodeClass = "running";
  else if (isCompleted) nodeClass = "completed";
  else if (isFailed) nodeClass = "failed";

  return (
    <div className="perplexity-step-row">
      <div className="perplexity-step-rail">
        <div className={`perplexity-step-node ${nodeClass}`} />
      </div>

      <div className="perplexity-step-content">
        <button
          onClick={() => hasTools && setOpen(!open)}
          className="w-full text-left"
        >
          <div className={`perplexity-step-title ${isRunning ? "running" : isPending ? "pending" : ""}`}>
            {truncate(step.description, open || isRunning ? 500 : 96)}
          </div>
          <div className="perplexity-step-sublabel">
            Step {index + 1} · {subLabel}
          </div>
        </button>

        {step.reasoning && (isRunning || open) && (
          <div className="perplexity-reasoning">
            {truncate(step.reasoning, 220)}
          </div>
        )}

        {open && hasTools && (
          <div className="perplexity-step-panel">
            <div className="perplexity-tool-pill-list">
              {step.tool_calls.map((tc, j) => (
                <ToolPill key={j} tc={tc} />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function StepTrackerV3() {
  const { steps = [], overall_status = "pending" } = props;
  const [showReasoning, setShowReasoning] = useState(true);

  const completedCount = steps.filter((s) => s.status === "completed").length;
  const isComplete = overall_status === "complete";
  const isSynthesizing = overall_status === "synthesizing";

  useEffect(() => {
    if (isComplete) setShowReasoning(false);
  }, [isComplete]);

  return (
    <div className="perplexity-tracker" style={{ padding: "18px 18px 20px 18px" }}>
      <div className="perplexity-tracker-header">
        <button
          onClick={() => setShowReasoning(!showReasoning)}
          className="flex items-center gap-2 text-left"
        >
          {isComplete ? (
            showReasoning ? (
              <EyeOff className="w-4 h-4 text-muted-foreground" />
            ) : (
              <Eye className="w-4 h-4 text-muted-foreground" />
            )
          ) : (
            <Loader2 className="w-4 h-4 text-muted-foreground animate-spin" />
          )}
          <span className="text-sm font-semibold">
            {isComplete ? (showReasoning ? "Hide reasoning" : "Show reasoning") : "Thinking"}
          </span>
        </button>

        <div className="flex items-center gap-2">
          <div className="perplexity-status-pill text-muted-foreground">
            {isComplete ? "Completed" : isSynthesizing ? "Finalizing" : "Processing"}
          </div>
          <div className="text-[12px] font-medium text-muted-foreground">
            {completedCount}/{steps.length} steps
          </div>
        </div>
      </div>

      {showReasoning && (
        <div className="perplexity-tracker-list">
          {steps.map((step, i) => (
            <StepRow key={step.id} step={step} index={i} />
          ))}

          {(isSynthesizing || isComplete) && (
            <div className="perplexity-synthesis-card">
              {isComplete ? (
                <Sparkles className="text-emerald-400 shrink-0" style={{ width: 16, height: 16 }} />
              ) : (
                <Loader2 className="text-purple-400 animate-spin shrink-0" style={{ width: 16, height: 16 }} />
              )}
              <span
                className={isComplete ? "text-foreground/90" : "text-muted-foreground/80"}
                style={{ fontSize: 15, fontWeight: 500 }}
              >
                {isComplete ? "Here is your report" : "Synthesizing final report…"}
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
