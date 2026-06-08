# Fix Session 2025-06-06 — Chat Widget Activity Styling + HITL Substeps

## Frontend Changes (build clean, `npm run build` passes)

### 1. `MessageThread.tsx` — ResearchStatusCard (line 780)
**Before**: 4px pulsing pink dot (`w-1.5 h-1.5 bg-indigo-500 animate-pulse`) + single-line `text-sm text-ink-dim` label.
**After**: 19px bordered circle with animated SVG spinner (executing) or amber checkmark (awaiting states), two-line layout (bold heading + muted progress sublabel). Shows "Running DCF workflow" + "2/6 steps complete".

### 2. `ActivityTrace.tsx` — ActivityTrace header button (line 2113)
**Before**: `w-1.5 h-1.5` pulsing dot with `bg-indigo-400 animate-pulse`.
**After**: 19px bordered circle with spinner SVG (running), checkmark SVG (complete), or X SVG (error).

### 3. `ActivityTrace.tsx` — ActivityRow (line 2233)
**Before**: `w-1 h-1` tiny dot + raw `"{tc.args_preview}"` wrapped in quotes.
**After**: 19px bordered circle + `fmtArgsPreview()` for clean label (e.g., "AMZN · review"), vertical connector lines between rows, `isLast` prop to suppress last connector.

### 4. `ActivityTrace.tsx` — WorkflowGroupRow (line 2164)
**Before**: `w-1 h-1` tiny dot.
**After**: 19px bordered circle with checkmark/spinner.

### 5. `ActivityTrace.tsx` — ResearchStepsTrace (line 2268)
**Before**: Rendered `PersistedStepRow` (old tiny dots + step numbers).
**After**: Renders `StepCard` (19px bordered circles, vertical timeline, `fmtArgsPreview()`).

### 6. `ActivityTrace.tsx` — Research plan button icon
**Before**: `w-1.5 h-1.5` tiny dot.
**After**: 19px bordered circle with checkmark SVG.

### 7. `StepCard.tsx` — `fmtArgsPreview()` exported
Made `export function fmtArgsPreview()` so it can be imported by `ActivityTrace.tsx`.

### 8. `ExecutionSidebar.tsx` — inline left padding removed
Removed `pl-3.5` from inline variant expanded rows (redundant with 19px circles).

---

## Backend Change

### 9. `graph.py` lines 424-446 — Fast-path workflow terminal emission
**Before**: Only `emit_step("valuation_pass", start/complete)` was called — no parent workflow terminal existed, so the frontend `BlockStack` couldn't link substeps to a parent.
**After**: `emit_workflow_terminal(parent_step_id, status="running")` is called BEFORE `dcf_valuation_app.invoke()`, and `status="completed"` is called in `finally`. This ensures:
- The parent activity (`workflow_dcf_{parent_step_id}`) exists before substeps arrive
- The parent transitions to "completed" when the valuation finishes
- The activity counter in the chat widget updates correctly

---

## UNRESOLVED: Substeps in right sidebar do not render after HITL in chat mode

### Symptoms
- Pre-HITL: DCF substeps appear correctly in the right sidebar (ExecutionSidebar → BlockStack)
- Post-HITL approval: substeps stop rendering in the right sidebar despite the valuation graph running successfully in the background
- The activity counter in the chat widget's inline ActivityTrace shows old state (e.g., "0/1 done · 1 running") even after the report is generated

### What was investigated
1. **Activity ID matching**: Verified that `emit_workflow_terminal` and `emit_step` use the same `parent_step_id` → `activity_id` / `parent_activity_id` format (`workflow_dcf_{parent_step_id}`). IDs match correctly.
2. **UI event handler**: The `emit_ui_event` handler is set via `contextvars.ContextVar`. Both the first (pre-HITL) and second (post-HITL) tool calls happen within the same `agent_graph.ainvoke()` scope, so the handler should be active. However, the handler is explicitly checked (`if handler is not None`) and events are silently dropped if None.
3. **Frontend activity merging**: `mergeActivity()` in `activity.ts` replaces entries by `activity_id`. Should correctly update existing parent from "running" to "completed".
4. **Scope filtering**: `groupActivities()` at line 67 includes both `scope === 'chat'` AND `scope === 'workflow'` entries. Workflow substeps should appear.

### Suspected root cause (unconfirmed)
The second tool invocation (post-HITL fast path) may execute in a context where `_ui_event_handler_ctx` is None. The `emit_step` calls within `dcf_valuation_app.invoke()` silently drop their events because:
```python
handler = _ui_event_handler_ctx.get()  # returns None
if handler is not None:                # skips
```
This would explain why substeps from the first call render but substeps from the second call do not.

### Suggested next steps
1. Add debug logging to `emit_ui_event()` in `utils.py` to confirm whether the handler is None during the second invocation.
2. Check if `agent_graph.ainvoke()` creates a new context that doesn't inherit the handler.
3. Consider making `run_dcf_workflow_sync()` explicitly call `set_ui_event_handler()` before running the valuation graph, so it doesn't depend on the parent context.
4. Alternatively, have `emit_activity()` log a warning when handler is None.
