# Analysis: Document-vs-Query Entity Mismatch Bug

## The Incident

User uploaded **Meta's** earnings PDF (`Earnings-Presentation-Q1-2026.pdf`) and asked to "analyse the earning call from **apple**". The agent ran web search for Apple earnings and completely ignored the uploaded Meta document. A correct response would have been: *"The uploaded document appears to be Meta's Q1 2026 earnings, but you asked about Apple. Which company would you like me to analyze?"*

## Verdict: **Structural issue with a prompting exacerbation**

Both layers contribute, but the root cause is structural — prompting alone cannot fix this without adding guard rails.

---

## Root Cause Breakdown

### Layer 1 — Plan-time blindness (structural)

The `plan_node` in `research.py` generates the execution plan **without any awareness of uploaded documents**.

```python
# research.py line ~530 — plan_node prompt
planner.invoke([
    HumanMessage(content=(
        "Create a concise exec plan (3-6 steps) for this task. "
        "Important constraints:\n"
        "- Data retrieval: use search_web for news/articles/text. "   # ← ONLY mentions web
        "For bulk structured data..., use execute_python...\n"
        # ... NO mention of search_documents or uploaded files
        "Task:\n" + str(query)
    ))
])
```

The planner has:
- The user's query string ("analyse the earning call from apple")
- Session memory (prior research summaries)
- **Zero knowledge** of what documents are uploaded

So the plan it produces is inherently web-search-oriented. Steps like "Search for Apple's Q1 2026 earnings call transcript" prime the executing LLM to reach for `search_web`, not `search_documents`.

**What's missing:** Before `plan_node` runs, the system should:
1. Query `list_docs(session_id)` to inventory uploaded documents
2. Extract document filenames (e.g., `Earnings-Presentation-Q1-2026.pdf`)
3. Inject document context into the plan prompt: "The user has uploaded: Earnings-Presentation-Q1-2026.pdf"

### Layer 2 — No entity extraction at ingestion (structural)

`documents.py` chunks and embeds documents but performs **zero document-level entity extraction**:

| What's stored | What's NOT stored |
|---|---|
| Filename | Company name mentioned |
| Page number | Ticker symbol |
| Chunk index | Document type (earnings call, 10-K, research report) |
| Session ID | Quarter/year covered |
| Text content | Subject entity |

```python
# documents.py — _chunk_pages() stores only page/chunk metadata
# No LLM call to extract: "this is Meta's Q1 2026 earnings call"
# No structured metadata like {ticker: "META", company: "Meta Platforms", ...}
```

**What's missing:** During ingestion, a cheap LLM call (or even filename heuristic) should extract:
- `document_subject: "Meta Platforms, Inc."`  
- `document_ticker: "META"`
- `document_type: "earnings_call"`
- `fiscal_period: "Q1 2026"`

This metadata would enable:
- Cross-checking the user's query subject against document subjects
- Filtering `search_documents` results by company relevance
- A pre-execution guard that flags mismatches

### Layer 3 — No pre-execution entity cross-check (structural)

The current flow never compares what the user asked about against what the uploaded documents contain. There's no step between "plan approved" and "execute step 1" that asks:

> *User asked about: Apple. Uploaded documents are about: Meta. Should we proceed with web search?*

**What's missing:** A `validate_document_relevance` node (or check inside `execute_one_step_node`) that:
1. Extracts the primary entity from the user's query (simple LLM call: "what company/ticker is this question about?")
2. Compares against document metadata extracted at ingestion
3. If mismatch detected → interrupt with a clarification question, not a silent web search

### Layer 4 — Prompt actively discourages flagging (prompting)

The `STATIC_SYSTEM_PROMPT` (research) and `_CHAT_SYSTEM` (chat) both contain directives that prevent the agent from flagging this:

```
"CLOSED LOOP. No additional user input arrives during execution."
"You never break character, never ask for clarification, and never offer optional follow-ups."
```

Even if the agent called `search_documents("Apple earnings")`, got back Meta's PDF (because BM25/embeddings match "earnings"), and **noticed** the document is about Meta not Apple — these directives tell it to continue silently.

The `_CHAT_SYSTEM` prompt is slightly better for chat mode (it allows natural conversation), but still has no instruction like:
```
"If a retrieved document appears to be about a different company than the user asked
about, tell the user: 'The uploaded document appears to be [company A], but you asked
about [company B]. Which should I analyze?'"
```

**What's missing:** Add an explicit escalation rule to both prompts:
```
"Document-query mismatch: If search_documents returns content about a different company
than what the user asked, IMMEDIATELY tell the user about the mismatch and ask which
company they want analyzed. Do NOT silently fall back to search_web."
```

### Layer 5 — `search_documents` returns chunks without document-level context (structural)

```python
# documents.py — hybrid_search returns individual chunks
def hybrid_search(query, session_id, n_results=8):
    # Returns: [{content: "...", metadata: {page: 3, chunk_index: 5}}, ...]
```

Each result is a text chunk. The LLM sees: "Revenue increased 21% YoY..." without the document-level context "This is from Meta's Q1 2026 earnings call." The LLM would need to read deeply to realize the company is wrong, and by then it may already be deep in tool calls.

**What's missing:** Each `search_documents` result should carry `document_filename` and ideally `document_subject` metadata in the tool output itself, so the LLM has a chance to notice the mismatch. Currently the tool output is a fixed-format string summarizing matches — the filename might appear, but the *subject entity* definitely doesn't.

---

## What Would Fix This (in priority order)

### Fix 1: Plan-time document inventory (structural, ~30 min)

Inject uploaded document filenames into `plan_node`'s prompt:

```python
# In plan_node, before calling planner:
docs = list_docs(session_id)
doc_context = ""
if docs:
    filenames = [d["filename"] for d in docs if d.get("status") == "ready"]
    if filenames:
        doc_context = (
            "\n\nUploaded documents available (search with search_documents):\n" +
            "\n".join(f"- {f}" for f in filenames) + "\n"
        )

planner.invoke([HumanMessage(content=(
    "Create a concise exec plan..."
    f"{doc_context}"  # ← injected
    "Task:\n" + str(query)
))])
```

This alone would make the planner produce steps like "Search uploaded documents (Earnings-Presentation-Q1-2026.pdf) for Apple earnings data" — still imperfect, but at least the document is visible.

### Fix 2: Document entity extraction at ingestion (structural, ~2h)

Add a lightweight metadata extraction step during `ingest_document`:

```python
# In documents.py, after _parse_file or _index_file_into_chroma:
def _extract_document_entities(filename, first_n_chunks):
    """Cheap LLM call to identify: company, ticker, document type, period."""
    # Or use filename heuristics: "Earnings-Presentation-Q1-2026.pdf"
    # → match against known ticker patterns, company name dictionaries
    pass
```

Store in `storage.py` document table: `company_name`, `ticker`, `doc_type`, `fiscal_period`.

### Fix 3: Entity cross-check guard (structural, ~1h)

Before executing step 1, compare query entity against document entities:

```python
# In execute_one_step_node, after plan approval:
query_entity = _extract_query_entity(state["objective"])  # "Apple" → "AAPL"
doc_entities = _get_document_entities(state.get("session_id"))  # [{ticker: "META"}]
if doc_entities and query_entity not in doc_entities:
    # Interrupt with clarification
    interrupt({
        "type": "entity_mismatch",
        "query_company": query_entity,
        "document_companies": doc_entities,
        "message": f"You asked about {query_entity} but uploaded docs are about {doc_entities}."
    })
```

### Fix 4: Prompt amendment (prompting, ~5 min)

Add to `STATIC_SYSTEM_PROMPT` and `_CHAT_SYSTEM`:

```
"- Document-query mismatch: If you retrieve an uploaded document and discover it is 
   about a DIFFERENT company than the user asked about, STOP and tell the user: 
   'The uploaded document [filename] is about [company from doc], but you asked 
   about [user's company]. Which should I analyze?' 
   Do NOT silently fall back to search_web."
```

### Fix 5: Document context in tool output (structural, ~30 min)

Modify `search_documents` / `hybrid_search` output to include `document_filename` and `document_subject` per result:

```python
# Current output format (simplified):
# "Found 3 results: [chunk 1 text], [chunk 2 text]..."

# Proposed:
# "Found 3 results from 'Earnings-Presentation-Q1-2026.pdf' (Meta Platforms):
#  [chunk 1 text], [chunk 2 text]..."
```

---

## Summary

| Layer | Type | Gap | Fix effort |
|-------|------|-----|------------|
| Plan generation | Structural | No document inventory in plan prompt | ~30 min |
| Document ingestion | Structural | No entity extraction from documents | ~2h |
| Execution guard | Structural | No query-vs-document cross-check | ~1h |
| Agent prompt | Prompting | No mismatch-escalation instruction | ~5 min |
| Tool output format | Structural | No document-subject in search results | ~30 min |

**Bottom line:** This is ~80% structural, ~20% prompting. The system is designed for a happy path where the user uploads relevant documents. There's no defensive layer that asks "does this document match what the user asked about?" Fixes 1 and 4 are quick wins (under 1h combined). Fixes 2+3 are the proper structural solution.
