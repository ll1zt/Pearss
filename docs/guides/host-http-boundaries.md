# Host-side HTTP egress boundaries

Status: partial. This document records what the host enforces on
reverse-proxied plugin HTTP today, what it does not, and why. It exists so
the gap is a recorded decision surface, not an assumption (problem review
A05; SSRF policy in ADR 0016; sandbox trust model in ADR 0023).

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

The child retains redirect authority: it performs method rewriting (303),
credential-header handling, and its own hop limit, exactly as the in-process
path does. Regression: `tests/integration/test_host_http_boundaries.py`
(real loopback origin; asserts the forbidden destination receives no
request).

## Total elapsed deadline (A09)

Direct and proxied fetches now enforce the `FetchPolicy.total_timeout`
budget as an elapsed wall-clock deadline across connect, headers, redirects
and streaming. `adapters.deadline_transport.DeadlineTransport` owns an async
connection pool on a dedicated event-loop thread and caps every blocking
operation by the remaining budget; expiry cancels the pending I/O, surfaces
as `FetchError(ErrorClass.NETWORK)` through the existing timeout mapping,
and releases the socket. The lifespan client (`app.main`) uses it, and
`tests/integration/test_http_deadline.py` covers a slow body, slow headers,
cumulative redirects and post-expiry reuse on real loopback sockets.

Known limits, recorded rather than hidden:

- DNS resolution is not inside the deadline: a stalled `getaddrinfo` can
  still exceed the budget before the socket phase starts.
- A proxied fetch is one host-side RPC; the child's call budget bounds the
  pipe read between calls, not each blocking reverse-HTTP operation. Each
  host-side request is capped by the transport's default total budget, but
  a policy tuned below the RPC budget is not propagated from the child.

## Not yet enforced host-side (known gaps)

- **No host-side response-size cap.** The child-side size checks in
  `adapters.http._read_capped` run after the host has buffered the full
  response into an RPC message (`protocol.MAX_MESSAGE_BYTES` bounds the
  line, not the response). That RPC cap does not bound the memory already
  allocated while buffering the origin response. Enforcing `max_bytes` during host-side streaming
  before base64 RPC serialization is the remaining half of A05. Not
  implemented here.
- **DNS re-resolution window.** `check_destination` resolves the hostname
  once; the connection re-resolves. Textbook DNS rebinding remains possible;
  the recorded threat model (ADR 0016) is a leaked token probing an internal
  network, not a hostile resolver racing the socket. Same limitation as the
  host-initiated fetch path.

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
