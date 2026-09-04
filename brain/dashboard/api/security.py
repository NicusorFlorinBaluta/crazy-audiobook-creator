"""Security helpers for local and reverse-proxied dashboard access."""

from __future__ import annotations

import ipaddress
import os
import secrets
from typing import Any

from dotenv import load_dotenv

load_dotenv()


TOKEN_ENV_VAR = "CRAZY_AUDIOBOOK_DASHBOARD_TOKEN"
DEFAULT_TRUSTED_LAN_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",  # Tailscale CGNAT range
    "fc00::/7",
    "fe80::/10",
)
SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_cross_site_mutation(method: str, sec_fetch_site: str | None) -> bool:
    """Detect modern-browser cross-site mutation attempts without blocking CLI/proxy clients."""
    return (
        method.upper() not in SAFE_HTTP_METHODS
        and (sec_fetch_site or "").strip().lower() == "cross-site"
    )


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


def is_private_client(
    host: str | None,
    trusted_cidrs: tuple[str, ...] | list[str] = DEFAULT_TRUSTED_LAN_CIDRS,
) -> bool:
    """Return whether the TCP peer belongs to an explicitly trusted LAN.

    ``ipaddress.is_private`` also classifies documentation and other reserved
    ranges as private on some Python versions. Explicit CIDRs avoid accidentally
    authorizing those addresses and make the LAN trust boundary auditable.
    """
    if not host:
        return False
    normalized = host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(normalized)
        return any(address in ipaddress.ip_network(cidr) for cidr in trusted_cidrs)
    except ValueError:
        return False


def configured_trusted_lan_cidrs(
    dashboard_config: dict[str, Any],
) -> tuple[str, ...]:
    """Return the operator-configured LAN trust boundary.

    ``dashboard.trusted_lan_cidrs`` is the documented, auditable control for
    unauthenticated LAN access. When it is absent the boundary falls back to
    ``DEFAULT_TRUSTED_LAN_CIDRS``, which spans all RFC1918 space plus the
    Tailscale CGNAT range -- far wider than most deployments want. Setting the
    key narrows it; an empty list disables token-free LAN access entirely and
    leaves only loopback plus an explicit token.
    """
    if "trusted_lan_cidrs" not in dashboard_config:
        return DEFAULT_TRUSTED_LAN_CIDRS
    configured = dashboard_config.get("trusted_lan_cidrs")
    if configured is None:
        return DEFAULT_TRUSTED_LAN_CIDRS
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, (list, tuple)):
        raise ValueError(
            "dashboard.trusted_lan_cidrs must be a list of CIDR strings"
        )
    return tuple(str(entry).strip() for entry in configured if str(entry).strip())


def dashboard_request_authorized(
    *,
    client_host: str | None,
    configured_token: str,
    presented_token: str | None,
    trusted_lan_cidrs: tuple[str, ...] | list[str] = DEFAULT_TRUSTED_LAN_CIDRS,
) -> bool:
    """Authorize local/LAN peers without a token and public peers by token.

    Authentication is based on the actual TCP peer. Forwarding headers are not
    trusted here, so a public client cannot spoof an RFC1918 address.

    Callers MUST pass ``trusted_lan_cidrs`` from configuration. The default is
    intentionally the widest sane boundary rather than a narrow one, so an
    omission fails open loudly in review rather than silently locking out a
    working deployment -- but relying on the default means the documented
    ``dashboard.trusted_lan_cidrs`` setting has no effect.
    """
    if is_loopback_client(client_host) or is_private_client(
        client_host, trusted_lan_cidrs
    ):
        return True
    if configured_token:
        return bool(
            presented_token
            and secrets.compare_digest(configured_token, presented_token)
        )
    return False
