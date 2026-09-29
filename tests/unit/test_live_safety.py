"""What the production fetcher refuses to ask for.

These are the release-blocking ones. A research worker takes addresses from
sitemaps, page links and an operator's paste buffer, and issues requests from
inside our network — so every check here is about not being turned into a proxy
for someone else's intentions.

No network: the resolver is injected, which is also the only honest way to test
that a *public-looking* hostname resolving to a private address is refused.
"""

from __future__ import annotations

import pytest

from boro_gtm.research.live.safety import (
    UnsafeTargetError,
    resolve_safely,
)


def _resolver(mapping: dict[str, list[str]]):
    def resolve(host: str, port: int) -> list[str]:
        try:
            return mapping[host]
        except KeyError:                                # pragma: no cover
            raise OSError(f"no such host {host}") from None
    return resolve


PUBLIC = _resolver({"bigmechanical.com": ["93.184.216.34"]})


def test_a_public_host_resolves_and_is_allowed():
    target = resolve_safely("https://bigmechanical.com/services", resolver=PUBLIC)
    assert target.host == "bigmechanical.com"
    assert target.port == 443
    assert target.addresses == ("93.184.216.34",)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://bigmechanical.com/pub",
    "gopher://bigmechanical.com/",
    "data:text/html,<p>hi</p>",
    "jar:https://bigmechanical.com/!/x",
])
def test_only_http_and_https_are_allowed(url):
    with pytest.raises(UnsafeTargetError, match="scheme"):
        resolve_safely(url, resolver=PUBLIC)


@pytest.mark.parametrize(("name", "address"), [
    ("loopback", "127.0.0.1"),
    ("loopback high", "127.99.1.2"),
    ("private 10/8", "10.0.0.5"),
    ("private 172.16/12", "172.16.31.9"),
    ("private 192.168/16", "192.168.1.1"),
    ("link-local", "169.254.1.1"),
    ("carrier NAT", "100.64.0.1"),
    ("TEST-NET documentation", "203.0.113.10"),
    ("benchmarking", "198.18.0.1"),
    ("unspecified", "0.0.0.0"),
    ("multicast", "239.1.1.1"),
    ("ipv6 loopback", "::1"),
    ("ipv6 unique local", "fd00::1"),
    ("ipv6 link-local", "fe80::1"),
])
def test_a_public_name_resolving_to_a_private_address_is_refused(name, address):
    """The check is at the address, not the string.

    `bigmechanical.com` is a perfectly ordinary hostname. Whoever controls its
    DNS decides where it points, and a URL allowlist cannot see that.

    Carrier-grade NAT is in this list on purpose: Python flags it as neither
    private nor loopback, so an enumerated property check let it through. The
    `is_global` backstop catches it, and TEST-NET and benchmarking space too.
    """
    resolver = _resolver({"bigmechanical.com": [address]})
    with pytest.raises(UnsafeTargetError) as caught:
        resolve_safely("https://bigmechanical.com/", resolver=resolver)
    assert caught.value.reason
    assert address in str(caught.value)


@pytest.mark.parametrize("address", [
    "169.254.169.254",      # AWS / Azure / GCP
    "100.100.100.100",      # Alibaba
    "192.0.0.192",          # Oracle legacy
])
def test_cloud_metadata_addresses_are_refused(address):
    """These hand out credentials to anything that asks from the host."""
    resolver = _resolver({"bigmechanical.com": [address]})
    with pytest.raises(UnsafeTargetError):
        resolve_safely("https://bigmechanical.com/", resolver=resolver)


def test_an_ipv4_mapped_ipv6_address_cannot_smuggle_a_private_address():
    """`::ffff:10.0.0.5` is 10.0.0.5 wearing a hat."""
    resolver = _resolver({"bigmechanical.com": ["::ffff:10.0.0.5"]})
    with pytest.raises(UnsafeTargetError):
        resolve_safely("https://bigmechanical.com/", resolver=resolver)


def test_one_private_address_among_several_refuses_the_whole_host():
    """A host that answers with both is a host we do not follow.

    Picking the public one would be choosing to race the resolver.
    """
    resolver = _resolver({"bigmechanical.com": ["203.0.113.10", "10.1.2.3"]})
    with pytest.raises(UnsafeTargetError):
        resolve_safely("https://bigmechanical.com/", resolver=resolver)


@pytest.mark.parametrize("host", [
    "localhost", "LOCALHOST", "ip6-localhost", "metadata.google.internal",
    "instance-data", "printer.local", "db.internal", "api.corp", "x.lan",
])
def test_non_public_hostnames_are_refused_before_resolution(host):
    with pytest.raises(UnsafeTargetError, match="non-public hostname"):
        resolve_safely(f"https://{host}/", resolver=_resolver({}))


def test_credentials_in_the_url_are_refused():
    """A URL carrying a password is not a public page we were meant to read."""
    with pytest.raises(UnsafeTargetError, match="credentials"):
        resolve_safely("https://user:secret@bigmechanical.com/", resolver=PUBLIC)


def test_a_host_that_does_not_resolve_is_refused_with_its_reason():
    with pytest.raises(UnsafeTargetError, match="cannot resolve host"):
        resolve_safely("https://nowhere-at-all.com/", resolver=_resolver({}))


def test_a_url_with_no_host_is_refused():
    with pytest.raises(UnsafeTargetError, match="no host"):
        resolve_safely("https:///services", resolver=PUBLIC)


def test_a_globally_routable_address_is_the_only_thing_allowed():
    """Stated as a property rather than a list, so it holds for new reservations."""
    import ipaddress

    from boro_gtm.research.live.safety import _address_is_public

    for address in ("93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"):
        allowed, reason = _address_is_public(address)
        assert allowed, f"{address}: {reason}"
        assert ipaddress.ip_address(address).is_global
    for address in ("100.64.0.1", "203.0.113.1", "198.18.0.1", "192.0.2.1"):
        allowed, _ = _address_is_public(address)
        assert not allowed, f"{address} is not globally routable but was allowed"


def test_a_trailing_dot_and_case_do_not_bypass_the_host_rules():
    """`LOCALHOST.` is `localhost`."""
    with pytest.raises(UnsafeTargetError, match="non-public hostname"):
        resolve_safely("https://LOCALHOST./", resolver=_resolver({}))
