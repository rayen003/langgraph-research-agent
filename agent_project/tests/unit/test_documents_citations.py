"""Uploaded-document RAG citation contract tests."""

from __future__ import annotations

import json


def test_doc_citation_id_and_label_are_stable():
    from documents import _make_doc_citation_id, _make_doc_citation_label

    meta = {
        "doc_id": "doc_123",
        "filename": "AAPL 10-K.pdf",
        "page": 42,
        "chunk_index": 7,
    }

    assert _make_doc_citation_id(meta) == "doc:doc_123:p42:c7"
    assert _make_doc_citation_label(meta) == "AAPL 10-K.pdf p.42"


def test_search_documents_skip_gate_returns_inline_citations(monkeypatch):
    import documents

    documents._session_ctx.set("session_1")
    monkeypatch.setattr(
        documents,
        "list_docs",
        lambda session_id: [
            {
                "status": "ready",
                "filename": "AAPL 10-K.pdf",
                "company": "Apple Inc.",
                "ticker": "AAPL",
                "doc_type": "sec_filing",
                "fiscal_period": "FY2025",
            }
        ],
    )
    monkeypatch.setattr(
        documents,
        "hybrid_search",
        lambda query, session_id: [
            {
                "text": "Net sales increased year over year.",
                "metadata": {
                    "doc_id": "doc_123",
                    "filename": "AAPL 10-K.pdf",
                    "page": 12,
                    "chunk_index": 3,
                    "doc_company": "Apple Inc.",
                    "doc_ticker": "AAPL",
                    "doc_type": "sec_filing",
                    "fiscal_period": "FY2025",
                },
            }
        ],
    )

    raw = documents.search_documents.invoke({"query": "revenue growth", "skip_gate": True})
    payload = json.loads(raw)

    assert payload["status"] == "gate_skipped"
    assert payload["chunks"][0]["citation_id"] == "doc:doc_123:p12:c3"
    assert payload["chunks"][0]["citation_label"] == "AAPL 10-K.pdf p.12"
    assert payload["chunks"][0]["text"] == "Net sales increased year over year."


def test_normalize_table_creates_headers_and_rows():
    import documents

    table = documents._normalize_table(
        [
            ["Metric", "FY24", "FY25", ""],
            ["Revenue", "10", "12", ""],
            ["Margin", "20%", "22%", ""],
        ],
        page=4,
        table_index=1,
        bbox=[1.0, 2.0, 3.0, 4.0],
    )

    assert table == {
        "table_id": "p4_t1",
        "page": 4,
        "table_index": 1,
        "caption": "",
        "headers": ["Metric", "FY24", "FY25"],
        "rows": [["Revenue", "10", "12"], ["Margin", "20%", "22%"]],
        "bbox": [1.0, 2.0, 3.0, 4.0],
        "confidence": 0.8,
    }


def test_normalize_table_rejects_slide_card_grid():
    import documents

    table = documents._normalize_table(
        [
            [
                "7.3% YoY Balance Sheet Growth",
                "8.6% YoY Growth in Financing Portfolio",
                "6.0% Growth in Liabilities",
                "LDR Below Regulatory Cap",
            ],
            [
                "",
                "Net Financing +8.6% 693.4bn 752.8bn FY24 FY25",
                "Total Liabilities +6.0% 849.3bn 900.4bn FY24 FY25",
                "Loan to Deposit Ratio 85.5% 82.8% FY24 FY25",
            ],
            [
                "25.7% higher net income YoY",
                "20.1% growth in net yield income",
                "28.2% higher Non yield income",
                "22.0% higher operating income",
            ],
        ],
        page=16,
        table_index=0,
    )

    assert table is None


def test_structured_sidecar_write_and_load(tmp_path, monkeypatch):
    import documents

    monkeypatch.setattr(documents, "UPLOADS_DIR", tmp_path)
    pages = [
        {
            "page": 1,
            "text": "Revenue table",
            "tables": [
                {
                    "table_id": "p1_t0",
                    "page": 1,
                    "table_index": 0,
                    "headers": ["Metric", "FY24", "FY25"],
                    "rows": [["Revenue", "10", "12"]],
                }
            ],
        }
    ]
    documents._attach_table_ids("doc_abc", pages)
    documents._write_structured_doc("doc_abc", "report.pdf", pages)

    loaded = documents._load_structured_doc("doc_abc")

    assert loaded["doc_id"] == "doc_abc"
    assert loaded["pages"][0]["table_ids"] == ["doc_abc_p1_t0"]
    assert loaded["tables"][0]["table_id"] == "doc_abc_p1_t0"


def test_get_document_citation_returns_chunk_and_neighbors(monkeypatch):
    import documents

    class FakeCollection:
        def get(self, where, include, limit):
            assert where == {"doc_id": "doc_123"}
            assert include == ["documents", "metadatas"]
            assert limit == 2000
            return {
                "documents": ["previous", "target cited text", "next"],
                "metadatas": [
                    {"doc_id": "doc_123", "filename": "ARB.pdf", "page": 2, "chunk_index": 2},
                    {
                        "doc_id": "doc_123",
                        "filename": "ARB.pdf",
                        "page": 3,
                        "chunk_index": 3,
                        "table_ids": "doc_123_p3_t0",
                    },
                    {"doc_id": "doc_123", "filename": "ARB.pdf", "page": 4, "chunk_index": 4},
                ],
            }

    monkeypatch.setattr(documents, "get_doc_status", lambda doc_id: {
        "doc_id": doc_id,
        "filename": "ARB.pdf",
        "company": "Al Rajhi Bank",
        "ticker": "ARB",
    })
    monkeypatch.setattr(documents, "_get_collection", lambda: FakeCollection())
    monkeypatch.setattr(documents, "_load_structured_doc", lambda doc_id: {
        "doc_id": doc_id,
        "filename": "ARB.pdf",
        "tables": [
            {
                "table_id": "doc_123_p3_t0",
                "page": 3,
                "headers": ["Metric", "FY24", "FY25"],
                "rows": [["Revenue", "10", "12"]],
            }
        ],
    })

    citation = documents.get_document_citation("doc:doc_123:p3:c3")

    assert citation["filename"] == "ARB.pdf"
    assert citation["page"] == 3
    assert citation["text"] == "target cited text"
    assert citation["previous_text"] == "previous"
    assert citation["next_text"] == "next"
    assert citation["tables"] == [
        {
            "table_id": "doc_123_p3_t0",
            "page": 3,
            "headers": ["Metric", "FY24", "FY25"],
            "rows": [["Revenue", "10", "12"]],
        }
    ]


def test_research_prompt_describes_inline_chunks_not_fake_fetch_step():
    from graphs.research import STATIC_SYSTEM_PROMPT

    assert "citation_id" in STATIC_SYSTEM_PROMPT
    assert "NO retrieve_tool_result(chunk_id)" in STATIC_SYSTEM_PROMPT
    assert "Fetch chunks with retrieve_tool_result(chunk_id)" not in STATIC_SYSTEM_PROMPT


def test_chat_extracts_document_citations_from_tool_history(monkeypatch):
    from langchain_core.messages import HumanMessage, ToolMessage
    import langgraph.prebuilt as prebuilt

    class DummyToolNode:
        def __init__(self, tools):
            self.tools = tools

    monkeypatch.setattr(prebuilt, "ToolNode", DummyToolNode, raising=False)

    from graphs.conversational import _doc_citations_from_history, _is_doc_focused_request

    history = [
        ToolMessage(
            content=json.dumps({
                "status": "relevant",
                "chunks": [
                    {
                        "citation_id": "doc:doc_123:p12:c3",
                        "citation_label": "AAPL 10-K.pdf p.12",
                        "source": "AAPL 10-K.pdf",
                        "page": 12,
                        "text": "Net sales increased.",
                    }
                ],
            }),
            tool_call_id="call_1",
        )
    ]

    assert _doc_citations_from_history(history) == [
        {
            "citation_id": "doc:doc_123:p12:c3",
            "citation_label": "AAPL 10-K.pdf p.12",
            "source": "AAPL 10-K.pdf",
            "page": 12,
        }
    ]

    forced_history = [
        HumanMessage(content=(
            "search_documents result:\n"
            + json.dumps({
                "status": "gate_skipped",
                "chunks": [
                    {
                        "citation_id": "doc:doc_456:p2:c0",
                        "citation_label": "ARB release.pdf p.2",
                        "source": "ARB release.pdf",
                        "page": 2,
                        "text": "Revenue increased.",
                    }
                ],
            })
            + "\n\nNow answer the user's document request."
        ))
    ]
    assert _doc_citations_from_history(forced_history)[0]["citation_id"] == "doc:doc_456:p2:c0"
    assert _is_doc_focused_request("let's analyse this document") is True
    assert _is_doc_focused_request("what is WACC?") is False
