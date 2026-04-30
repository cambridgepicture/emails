from __future__ import annotations

import json
from typing import Any

import msal
import requests

from .db import delete_graph_token_cache, get_graph_token_cache, set_graph_token_cache
from .security import decrypt_bytes, encrypt_str, get_fernet


def build_msal_app(config, token_cache: msal.SerializableTokenCache) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        client_id=config.CLIENT_ID,
        authority=config.GRAPH_AUTHORITY,
        client_credential=config.CLIENT_SECRET,
        token_cache=token_cache,
    )


def load_token_cache(conn, config, portal_email: str) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    blob = get_graph_token_cache(conn, portal_email)
    if not blob:
        return cache
    try:
        fernet = get_fernet(config.DB_ENCRYPTION_KEY)
        cache.deserialize(decrypt_bytes(fernet, blob))
    except Exception:
        pass
    return cache


def persist_token_cache(conn, config, portal_email: str, cache: msal.SerializableTokenCache) -> None:
    if not cache.has_state_changed:
        return
    fernet = get_fernet(config.DB_ENCRYPTION_KEY)
    set_graph_token_cache(conn, portal_email, encrypt_str(fernet, cache.serialize()))


def clear_token_cache(conn, portal_email: str) -> None:
    delete_graph_token_cache(conn, portal_email)


def get_access_token(conn, config, portal_email: str) -> str | None:
    cache = load_token_cache(conn, config, portal_email)
    app = build_msal_app(config, cache)
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(scopes=config.GRAPH_SCOPES, account=accounts[0])
    persist_token_cache(conn, config, portal_email, cache)
    if result and "access_token" in result:
        return result["access_token"]
    return None


def auth_url(conn, config, portal_email: str, state: str) -> str:
    cache = load_token_cache(conn, config, portal_email)
    app = build_msal_app(config, cache)
    url = app.get_authorization_request_url(
        scopes=config.GRAPH_SCOPES,
        state=state,
        redirect_uri=config.REDIRECT_URI,
        prompt="select_account",
    )
    persist_token_cache(conn, config, portal_email, cache)
    return url


def complete_auth(conn, config, portal_email: str, auth_code: str) -> dict[str, Any]:
    cache = load_token_cache(conn, config, portal_email)
    app = build_msal_app(config, cache)
    result = app.acquire_token_by_authorization_code(
        code=auth_code,
        scopes=config.GRAPH_SCOPES,
        redirect_uri=config.REDIRECT_URI,
    )
    persist_token_cache(conn, config, portal_email, cache)
    return result


def graph_get(access_token: str, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def _first_email_address(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    email_address = value.get("emailAddress")
    if isinstance(email_address, dict):
        return str(email_address.get("address") or "").strip()
    return ""


def _recipient_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    recipients: list[str] = []
    for value in values:
        address = _first_email_address(value)
        if address:
            recipients.append(address)
    return recipients


def _attachment_names(access_token: str, config, message_id: str, has_attachments: bool) -> list[str]:
    if not has_attachments:
        return []
    url = f"{config.GRAPH_API_BASE}/me/messages/{message_id}/attachments"
    data = graph_get(access_token, url, params={"$select": "name"})
    names: list[str] = []
    for item in data.get("value", []):
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def _message_payload(access_token: str, config, message: dict[str, Any]) -> dict[str, Any]:
    sender = message.get("from") or {}
    sender_name = ""
    sender_email = ""
    if isinstance(sender, dict):
        sender_address = sender.get("emailAddress")
        if isinstance(sender_address, dict):
            sender_name = str(sender_address.get("name") or "").strip()
            sender_email = str(sender_address.get("address") or "").strip()
    message_id = str(message.get("id") or "").strip()
    return {
        "graph_message_id": message_id or None,
        "internet_message_id": str(message.get("internetMessageId") or "").strip() or None,
        "conversation_id": str(message.get("conversationId") or "").strip() or None,
        "sender_email": sender_email,
        "sender_name": sender_name,
        "subject": str(message.get("subject") or "").strip(),
        "body_excerpt": str(message.get("bodyPreview") or "").strip(),
        "received_at": str(message.get("receivedDateTime") or "").strip() or None,
        "importance": str(message.get("importance") or "normal").strip() or "normal",
        "to_recipients": _recipient_list(message.get("toRecipients")),
        "cc_recipients": _recipient_list(message.get("ccRecipients")),
        "attachment_names": _attachment_names(access_token, config, message_id, bool(message.get("hasAttachments"))),
        "initial_folder": "Inbox",
        "current_folder": "Inbox",
    }


def fetch_messages(access_token: str, config, page_size: int = 25) -> list[dict[str, Any]]:
    url = f"{config.GRAPH_API_BASE}/me/mailFolders/inbox/messages"
    params = {
        "$top": str(page_size),
        "$orderby": "receivedDateTime desc",
        "$select": ",".join(
            [
                "id",
                "internetMessageId",
                "conversationId",
                "subject",
                "bodyPreview",
                "receivedDateTime",
                "importance",
                "from",
                "toRecipients",
                "ccRecipients",
                "hasAttachments",
            ]
        ),
    }
    messages: list[dict[str, Any]] = []
    while url and len(messages) < page_size:
        data = graph_get(access_token, url, params=params)
        params = None
        for item in data.get("value", []):
            if not isinstance(item, dict):
                continue
            messages.append(_message_payload(access_token, config, item))
            if len(messages) >= page_size:
                break
        url = data.get("@odata.nextLink")
    return messages


def move_message(*args, **kwargs) -> None:
    raise NotImplementedError("Microsoft Graph message moves are intentionally deferred for phase 1.")
