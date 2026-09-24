# Project North Star

This project is moving from chatbot prototype to case-based finance workflow workspace. DCF remains strongest current method, but valuation is one downstream workflow, not the product spine.

## Achieved

- Custom React/FastAPI UI with chat-first interface, activity trace, source drawer, workspace objects rail, document upload, deck preview, DCF trace, and KG panel.
- Chat workflow with tool access, streaming final answers, document RAG, DCF execution, deck generation, and KG freshness checks.
- DCF workflow with evidence gathering, assumptions, HITL review, scenario runner, adversarial review, market reconciliation, PDF export, and workspace persistence.
- RAG citations are clickable and source drawer hides raw chunk details from users.
- Uploaded documents, DCF runs, and generated decks surface as workspace objects.
- Workflow setup now uses native LangGraph `interrupt()`, not duplicate compiled graphs.
- Thin `WorkflowContext` added for setup scope: workflow id, selected docs/artifacts/KG nodes, focus areas, primary entity, and output requirements.
- Shared domain contracts added for retrievable agent objects: `AgentObject`, `ObjectReference`, `ResearchCase`, `CaseTask`, `TaskResult`, and valuation method specs.
- Main graph now uses semantic routing, local playbook loading, runtime policy, and dynamic chat tool binding before dispatching to existing chat/research/workflow paths.
- Main graph emits execution-step trace events for router, playbook load, runtime policy, route branch, and case placeholder.

## Product Direction

Strategic north star:

1. User creates or resumes a `ResearchCase` / engagement.
2. Case captures mandate, target entity/security, listed/private status, audience, source policy, timeline, and desired artifacts.
3. Orchestrator decomposes case into typed tasks: data, filings/documents, business analysis, market/TAM, macro/industry, valuation, legal/document drafting, presentation, QA.
4. Independent subagents run in parallel where possible and append typed `TaskResult`s to case context.
5. Valuation method registry chooses DCF, FCFE, DDM, residual income, trading comps, precedent transactions, SOTP, NAV, LBO, or VC method based on company type and available evidence.
6. Outputs become durable workspace artifacts: `data_room`, `tam_model`, `valuation_run`, `memo`, `deck`, `prospectus_draft`, `nda_draft`, `comparison`, `risk_register`.
7. Chat stays command layer over cases, tasks, sources, and artifacts.

Current golden path:

1. User starts in chat or launcher.
2. Product detects workflow intent or user chooses workflow card.
3. Setup gate confirms sources, entity, focus, audience, depth, and format.
4. Workflow runs with visible trace and HITL only at meaningful gates.
5. Output becomes durable workspace object.
6. User can keep chatting against saved objects.

## Current Principles

- Chat is command layer, not only output container.
- KG is cache and memory index, not replacement for source-grounded retrieval.
- WorkflowContext must stay thin. Do not duplicate chat history, KG payloads, or DCF assumptions.
- DCF assumptions stay inside DCF workflow and its own assumption-review HITL gate.
- ResearchCase should become root unit of work; workflows, runs, documents, KG nodes, and artifacts attach to it.
- Valuation methods stay pluggable. Do not put DDM, LBO, comps, private-company, or SOTP logic inside `DCFState`.
- Data connectors need provider/license/provenance contracts before more ad hoc API tools.
- Subagents must emit typed append-only `TaskResult`s with evidence refs, artifact refs, confidence, open questions, blocking gaps, and raw payload pointer.
- User-facing UI should say source, page, evidence, run, assumption; avoid chunk, node id, graph jargon.
- Default mode is Chat. Research mode stays disabled until it is reliable.

## Next Work

- Use `agent_project/docs/ROUTING_ESCALATION_POLICY.md` as first routing contract. It separates execution intensity from playbook behavior.
- Use `agent_project/docs/PLAYBOOK_CONTRACT.md` as playbook contract. Runtime access is deterministic registry lookup by `playbook_id`, then selected playbook injection into task prompt; not global system-prompt stuffing.
- Add runtime validators for tool budget, route/playbook compatibility, freshness, citation presence, and object policy.
- Draft Source Acquisition Contract after playbooks: data requirements, source objects, extracted facts, provenance, freshness, license state.
- Draft Case Orchestrator Contract last: task DAG, subagent assignment, parallel scheduling, context delegation, merge/reducer, HITL locks.
- Persist prototype `research_case` and `case_task` objects through `workspace_objects.payload`.
- Wrap existing DCF behind a method registry before adding new methods.
- Add DDM and trading-comps skeletons after method registry exists.
- Audit HEC-accessible data sources for API access, license limits, auth, rate limits, and dataset fit before integration.
- Design orchestrator graph that fans out independent typed tasks and appends results to case context.
- Finish dedicated workflow engines for memo and comparison.
- Make deck workflow use selected workspace objects from setup, not only chat text.
- Add structured workflow input cards per workflow after launcher click.
- Add document-analysis object persistence, not only chat answers.
- Add RAG evals: faithfulness, citation precision, table QA exactness, long-document recall.
- Improve structured ingestion for tables and charts.
- Reduce latency through retrieval planning, cache reuse, and smaller model stages.
