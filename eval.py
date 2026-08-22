"""Evaluate hybrid retrieval and confidence-based escalation."""

import app
from unittest.mock import patch

CASES = [
    ("I need to reset my password", "answer", "faq-001"),
    ("How do I reset my password?", "answer", "faq-001"),
    ("I forgot my login password", "answer", "faq-001"),
    ("How can I change my billing address?", "answer", "faq-002"),
    ("Where do I update the address on my invoices?", "answer", "faq-002"),
    ("How do I turn on two factor authentication?", "answer", "faq-003"),
    ("How can I download a copy of my data?", "answer", "faq-004"),
    ("I want to cancel my subscription", "answer", "faq-005"),
    ("What steps should I follow to change a billing address?", "answer", "process-001"),
    ("What should I do if the password reset link fails?", "answer", "process-002"),
    ("How should support handle a billing dispute?", "answer", "process-003"),
    ("My invoice still has the old billing address", "answer", "ticket-001"),
    ("The customer cannot receive the password reset email", "answer", "ticket-002"),
    ("What is the associate billing settings procedure?", "answer", "manual-001"),
    ("When should an associate escalate a request?", "answer", "manual-002"),
    ("Can the product integrate with Salesforce?", "escalate", None),
    ("Does the product have a dark mode?", "escalate", None),
    ("Where is my physical order delivery?", "escalate", None),
    ("Can I use the product with my game console?", "escalate", None),
    ("Do you offer student pricing?", "escalate", None),
    ("Can I speak with someone about a feature request?", "escalate", None),
]


def test_bm25_provider_fallback():
    """A provider outage must not suppress a strong local FAQ match."""
    app.nlp._rag_cache = {}
    with patch.object(app.nlp, "_build_vector_store", side_effect=RuntimeError("provider unavailable")):
        result = app.nlp.answer_from_knowledge_base("I need to reset my password")
    sources = result["trace"].get("sources", [])
    assert result["decision"] == "answer", result
    assert sources and sources[0]["id"] == "faq-001", result
    assert result["trace"]["retrieval"] == "bm25_fallback", result
    print("PASS BM25 fallback answers faq-001 during provider failure")


def test_bm25_fallback_rejects_irrelevant_query():
    """Local fallback must not answer when there is no lexical evidence."""
    app.nlp._rag_cache = {}
    with patch.object(app.nlp, "_build_vector_store", side_effect=RuntimeError("provider unavailable")):
        result = app.nlp.answer_from_knowledge_base("Can the product integrate with Salesforce?")
    assert result["decision"] == "escalate", result
    print("PASS BM25 fallback escalates an irrelevant query")


def main():
    test_bm25_provider_fallback()
    test_bm25_fallback_rejects_irrelevant_query()
    correct = 0
    false_answers = 0
    false_escalations = 0

    for number, (query, expected, expected_source) in enumerate(CASES, 1):
        result = app.nlp.answer_from_knowledge_base(query)
        actual = result["decision"]
        sources = result["trace"].get("sources", [])
        top_source = sources[0]["id"] if sources else None
        is_correct = (
            actual == "answer" and expected == "answer" and top_source == expected_source
        ) or (
            actual == "escalate" and expected == "escalate"
        )
        correct += int(is_correct)
        if expected == "escalate" and actual == "answer":
            false_answers += 1
        if expected == "answer" and actual == "escalate":
            false_escalations += 1
        mark = "PASS" if is_correct else "FAIL"
        print(f"{mark} [{number:02d}] expected={expected} got={actual} top={top_source} :: {query}")

    fcr = correct / len(CASES)
    print(f"First-contact resolution: {correct}/{len(CASES)} = {fcr:.1%}")
    print(f"False answers: {false_answers}")
    print(f"False escalations: {false_escalations}")
    return 0 if fcr >= 0.70 and false_answers == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
