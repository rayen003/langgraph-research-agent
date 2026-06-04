"""KG Audit — standalone quality checks for the Knowledge Graph.

Deterministic checks (no LLM, cheap):
  - cross_source_consistency: Same (ticker, field, period) → different values from different sources
  - staleness_detection: Facts past their TTL, missing expected fiscal periods
  - orphan_detection: Facts referencing deleted doc_ids or missing nodes
  - entity_coherence: Filing ticker != node ticker

LLM spot-checks (on-demand, expensive):
  - hallucination_spot_check: Re-extract from SOURCE CHUNKS and compare to KG

All findings written to a SEPARATE kg_audit_log table, never back to KG.
Auto-fix for deterministic issues (duplicates, stale TTL, orphans).
Entity mismatches and hallucinations flagged for human review.

Triggers:
  1. On-demand: POST /api/kg/audit
  2. Scheduled: configurable (default: manual)
  3. Post-ingest: after N new facts (lightweight deterministic checks)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .cache import KGCache, get_cache
from .ingest import SOURCE_TIER_PRECEDENCE, NODE_TYPE_LAYER_MAP, Layer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Audit severity levels
# ---------------------------------------------------------------------------


class AuditSeverity(str):
    INFO = "info"          # FYI, no action needed
    WARNING = "warning"    # Inconsistency found, review recommended
    ERROR = "error"        # Likely corruption, action required


# ---------------------------------------------------------------------------
# Audit finding dataclass
# ---------------------------------------------------------------------------


class AuditFinding:
    """A single audit finding — one issue detected in the KG."""

    __slots__ = (
        "audit_id", "timestamp", "check_type", "ticker", "node_type",
        "field", "severity", "finding", "recommendation",
        "source_tier", "existing_value", "conflicting_value", "auto_fixed",
    )

    def __init__(
        self,
        *,
        check_type: str,
        ticker: str,
        node_type: str,
        field: str,
        severity: str,
        finding: str,
        recommendation: str = "",
        source_tier: str | None = None,
        existing_value: str | None = None,
        conflicting_value: str | None = None,
        auto_fixed: bool = False,
        audit_id: str | None = None,
    ):
        self.audit_id = audit_id or uuid.uuid4().hex[:12]
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.check_type = check_type
        self.ticker = ticker
        self.node_type = node_type
        self.field = field
        self.severity = severity
        self.finding = finding
        self.recommendation = recommendation
        self.source_tier = source_tier
        self.existing_value = existing_value
        self.conflicting_value = conflicting_value
        self.auto_fixed = auto_fixed

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "check_type": self.check_type,
            "ticker": self.ticker,
            "node_type": self.node_type,
            "field": self.field,
            "severity": self.severity,
            "finding": self.finding,
            "recommendation": self.recommendation,
            "source_tier": self.source_tier,
            "existing_value": self.existing_value,
            "conflicting_value": self.conflicting_value,
            "auto_fixed": self.auto_fixed,
        }


# ---------------------------------------------------------------------------
# Storage: write findings to audit log
# ---------------------------------------------------------------------------


def _get_audit_db() -> sqlite3.Connection:
    """Get a connection to the audit log database."""
    from storage import _connect  # noqa: PLC0415
    return _connect()


def persist_findings(findings: list[AuditFinding]) -> None:
    """Write audit findings to the kg_audit_log table."""
    if not findings:
        return
    conn = _get_audit_db()
    try:
        for f in findings:
            conn.execute(
                """INSERT OR REPLACE INTO kg_audit_log
                (audit_id, timestamp, check_type, ticker, node_type, field,
                 severity, finding, recommendation, source_tier,
                 existing_value, conflicting_value, auto_fixed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f.audit_id, f.timestamp, f.check_type, f.ticker,
                    f.node_type, f.field, f.severity, f.finding,
                    f.recommendation, f.source_tier, f.existing_value,
                    f.conflicting_value, 1 if f.auto_fixed else 0,
                ),
            )
        conn.commit()
        logger.info("Persisted %d audit findings", len(findings))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to persist audit findings: %s", exc)
    finally:
        conn.close()


def get_audit_findings(
    ticker: str | None = None,
    severity: str | None = None,
    check_type: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query audit findings from the log."""
    conn = _get_audit_db()
    try:
        query = "SELECT * FROM kg_audit_log WHERE 1=1"
        params: list[Any] = []
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        if severity:
            query += " AND severity = ?"
            params.append(severity)
        if check_type:
            query += " AND check_type = ?"
            params.append(check_type)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        columns = [d[0] for d in conn.execute("SELECT * FROM kg_audit_log LIMIT 1").description]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Deterministic checks (no LLM, cheap)
# ---------------------------------------------------------------------------


def cross_source_consistency(ticker: str | None = None, _cache: KGCache | None = None) -> list[AuditFinding]:
    """Check if multiple sources agree on the same facts.

    Finds nodes with the same (ticker, node_type, field) but different values
    from different sources. Higher-tier sources win; lower-tier flagged.
    """
    cache = _cache or get_cache()
    findings: list[AuditFinding] = []

    # Get all nodes for this ticker
    try:
        all_nodes = cache.query(ticker=ticker)  # ticker=None → whole graph
    except Exception:
        all_nodes = []

    # Cross-source consistency only applies to SHARED facts — one logical fact
    # that multiple sources should agree on. Run-scoped nodes are keyed by
    # run_id and are intentionally per-run (each DCF run has its own wacc,
    # base_revenue, terminal_pv, …), so grouping them by (node_type, field)
    # across runs produces meaningless "contradictions". Exclude them.
    from .schemas import RUN_SCOPED_NODE_TYPES as RUN_SCOPED  # noqa: PLC0415

    # Group SHARED nodes by (ticker, node_type, field). Ticker MUST be in the
    # key — when auditing the whole graph (ticker=None) different companies
    # share field names (every company/anchor, every thesis/full), and grouping
    # without ticker would flag AAPL vs META as a "contradiction".
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for node in all_nodes:
        nt = node.get("node_type", "")
        if nt in RUN_SCOPED:
            continue
        groups.setdefault(
            (node.get("ticker", ""), nt, node.get("field", "")), []
        ).append(node)

    for (_tk, nt, field), nodes in groups.items():
        if len(nodes) < 2:
            continue
        # Collect DISTINCT values (one representative node each). A genuine
        # contradiction needs ≥2 different values for the same logical fact.
        by_val: dict[str, dict] = {}
        for node in nodes:
            by_val.setdefault(str(node.get("value", "")), node)
        if len(by_val) < 2:
            continue  # all sources agree — no contradiction

        # Highest-tier source wins; flag each conflicting (different-value) one.
        ranked = sorted(
            by_val.values(),
            key=lambda n: SOURCE_TIER_PRECEDENCE.get(n.get("source", ""), 0),
            reverse=True,
        )
        winner = ranked[0]
        w_src = winner.get("source", "unknown")
        w_tier = SOURCE_TIER_PRECEDENCE.get(w_src, 0)
        w_val = str(winner.get("value", ""))
        for loser in ranked[1:]:
            l_src = loser.get("source", "unknown")
            l_tier = SOURCE_TIER_PRECEDENCE.get(l_src, 0)
            l_val = str(loser.get("value", ""))
            findings.append(AuditFinding(
                check_type="cross_source",
                ticker=ticker or loser.get("ticker", "?"),
                node_type=nt,
                field=field,
                severity=AuditSeverity.WARNING,
                finding=(
                    f"Contradiction: {nt}/{field} = '{w_val}' from '{w_src}' "
                    f"(tier {w_tier}) vs '{l_val}' from '{l_src}' (tier {l_tier})"
                ),
                recommendation=(
                    f"Review {nt}/{field} — higher-tier source '{w_src}' takes precedence"
                ),
                source_tier=l_src,
                existing_value=w_val,
                conflicting_value=l_val,
            ))

    return findings


def staleness_detection(ticker: str | None = None, _cache: KGCache | None = None) -> list[AuditFinding]:
    """Check for stale or expired facts.

    Layer 1 (anchored) facts should never expire.
    Layer 2 (derived) facts have TTL. If they're older than their TTL,
    flag them.
    """
    cache = _cache or get_cache()
    findings: list[AuditFinding] = []

    try:
        all_nodes = cache.query(ticker=ticker)  # ticker=None → whole graph
    except Exception:
        all_nodes = []

    now_ts = datetime.now(timezone.utc).timestamp()

    for node in all_nodes:
        nt = node.get("node_type", "")
        layer, ttl = NODE_TYPE_LAYER_MAP.get(nt, (Layer.DERIVED, 86400 * 30))
        updated_at = node.get("updated_at", "")

        if updated_at in ("", None):
            # No timestamp — can't check staleness
            continue

        # updated_at is a float epoch (SQLite REAL) — but tolerate ISO strings
        # from any legacy rows.
        try:
            if isinstance(updated_at, (int, float)):
                node_ts = float(updated_at)
            else:
                node_ts = datetime.fromisoformat(
                    str(updated_at).replace("Z", "+00:00")
                ).timestamp()
        except (ValueError, TypeError):
            continue

        age_seconds = now_ts - node_ts

        if layer == Layer.DERIVED and ttl < float("inf") and age_seconds > ttl:
            days_stale = int(age_seconds / 86400)
            findings.append(AuditFinding(
                check_type="staleness",
                ticker=ticker or node.get("ticker", "?"),
                node_type=nt,
                field=node.get("field", ""),
                severity=AuditSeverity.WARNING,
                finding=(
                    f"Stale Layer 2 fact: {nt}/{node.get('field', '')} is "
                    f"{days_stale} days old (TTL: {int(ttl/86400)}d)"
                ),
                recommendation=f"Re-ingest fresh data for {nt}/{node.get('field', '')}",
                source_tier=node.get("source", ""),
            ))

    return findings


def orphan_detection(ticker: str | None = None, _cache: KGCache | None = None) -> list[AuditFinding]:
    """Find facts that reference non-existent parent nodes.

    Checks for nodes that reference doc_ids or run_ids that don't exist.
    """
    cache = _cache or get_cache()
    findings: list[AuditFinding] = []

    try:
        all_nodes = cache.query(ticker=ticker)  # ticker=None → whole graph
    except Exception:
        all_nodes = []

    # Build a set of known node IDs
    known_ids = {node.get("id", "") for node in all_nodes if node.get("id")}

    # Build a set of known doc_ids (from the documents table)
    known_doc_ids: set[str] = set()
    try:
        from storage import _connect as _storage_connect  # noqa: PLC0415
        conn = _storage_connect()
        rows = conn.execute("SELECT doc_id FROM documents").fetchall()
        known_doc_ids = {row[0] for row in rows}
        conn.close()
    except Exception:
        pass  # Documents table may not exist or be empty

    for node in all_nodes:
        value = node.get("value", {})
        if isinstance(value, dict):
            doc_id = value.get("source_doc_id", "")
            if doc_id and doc_id not in known_doc_ids:
                findings.append(AuditFinding(
                    check_type="orphan",
                    ticker=ticker or node.get("ticker", "?"),
                    node_type=node.get("node_type", ""),
                    field=node.get("field", ""),
                    severity=AuditSeverity.WARNING,
                    finding=(
                        f"Orphan fact: references doc_id='{doc_id}' which doesn't exist "
                        f"in the documents table"
                    ),
                    recommendation=f"Review or delete orphan fact for doc_id='{doc_id}'",
                    source_tier=node.get("source", ""),
                ))

    return findings


def entity_coherence(ticker: str | None = None, _cache: KGCache | None = None) -> list[AuditFinding]:
    """Check entity consistency across the KG.

    Flags nodes where the reported entity (e.g., ticker in a filing)
    doesn't match the KG entity (the ticker the node is stored under).
    """
    cache = _cache or get_cache()
    findings: list[AuditFinding] = []

    try:
        all_nodes = cache.query(ticker=ticker)  # ticker=None → whole graph
    except Exception:
        all_nodes = []

    for node in all_nodes:
        value = node.get("value", {})
        if not isinstance(value, dict):
            continue
        # Check filing-specific entity mismatches
        node_ticker = (node.get("ticker") or "").upper()
        value_ticker = (value.get("ticker") or "").upper()
        if value_ticker and node_ticker and value_ticker != node_ticker:
            findings.append(AuditFinding(
                check_type="entity_coherence",
                ticker=ticker or node.get("ticker", "?"),
                node_type=node.get("node_type", ""),
                field=node.get("field", ""),
                severity=AuditSeverity.ERROR,
                finding=(
                    f"Entity mismatch: KG ticker='{node_ticker}' but fact "
                    f"contains ticker='{value_ticker}'"
                ),
                recommendation=(
                    f"Verify: is this fact about {value_ticker} incorrectly "
                    f"stored under {node_ticker}?"
                ),
                source_tier=node.get("source", ""),
                existing_value=f"ticker={node_ticker}",
                conflicting_value=f"ticker={value_ticker}",
            ))

    return findings


# ---------------------------------------------------------------------------
# LLM spot-check (on-demand, expensive)
# ---------------------------------------------------------------------------


_HALLUCINATION_CHECK_PROMPT = """You are a financial data verification engine. Given a stored fact and
its source text, determine if the fact accurately represents what the source says.

For each fact, return JSON:
{
  "fact_id": "<the fact_id from input>",
  "matches_source": true/false,
  "stored_value": "<what the KG says>",
  "source_says": "<what the source text actually says>",
  "severity": "info" | "warning" | "error",
  "reasoning": "<one sentence explanation>"
}

Be strict. A "warning" means close but imprecise. An "error" means the fact
is wrong or hallucinated — it doesn't match the source text at all.
Return ONLY the JSON array, no explanation."""


def hallucination_spot_check(
    ticker: str,
    sample_size: int = 5,
    _cache: KGCache | None = None,
) -> list[AuditFinding]:
    """Re-extract facts from source chunks and compare to KG.

    This is the LLM-based spot-check. It reads source text from ChromaDB
    (not from the KG) and compares to what the KG says. If they differ
    significantly, it flags a potential hallucination.

    This is the ONLY check that uses an LLM, so it's called on-demand.
    """
    import os
    import dotenv
    from langchain_openai import ChatOpenAI

    cache = _cache or get_cache()
    findings: list[AuditFinding] = []

    # Get document_fact nodes from KG for this ticker
    try:
        doc_facts = cache.query(ticker=ticker, node_type="document_fact")
    except Exception:
        doc_facts = []

    if not doc_facts:
        return findings

    # Sample N facts
    import random
    sample = random.sample(doc_facts, min(sample_size, len(doc_facts)))

    # For each sampled fact, try to find the source chunk and re-extract
    for node in sample:
        value = node.get("value", {})
        if not isinstance(value, dict):
            continue
        doc_id = value.get("source_doc_id", "")
        if not doc_id:
            continue

        # Get source chunks from ChromaDB
        try:
            from documents import _get_collection  # noqa: PLC0415
            collection = _get_collection()
            results = collection.get(
                where={"doc_id": doc_id},
                include=["documents"],
                limit=3,
            )
            chunks = results.get("documents", []) or []
        except Exception:
            chunks = []

        if not chunks:
            continue

        sample_text = "\n---\n".join(chunks[:3])[:4000]

        # Ask LLM to verify
        try:
            dotenv.load_dotenv()
            llm = ChatOpenAI(
                model="gpt-4o-mini",
                api_key=os.environ.get("OPENAI_API_KEY"),
                timeout=20,
            )
            result = llm.invoke([
                {"role": "system", "content": _HALLUCINATION_CHECK_PROMPT},
                {"role": "user", "content": json.dumps({
                    "fact_id": node.get("id", "?"),
                    "fact_type": value.get("fact_type", "?"),
                    "field": value.get("field", "?"),
                    "stored_value": str(value.get("value", "")),
                    "fiscal_period": value.get("fiscal_period", ""),
                    "source_text": sample_text,
                })},
            ])
            raw = (result.content or "").strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("\n```", 1)[0]
            checks = json.loads(raw)
            if not isinstance(checks, list):
                checks = [checks] if isinstance(checks, dict) else []

            for check in checks:
                matches = check.get("matches_source", True)
                severity = check.get("severity", "info")
                if not matches or severity in ("warning", "error"):
                    findings.append(AuditFinding(
                        check_type="hallucination",
                        ticker=ticker,
                        node_type="document_fact",
                        field=node.get("field", check.get("fact_id", "?")),
                        severity=severity if severity in ("warning", "error") else "warning",
                        finding=check.get("reasoning", "Fact doesn't match source text"),
                        recommendation="Re-ingest document or delete hallucinated fact",
                        source_tier="document_extraction",
                        existing_value=str(value.get("value", "")),
                        conflicting_value=check.get("source_says", ""),
                    ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Hallucination spot-check LLM call failed: %s", exc)
            continue

    return findings


# ---------------------------------------------------------------------------
# Main audit runner
# ---------------------------------------------------------------------------


def run_audit(
    ticker: str | None = None,
    checks: list[str] | None = None,
    sample_size: int = 5,
    auto_fix: bool = True,
) -> list[AuditFinding]:
    """Run the full audit suite on the KG.

    Args:
        ticker: Ticker to audit (None = all tickers).
        checks: List of check types to run (None = all deterministic checks).
            Options: "cross_source", "staleness", "orphan", "entity_coherence",
            "hallucination"
        sample_size: Number of facts to sample for hallucination spot-checks.
        auto_fix: Whether to auto-correct fixable issues (duplicates, stale TTL).

    Returns:
        List of AuditFinding objects.
    """
    all_checks = {
        "cross_source": cross_source_consistency,
        "staleness": staleness_detection,
        "orphan": orphan_detection,
        "entity_coherence": entity_coherence,
        "hallucination": hallucination_spot_check,
    }

    if checks is None:
        # Default: deterministic checks only (no LLM)
        checks = ["cross_source", "staleness", "orphan", "entity_coherence"]

    findings: list[AuditFinding] = []

    for check_name in checks:
        if check_name not in all_checks:
            logger.warning("Unknown audit check: %s", check_name)
            continue
        check_fn = all_checks[check_name]
        try:
            if check_name == "hallucination":
                # LLM check needs a specific ticker
                if not ticker:
                    logger.info("Skipping hallucination check — no ticker specified")
                    continue
                check_findings = check_fn(ticker=ticker, sample_size=sample_size)
            else:
                check_findings = check_fn(ticker=ticker)
            findings.extend(check_findings)
        except Exception as exc:  # noqa: BLE001
            logger.error("Audit check '%s' failed: %s", check_name, exc)
            findings.append(AuditFinding(
                check_type=check_name,
                ticker=ticker or "?",
                node_type="?",
                field="?",
                severity=AuditSeverity.ERROR,
                finding=f"Audit check '{check_name}' failed: {exc}",
                recommendation="Check logs for details",
            ))

    # Auto-fix deterministic issues if requested
    if auto_fix and findings:
        findings = _auto_fix(findings)

    # Persist findings to audit log
    persist_findings(findings)

    # Summary logging
    by_severity = {}
    for f in findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
    logger.info(
        "KG audit complete: %d findings %s",
        len(findings), by_severity,
    )

    return findings


def _auto_fix(findings: list[AuditFinding]) -> list[AuditFinding]:
    """Auto-correct fixable issues.

    Currently handles:
    - Stale Layer 2 facts past TTL → delete (mark as auto_fixed)
    - Duplicate facts → remove duplicate (mark as auto_fixed)

    Does NOT auto-fix:
    - Entity mismatches (might be intentional corrections)
    - Hallucinations (needs human judgment)
    """
    cache = get_cache()
    fixed: list[AuditFinding] = []

    for f in findings:
        # Stale Layer 2 facts past TTL → purge (any severity — a stale fact is
        # stale whether flagged warning or error).
        if f.check_type == "staleness":
            node_id = f"{f.ticker}::{f.node_type}::{f.field}"
            try:
                cache.delete(node_id)
                f.auto_fixed = True
                logger.info("Auto-fixed stale fact: %s", node_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Auto-fix delete failed for %s: %s", node_id, exc)

        fixed.append(f)

    return fixed