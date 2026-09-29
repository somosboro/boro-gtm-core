"""What this fetcher refuses to ask for.

A research worker takes addresses from third parties — sitemaps, page links, an
operator's paste buffer — and issues requests from inside our network. That is
the shape of an SSRF, and the defence has to sit at the socket, not at the URL
string: `http://internal.example/` can resolve to `10.0.0.5`, and a public host
can redirect to `169.254.169.254`. So every hop is re-checked against the
addresses it actually resolves to (M3-ADR-073).
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

#: The only schemes a web research fetch may use. `file://`, `gopher://`,
#: `ftp://` and friends are not "unsupported", they are refused.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Cloud instance-metadata endpoints. These are ordinary public-looking
#: addresses that hand out credentials to whoever asks from inside the host, so
#: they are named explicitly as well as caught by the link-local rule.
METADATA_ADDRESSES = frozenset({
    "169.254.169.254",      # AWS, Azure, GCP, DigitalOcean, Oracle
    "100.100.100.100",      # Alibaba Cloud
    "192.0.0.192",          # Oracle Cloud legacy
    "fd00:ec2::254",        # AWS IMDSv2 over IPv6
})

#: Hostnames that never belong to a company's public website.
DENIED_HOSTS = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal", "instance-data",
})

#: Suffixes reserved for private or non-public use (RFC 6761, RFC 8375).
DENIED_HOST_SUFFIXES = (
    ".localhost", ".local", ".internal", ".intranet", ".private", ".corp",
    ".home", ".home.arpa", ".lan", ".test", ".example", ".invalid",
)


class UnsafeTargetError(Exception):
    """The address may not be fetched. Never retried, never worked around."""

    def __init__(self, reason: str, target: str) -> None:
        super().__init__(f"{reason}: {target}")
        self.reason = reason
        self.target = target


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """A URL that passed every check, with the addresses it resolved to."""

    url: str
    host: str
    port: int
    addresses: tuple[str, ...]


def _address_is_public(address: str) -> tuple[bool, str]:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:                                  # pragma: no cover
        return False, "unparseable address"
    if address in METADATA_ADDRESSES:
        return False, "cloud metadata address"
    if ip.is_loopback:
        return False, "loopback address"
    if ip.is_link_local:
        return False, "link-local address"
    if ip.is_private:
        return False, "private address"
    if ip.is_reserved:
        return False, "reserved address"
    if ip.is_multicast:
        return False, "multicast address"
    if ip.is_unspecified:
        return False, "unspecified address"
    # An IPv4-mapped or 6to4 address can smuggle a private v4 address through a
    # v6 literal, so unwrap before trusting it.
    mapped = getattr(ip, "ipv4_mapped", None) or getattr(ip, "sixtofour", None)
    if mapped is not None:
        return _address_is_public(str(mapped))
    # The backstop, and it is not redundant. Carrier-grade NAT space
    # (100.64.0.0/10, RFC 6598) is flagged by *no* property above —
    # `is_private` is False and `is_loopback` is False — yet it reaches other
    # customers of the same carrier. `is_global` tracks the IANA
    # special-purpose registries, so it also covers whatever gets reserved
    # next, which an enumerated list never will.
    if not ip.is_global:
        return False, "not globally routable"
    return True, ""


def resolve_safely(
    url: str, *, resolver=None, allow_private: bool = False
) -> ResolvedTarget:
    """Check one hop, and return the addresses it is allowed to reach.

    `allow_private` exists for the test suite's own local HTTP server and is
    never set by the pipeline or the CLI. It is a parameter rather than an
    environment variable so that turning it on is visible in the call site.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeTargetError(f"scheme {scheme or '(none)'} is not allowed", url)
    if parts.username or parts.password:
        raise UnsafeTargetError("credentials in the URL", url)

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeTargetError("no host", url)
    if host in DENIED_HOSTS or host.endswith(DENIED_HOST_SUFFIXES):
        if not allow_private:
            raise UnsafeTargetError("non-public hostname", url)

    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeTargetError("unparseable port", url) from exc

    lookup = resolver or _system_resolver
    try:
        addresses = lookup(host, port)
    except OSError as exc:
        raise UnsafeTargetError(f"cannot resolve host ({exc.strerror or exc})", url) from exc
    if not addresses:
        raise UnsafeTargetError("host resolved to nothing", url)

    if not allow_private:
        for address in addresses:
            public, reason = _address_is_public(address)
            if not public:
                raise UnsafeTargetError(f"{reason} ({address})", url)

    return ResolvedTarget(url=url, host=host, port=port, addresses=tuple(addresses))


def _system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    seen: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen
