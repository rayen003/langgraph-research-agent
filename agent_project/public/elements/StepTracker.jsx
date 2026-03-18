import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Circle,
  Eye,
  EyeOff,
  Loader2,
  Sparkles,
} from "lucide-react";

const STEP_ACCENTS = [
  { label: "text-amber-400", line: "bg-amber-400/90" },
  { label: "text-cyan-400", line: "bg-cyan-400/90" },
  { label: "text-violet-400", line: "bg-violet-400/90" },
  { label: "text-emerald-400", line: "bg-emerald-400/90" },
  { label: "text-pink-400", line: "bg-pink-400/90" },
];

const TOOL_ICON = {
  search_web: "\u{1F50D}",
  calculator: "\u{1F9EE}",
  retrieve_context: "\u{1F4CB}",
  retrieve_tool_result: "\u{1F4C4}",
  execute_python: "\u{1F40D}",
};

const TOOL_LABEL = {
  search_web: "Web Search",
  calculator: "Calculator",
  retrieve_context: "Context",
  retrieve_tool_result: "Tool Result",
  execute_python: "Python",
};

function truncate(text, max) {
  if (!text || text.length <= max) return text || "";
  return text.slice(0, max) + "…";
}

function parseArgsHint(toolName, raw) {
  if (!raw) return "";
  try {
    const args = JSON.parse(raw);
    if (toolName === "search_web" && args.query) return `"${args.query}"`;
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
  return truncate(s, 72);
}

function ToolEntry({ tc }) {
  const [expanded, setExpanded] = useState(false);
  const icon = TOOL_ICON[tc.tool_name] || "\u{1F527}";
  const label = TOOL_LABEL[tc.tool_name] || tc.tool_name;
  const hint = parseArgsHint(tc.tool_name, tc.args_preview);
  const isRunning = tc.status === "running";
  const isDone = tc.status === "done";
  const isError = tc.status === "error";
  const short = cleanSummary(tc.summary);

  return (
    <div
      className="rounded-xl border border-white/5 bg-white/[0.02]"
      style={{ padding: "10px 12px" }}
    >
      <div className="flex items-start gap-2">
        <div className="pt-[2px] shrink-0">
          {isRunning ? (
            <Loader2 className="animate-spin text-blue-400" style={{ width: 13, height: 13 }} />
          ) : isDone ? (
            <Check className="text-emerald-400" style={{ width: 13, height: 13 }} />
          ) : isError ? (
            <AlertTriangle className="text-red-400" style={{ width: 13, height: 13 }} />
          ) : (
            <Circle className="text-muted-foreground/30" style={{ width: 13, height: 13 }} />
          )}
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <span
              className={isRunning ? "text-blue-400" : "text-muted-foreground"}
              style={{ fontSize: 13, fontWeight: 400, lineHeight: "18px" }}
            >
              {icon} {label}
            </span>
            {hint && (
              <span
                className={isRunning ? "text-blue-400/70" : "text-muted-foreground/50"}
                style={{ fontSize: 13, fontWeight: 400, lineHeight: "18px" }}
              >
                {"→"} {truncate(hint, 54)}
              </span>
            )}
          </div>

          {isRunning && !tc.summary && (
            <div
              className="flex items-center gap-1.5 text-blue-300"
              style={{ marginTop: 6, marginLeft: 16, fontSize: 12, lineHeight: "16px" }}
            >
              <div className="w-1.5 h-1.5 rounded-full bg-blue-400 animate-pulse" />
              <span>Working…</span>
            </div>
          )}

          {short && (
            <button
              onClick={() => setExpanded(!expanded)}
              className="flex items-center gap-1.5 text-left text-muted-foreground/70 hover:text-muted-foreground transition-colors"
              style={{ marginTop: 6, marginLeft: 16, fontSize: 12, lineHeight: "16px" }}
            >
              <span className="opacity-40">{"└─"}</span>
              <span>{isError ? "⚠️" : "✓"} {short}</span>
              {tc.summary && tc.summary.length > 72 && (
                <span className="text-muted-foreground/40 text-[11px]">
                  {expanded ? "(collapse)" : "(details)"}
                </span>
              )}
            </button>
          )}

          {expanded && tc.summary && (
            <div
              className="rounded-lg border border-white/6 bg-black/20 text-muted-foreground/60 whitespace-pre-wrap break-words"
              style={{
                marginTop: 6,
                marginLeft: 16,
                padding: "8px 10px",
                fontSize: 11,
                lineHeight: "16px",
              }}
            >
              {tc.summary}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function StepCard({ step, index }) {
  const [open, setOpen] = useState(false);
  const isRunning = step.status === "running";
  const isCompleted = step.status === "completed";
  const isFailed = step.status === "failed";
  const isPending = step.status === "pending";
  const hasTools = step.tool_calls && step.tool_calls.length > 0;
  const accent = STEP_ACCENTS[index % STEP_ACCENTS.length];

  useEffect(() => {
    if (isRunning) setOpen(true);
  }, [isRunning]);

  const descText = isRunning || open ? step.description : truncate(step.description, 62);
  const panelClasses = isRunning
    ? "border-blue-500/40 bg-blue-500/[0.05]"
    : isCompleted
      ? "border-white/8 bg-white/[0.02]"
      : isPending
        ? "border-white/6 bg-transparent"
        : "border-red-500/30 bg-red-500/[0.03]";

  return (
    <div
      className="grid items-start"
      style={{
        gridTemplateColumns: "108px 1fr",
        columnGap: 22,
      }}
    >
      <div className="pt-3">
        <div className={accent.label} style={{ fontSize: 14, fontWeight: 500 }}>
          Step {index + 1}
        </div>
      </div>

      <div className="flex items-stretch gap-5">
        <div className={`w-px rounded-full ${accent.line}`} style={{ minHeight: 88, opacity: 0.95 }} />

        <div className={`flex-1 rounded-2xl border ${panelClasses} transition-all`} style={{ padding: "16px 18px" }}>
          <button
            onClick={() => hasTools && setOpen(!open)}
            className={`flex w-full items-start gap-3 text-left ${hasTools ? "cursor-pointer" : "cursor-default"}`}
          >
            <div className="pt-[3px] shrink-0">
              {isRunning ? (
                <div className="w-2.5 h-2.5 rounded-full bg-blue-500 shadow-[0_0_8px_rgba(59,130,246,0.65)] animate-pulse" />
              ) : isCompleted ? (
                <Check className="text-emerald-400" style={{ width: 16, height: 16 }} />
              ) : isFailed ? (
                <AlertTriangle className="text-red-400" style={{ width: 16, height: 16 }} />
              ) : (
                <Circle className="text-muted-foreground/25" style={{ width: 16, height: 16 }} />
              )}
            </div>

            <div className="min-w-0 flex-1">
              <div
                className={
                  isRunning
                    ? "text-foreground"
                    : isPending
                      ? "text-muted-foreground/45"
                      : isCompleted
                        ? "text-foreground/90"
                        : "text-red-300"
                }
                style={{ fontSize: 16, fontWeight: 500, lineHeight: "24px" }}
              >
                {descText}
              </div>

              {isRunning && (
                <div className="text-blue-400" style={{ marginTop: 6, fontSize: 12, fontWeight: 500 }}>
                  Processing
                </div>
              )}
            </div>

            {hasTools && (
              <div className="shrink-0 text-muted-foreground/40">
                {open ? (
                  <ChevronDown style={{ width: 15, height: 15 }} />
                ) : (
                  <ChevronRight style={{ width: 15, height: 15 }} />
                )}
              </div>
            )}
          </button>

          {step.reasoning && (isRunning || open) && (
            <div
              className="text-muted-foreground/55 italic"
              style={{ marginTop: 8, marginLeft: 28, fontSize: 12, lineHeight: "18px" }}
            >
              {truncate(step.reasoning, 220)}
            </div>
          )}

          {open && hasTools && (
            <div
              style={{
                marginTop: 14,
                marginLeft: 20,
                display: "flex",
                flexDirection: "column",
                gap: 12,
              }}
            >
              {step.tool_calls.map((tc, j) => (
                <ToolEntry key={j} tc={tc} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function StepTracker() {
  const { steps = [], overall_status = "pending" } = props;
  const [showReasoning, setShowReasoning] = useState(true);

  const completedCount = steps.filter((s) => s.status === "completed").length;
  const isComplete = overall_status === "complete";
  const isSynthesizing = overall_status === "synthesizing";

  useEffect(() => {
    if (isComplete) setShowReasoning(false);
  }, [isComplete]);

  return (
    <div
      className="rounded-[28px] border border-white/10 bg-card/70 backdrop-blur overflow-hidden my-4"
      style={{ padding: "18px 18px 20px 18px" }}
    >
      <div className="flex items-center justify-between gap-3" style={{ marginBottom: 18 }}>
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
            {isComplete ? (showReasoning ? "Hide reasoning" : "Show reasoning") : "Reasoning"}
          </span>
        </button>

        <div className="flex items-center gap-2">
          <div
            className="rounded-xl border border-white/8 bg-white/[0.04] text-muted-foreground"
            style={{ padding: "8px 12px", fontSize: 12, fontWeight: 500 }}
          >
            {isComplete ? "Completed" : isSynthesizing ? "Finalizing" : "Processing"}
          </div>
          <div className="text-[12px] font-medium text-muted-foreground">
            {completedCount}/{steps.length} steps
          </div>
        </div>
      </div>

      {showReasoning && (
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          {steps.map((step, i) => (
            <StepCard key={step.id} step={step} index={i} />
          ))}

          {(isSynthesizing || isComplete) && (
            <div
              className="rounded-2xl border border-white/8 bg-white/[0.02] flex items-center gap-3"
              style={{ padding: "16px 18px" }}
            >
              {isComplete ? (
                <Sparkles className="text-emerald-400 shrink-0" style={{ width: 16, height: 16 }} />
              ) : (
                <Loader2 className="text-purple-400 animate-spin shrink-0" style={{ width: 16, height: 16 }} />
              )}
              <span
                className={isComplete ? "text-foreground/90" : "text-muted-foreground/80"}
                style={{ fontSize: 15, fontWeight: 500 }}
              >
                {isComplete ? "Report ready" : "Synthesizing final report…"}
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
