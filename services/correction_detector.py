from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .db import (
    count_corrections,
    create_rule,
    create_suggestion,
    get_message,
    get_suggestion,
    insert_correction,
    list_messages,
    list_suggestions,
    load_json_text,
    update_suggestion_status,
)


def _normalized_subject(subject: str | None) -> str:
    value = (subject or "").strip().lower()
    for prefix in ("re:", "fw:", "fwd:"):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
    return value


def _sender_domain(sender_email: str | None) -> str:
    if not sender_email or "@" not in sender_email:
        return ""
    return sender_email.rsplit("@", 1)[-1].strip().lower()


def record_correction(conn, message_id: int, from_folder: str, to_folder: str, correction_type: str = "user_move") -> int:
    return insert_correction(conn, message_id, from_folder, to_folder, correction_type)


def _collect_examples(conn, from_folder: str, to_folder: str, limit: int = 5) -> list[int]:
    rows = conn.execute(
        """
        SELECT message_id
        FROM corrections
        WHERE from_folder = ? AND to_folder = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (from_folder, to_folder, limit),
    ).fetchall()
    return [int(row["message_id"]) for row in rows]


def _build_rule_from_messages(conn, message_ids: list[int], target_folder: str) -> dict[str, Any]:
    messages = [get_message(conn, message_id) for message_id in message_ids]
    messages = [message for message in messages if message is not None]
    sender_emails = [str(message["sender_email"] or "").strip().lower() for message in messages if message["sender_email"]]
    sender_domains = [_sender_domain(email) for email in sender_emails if email]
    subjects = [_normalized_subject(str(message["subject"] or "")) for message in messages if message["subject"]]

    conditions: dict[str, Any] = {}
    if sender_emails and len(set(sender_emails)) == 1:
        conditions["from_email"] = sender_emails[0]
    elif sender_domains and len(set(sender_domains)) == 1:
        conditions["from_domain"] = sender_domains[0]

    common_keywords: list[str] = []
    if subjects:
        words_by_subject = [set(word for word in subject.split() if len(word) > 2) for subject in subjects]
        shared_words = set.intersection(*words_by_subject) if words_by_subject else set()
        common_keywords = sorted(shared_words)
    if common_keywords:
        conditions["subject_contains_any"] = common_keywords[:5]

    if not conditions:
        sample_subjects = [message["subject"] for message in messages if message["subject"]]
        if sample_subjects:
            conditions["subject_contains_any"] = [str(sample_subjects[0]).split(" ")[0].lower()]

    return {
        "conditions": conditions,
        "action": {"move_to": target_folder},
    }


def maybe_create_suggestion_for_correction(conn, message_id: int, from_folder: str, to_folder: str) -> int | None:
    similar_count = count_corrections(conn, from_folder, to_folder)
    if similar_count < 2:
        return None

    existing = conn.execute(
        """
        SELECT id
        FROM rule_suggestions
        WHERE target_folder = ? AND status IN ('pending', 'approved')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (to_folder,),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])

    example_ids = _collect_examples(conn, from_folder, to_folder, limit=5)
    proposed = _build_rule_from_messages(conn, example_ids, to_folder)
    confidence = min(0.95, 0.45 + (similar_count * 0.12))
    name = f"Move {from_folder} to {to_folder}"
    return create_suggestion(
        conn,
        name=name,
        proposed_rule_json=json.dumps(proposed, ensure_ascii=True, sort_keys=True),
        based_on_correction_count=similar_count,
        example_message_ids_json=json.dumps(example_ids),
        target_folder=to_folder,
        confidence=confidence,
    )


def approve_suggestion(conn, suggestion_id: int) -> int | None:
    suggestion = get_suggestion(conn, suggestion_id)
    if suggestion is None:
        return None
    proposed = load_json_text(suggestion["proposed_rule_json"], {})
    rule_id = create_rule(
        conn,
        name=suggestion["name"],
        priority=50,
        enabled=True,
        conditions_json=json.dumps(proposed.get("conditions") or {}, ensure_ascii=True, sort_keys=True),
        action_json=json.dumps(proposed.get("action") or {"move_to": suggestion["target_folder"]}, ensure_ascii=True, sort_keys=True),
        source="suggested",
        confidence=float(suggestion["confidence"] or 0.5),
    )
    update_suggestion_status(conn, suggestion_id, "approved")
    return rule_id


def reject_suggestion(conn, suggestion_id: int) -> None:
    if get_suggestion(conn, suggestion_id) is None:
        return
    update_suggestion_status(conn, suggestion_id, "rejected")


def summarize_pending_suggestions(conn) -> list[dict[str, Any]]:
    suggestions = list_suggestions(conn, status="pending")
    return [
        {
            "id": int(row["id"]),
            "name": row["name"],
            "target_folder": row["target_folder"],
            "confidence": float(row["confidence"] or 0.0),
            "based_on_correction_count": int(row["based_on_correction_count"] or 0),
        }
        for row in suggestions
    ]

