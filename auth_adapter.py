from __future__ import annotations

import os
from functools import wraps
from types import SimpleNamespace

AUTH_ENABLED = os.getenv("AUTH_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}

if AUTH_ENABLED:
    from shared.auth import current_user, redirect_to_login, require_app_access
else:
    STANDALONE_USER = SimpleNamespace(
        id=1,
        user_id=1,
        email=os.getenv("STANDALONE_USER_EMAIL", "standalone@example.local"),
        display_name=os.getenv("STANDALONE_USER_NAME", "Standalone User"),
        is_admin=True,
        allowed_apps=("emails",),
    )

    def current_user():
        return STANDALONE_USER

    def redirect_to_login(next_url: str | None = None):
        return None

    def require_app_access(app_slug: str):
        def decorator(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                return view(*args, **kwargs)

            return wrapped

        return decorator

