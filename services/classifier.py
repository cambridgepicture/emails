from __future__ import annotations

from typing import Any

from .db import (
    add_classification_event,
    dump_json,
    get_folder_by_display_name,
    get_folder_by_role,
    list_folders,
    load_json_text,
    utc_now,
    update_message_fields,
)
from .rule_engine import evaluate_rules


URGENT_KEYWORDS = ("urgent", "asap", "immediate", "important", "action required")
INVOICE_KEYWORDS = ("invoice", "payment", "fee", "billing", "amount due", "statement")
NEWSLETTER_KEYWORDS = ("unsubscribe", "newsletter", "digest", "marketing", "promotion")


def _text(message: dict[str, Any]) -> str:
    return " ".join(
        [
            str(message.get("subject") or ""),
            str(message.get("body_excerpt") or ""),
            " ".join(message.get("attachment_names") or []),
        ]
    ).lower()


def _target_folder_name(conn, role: str) -> str:
    folder = get_folder_by_role(conn, role)
    if folder is not None:
        return str(folder["display_name"])
    return role


def score_message(conn, message: dict[str, Any]) -> dict[str, Any]:
    score = 0
    reasons: list[str] = []
    text = _text(message)
    to_recipients = message.get("to_recipients") or []
    cc_recipients = message.get("cc_recipients") or []
    importance = str(message.get("importance") or "normal").lower()
    urgent_hit = any(keyword in text for keyword in URGENT_KEYWORDS)
    invoice_hit = any(keyword in text for keyword in INVOICE_KEYWORDS)
    newsletter_hit = any(keyword in text for keyword in NEWSLETTER_KEYWORDS)

    if importance in {"high", "highest"}:
        score += 25
        reasons.append("high importance")

    if to_recipients and not cc_recipients:
        score += 20
        reasons.append("direct recipient")

    if cc_recipients and not to_recipients:
        score -= 20
        reasons.append("cc only")

    if urgent_hit:
        score += 25
        reasons.append("urgent keywords")

    if invoice_hit:
        score += 20
        reasons.append("financial keywords")

    if newsletter_hit:
        score += 15
        reasons.append("newsletter signals")

    if urgent_hit:
        target_role = "urgent"
    elif invoice_hit:
        target_role = "financial"
    elif newsletter_hit:
        target_role = "later"
    elif score >= 20:
        target_role = "action"
    else:
        target_role = "archive_candidate"

    confidence = min(0.95, 0.35 + abs(score) / 100.0)
    return {
        "target_folder": _target_folder_name(conn, target_role),
        "source": "score",
        "confidence": confidence,
        "reason": "; ".join(reasons) if reasons else "score fallback",
        "score": score,
    }


def classify_message(conn, message_row) -> dict[str, Any]:
    message = dict(message_row)
    message["to_recipients"] = load_json_text(message.get("to_recipients_json"), [])
    message["cc_recipients"] = load_json_text(message.get("cc_recipients_json"), [])
    message["attachment_names"] = load_json_text(message.get("attachment_names_json"), [])

    rule_result = evaluate_rules(conn, message)
    if rule_result:
        return {
            "target_folder": rule_result["target_folder"],
            "source": "rule",
            "confidence": rule_result["confidence"],
            "reason": rule_result["reason"],
            "details": rule_result,
        }

    scored = score_message(conn, message)
    return {
        "target_folder": scored["target_folder"],
        "source": scored["source"],
        "confidence": scored["confidence"],
        "reason": scored["reason"],
        "details": scored,
    }


def apply_classification(conn, message_id: int, classification: dict[str, Any]) -> None:
    message_row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if message_row is None:
        return
    current_folder = str(message_row["current_folder"] or message_row["initial_folder"] or "inbox")
    target_folder = str(classification["target_folder"])
    moved_at = message_row["app_moved_at"]
    if current_folder != target_folder and not moved_at:
        moved_at = utc_now()
    update_message_fields(
        conn,
        message_id,
        classified_folder=target_folder,
        current_folder=target_folder,
        classification_source=str(classification["source"]),
        classification_confidence=float(classification["confidence"]),
        classification_reason=str(classification["reason"]),
        app_moved_at=moved_at,
    )
    add_classification_event(
        conn,
        message_id,
        "classified",
        {
            "target_folder": target_folder,
            "source": classification["source"],
            "confidence": classification["confidence"],
            "reason": classification["reason"],
            "details": classification.get("details", {}),
        },
    )


def upsert_and_classify_message(conn, payload: dict[str, Any]) -> dict[str, Any]:
    from .db import upsert_message

    normalized = dict(payload)
    normalized["to_recipients"] = normalized.get("to_recipients") or []
    normalized["cc_recipients"] = normalized.get("cc_recipients") or []
    normalized["attachment_names"] = normalized.get("attachment_names") or []
    message_id = upsert_message(conn, normalized)
    message_row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    classification = classify_message(conn, message_row)
    apply_classification(conn, message_id, classification)
    return {
        "message_id": message_id,
        "classification": classification,
    }
