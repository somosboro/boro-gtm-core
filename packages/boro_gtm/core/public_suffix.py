"""Registrable domains, from a vendored Public Suffix List snapshot.

"The last two labels" is not what a registrable domain is. `acme.co.uk` has
three, and `someone.lovable.app` is a *tenant* of a hosting platform: under the
list's private section `lovable.app` is a suffix other people register beneath,
exactly as `co.uk` is. Treating it as one organisation's domain merges every
tenant on the platform into a single company.

The list is read from a file committed beside this module. Nothing here opens a
socket, so normalization is the same offline, in CI and in production, and a
change of semantics is a reviewed change to that file rather than whatever a
server returned today (M2-ADR-053).

Algorithm: https://github.com/publicsuffix/list/wiki/Format
"""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SNAPSHOT_PATH = Path(__file__).with_name("data") / "public_suffix_list.dat"
#: Provenance of the committed snapshot, as stated in its own header.
SNAPSHOT_VERSION = "2026-10-01_23-02-52_UTC"
SNAPSHOT_COMMIT = "6cd82aff889e3d64e5e03bc5c1f43da1934a960a"
#: Pinned so a silently edited or truncated list fails loudly instead of
#: quietly changing which companies are the same company.
SNAPSHOT_SHA256 = "e0fe072d26b0536525badea237953ff451c9f8e64c9d02c6daa81a4491d2fc66"

_PRIVATE_BEGIN = "===BEGIN PRIVATE DOMAINS==="
_PRIVATE_END = "===END PRIVATE DOMAINS==="


class PublicSuffixSnapshotError(RuntimeError):
    """The committed list is not the list this build was reviewed against."""


@dataclass(frozen=True, slots=True)
class _Rules:
    #: rule -> whether it sits in the PRIVATE section.
    exact: dict[str, bool]
    #: `*.foo` stored as `foo`.
    wildcard: dict[str, bool]
    #: `!www.foo` stored as `www.foo`.
    exception: dict[str, bool]


@dataclass(frozen=True, slots=True)
class DomainParts:
    """What the list says about one host."""

    host: str
    #: The suffix under which names are registered, private rules included.
    public_suffix: str
    #: The suffix plus one label. `None` when the host *is* a suffix.
    registrable: str | None
    #: Whether that suffix comes from the PRIVATE section: a platform handing
    #: out names to its own customers, not a registry.
    private: bool
    #: The registrable domain ignoring the PRIVATE section — who operates the
    #: platform, as opposed to who its tenant is.
    icann_registrable: str | None


def _a_label(label: str) -> str:
    """One label in its ASCII form, so `食狮` and `xn--85x722f` compare equal."""
    if label.isascii():
        return label
    try:
        return label.encode("idna").decode("ascii")
    except UnicodeError:
        return label


def _ascii(name: str) -> str:
    return ".".join(_a_label(label) for label in name.split("."))


@lru_cache(maxsize=1)
def _rules() -> _Rules:
    raw = SNAPSHOT_PATH.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SNAPSHOT_SHA256:
        raise PublicSuffixSnapshotError(
            f"{SNAPSHOT_PATH.name} has sha256 {digest}, expected {SNAPSHOT_SHA256}. "
            "Replace the snapshot and its pinned digest together, in one reviewed "
            "change."
        )
    exact: dict[str, bool] = {}
    wildcard: dict[str, bool] = {}
    exception: dict[str, bool] = {}
    private = False
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("//"):
            if _PRIVATE_BEGIN in line:
                private = True
            elif _PRIVATE_END in line:
                private = False
            continue
        rule = line.split()[0].lower()
        if rule.startswith("!"):
            exception[_ascii(rule[1:])] = private
        elif rule.startswith("*."):
            wildcard[_ascii(rule[2:])] = private
        else:
            exact[_ascii(rule)] = private
    return _Rules(exact=exact, wildcard=wildcard, exception=exception)


def _suffix_length(labels: list[str], *, include_private: bool) -> tuple[int, bool]:
    """How many trailing labels form the public suffix, and from which section."""
    rules = _rules()

    def usable(table: dict[str, bool], key: str) -> bool | None:
        section = table.get(key)
        if section is None or (section and not include_private):
            return None
        return section

    count = len(labels)
    # An exception rule prevails over everything, and names the registrable
    # domain itself: the suffix is the rule minus its leftmost label.
    for start in range(count):
        section = usable(rules.exception, ".".join(labels[start:]))
        if section is not None:
            return count - start - 1, section
    # Otherwise the rule with the most labels wins. Scanning from the longest
    # candidate, the first match is that rule.
    for start in range(count):
        candidate = ".".join(labels[start:])
        section = usable(rules.exact, candidate)
        if section is not None:
            return count - start, section
        if start + 1 < count:
            section = usable(rules.wildcard, ".".join(labels[start + 1:]))
            if section is not None:
                return count - start, section
    # No rule: the implicit `*`. An unknown TLD is a one-label suffix.
    return 1, False


def _registrable(labels: list[str], suffix_length: int) -> str | None:
    if len(labels) <= suffix_length:
        return None
    return ".".join(labels[-(suffix_length + 1):])


def split(host: str | None) -> DomainParts | None:
    """Apply the list to a bare host. `None` for anything that is not a name."""
    if not host:
        return None
    name = _ascii(host.strip().lower().rstrip("."))
    if not name or "." not in name:
        return None
    try:
        ipaddress.ip_address(name.strip("[]"))
    except ValueError:
        pass
    else:
        return None
    labels = name.split(".")
    if any(not label for label in labels):
        return None

    length, private = _suffix_length(labels, include_private=True)
    icann_length, _ = _suffix_length(labels, include_private=False)
    return DomainParts(
        host=name,
        public_suffix=".".join(labels[-length:]),
        registrable=_registrable(labels, length),
        private=private,
        icann_registrable=_registrable(labels, icann_length),
    )


def registrable_domain(host: str | None) -> str | None:
    """The registrable domain of a host, private suffixes honoured.

    A host that is itself a private suffix (`github.io`) is still somebody's
    site, so it falls back to the ICANN reading rather than to nothing.
    """
    parts = split(host)
    if parts is None:
        return None
    return parts.registrable or parts.icann_registrable
