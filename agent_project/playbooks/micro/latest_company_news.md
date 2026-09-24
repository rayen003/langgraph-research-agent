---
id: latest_company_news
kind: micro
version: 0.1
title: Latest Company News
summary: Fast current-news lookup for a public company.
route_levels:
  - tool
applies_when:
  - user asks for latest, current, or recent company news
  - answer can fit in concise bullets
reject_when:
  - user asks for full research case
  - user asks for valuation, memo, deck, or multi-step diligence
allowed_tools:
  - query_knowledge_graph
  - search_web
  - retrieve_tool_result
allowed_workflows: []
tool_budget:
  max_calls: 2
  target_latency_s: 8
object_policy:
  creates_objects: true
  default_object_type: news_digest
required_inputs:
  - company_or_ticker
required_outputs:
  schema: short_answer
  citations_required: true
validators:
  - route_level_allowed
  - tool_budget
  - freshness
  - citation_presence
stop_conditions:
  - enough current sources found
  - max tool calls reached
fallback:
  on_no_sources: say no reliable current sources found and state query date
---

# Latest Company News

## Objective

Answer recent company-news requests quickly with dated source context.

## Procedure

1. Use `query_knowledge_graph` only as cache or hint.
2. If request says latest, current, today, this week, or recent, use `search_web` unless KG explicitly has fresh news.
3. Prefer 3-5 bullets. Group duplicate stories.
4. Mention time window and source dates when available.

## Source Policy

Accept current reputable news, company releases, filings, exchange notices, or market-data providers.

Do not treat stale KG memory as source of truth for current news.

## Output Shape

- one-sentence lead
- 3-5 bullets
- source/date notes where available
- short caveat if sources conflict or no current sources found

## Guardrails

- Do not start a workflow.
- Do not create a case.
- Do not produce valuation conclusions from news alone.
- Do not exceed tool budget unless router escalates.

## Escalation

Ask to switch to workflow or case only if user requests deeper analysis, valuation impact, memo, deck, or ongoing research case.
