# Host-side HTTP egress boundaries

Status: partial. This document records what the host enforces on
reverse-proxied plugin HTTP today, what it does not, and why. It exists so
the gap is a recorded decision surface, not an assumption (problem review
A05; SSRF policy in ADR 0016; sandbox trust model in ADR 0023). One residual
is not an open gap but an *accepted risk with a trigger*: decoded peak memory
(issue #358), recorded in its own section below.

## What is enforced host-side today

`app.plugin_runtime.HostClientTransport` is the single egress path for
sandboxed children (telegram, twitter, zhihu, zsxq, and installed plugins).
As of this change it:

1. Validates the requested URL before any host connection
   (`adapters.http.check_destination`): scheme must be http/https, and the
   destination must resolve to a non-private address unless the deployment
   sets `PEARSS_ALLOW_PRIVATE_FETCH` (ADR 0016).
2. Sends through the shared client with `follow_redirects=False`. The host
   never follows a redirect on the child's behalf: the shared client's
   redirect policy would otherwise carry the host's connection to a
   destination the child never inspected.
3. Validates the resolved `Location` of any 301/302/303/307/308 before
   returning the redirect to the child. Refusals surface as
   `FetchError(ErrorClass.BLOCKED)`, which the RPC layer maps to the same
   classified failure an in-process fetch would produce.
4. Caps every decoded response body at `adapters.http.DEFAULT_MAX_BYTES`
   (5 MiB) by streaming the shared client's response in chunks
   (`adapters.http.read_capped`, the same helper the direct fetch path uses)
   and closing the response even on refusal. Oversized bodies, including
   gzip-decoded bodies, raise `FetchError(ErrorClass.NETWORK)` rather than
   crossing RPC. An oversized unfinished stream is rejected without waiting
   for EOF. Subsequent requests on the shared client still succeed.
   Exactly-at-limit bodies pass through intact. This caps accepted cumulative
   decoded body bytes, not transient per-chunk allocation: HTTPX can allocate
   a large decoded chunk before the helper checks its length, and final
   joining/base64 serialization also require additional bounded copies. That
   residual is an accepted risk with a recorded trigger -- see the section
   below.

The child retains redirect authority: it performs method rewriting (303),
credential-header handling, and its own hop limit, exactly as the in-process
path does. Regression: `tests/integration/test_host_http_boundaries.py`
(real loopback origin; asserts the forbidden destination receives no
request).

## Decoded peak memory: declined, with a trigger (issue #358)

`read_capped` bounds the *cumulative decoded* body, which is what decides
whether a response is accepted. It does not bound a single transient
allocation: HTTPX decodes each raw network chunk in one `decode()` call, and
for a high-ratio payload (a "decompression bomb") that call can materialize a
large buffer before the helper ever sees its length. The final `b"".join(...)`
and base64 serialization copy the body again, though those copies are at least
bounded by the 5 MiB accepted total.

**Decision: accepted risk, not a defect left open.** Bounding the transient
allocation means replacing HTTPX's decoder with our own -- a
`decompressobj().decompress(data, max_length)` per chunk, or an `iter_raw()`
feed with a bounded raw chunk size -- and then keeping that correct for gzip,
deflate, brotli and zstd (`Response.iter_bytes` reaches for
`self._get_content_decoder()`, a private API, and its `chunk_size` bounds only
the *output* chunking, not the `decoded` buffer the call already produced).
That is a real cost against a threat this deployment does not carry: every
origin is a feed the operator subscribed to, and the same cap already bounds
the accepted result. It becomes worth paying when sources stop being
operator-chosen, which is the trigger below.

**Re-evaluate when any of these holds** (ADR 0041's convention -- a reviewable
condition, not "after a while"):

1. A source can be added without the operator choosing it (multi-user, or an
   import path that takes an arbitrary URL), so the response is no longer
   operator-vetted input.
2. A response is measured whose decompressed size exceeds the accepted cap by
   more than an order of magnitude -- the amplification observed, not assumed.
3. The process runs under a memory limit tight enough that a single decoded
   chunk can exhaust it (for example a container with a low `memory.max`).

Until then the guard is the cumulative cap, and it is tested on real sockets: a
compressed body that decompresses past 5 MiB is refused, and an unfinished
stream is refused without waiting for EOF
(`tests/integration/test_host_http_boundaries.py`)

## Total elapsed deadline (A09)

Status: **partial**. Direct `fetch`/`post_json` calls using the lifespan-owned
client propagate one monotonic `FetchPolicy.total_timeout` deadline through
HTTP redirects and streamed response reads. `DeadlineTransport` bridges the
synchronous interface to cancellable asynchronous HTTP I/O on its own loop.
Expiry maps to `FetchError(ErrorClass.NETWORK)` through the fetch adapter.
Real loopback tests cover slow headers, slow bodies, cumulative redirects,
connection release after slow-stream expiry, and subsequent client reuse.

The application also uses this transport for reverse-proxied child requests,
but those receive only the transport's default 60-second **per-request** cap.
The child's policy deadline is not propagated across RPC or multiple child
requests. This is not an end-to-end deadline for a plugin collection.

Known limits, recorded rather than hidden:

- The synchronous destination-policy DNS checks run outside cancellable I/O.
  Connection-time resolver work may also outlive cancellation in its resolver
  thread. No hard bound on resolver resource lifetime is claimed.
- A plain injected `httpx.Client` without `DeadlineTransport` still provides
  only phase timeouts. Custom synchronous upload streams are materialized
  before asynchronous I/O; that materialization is not cancellable.
- The child's call budget bounds pipe reads, not a blocking reverse-HTTP
  operation. Its requested policy and remaining call budget still need explicit
  host-side propagation. TLS stalls, pool contention and cancellation under
  concurrent shutdown have not received dedicated acceptance tests.

## Not yet enforced host-side (known gaps)

- **No child-wide elapsed deadline.** Closed (A09): the outer RPC call budget
  now reaches the reverse-call servicing thread, which answers the child a
  classified network error on expiry instead of extending the call. What
  remains unbounded is a single abandoned servicing thread's own lifetime
  after that answer — it finishes or dies in the background either way.
- **DNS re-resolution window.** `check_destination` resolves the hostname
  once; the connection re-resolves. Textbook DNS rebinding remains possible;
  the recorded threat model (ADR 0016) is a leaked token probing an internal
  network, not a hostile resolver racing the socket. Same limitation as the
  host-initiated fetch path.

## Tailnet exemption and refusal detail (ADR 0027)

- Tailscale's CGNAT (`100.64.0.0/10`) and ULA (`fd7a:115c:a1e0::/48`) ranges
  are exempt from the private-address refusal: the instance is
  tailnet-hosted and its internal services (CookieCloud, translate gateway)
  are tailnet services. Guarding them broke every self-hosted dependency the
  moment the guard reached sandboxed egress (issue #1). Other private
  ranges — RFC1918, loopback, link-local, metadata endpoints, and ULAs
  outside Tailscale's allocation — keep the refusal.
- A refusal's detail names its reason and the destination:
  `private-address fetch refused for <url>` vs
  `unresolvable-host fetch refused for <url>`. The old single message sent
  operators debugging DNS when the answer was the guard, and the
  sandbox-egress variant carried no URL at all.

## Deliberate deployment exceptions

- `PEARSS_ALLOW_PRIVATE_FETCH=1` turns the private-address refusal off for
  both host-initiated and proxied egress. The variable is on the child env
  allowlist (`app.plugin_runtime.child_environment`) so the operator's
  decision reaches the guard in both processes; see the allowlist comment in
  `app/plugin_runtime.py`.
- `PEARSS_TEST_DNS` pins resolver answers for hermetic tests, including
  inside sandboxed children; it names no credential.

## Regression evidence

- Red (before fix): the shared client followed `/redirect` into `/forbidden`
  itself; the test failed with `DID NOT RAISE FetchError` and the handler
  log shows the forbidden path was visited.
- Green (after fix): `FetchError(BLOCKED)` raised host-side;
  `visited == ["/redirect"]` proves the destination received no request.
- Injection verified in an isolated worktree: restoring the original
  `HostClientTransport` reproduced the missing refusal (one failed test).
  Restoring the fix passed the same test. No shared source was mutated.
- Body cap red: a 5 MiB + 1 response returned successfully rather than raising
  `FetchError`. The fix passes seven new real-socket cases: oversized plain,
  gzip and unfinished bodies on both plain HTTPX and the lifespan transport,
  plus exact-limit preservation. The plain HTTPX variant limits the pool to
  one connection; the lifespan transport owns a separate internal pool, so
  that variant proves continued client usability, not one-slot pool release.
- In an isolated source copy, replacing the capped read with `response.read()`
  made all six oversized cases fail; restoring it passed all six. The unfinished
  fixture withholds EOF until after the assertion, detecting eager buffering.
- Full `nix develop --command just ci` passed: 918 unit/fixture, 512 integration,
  57 E2E and 3 contract tests (1,490 total), plus Ruff, mypy, import boundaries
  and migration roundtrip. These new cases exercise the host HTTP boundary,
  not the complete child/RPC/API journey; B02 remains open.
