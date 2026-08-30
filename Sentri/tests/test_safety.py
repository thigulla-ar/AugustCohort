from sentri.models import PlannedAction
from sentri.redaction import redact, redact_text
from sentri.safety import evaluate_actions


def codes(action: PlannedAction) -> set[str]:
    return {alert.code for alert in evaluate_actions([action])}


def test_blocks_financial_transactions() -> None:
    action = PlannedAction(tool="stripe.payment", operation="charge", arguments={})
    assert "HARD_NO_MONEY" in codes(action)


def test_blocks_record_deletion() -> None:
    action = PlannedAction(tool="database", operation="delete record", arguments={})
    assert "HARD_NO_DELETE" in codes(action)


def test_blocks_unhashed_pii_transmission() -> None:
    action = PlannedAction(
        tool="email.send",
        operation="share report",
        arguments={"contact": "person@example.com"},
    )
    assert "HARD_NO_PII_SHARING" in codes(action)


def test_blocks_generic_http_payment_hidden_in_arguments() -> None:
    action = PlannedAction(
        tool="http.request",
        operation="post",
        arguments={"url": "https://example.test/payments/charge"},
    )
    assert "HARD_NO_MONEY" in codes(action)


def test_blocks_classified_pii_even_when_pattern_is_not_recognized() -> None:
    action = PlannedAction(
        tool="webhook.send",
        operation="upload",
        arguments={"full_name": "Example Person"},
        data_classification=["pii"],
    )
    assert "HARD_NO_PII_SHARING" in codes(action)


def test_redacts_pii() -> None:
    output = redact_text("Send to person@example.com or 212-555-1212")
    assert "person@example.com" not in output
    assert "212-555-1212" not in output
    assert output.count("[HASHED_") == 2


def test_redacts_secret_bearing_metadata_keys_without_hiding_usage_metrics() -> None:
    output = redact(
        {"api_key": "secret-value", "access_token": "token-value", "prompt_tokens": 12}
    )
    assert output["api_key"] == "[REDACTED_SECRET]"
    assert output["access_token"] == "[REDACTED_SECRET]"
    assert output["prompt_tokens"] == 12


def test_blocks_erase_alias_as_hard_deletion() -> None:
    action = PlannedAction(
        tool="filesystem", operation="erase", arguments={"path": "records.db"}
    )
    assert "HARD_NO_DELETE" in codes(action)


def test_blocks_payment_intent_confirmation() -> None:
    action = PlannedAction(
        tool="stripe.paymentIntents",
        operation="confirm",
        arguments={"amount": 100},
    )
    assert "HARD_NO_MONEY" in codes(action)


def test_blocks_postal_address_upload() -> None:
    action = PlannedAction(
        tool="http.request",
        operation="post",
        arguments={"body": "Jane Doe, 1 Main Street, Boston, MA 02108"},
    )
    assert "HARD_NO_PII_SHARING" in codes(action)


def test_blocks_pii_sent_to_model_provider() -> None:
    action = PlannedAction(
        tool="openai.responses",
        operation="create",
        arguments={"model": "test-model", "input": "Email person@example.com"},
    )
    assert "HARD_NO_PII_SHARING" in codes(action)


def test_non_mutating_model_generation_does_not_require_mutation_review() -> None:
    action = PlannedAction(
        tool="openai.responses",
        operation="create",
        arguments={"model": "test-model", "input": "Summarize public text"},
    )
    assert "REVIEW_MUTATION" not in codes(action)
