# Review edge cases

These behaviors close A08, A16, A17 and A18 from the September 2026 review.

## Concurrent body storage (A08)

Identical bodies can be stored concurrently. Each writer owns a unique temporary
file beside the destination; installation is atomic and cleanup only removes
that writer's temporary file. All successful writers return the same existing
`cas://` content reference and exact UTF-8 bytes. A failed installation removes
its temporary payload. This does not add crash-time garbage collection or an
fsync durability guarantee.

Regression coverage: `tests/integration/test_filestore_concurrent_writes.py`.
It uses real files in a fresh interpreter, with a barrier at the OS rename audit
boundary rather than sleeps or replaced internal functions. A second scenario
creates a real destination obstruction and checks cleanup after failure.

## Fixed-schedule failure floor (A16)

A fixed schedule uses exponential failure backoff capped at the greater of one
day and its configured interval. Failure never makes collection more frequent:
a seven-day source remains seven-day after one or repeated failures. Shorter
schedules retain their existing exponential backoff and one-day ceiling.
The next due time remains derived from policy and fetch history, not persisted.

Regression coverage: `tests/unit/test_scheduling_backoff_floor.py`, including
exact intervals around the one-day boundary and the weekly next-due timestamp.

## IPv6 canonical URLs (A17)

IPv6 literal brackets are retained when canonical URLs are reconstructed.
Default ports are removed as before; non-default ports follow the closing
bracket. Repeated canonicalization produces the same valid URL.
For example, `https://[2001:db8::1]:443/a` becomes
`https://[2001:db8::1]/a`, not an unparseable unbracketed authority.

Regression coverage: `tests/unit/test_canonical_ipv6.py`.
No stored identities are rewritten automatically. Existing malformed IPv6
canonical values need case-by-case reconciliation before re-importing a source;
this repair does not migrate previously persisted rows.

## Supplementary Unicode in retained RSS rendering (A18)

RSS body rendering preserves the XML 1.0 range U+10000–U+10FFFF, including
supplementary CJK and emoji. Forbidden controls, surrogate code points and
U+FFFE/U+FFFF remain removed. XML permits U+007F; an XML parser also normalizes
CR to LF. These are not evidence of content loss from the Unicode filter.

Regression coverage: `tests/unit/test_rss_out_supplementary.py`, which renders
then parses actual XML and asserts exact description text at range boundaries.
This concerns the retained renderer only: the public RSS route remains retired
under ADR 0016, and no Reader API behavior or public route is changed.
