from __future__ import annotations

import json
import re
import unicodedata
from urllib.parse import unquote
from collections.abc import Iterable

from sentri.models import PlannedAction, RiskAlert
from sentri.redaction import pii_types


MONEY_DIRECT_RE = re.compile(
    r"\b(send\s*money|wire|transfer|charge|refund|withdraw|purchase|checkout|payout|"
    r"disburse|debit|credit\s+card|ach)\b",
    re.I,
)
MONEY_CONTEXT_RE = re.compile(
    r"\b(payments?|payment\s*intents?|stripe|invoice|bank|currency|amount|card|funds?)\b",
    re.I,
)
TRANSACTION_VERB_RE = re.compile(
    r"\b(confirm|capture|execute|create|submit|send|post|authorize|settle|buy|sell)\b",
    re.I,
)
DELETE_RE = re.compile(
    r"\b(delete|destroy|drop|truncate|unlink|erase|purge|shred|rmtree|"
    r"remove\s*(?:file|record|row|asset|document|object)|rm)\b",
    re.I,
)
TRANSMIT_RE = re.compile(
    r"\b(send|share|post|upload|transmit|email|publish|export|webhook|recipient|"
    r"destination|external\s*url)\b",
    re.I,
)
MODEL_PROVIDER_RE = re.compile(
    r"\b(openai|gemini|responses?\s+create|generate\s+content|model\s+inference)\b",
    re.I,
)
MUTATION_RE = re.compile(
    r"\b(create|update|write|append|patch|put|post|modify|rename|move|confirm|"
    r"capture|execute|submit|authorize|settle|erase|purge|delete|remove)\b",
    re.I,
)


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", unquote(unquote(value)))
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"[_./:\\-]+", " ", text)
    return re.sub(r"\s+", " ", text).casefold()


class HardLimitViolation(RuntimeError):
    def __init__(self, alerts: list[RiskAlert]) -> None:
        super().__init__("Sentri hard safety limit violated")
        self.alerts = alerts


def evaluate_actions(actions: Iterable[PlannedAction]) -> list[RiskAlert]:
    alerts: list[RiskAlert] = []
    for action in actions:
        serialized_args = json.dumps(action.arguments, default=str, ensure_ascii=False)
        signature = _normalize(f"{action.tool} {action.operation}")
        semantic_signature = _normalize(f"{signature} {serialized_args}")

        financial_transaction = bool(MONEY_DIRECT_RE.search(semantic_signature)) or bool(
            MONEY_CONTEXT_RE.search(semantic_signature)
            and TRANSACTION_VERB_RE.search(semantic_signature)
        )
        if financial_transaction:
            alerts.append(
                RiskAlert(
                    code="HARD_NO_MONEY",
                    severity="critical",
                    message="Financial transactions and payment calls are forbidden.",
                    action_id=action.id,
                    hard_limit=True,
                    requires_human=True,
                )
            )
        if DELETE_RE.search(semantic_signature):
            alerts.append(
                RiskAlert(
                    code="HARD_NO_DELETE",
                    severity="critical",
                    message="File, database, and record deletion calls are forbidden.",
                    action_id=action.id,
                    hard_limit=True,
                    requires_human=True,
                )
            )
        found_pii = pii_types(serialized_args)
        classified_pii = any(
            label.lower() in {"pii", "personal", "sensitive_personal"}
            for label in action.data_classification
        )
        if (found_pii or classified_pii) and (
            TRANSMIT_RE.search(semantic_signature)
            or MODEL_PROVIDER_RE.search(signature)
        ):
            alerts.append(
                RiskAlert(
                    code="HARD_NO_PII_SHARING",
                    severity="critical",
                    message=(
                        "Unhashed PII transmission is forbidden"
                        + (f" ({', '.join(found_pii)})" if found_pii else "")
                        + "."
                    ),
                    action_id=action.id,
                    hard_limit=True,
                    requires_human=True,
                )
            )
        model_generation = bool(MODEL_PROVIDER_RE.search(signature))
        inferred_mutation = action.mutates_state or bool(
            MUTATION_RE.search(signature) and not model_generation
        )
        if inferred_mutation and not any(
            alert.action_id == action.id for alert in alerts
        ):
            alerts.append(
                RiskAlert(
                    code="REVIEW_MUTATION",
                    severity="medium",
                    message="State-changing action requires human approval.",
                    action_id=action.id,
                    requires_human=True,
                )
            )
    return alerts


def enforce_hard_limits(actions: Iterable[PlannedAction]) -> None:
    hard = [alert for alert in evaluate_actions(actions) if alert.hard_limit]
    if hard:
        raise HardLimitViolation(hard)
