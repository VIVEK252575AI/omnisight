"""Admission policy for targets that a local owner explicitly authorizes."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class TargetSafetyError(ValueError):
    """The configured target cannot be safely admitted under the local policy."""


def resolve_target_addresses(host: str) -> set[str]:
    """Resolve all addresses so one unsafe answer cannot be hidden by DNS order."""
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise TargetSafetyError("The target host did not resolve; check the URL and try again") from None
    addresses: set[str] = set()
    for answer in answers:
        try:
            addresses.add(str(ipaddress.ip_address(answer[4][0])))
        except (ValueError, IndexError, TypeError):
            raise TargetSafetyError("The target host returned an invalid network address") from None
    if not addresses:
        raise TargetSafetyError("The target host did not resolve; check the URL and try again")
    return addresses


def admit_target(base_url: str, environment_kind: str) -> str:
    """Check public HTTPS target constraints and return its exact allowed host.

    This admission check does not make an HTTP request. Runtime browser execution
    must repeat policy checks at connection and redirect time inside a restricted
    worker; DNS checks at approval time do not prevent DNS rebinding on their own.
    """
    if environment_kind not in {"development", "staging"}:
        raise TargetSafetyError("Only development or staging targets can be authorized")
    parsed = urlsplit(base_url)
    try:
        port = parsed.port
    except ValueError:
        raise TargetSafetyError("The target URL contains an invalid port") from None
    if parsed.scheme.lower() != "https":
        raise TargetSafetyError("Only HTTPS targets can be authorized")
    if port not in {None, 443}:
        raise TargetSafetyError("Only the standard HTTPS port 443 is supported")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise TargetSafetyError("The target URL must not contain credentials, a query, or a fragment")
    raw_host = (parsed.hostname or "").rstrip(".").lower()
    if not raw_host or raw_host.endswith((".localhost", ".local", ".internal", ".test", ".example", ".invalid")):
        raise TargetSafetyError("Local, reserved, and test-only hostnames cannot be authorized")
    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        address = None
    if address is not None:
        raise TargetSafetyError("IP-literal targets cannot be authorized; use a verified HTTPS hostname")
    try:
        host = raw_host.encode("idna").decode("ascii")
    except UnicodeError:
        raise TargetSafetyError("The target hostname is invalid") from None
    if "." not in host or len(host) > 253:
        raise TargetSafetyError("The target must use a fully qualified hostname")

    addresses = resolve_target_addresses(host)
    if any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise TargetSafetyError("The target host resolves to a private or reserved network address")
    return host
