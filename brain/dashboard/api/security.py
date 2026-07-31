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


def is_private_client(host: str | None) -> bool:
    """Return whether a request originated on a private local network (LAN)."""
    if not host:
        return False
    normalized = host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(normalized)
        return address.is_private
    except ValueError:
        return False


def dashboard_request_authorized(
    *,
    client_host: str | None,
    configured_token: str,
    presented_token: str | None,
    is_forwarded: bool = False,
) -> bool:
    """Allow direct loopback & private LAN requests when no token configured, otherwise require token."""
    if configured_token:
        return bool(
            presented_token
            and secrets.compare_digest(configured_token, presented_token)
        )
    if is_loopback_client(client_host) or is_private_client(client_host):
        return True
    return False
