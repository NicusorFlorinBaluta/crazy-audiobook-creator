"""Security helpers for local and reverse-proxied dashboard access."""

from __future__ import annotations

import ipaddress
import os
import secrets
from typing import Any


TOKEN_ENV_VAR = "CRAZY_AUDIOBOOK_DASHBOARD_TOKEN"


def configured_dashboard_token(dashboard_config: dict[str, Any]) -> str:
    """Return the runtime token without requiring secrets in tracked YAML."""
    return os.environ.get(TOKEN_ENV_VAR, "").strip() or str(
        dashboard_config.get("api_token", "")
    ).strip()


def is_loopback_client(host: str | None) -> bool:
    """Return whether a request originated on the dashboard host itself."""
    if not host:
        return False
    normalized = host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return normalized.lower() == "localhost"
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def dashboard_request_authorized(
    *,
    client_host: str | None,
    configured_token: str,
    presented_token: str | None,
) -> bool:
    """Allow loopback requests, otherwise require a constant-time token match."""
    if is_loopback_client(client_host):
        return True
    return bool(
        configured_token
        and presented_token
        and secrets.compare_digest(configured_token, presented_token)
    )
