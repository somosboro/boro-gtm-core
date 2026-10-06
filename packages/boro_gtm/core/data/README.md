# Vendored data

## `public_suffix_list.dat`

The Public Suffix List, from <https://publicsuffix.org/list/public_suffix_list.dat>.
Maintained by Mozilla and contributors, licensed under the Mozilla Public
License 2.0; the file carries its own licence header and is committed unmodified.

| | |
|---|---|
| Version | `2026-10-01_23-02-52_UTC` |
| Commit | `6cd82aff889e3d64e5e03bc5c1f43da1934a960a` |
| SHA-256 | `e0fe072d26b0536525badea237953ff451c9f8e64c9d02c6daa81a4491d2fc66` |

`boro_gtm.core.public_suffix` reads this file and refuses any other content: the
digest is pinned in code, because the list decides which hostnames are the same
company (M2-ADR-053). Nothing downloads it at runtime or in CI.

To update: replace the file with a fresh copy from the URL above, then update
`SNAPSHOT_VERSION`, `SNAPSHOT_COMMIT` and `SNAPSHOT_SHA256` in
`public_suffix.py` and the table here, in the same commit. Re-run the suite; a
rule change that moves a registrable domain shows up as a test failure, not as a
silent re-identification.
