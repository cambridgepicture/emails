from __future__ import annotations

from typing import Any


def fetch_messages(*args, **kwargs) -> list[dict[str, Any]]:
    raise NotImplementedError("Microsoft Graph ingestion is intentionally deferred for phase 1.")


def move_message(*args, **kwargs) -> None:
    raise NotImplementedError("Microsoft Graph message moves are intentionally deferred for phase 1.")

