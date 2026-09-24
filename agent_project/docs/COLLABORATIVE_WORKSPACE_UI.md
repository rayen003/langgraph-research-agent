# Collaborative Workspace UI

## Goal

Turn current single-user agent workbench into shared finance workspace where humans
and agents coordinate through conversation, cases, and versioned objects. UI must
show who did what, why work ran, what evidence supported it, and which output version
resulted.

## Product Boundaries

- Channels coordinate people and agents. They are not durable artifact storage.
- Cases own goals, task DAGs, assignments, runs, and deliverables.
- Objects own durable content, versions, lineage, evidence, review, and comments.
- Runs own routing, playbook, tool calls, delegation, timing, and errors.
- Notifications surface attention requests: mentions, assignments, review requests,
  approvals, failures, and completed background work.
- Knowledge graph supports retrieval and audit. It is not primary navigation.

Every message, comment, task, action, and object version uses same actor identity:
`human`, `agent`, `workflow`, or `system`.

## Information Architecture

```text
Workspace
|
+-- Inbox
|   +-- mentions
|   +-- review requests
|   +-- assignments
|   +-- completed/failed agent work
|
+-- Channels
|   +-- public/private team channels
|   +-- direct messages
|   +-- human-human conversation
|   +-- human-agent conversation and assignments
|
+-- Cases
|   +-- mandate and scope
|   +-- task DAG
|   +-- participants and assigned agents
|   +-- sources and evidence
|   +-- deliverables
|
+-- Objects
|   +-- documents, research, DCFs, memos, decks, comparisons
|   +-- versions, lineage, evidence, activity, comments
|
+-- Agents
    +-- role, capabilities, availability, current tasks, run history
```

## Primary Shell

### Left navigation

Stable workspace navigation:

- Inbox with unread count.
- Channels and direct messages.
- Cases with active/background status.
- Objects grouped by type and recency.
- Agents with availability and active assignment indicator.

Current chat-session list becomes channel/case-aware conversation history. Current
object rail becomes Objects view inside workspace navigation rather than permanent
second sidebar.

### Center workspace

Center changes by selected context:

- Channel: message timeline and composer.
- Case: case summary, task DAG, deliverables, and case conversation.
- Object: editable/read-only object content with inline collaboration.
- Run: live progress and partial results.

Composer supports `@human`, `@agent`, `#channel`, and object/case attachments. Mention
picker shows actor type and agent capability. Agent mention does not automatically
start heavy work without assignment intent or explicit run action.

### Right inspector

Contextual inspector, never separate product silo:

- Object: Overview, Lineage, Versions, Evidence, Activity, Comments.
- Case: Tasks, Runs, Sources, People, Activity.
- Run: Route, Plan, Delegations, Tool calls, Memory, Outputs, Errors.
- Actor: Profile, Role, Permissions, Assignments, Activity.

## Interaction Model

### Human-human

- Message in channels or DMs.
- Mention collaborator.
- Attach exact object version or case.
- Comment on object or exact block/section.
- Suggest edits without overwriting current version.
- Assign task and request review.
- Resolve thread, accept/reject suggestion, approve/reject version.

### Human-agent

- Mention agent for question, scoped task, review, or workflow.
- Agent receives only delegated context: prompt, case/object IDs, selected versions,
  evidence policy, permissions, budget, and output contract.
- Agent posts status and partial results into originating thread.
- Agent output creates object/version or proposed change, never silently overwrites.
- Human can pause, redirect, edit assumptions, approve, reject, or reassign.
- Agent can mention human when blocked or when approval is required.

### Agent-agent

- Orchestrator creates typed tasks and dependencies.
- Independent tasks run in parallel.
- Results return as `TaskResult` references, not copied full histories.
- Merge creates new version with lineage to all consumed results.
- Conflicts become explicit review items; last writer never wins silently.

## Visible Traceability

### Object inspector

Every object exposes:

- stable object ID and selected immutable version;
- lifecycle, review status, creator, updater, case, and thread;
- version history and change actor;
- upstream/downstream object relations;
- evidence/source references;
- immutable action history;
- comments and unresolved review threads.

### Turn and run trace

Normal answer shows compact status line. Expandable trace shows:

```text
User message
  -> semantic route and confidence
  -> loaded playbook and policy
  -> memory/object retrieval
  -> tool calls or workflow selection
  -> delegated tasks and dependencies
  -> HITL interrupts and decisions
  -> merged result
  -> created/updated object versions
  -> final answer
```

Internal graph node names remain diagnostic-only. Default labels use analyst language.

### Case DAG

Case view shows task state, owner, dependencies, blockers, inputs, outputs, duration,
and review state. Small requests never render empty DAG ceremony. DAG appears only
when orchestration created multiple tasks or durable case work exists.

## Routing and Latency UX

| Route | UI treatment |
|---|---|
| Direct answer | Immediate response; route hidden unless trace expanded |
| Tool call | Compact source/tool status; no case or DAG created by default |
| Small task | Short checklist and optional saved object |
| Workflow | Progress, review gates, resumable background run, output object |
| Case | Task DAG, parallel assignments, notifications, multiple output objects |

User can choose Auto, quick answer, or named workflow. Semantic router remains default.
UI choice constrains router; it does not replace routing policy.

## Permission Model

Permissions apply at workspace, channel, case, object, and tool/data-source levels.
Minimum roles:

- Owner: workspace administration and all grants.
- Editor: create/edit objects and run permitted agents.
- Reviewer: comment, suggest, approve, or reject.
- Viewer: read permitted content.
- Agent: scoped service identity with explicit capabilities and source/tool grants.

Agent cannot inherit unrestricted access from mentioning user. Delegation computes
intersection of user permission, agent permission, case scope, and tool policy.

## Realtime Model

- Server-sent run events remain for agent execution.
- WebSocket or equivalent channel handles messages, comments, presence, typing, and
  notifications.
- Durable database event is source of truth; realtime payload only announces change.
- Optimistic UI allowed for messages/comments, reconciled by server IDs.
- Presence is ephemeral and never part of object history.

## Delivery Slices

### Tracked assignments: first implementation

Tracked tasks reuse `collaboration_assignments`; they do not introduce a second
execution graph. Existing actionable agent mentions create an assignment linked to
the initiating channel message and main-agent thread. The original request stays
unchanged; task metadata is included in the pre-routing turn context.

```text
Channel message -> assignment -> existing main graph
                                    |
                                    +-> tools / workflows
                                    +-> existing run events -> Task view
                                    +-> versioned result -> channel reply
```

Assignments retain input version IDs, output version IDs, channel/message/thread
links, status, and errors. Tasks appear in workspace navigation and beneath their
initiating channel message. Task view reuses `useAgentRun`, `ActivityTrace`, and
existing workflow review components; object links open the existing inspector.

Current scope is a first integration slice, not the complete collaboration lifecycle.
Task creation from arbitrary chat messages, revision runs, reassignment, durable
task action history, exact-version resolution outside the loaded object list,
and task-aware restart recovery remain outstanding. Existing mention-action
classification also remains a limitation: it still uses frontend keyword matching.
The task status selector records status only; it does not cancel graph execution.


1. Object inspector with versions, lineage, evidence, actions, and version comments.
2. Workspace actors, memberships, channels, messages, mentions, and inbox APIs.
3. Workspace navigation for Inbox, Channels, Cases, Objects, and Agents.
4. Actor-aware composer with mention picker and object/version attachments.
5. Agent mention dispatch into orchestrator with scoped context and visible assignment.
6. Object comment threads, suggestions, approvals, and review status.
7. Case task DAG and run/delegation inspector.
8. Realtime notifications, presence, permission enforcement, and multi-user tests.

## Acceptance Rules

- User can identify human/agent author for every meaningful action.
- Comment or citation points to exact object version and optional block.
- Agent work shows route, inputs, assignment, progress, outputs, and approval state.
- Human edits create new version; old versions remain accessible.
- Memo/deck can exist without DCF; task graph derives from request, not fixed chain.
- One-tool query stays fast and does not create case/DAG unless user asks to save it.
- Background work remains visible and resumable after navigation or refresh.
- Permission checks occur server-side for every read, write, tool, and delegation.
