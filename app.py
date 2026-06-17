from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from auth_adapter import AUTH_ENABLED, current_user as app_current_user, require_app_access
from config import APPLICATION_ROOT, BASE_URL, Config, GRAPH_API_BASE, GRAPH_AUTHORITY, GRAPH_SCOPES, REDIRECT_URI, SQLITE_PATH, STATIC_URL_PATH
from services.classifier import upsert_and_classify_message
from services.correction_detector import approve_suggestion, maybe_create_suggestion_for_correction, record_correction, reject_suggestion
from services.graph_client import auth_url, clear_token_cache, complete_auth, fetch_messages, get_access_token
from shared import install_shared_header
from shared.auth import graph_token_cache_key
from services.db import (
    add_classification_event,
    connect_db,
    create_rule,
    delete_rule,
    dump_json,
    get_message,
    get_rule,
    get_suggestion,
    init_db,
    list_classification_events,
    list_folders,
    list_messages,
    list_rules,
    list_suggestions,
    load_json_text,
    update_message_fields,
    update_rule_enabled,
    utc_now,
)


BASE_PATH = Path(__file__).resolve().parent

app = Flask(__name__, template_folder="templates", static_folder="static", static_url_path=STATIC_URL_PATH)
app.config.from_object(Config)
app.config.update(
    PREFERRED_URL_SCHEME="https",
    SESSION_COOKIE_PATH="/",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
install_shared_header(app, auth_enabled=AUTH_ENABLED)
login_required = require_app_access("emails")


def _app_path(path: str) -> str:
    if not path.startswith("/"):
        return path
    prefix = (request.script_root or "").rstrip("/")
    return f"{prefix}{path}" if prefix else path


def _get_db():
    db = g.get("emails_db")
    if db is None:
        db = connect_db(SQLITE_PATH)
        g.emails_db = db
    return db


def portal_email() -> str:
    user = app_current_user()
    if user is None:
        raise RuntimeError("No portal user available.")
    return str(user.email).lower().strip()


def portal_graph_cache_key() -> str:
    user = app_current_user()
    if user is None:
        raise RuntimeError("No portal user available.")
    return graph_token_cache_key(user)


def graph_access_token() -> str | None:
    return get_access_token(_get_db(), Config, portal_graph_cache_key())


def require_graph_connection():
    token = graph_access_token()
    if token is None:
        flash("Connect your Microsoft 365 account to continue.", "error")
        return redirect(url_for("settings"))
    return token


def sync_live_messages(page_size: int = 25) -> dict[str, int]:
    token = graph_access_token()
    if token is None:
        return {"synced": 0, "classified": 0}
    db = _get_db()
    payloads = fetch_messages(token, Config, page_size=page_size)
    classified = 0
    for payload in payloads:
        upsert_and_classify_message(db, payload)
        classified += 1
    return {"synced": len(payloads), "classified": classified}


@app.before_request
def _load_db() -> None:
    _get_db()


@app.teardown_appcontext
def _close_db(exc: BaseException | None) -> None:
    db = g.pop("emails_db", None)
    if db is not None:
        db.close()


@app.context_processor
def inject_context():
    return {
        "app_path": _app_path,
        "current_user": app_current_user(),
        "shared_auth_enabled": AUTH_ENABLED,
        "graph_connected": graph_access_token() is not None,
    }


def _serialize_message_row(row) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "id": int(row["id"]),
        "graph_message_id": row["graph_message_id"],
        "internet_message_id": row["internet_message_id"],
        "conversation_id": row["conversation_id"],
        "sender_email": row["sender_email"],
        "sender_name": row["sender_name"],
        "subject": row["subject"],
        "body_excerpt": row["body_excerpt"],
        "received_at": row["received_at"],
        "importance": row["importance"],
        "to_recipients": load_json_text(row["to_recipients_json"], []),
        "cc_recipients": load_json_text(row["cc_recipients_json"], []),
        "attachment_names": load_json_text(row["attachment_names_json"], []),
        "initial_folder": row["initial_folder"],
        "classified_folder": row["classified_folder"],
        "current_folder": row["current_folder"],
        "classification_source": row["classification_source"],
        "classification_confidence": row["classification_confidence"],
        "classification_reason": row["classification_reason"],
        "app_moved_at": row["app_moved_at"],
        "last_seen_at": row["last_seen_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _parse_csv_lines(value: str | None) -> list[str]:
    if not value:
        return []
    items: list[str] = []
    for line in value.splitlines():
        for chunk in line.split(","):
            cleaned = chunk.strip()
            if cleaned:
                items.append(cleaned)
    return items


def _build_rule_payload(form) -> tuple[str, str, str, int, float | None]:
    name = (form.get("name") or "").strip()
    priority = int(form.get("priority") or 100)
    confidence_raw = (form.get("confidence") or "").strip()
    confidence = float(confidence_raw) if confidence_raw else None
    conditions: dict[str, Any] = {}
    action: dict[str, Any] = {"move_to": (form.get("move_to") or "").strip() or "Inbox"}

    for key in ("from_email", "from_domain", "importance"):
        value = (form.get(key) or "").strip()
        if value:
            conditions[key] = value

    for key in ("to_me_only", "cc_only"):
        if form.get(key) == "on":
            conditions[key] = True

    for key in ("subject_contains_any", "body_contains_any", "attachment_contains_any", "exclude_contains_any"):
        values = _parse_csv_lines(form.get(key))
        if values:
            conditions[key] = values

    conditions_json = json.dumps(conditions, ensure_ascii=True, sort_keys=True)
    action_json = json.dumps(action, ensure_ascii=True, sort_keys=True)
    return name, conditions_json, action_json, priority, confidence


@app.get("/")
@login_required
def root_redirect():
    return redirect(url_for("dashboard"))


@app.get("/login")
@login_required
def login():
    state = utc_now()
    session["oauth_state"] = state
    return redirect(auth_url(_get_db(), Config, portal_graph_cache_key(), state))


@app.get("/auth/callback")
@login_required
def auth_callback():
    if request.args.get("state") != session.get("oauth_state"):
        return "Invalid state", 400
    code = request.args.get("code")
    if not code:
        return "Missing code", 400
    result = complete_auth(_get_db(), Config, portal_graph_cache_key(), code)
    if "error" in result:
        return f"Auth error: {result.get('error_description') or result.get('error')}", 400
    flash("Microsoft 365 account connected.", "message")
    stats = sync_live_messages(page_size=25)
    flash(f"Synced {stats['synced']} live messages from Microsoft 365.", "message")
    return redirect(url_for("dashboard"))


@app.post("/disconnect")
@login_required
def disconnect():
    clear_token_cache(_get_db(), portal_graph_cache_key())
    flash("Microsoft 365 connection removed.", "message")
    return redirect(url_for("settings"))


@app.post("/sync")
@login_required
def sync():
    token = require_graph_connection()
    if not isinstance(token, str):
        return token
    stats = sync_live_messages(page_size=25)
    flash(f"Synced {stats['synced']} live messages from Microsoft 365.", "message")
    return redirect(url_for("dashboard"))


@app.get("/dashboard")
@login_required
def dashboard():
    db = _get_db()
    messages = list_messages(db, limit=25)
    rules = list_rules(db)
    suggestions = list_suggestions(db, status="pending")
    folders = list_folders(db)
    graph_connected = graph_access_token() is not None
    counts = {
        "messages": db.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"],
        "rules": db.execute("SELECT COUNT(*) AS count FROM rules WHERE enabled = 1").fetchone()["count"],
        "suggestions": db.execute("SELECT COUNT(*) AS count FROM rule_suggestions WHERE status = 'pending'").fetchone()["count"],
        "corrections": db.execute("SELECT COUNT(*) AS count FROM corrections").fetchone()["count"],
    }
    return render_template(
        "dashboard.html",
        counts=counts,
        messages=[_serialize_message_row(row) for row in messages],
        rules=rules,
        suggestions=suggestions,
        folders=folders,
        graph_connected=graph_connected,
        graph_authority=GRAPH_AUTHORITY,
        graph_scopes=GRAPH_SCOPES,
        redirect_uri=REDIRECT_URI,
        base_url=BASE_URL,
        graph_api_base=GRAPH_API_BASE,
    )


@app.get("/rules")
@login_required
def rules():
    db = _get_db()
    return render_template("rules.html", rules=list_rules(db), folders=list_folders(db))


@app.route("/rules/new", methods=["GET", "POST"])
@login_required
def rule_form():
    db = _get_db()
    folders = list_folders(db)
    if request.method == "POST":
        try:
            name, conditions_json, action_json, priority, confidence = _build_rule_payload(request.form)
            if not name:
                raise ValueError("Rule name is required.")
            create_rule(db, name, priority, True, conditions_json, action_json, source="manual", confidence=confidence)
            flash("Rule created.", "message")
            return redirect(url_for("rules"))
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")
    return render_template("rule_form.html", folders=folders)


@app.post("/rules/<int:rule_id>/toggle")
@login_required
def toggle_rule(rule_id: int):
    rule = get_rule(_get_db(), rule_id)
    if rule is None:
        flash("Rule not found.", "error")
        return redirect(url_for("rules"))
    update_rule_enabled(_get_db(), rule_id, not bool(rule["enabled"]))
    flash("Rule updated.", "message")
    return redirect(url_for("rules"))


@app.post("/rules/<int:rule_id>/delete")
@login_required
def remove_rule(rule_id: int):
    delete_rule(_get_db(), rule_id)
    flash("Rule deleted.", "message")
    return redirect(url_for("rules"))


@app.get("/suggestions")
@login_required
def suggestions():
    db = _get_db()
    pending = list_suggestions(db)
    return render_template("suggestions.html", suggestions=pending)


@app.post("/suggestions/<int:suggestion_id>/approve")
@login_required
def approve_rule_suggestion(suggestion_id: int):
    db = _get_db()
    rule_id = approve_suggestion(db, suggestion_id)
    if rule_id is None:
        flash("Suggestion not found.", "error")
    else:
        flash(f"Suggestion approved and rule {rule_id} created.", "message")
    return redirect(url_for("suggestions"))


@app.post("/suggestions/<int:suggestion_id>/reject")
@login_required
def reject_rule_suggestion(suggestion_id: int):
    db = _get_db()
    reject_suggestion(db, suggestion_id)
    flash("Suggestion rejected.", "message")
    return redirect(url_for("suggestions"))


@app.get("/messages/<int:message_id>")
@login_required
def message_detail(message_id: int):
    db = _get_db()
    row = get_message(db, message_id)
    if row is None:
        flash("Message not found.", "error")
        return redirect(url_for("dashboard"))
    events = list_classification_events(db, message_id)
    folders = list_folders(db)
    return render_template(
        "message_detail.html",
        message=_serialize_message_row(row),
        events=events,
        folders=folders,
    )


@app.post("/messages/<int:message_id>/simulate-correction")
@login_required
def simulate_correction(message_id: int):
    db = _get_db()
    row = get_message(db, message_id)
    if row is None:
        flash("Message not found.", "error")
        return redirect(url_for("dashboard"))
    from_folder = (request.form.get("from_folder") or row["current_folder"] or row["initial_folder"] or "inbox").strip()
    to_folder = (request.form.get("to_folder") or "").strip()
    correction_type = (request.form.get("correction_type") or "user_move").strip()
    if not to_folder:
        flash("Choose a target folder.", "error")
        return redirect(url_for("message_detail", message_id=message_id))
    record_correction(db, message_id, from_folder, to_folder, correction_type)
    moved_at = utc_now()
    update_message_fields(
        db,
        message_id,
        current_folder=to_folder,
        classified_folder=to_folder,
        classification_source="manual",
        classification_reason=f"Manual correction to {to_folder}",
        classification_confidence=1.0,
        app_moved_at=row["app_moved_at"] or moved_at,
    )
    add_classification_event(
        db,
        message_id,
        "correction",
        {"from_folder": from_folder, "to_folder": to_folder, "correction_type": correction_type},
    )
    maybe_create_suggestion_for_correction(db, message_id, from_folder, to_folder)
    flash("Correction recorded.", "message")
    return redirect(url_for("message_detail", message_id=message_id))


@app.route("/test-message", methods=["GET", "POST"])
@login_required
def test_message():
    db = _get_db()
    result = None
    if request.method == "POST":
        try:
            payload = {
                "graph_message_id": request.form.get("graph_message_id") or None,
                "internet_message_id": request.form.get("internet_message_id") or None,
                "conversation_id": request.form.get("conversation_id") or None,
                "sender_email": request.form.get("sender_email") or "",
                "sender_name": request.form.get("sender_name") or "",
                "subject": request.form.get("subject") or "",
                "body_excerpt": request.form.get("body_excerpt") or "",
                "received_at": request.form.get("received_at") or None,
                "importance": request.form.get("importance") or "normal",
                "to_recipients": _parse_csv_lines(request.form.get("to_recipients")),
                "cc_recipients": _parse_csv_lines(request.form.get("cc_recipients")),
                "attachment_names": _parse_csv_lines(request.form.get("attachment_names")),
                "initial_folder": request.form.get("initial_folder") or "inbox",
                "current_folder": request.form.get("initial_folder") or "inbox",
            }
            result = upsert_and_classify_message(db, payload)
            flash("Test message classified.", "message")
            return redirect(url_for("message_detail", message_id=result["message_id"]))
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")
    return render_template("test_message.html", result=result, folders=list_folders(db))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    db = _get_db()
    folders = list_folders(db)
    graph_connected = graph_access_token() is not None
    if request.method == "POST":
        flash("Settings page is a placeholder for phase 1.", "message")
    return render_template(
        "settings.html",
        folders=folders,
        graph_connected=graph_connected,
        graph_authority=GRAPH_AUTHORITY,
        graph_scopes=GRAPH_SCOPES,
        redirect_uri=REDIRECT_URI,
        base_url=BASE_URL,
        graph_api_base=GRAPH_API_BASE,
    )


def create_app() -> Flask:
    conn = connect_db(SQLITE_PATH)
    init_db(conn)
    conn.close()
    return app


application = create_app()


if __name__ == "__main__":
    app.run(debug=True)
