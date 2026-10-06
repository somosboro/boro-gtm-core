"""The vendored Public Suffix List, and the registrable domains read from it.

No database and no network: the list is a file in the repository, so these are
the same answers in CI, offline and in production (M2-ADR-053).
"""

from __future__ import annotations

import ast
import hashlib
import socket

import pytest

from boro_gtm.core import public_suffix
from boro_gtm.core.public_suffix import registrable_domain, split


def test_the_snapshot_is_the_one_this_build_was_reviewed_against():
    raw = public_suffix.SNAPSHOT_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == public_suffix.SNAPSHOT_SHA256
    header = raw[:2000].decode("utf-8")
    assert f"VERSION: {public_suffix.SNAPSHOT_VERSION}" in header
    assert f"COMMIT: {public_suffix.SNAPSHOT_COMMIT}" in header


def test_a_tampered_snapshot_is_refused_rather_than_quietly_used(monkeypatch, tmp_path):
    """Which companies are the same company must not change because a file did."""
    edited = tmp_path / "public_suffix_list.dat"
    edited.write_bytes(public_suffix.SNAPSHOT_PATH.read_bytes() + b"\nexample-edit\n")
    monkeypatch.setattr(public_suffix, "SNAPSHOT_PATH", edited)
    public_suffix._rules.cache_clear()
    try:
        with pytest.raises(public_suffix.PublicSuffixSnapshotError):
            split("example.com")
    finally:
        monkeypatch.undo()
        public_suffix._rules.cache_clear()


def test_reading_the_list_opens_no_socket(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("normalization must never reach the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    public_suffix._rules.cache_clear()
    assert registrable_domain("www.example.com") == "example.com"


def test_the_module_imports_no_network_client():
    tree = ast.parse(public_suffix.__file__ and open(public_suffix.__file__).read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"httpx", "requests", "urllib", "socket", "http", "aiohttp"})


# The cases the list's own maintainers publish as its test vectors.
@pytest.mark.parametrize(("host", "expected"), [
    ("example.com", "example.com"),
    ("b.example.com", "example.com"),
    ("a.b.example.com", "example.com"),
    ("EXAMPLE.COM.", "example.com"),
    ("example.co.uk", "example.co.uk"),
    ("a.b.example.co.uk", "example.co.uk"),
    ("acme.com.au", "acme.com.au"),
    ("acme.co.nz", "acme.co.nz"),
    ("acme.com.br", "acme.com.br"),
    ("acme.co.jp", "acme.co.jp"),
    ("acme.com.mx", "acme.com.mx"),
    # Wildcard and exception rules.
    ("b.test.ck", "b.test.ck"),
    ("a.b.test.ck", "b.test.ck"),
    ("www.ck", "www.ck"),
    ("www.www.ck", "www.ck"),
    ("city.kobe.jp", "city.kobe.jp"),
    ("www.city.kobe.jp", "city.kobe.jp"),
    ("a.b.c.kobe.jp", "b.c.kobe.jp"),
    # US locality names.
    ("temptitans.us", "temptitans.us"),
    ("test.ak.us", "test.ak.us"),
    ("www.test.k12.ak.us", "test.k12.ak.us"),
    # An unlisted TLD falls to the implicit `*` rule.
    ("a.b.example.example", "example.example"),
    # Internationalised names compare in their ASCII form.
    ("食狮.com.cn", "xn--85x722f.com.cn"),
    ("www.xn--85x722f.com.cn", "xn--85x722f.com.cn"),
])
def test_registrable_domain(host, expected):
    assert registrable_domain(host) == expected


@pytest.mark.parametrize("host", [None, "", "localhost", "com", "co.uk", "test.ck",
                                  "10.0.0.1", "[::1]", "a..b.com", "."])
def test_what_is_not_a_registrable_name_is_none(host):
    assert registrable_domain(host) is None


@pytest.mark.parametrize("platform", [
    "lovable.app", "web.app", "pages.dev", "vercel.app", "github.io",
    "netlify.app", "wixsite.com", "herokuapp.com", "azurewebsites.net",
    "webflow.io", "firebaseapp.com",
])
def test_two_tenants_of_a_platform_are_two_registrable_domains(platform):
    foo, bar = split(f"foo.{platform}"), split(f"bar.{platform}")
    assert foo.registrable == f"foo.{platform}"
    assert bar.registrable == f"bar.{platform}"
    assert foo.registrable != bar.registrable
    assert foo.private and bar.private
    # Who runs the platform is still knowable; it is just not who the tenant is.
    assert foo.icann_registrable == platform
    # A deeper name belongs to the same tenant.
    assert split(f"www.foo.{platform}").registrable == f"foo.{platform}"


def test_a_platform_domain_is_a_suffix_not_a_tenant():
    parts = split("lovable.app")
    assert parts.registrable is None and parts.private
    # Somebody does own that site, so it still has a name to be recorded under.
    assert registrable_domain("lovable.app") == "lovable.app"


def test_an_ordinary_domain_is_not_private():
    parts = split("www.mckenneys.com")
    assert parts.registrable == "mckenneys.com"
    assert parts.private is False
    assert parts.public_suffix == "com"
