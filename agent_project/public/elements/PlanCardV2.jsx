import { useState } from "react";
import { ChevronDown, ChevronRight, FileText } from "lucide-react";

const STATUS_STYLES = {
  draft: {
    dot: "bg-blue-400 shadow-[0_0_8px_rgba(96,165,250,0.45)]",
    text: "text-blue-400",
    pill: "border-blue-500/20 bg-blue-500/[0.08]",
  },
  approved: {
    dot: "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.45)]",
    text: "text-emerald-400",
    pill: "border-emerald-500/20 bg-emerald-500/[0.08]",
  },
  running: {
    dot: "bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.45)] animate-pulse",
    text: "text-amber-400",
    pill: "border-amber-500/20 bg-amber-500/[0.08]",
  },
  completed: {
    dot: "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.45)]",
    text: "text-emerald-400",
    pill: "border-emerald-500/20 bg-emerald-500/[0.08]",
  },
};

function truncate(text, max) {
  if (!text || text.length <= max) return text || "";
  return text.slice(0, max) + "…";
}

export default function PlanCardV2() {
  const [open, setOpen] = useState(false);
  const { query = "", status = "draft", steps = [] } = props;
  const styles = STATUS_STYLES[status] || STATUS_STYLES.draft;

  return (
    <div
      className="rounded-[24px] border border-white/10 bg-card/70 backdrop-blur overflow-hidden my-4"
      style={{ padding: "14px 16px" }}
    >
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between gap-4 text-left"
      >
        <div className="min-w-0 flex items-center gap-3">
          <div className="rounded-xl border border-white/8 bg-white/[0.03] p-2 shrink-0">
            <FileText className="w-4 h-4 text-muted-foreground" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2.5 flex-wrap">
              <span className="text-sm font-semibold text-foreground">Execution Plan</span>
              <span
                className="rounded-full border border-white/8 bg-white/[0.04] text-muted-foreground"
                style={{ padding: "4px 10px", fontSize: 11, fontWeight: 500 }}
              >
                {steps.length} steps
              </span>
            </div>
            {!open && query && (
              <div className="text-muted-foreground/55" style={{ marginTop: 6, fontSize: 12, lineHeight: "16px" }}>
                {truncate(query, 88)}
              </div>
            )}
          </div>
        </div>

        <div className="flex items-center gap-3 shrink-0">
          <div className={`rounded-xl border ${styles.pill} flex items-center gap-2`} style={{ padding: "8px 10px" }}>
            <div className={`w-1.5 h-1.5 rounded-full ${styles.dot}`} />
            <span className={`text-xs font-medium capitalize ${styles.text}`}>{status}</span>
          </div>
          {open ? (
            <ChevronDown className="w-4 h-4 text-muted-foreground/60" />
          ) : (
            <ChevronRight className="w-4 h-4 text-muted-foreground/60" />
          )}
        </div>
      </button>

      {open && (
        <div className="border-t border-white/8" style={{ marginTop: 14, paddingTop: 14 }}>
          {query && (
            <div
              className="rounded-2xl border border-white/6 bg-white/[0.02] text-muted-foreground/70 italic"
              style={{ padding: "12px 14px", marginBottom: 14, fontSize: 13, lineHeight: "18px" }}
            >
              {query}
            </div>
          )}

          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {steps.map((step, i) => (
              <div
                key={step.id}
                className="rounded-2xl border border-white/6 bg-white/[0.02]"
                style={{ padding: "14px 16px" }}
              >
                <div className="flex items-start gap-3">
                  <div
                    className="rounded-full border border-white/10 bg-white/[0.04] text-muted-foreground shrink-0"
                    style={{
                      width: 26,
                      height: 26,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      fontSize: 12,
                      fontWeight: 600,
                    }}
                  >
                    {i + 1}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-foreground" style={{ fontSize: 15, fontWeight: 500, lineHeight: "22px" }}>
                      {step.description}
                    </div>
                    {step.depends_on && step.depends_on.length > 0 && (
                      <div style={{ marginTop: 8 }}>
                        <span
                          className="rounded-full border border-white/8 bg-white/[0.04] text-muted-foreground"
                          style={{ padding: "4px 8px", fontSize: 11, fontWeight: 500 }}
                        >
                          depends on {step.depends_on.join(", ")}
                        </span>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
