from __future__ import annotations

import json
import re
from typing import Any

from .db import list_rules, load_json_text


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip().lower() for item in value if str(item).strip()]
    return [str(value).strip().lower()]


def _message_text(message: dict[str, Any]) -> str:
    parts = [
        message.get("subject") or "",
        message.get("body_excerpt") or "",
        " ".join(message.get("attachment_names") or []),
    ]
    return " ".join(part for part in parts if part).lower()


def _sender_domain(sender_email: str | None) -> str:
    if not sender_email or "@" not in sender_email:
        return ""
    return sender_email.rsplit("@", 1)[-1].strip().lower()


def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def rule_matches(message: dict[str, Any], conditions: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    sender_email = (message.get("sender_email") or "").strip().lower()
    sender_domain = _sender_domain(sender_email)
    subject = (message.get("subject") or "").lower()
    body = (message.get("body_excerpt") or "").lower()
    attachment_names = [str(item).lower() for item in message.get("attachment_names") or []]
    to_recipients = message.get("to_recipients") or []
    cc_recipients = message.get("cc_recipients") or []
    importance = (message.get("importance") or "normal").lower()

    from_email = str(conditions.get("from_email") or "").strip().lower()
    if from_email and sender_email != from_email:
        return False, []
    if from_email:
        reasons.append(f"from_email={from_email}")

    from_domain = str(conditions.get("from_domain") or "").strip().lower()
    if from_domain and sender_domain != from_domain:
        return False, []
    if from_domain:
        reasons.append(f"from_domain={from_domain}")

    subject_contains_any = _as_list(conditions.get("subject_contains_any"))
    if subject_contains_any and not _contains_any(subject, subject_contains_any):
        return False, []
    if subject_contains_any:
        reasons.append(f"subject_contains_any={subject_contains_any}")

    body_contains_any = _as_list(conditions.get("body_contains_any"))
    if body_contains_any and not _contains_any(body, body_contains_any):
        return False, []
    if body_contains_any:
        reasons.append(f"body_contains_any={body_contains_any}")

    attachment_contains_any = _as_list(conditions.get("attachment_contains_any"))
    if attachment_contains_any:
        attachment_text = " ".join(attachment_names)
        if not _contains_any(attachment_text, attachment_contains_any):
            return False, []
        reasons.append(f"attachment_contains_any={attachment_contains_any}")

    if conditions.get("to_me_only") is not None:
        expected = bool(conditions.get("to_me_only"))
        actual = bool(to_recipients) and not bool(cc_recipients)
        if actual != expected:
            return False, []
        reasons.append(f"to_me_only={expected}")

    if conditions.get("cc_only") is not None:
        expected = bool(conditions.get("cc_only"))
        actual = (not bool(to_recipients)) and bool(cc_recipients)
        if actual != expected:
            return False, []
        reasons.append(f"cc_only={expected}")

    if conditions.get("importance"):
        expected = str(conditions.get("importance")).strip().lower()
        if expected and importance != expected:
            return False, []
        if expected:
            reasons.append(f"importance={expected}")

    exclude_contains_any = _as_list(conditions.get("exclude_contains_any"))
    if exclude_contains_any:
        full_text = _message_text(message)
        if _contains_any(full_text, exclude_contains_any):
            return False, []

    return True, reasons


def evaluate_rules(conn, message: dict[str, Any]) -> dict[str, Any] | None:
    for row in list_rules(conn):
        if not row["enabled"]:
            continue
        conditions = load_json_text(row["conditions_json"], {})
        action = load_json_text(row["action_json"], {})
        matched, reasons = rule_matches(message, conditions)
        if not matched:
            continue
        return {
            "rule_id": int(row["id"]),
            "rule_name": row["name"],
            "source": row["source"],
            "confidence": float(row["confidence"] or 1.0),
            "target_folder": str(action.get("move_to") or message.get("current_folder") or "inbox"),
            "reason": ", ".join(reasons) if reasons else f"rule {row['name']}",
        }
    return None


def parse_rule_json(text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    data = json.loads(text)
    conditions = data.get("conditions") or {}
    action = data.get("action") or {}
    if not isinstance(conditions, dict) or not isinstance(action, dict):
        raise ValueError("Rule JSON must contain object conditions and action.")
    return conditions, action

