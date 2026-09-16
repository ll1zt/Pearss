# Pearss deep review — problems to fix

Reviewed: 2026-09-17 · Baseline: `8502cc5` · Scope: correctness, behavior-first testing, and long-term maintainability.

## Repair ledger — 2026-09-17

Branch: `fix/deep-review-findings`. The findings below describe the original
`8502cc5` review baseline, not the current behavior of repaired paths.

| Finding | Status | Commit |
|---|---|---|
| A01 folder-scoped bulk read | Fixed | `d3f8a33` |
| A02 revision gate and publication race | Fixed | `8437193` |
| A03 shipped collector retention | Fixed | `508f9c9` |
| A04 partial processing configuration | Fixed | `5a6a9c3` |
| A05 host-proxied HTTP | Partial: redirect destination checks only | `69f96c3` |
| A06 installed plugin restart | Fixed for approved manifests; unavailable records remain stored but absent from runtime list | `379ea62` |
| A09 total fetch deadline | Partial: cancellable direct HTTP I/O; DNS and end-to-end child budgets remain open | `744eefa` |
| A08 concurrent body storage | Fixed | `2142c29` |
| A16 failure backoff floor | Fixed | `6e6b130` |
| A17 IPv6 canonicalization | Fixed for new normalization; no stored-data migration | `5b9651a` |
| A18 retained RSS supplementary Unicode | Fixed | `0b16306` |
| B01 real scheduler maintenance assertions | Fixed | `fd74926` |
| B04 attributable audit/redaction assertions | Fixed | `acd6fc3` |
| B05 effective markers and empty-suite failure | Fixed | `09c4846` |

**Still open:** A05 host-side body bounds; A07; A09 remaining deadline
boundaries; A10–A15;
B02/B03/B06/B07; C01–C10. Test improvements reduce specific debt but are
not a completed registry/Reader architecture refactor. A10/A11 were inspected
again without implementation. Earlier delegation attempts for A06/A09/A12
failed due to provider authentication/rate limits. A09 was partially repaired
directly (`744eefa`): direct slow HTTP streams are cancellable with the new
transport, but DNS and child-side budget propagation remain open. See
`docs/guides/host-http-boundaries.md` for the precise boundary.

**A09 verification:** full `just ci` passed: 918 unit/fixture, 505 integration,
54 E2E and 3 contract tests (**1,480 total**), plus formatting, Ruff, mypy
(198 files), import boundaries and migration roundtrip. The original slow-body
regression failed because the 0.2-second policy did not finish within the
1-second test safety window. Slow body/header disconnect and client reuse,
and cumulative redirects, pass with the new transport. An isolated cancellation
bypass made all three deadline cases fail; restoring the transport passed all three.

**A06 verification:** full `nix develop --command just ci` and a separate full
`uv run pytest -q` passed **1,483 tests**: 918 unit/fixture, 505 integration,
57 E2E, 3 contract; mypy checked 199 files. The original fresh-process test
failed with RSS fallback parsing instead of installed-plugin collection.
Three real-process E2E scenarios now cover restart, disabled status, uninstall,
disk-only directories, broken code, missing manifests and changed credential
requests. In an isolated source copy, bypassing the checksum comparison caused
unapproved collection (HTTP 200 and one inserted item instead of rejection);
the test failed, then passed after restoring the guard. See ADR 0025 for
startup validation, runtime-list limitations and manifest-only checksums.

**Combined verification:** `nix develop --command just ci` passed with exit 0
on the stabilized combined source/test changes: 918 unit/fixture, 502 integration,
54 E2E and 3 contract tests (**1,477 total**). Formatting, Ruff, mypy (196 files),
import boundaries and migration upgrade/downgrade/upgrade passed. The initial
combined run found a test fixture typing error; it was fixed before this result.

**Regression evidence:** A01–A04 have red-before-green and injected-regression
checks recorded during repair. A05/A08/A16/A17/A18 were additionally verified in
an isolated worktree by restoring original implementations: respectively 1, 1,
4, 5 and 2 expected failing cases. Restoring fixes passed all 22 focused cases.
B01/B04/B05 mutation checks reject missing maintenance, absent audit rows,
missing redaction, wrong rejection outcomes and disabled marker enforcement.
Temporary shared-tree probes earlier in the session were restored; subsequent
probes used isolated copies. No real-client NNW/Read You manual smoke, new
coverage measurement or complete mutation campaign was performed.

See `docs/guides/host-http-boundaries.md` for the remaining host egress gaps
and `docs/guides/review-edge-cases.md` for edge-case behavior and limitations.

## Executive assessment

**The project has strong foundations, but its cross-boundary guarantees are weaker than its green suite suggests.** The most urgent problems involve folder-scoped writes, processing visibility, plugin lifecycle, and host-proxied HTTP. These are specific repair targets, not reasons for a rewrite.

The tests predominantly assert real behavior: migrated SQLite, real HTTP/MCP E2E journeys, literal persistence identifiers, and refusal-without-mutation checks. However, lifecycle transitions, concurrent execution, and partial-update semantics are underrepresented. A small number of implementation-coupled or permissive tests give misleading reassurance precisely around these boundaries.

**Rot outlook:** not actively rotting, but accumulating concentrated application-layer debt. Over two years, Reader-client exceptions and plugin process ownership will hurt before the pure domain model. Automatic quality gates and explicit lifecycle ownership are more valuable than indiscriminate file splitting or more function-by-function tests.

## Verification and limits

- Four parallel reviews covered core/adapters, application logic, tests, and maintainability; important findings were cross-checked against their surrounding source.
- Review agents ran isolated reproductions with temporary databases/storage and local HTTP servers. Findings below distinguish those reproductions from source-inspected risks. Those diagnostic probes were not added as committed regression tests.
- **`nix develop --command just ci` passed, exit 0:**
  - Formatting, Ruff, mypy (185 source files), and import-linter passed.
  - Unit/fixture: **891 passed**.
  - Integration: **492 passed**.
  - E2E: **37 passed**.
  - Contract: **3 passed**.
  - Alembic upgrade → downgrade → upgrade passed against a throwaway database.
  - Total: **1,423 passing tests**.
- No production code, assertions, or accepted ADRs were changed. This report is the deliverable.
- This is not an exhaustive security certification, dependency vulnerability audit, or real-device NNW/Read You acceptance run. No fresh coverage or mutation campaign was run. A passing snapshot cannot establish whether tests historically preceded implementation.
- Priorities: **P1** = fix before relying on the affected workflow; **P2** = normal-priority correctness/resilience work; **P3** = limited exposure or longer-term maintenance. No universal P0 emergency was established.

## A. Correctness and operational problems

### A01 · P1 · Folder-scoped “mark all read” changes the entire library

**Evidence:** `src/app/reader_routes.py:1135-1150`; `src/adapters/repository/reader.py:314-315`. **Reproduced through HTTP.**

`mark_all_as_read` passes `parse_feed_stream_id(s)` directly to the repository. A label stream such as `user/-/label/Work` is not a feed, so it becomes `None`; the repository interprets that as no source restriction. The reproduction marked News items read while requesting only Work. Unknown and unsupported non-feed scopes also risk collapsing to a global operation.

**Repair:** resolve and validate the stream scope before writing. Only the recognized whole-library stream may select the whole library; unsupported scopes must not silently widen the update.

**Acceptance — user marks one folder read:** Given unread items in Work and News, when the user marks Work read, then only eligible Work items change. A separate scenario submits an unsupported stream and asserts an exact rejection plus unchanged read state.

### A02 · P1 · Updating a ready item's body does not restore the processing gate

**Evidence:** `src/core/normalize.py:535-548`; `src/adapters/repository/items.py:109-124`. **Reproduced through normalization/storage and the retained RSS renderer.**

Every changed, non-published identity match becomes `UPDATE_PENDING`, including `ready`. `_update_pending` replaces body/hash fields without changing publication status. In the reproduction, revised content remained `ready` while its required enrichment was queued and was rendered before that processing completed.

This is also a current visibility concern: ready rows are eligible for Reader reads. However, the concrete rendering reproduction used `render_feed`, not a real reader client; public RSS routes were retired in ADR 0016.

**Repair:** make content replacement, enrichment invalidation/requeueing, and publication-state reset a coherent transition. Use a database status guard so a concurrently published row cannot be overwritten.

**Acceptance — user never receives an unprocessed revision:** Given a ready but unpublished item requiring processing, when collection revises its body, then it is absent from current Reader/body output until processing of that revision succeeds. A separate concurrency case must preserve already-published content.

### A03 · P1 · Plugin import removes shipped sandboxed collectors

**Evidence:** `src/app/collector_registry.py:256,415-437`. **Reproduced through HTTP.**

Shipped sandboxed collectors receive `trust="third-party"`, but `_rebuild_installed` preserves shipped registrations with `r.trust == "builtin"`. Importing a plugin therefore drops Telegram, Twitter, Zhihu, and Zsxq. The observed list changed from five shipped collectors to only RSS plus the imported plugin.

**Repair:** distinguish installation provenance from trust/isolation. Preserve shipped registrations using their declared identities or an explicit provenance field.

**Acceptance — user installs a plugin without losing existing sources:** Given working shipped collectors, when a valid plugin is imported, then all shipped collectors remain listed and usable, and the new collector is usable too. Listing alone is insufficient: collect from an existing sandboxed source.

### A04 · P1 · A language-only PATCH silently deletes the processing chain

**Evidence:** `src/app/sources_routes.py:469-498`. **Reproduced through HTTP and persistence.**

The handler enters its paired-field branch when either `pipeline_chain` or `target_lang` was supplied, but validates and writes both values from the request. Omission becomes null. Updating only `target_lang` on a configured translation source persisted `pipeline_chain=null`; a chain-only update can clear the language or fail instead of preserving the stored value.

**Repair:** merge explicitly supplied fields with current configuration before validating the pair, then persist only intended changes. Keep omitted distinct from explicitly null.

**Acceptance — user changes only the translation language:** Given an existing chain and language, when PATCH changes only the language, then the chain is byte-for-byte/semantically unchanged and newly collected items still require processing. Add separate chain-only, explicit-clear, and invalid-combination scenarios.

### A05 · P1 · Reverse-proxied plugin HTTP lacks host-side redirect and size enforcement

**Evidence:** `src/app/plugin_runtime.py:531-565`; `src/app/main.py:177,222-238`; `src/adapters/http.py:309-358`. **Source-inspected; no live private-network exploit attempted.**

`HostClientTransport` delegates to a client configured to follow redirects and buffers the response. The SSRF checks live in the normal fetch path, not the shared client. A public origin's redirect can therefore be followed by the host before the child can inspect the destination. Child-side size checks likewise occur after host buffering.

The transport docstring acknowledges reliance on an upstream proxy/firewall; the lifespan comment nevertheless says this wiring supplies the normal SSRF and size guards. No equivalent policy wrapper is installed there. ADR 0023's operator-trust decision does not make unexpected origin redirects or oversized responses harmless.

**Repair:** enforce destination policy on every host-side hop and cap streamed bytes before buffering/RPC serialization. Preserve permitted credential/header and compression behavior. Document any deliberate deployment-level exception explicitly.

**Acceptance — user collection cannot redirect into forbidden destinations:** Use a controlled local origin and explicit test policy to return a forbidden redirect; assert the destination receives no request. Separately return oversized/compressed content and assert a bounded classified failure through the real child/host transport.

### A06 · P1 · Installed plugins are not restored after process restart

**Evidence:** `src/app/collector_registry.py:263-266,384,399-400`; `src/app/main.py:215-238`. **Source-inspected.**

Installed registrations come from process-local `_INSTALLED_PLUGINS`. Startup synchronizes registry entries into the plugin table but does not reconstruct the installed registry from durable installation records. Files and database entries can survive while executable registrations disappear. Replaying a cached import success is not a reliable repair because it need not execute installation again.

**Repair:** restore explicitly installed plugins from persisted records at startup, validating files and preserving enabled/disabled status. Do not replace explicit installation with arbitrary directory auto-import.

**Acceptance — user restarts without reinstalling:** Given an imported plugin and source, when a fresh OS process starts against the same database/plugin directory, then the plugin remains correctly listed and usable. In-process app reconstruction is insufficient because module globals may survive.

### A07 · P1 · An escaping infrastructure error permanently ends a background worker

**Evidence:** `src/app/scheduler.py:270-271,415-431`; `src/app/enrichment_worker.py:364-372`. **Source-inspected.**

Neither long-running loop has recovery/supervision around an iteration. Database enumeration, claiming, maintenance, or result-commit errors can escape the narrower per-source handling. The task dies while HTTP service remains available; awaiting the failed task during shutdown can also interrupt subsequent cleanup.

**Repair:** define bounded recovery for operational failures, keep cancellation distinct, expose worker liveness/failure in operational health, and make shutdown cleanup independent of one task's failure. Do not silently catch programming errors forever.

**Acceptance — user collection resumes after a transient storage failure:** Introduce a controlled real storage failure, verify degraded worker health, restore storage, and assert a later item is collected/processed without restarting the service. No assertion should depend on how many times an internal method ran.

### A08 · P2 · Concurrent identical body writes race over one temporary pathname

**Evidence:** `src/adapters/filestore.py:58-69`. **Reproduced with concurrent real file writes.**

All writers of an unseen digest use the same `<digest>.tmp`. One writer renames it while another still expects it to exist. Sixteen simultaneous identical writes yielded 13 successes and three `FileNotFoundError`s in the diagnostic run; exact counts are timing-dependent, not a test contract.

**Repair:** allocate a unique temporary file per writer in the destination directory, atomically install it, and clean up only that writer's temporary file.

**Acceptance — user imports identical content concurrently:** Given an empty store, when multiple writes of the same body overlap, then every operation succeeds with the same reference, the stored bytes are exact, and no temporary files remain. A deterministic barrier is preferable to sleeps.

### A09 · P2 · `total_timeout` is not a total HTTP deadline

**Evidence:** `src/adapters/http.py:290-300,450-462`. **Reproduced with a real local streaming server.**

`limits.total_timeout` configures HTTPX's pool-acquisition timeout. Read timeout limits inactivity, not total duration. A server sending periodic chunks can keep a fetch alive beyond its advertised budget. A four-byte response completed in approximately 0.533 seconds despite `total_timeout=0.05`.

**Repair:** implement a total elapsed deadline across connection, redirects, and streaming; keep inactivity timeouts too. Expiry must retain the existing `FetchError(ErrorClass.NETWORK)` contract.

**Acceptance — user slow-drip collection ends within its budget:** Given an origin that delivers frequent small chunks without finishing, when collection starts with a short total deadline, then it fails within a bounded tolerance and releases its resources. Use events to coordinate the real server, not an arbitrary sleep-and-pray test.

### A10 · P2 · Enrichment claims admit a freshly running row

**Evidence:** `src/app/enrichment_worker.py:141-169,197` onward. **Source-inspected concurrency risk.**

Selection distinguishes queued rows from stale running rows, but the UPDATE matches either `queued` or any `running` status. Two sweepers can select the same row and both claim it after the first commits. Duplicate provider work and competing completion writes follow. A single worker's sweep lock reduces normal exposure but does not establish a cross-process guarantee.

**Repair:** atomically claim using full eligibility or an observed-version predicate; fence completion with an ownership token. Coordinate this with the execution deadline below.

**Acceptance — user pays for one concurrent processing attempt:** Given one queued row and two overlapping claimers, when both attempt processing, then only one provider operation is admitted and one owned result persists. Separately exercise stale-lease recovery and reject late completion from its former owner.

### A11 · P2 · Processor execution has no enforced total deadline

**Evidence:** `src/app/enrichment_worker.py:190-193,255-265,369-372`. **Source-inspected.**

`future.result()` waits without a deadline. The nominal call timeout governs reclamation rather than bounding `processor.process()`. Slow-drip HTTP, multiple chunks, and model fallback can exceed that window. One hung processor stalls the sweep, while another process can consider its work reclaimable.

**Repair:** establish a genuinely cancellable execution boundary and lease policy. Adding only `Future.result(timeout=...)` does not terminate a Python thread, and executor shutdown may still wait.

**Acceptance — user processing makes progress after a stalled job:** Given one processor that never completes and a later eligible item, when the first exhausts its budget, then its failure is recorded and later work progresses without unsafe concurrent completion or blocked shutdown.

### A12 · P2 · Idempotency does not atomically cover mutation and replay

**Evidence:** `src/app/api_conventions.py:247-283`; `src/app/sources_routes.py:515-531`; `src/adapters/repository/idempotency.py:41-60`. **Source-inspected.**

Replay lookup, business mutation, and stored response use separate transactions. Concurrent same-key requests can both see no record and execute; a crash after mutation but before `remember` leaves a replay gap. Existing records may be overwritten. No request fingerprint distinguishes an identical retry from a different request using the same key.

Sequential retries generally return the existing response: the overwrite issue is not proof that every ordinary different-body retry overwrites it. The defects concern concurrency/crash composition and an undefined mismatch contract.

**Repair:** reserve a scoped key atomically, bind it to a normalized request fingerprint, and define in-progress/recovery behavior. Couple database mutation and durable outcome where possible; nontransactional external effects need an explicit recovery protocol.

**Acceptance — user retries cannot duplicate work:** Separate scenarios cover identical sequential replay, two concurrent requests, same-key/different-body conflict, and recovery after mutation before response storage. Assert exact response semantics and persistent/external effects, not merely one source row.

### A13 · P2 · Plugin uninstall/reload omit idempotency handling

**Evidence:** `src/app/plugins_routes.py:278-283,346-351`. **Source-inspected.**

These write handlers require write authorization but lack the validated-key/replay handling used by other Core API writes. Retried uninstall changes success to 404; retried reload repeats child replacement.

**Repair:** apply the defined idempotency contract to these lifecycle operations, coordinated with A12 rather than copying its race-prone composition.

**Acceptance — user safely retries a lifecycle write:** Given a completed uninstall/reload, when an identical key/request is retried, then its result replays without a second lifecycle effect. Missing keys and mismatched requests have exact refusal outcomes.

### A14 · P2 · Reader continuation offsets skip items when membership changes

**Evidence:** `src/app/reader_routes.py:631-650`. **Source-inspected.**

A continuation is an offset into the current filtered set, not a stable boundary or snapshot. If first-page unread items become read before page two, the list shrinks and the old offset skips previously unseen items. Earlier deletions also skip items; new insertions can duplicate results. The recent cap fix prevents an endless continuation but not this problem.

**Repair:** specify continuation semantics and use a stable ordering boundary or bounded membership snapshot, scoped to the query.

**Acceptance — user sync does not lose unread items between pages:** Given more unread items than fit in a page, when the first page is fetched and its items become read, then continuing still returns every remaining eligible item. Exercise insertion and deletion separately.

### A15 · P2 · Audit transactions can block the async event loop

**Evidence:** `src/app/middleware.py:134-166`. **Source-inspected.**

The async middleware calls `_record` directly; `_record` performs synchronous SQLAlchemy work and commits. Under SQLite writer contention, that can block the shared event loop for the database wait timeout, delaying unrelated requests and worker scheduling.

**Repair:** execute the complete transaction in a worker thread or another bounded audit mechanism, with explicit completion/failure and overload semantics. Keep the session owned by its execution context.

**Acceptance — user reads remain responsive during audit contention:** Hold a real database writer lock while an audited request finishes, then verify an unrelated non-writing request remains responsive within a bounded budget. Verify audit success/failure is still observable.

### A16 · P2 · Failure backoff shortens long fixed schedules

**Evidence:** `src/core/scheduling.py:79-85`. **Reproduced directly against the pure function.**

The failed fixed interval is capped at 24 hours even if the configured interval is longer. A seven-day policy returned 604800 seconds when healthy and 86400 after one failure: failure makes fetching seven times more frequent.

**Repair:** ensure backoff never shortens the configured interval; define the cap relative to the base where necessary.

**Acceptance — user weekly sources do not become daily on failure:** Given a weekly fixed policy, when a failure is recorded, then the next interval is at least the configured week. Pin exact values for the selected cap policy and boundary cases.

### A17 · P2 · URL canonicalization corrupts valid IPv6 literals

**Evidence:** `src/core/normalize.py:104-112`. **Reproduced directly against the pure function.**

`urlsplit().hostname` strips IPv6 brackets; reconstruction does not restore them. `https://[2001:4860:4860::8888]/feed` becomes `https://2001:4860:4860::8888/feed`. Parsing the output again raises a port-conversion error.

**Repair:** bracket IPv6 literals before appending any port; preserve the normal default-port/canonicalization rules.

**Acceptance — user IPv6 URLs remain valid:** Test exact canonical URLs with default and non-default ports, plus idempotent re-canonicalization. This is appropriately a pure codec test, not an HTTP E2E test.

### A18 · P3 · Retained RSS renderer deletes valid supplementary Unicode

**Evidence:** `src/adapters/rss_out.py:139-147`; exposure qualification in `docs/adr/0016-public-exposure-hardening.md:29-31`. **Reproduced through `render_feed` and XML parsing.**

The XML character filter only accepts BMP ranges. Body `A😀𠀀B` becomes `AB`, although XML 1.0 permits U+10000–U+10FFFF. This is real content loss in the retained renderer, **not evidence that the current Reader API deletes these characters**; the public RSS route was removed.

**Repair:** include the supplementary XML-valid range while continuing to remove forbidden controls/surrogates.

**Acceptance — user exported RSS preserves valid text:** Given supplementary CJK and emoji alongside prohibited controls, when a feed is rendered and parsed, then valid characters survive exactly and forbidden characters do not. A renderer-to-parser boundary test is sufficient; do not resurrect a removed public route solely to call it E2E.

## B. Behavior-first test audit

### What is already strong

- No `unittest.mock` imports or `assert_called`/`assert_awaited` assertions were found in the scanned suite. This does not mean there are no fakes or internal substitutions.
- Database integration uses real Alembic-migrated SQLite. In-process ASGI integration tests exercise routing without a real socket; E2E tests add real uvicorn sockets and MCP clients. These provide complementary evidence, not identical transport coverage.
- Persistence IDs, GUIDs, and hashes are pinned to literals with migration warnings.
- Failure-path tests often check both refusal and unchanged state; pending visibility tests establish that seeded data actually exists before asserting it cannot be read.
- Fake clocks and event gates are present. Structural guards prohibit several common assertion/mock mistakes.
- Client journeys cover NNW and Read You protocol differences. Preserve those business assertions when refactoring.

### B01 · P2 · Scheduler loop test substitutes internal behavior and asserts call order

**Evidence:** `tests/integration/test_scheduler_maintenance.py:339-393`.

The test replaces `tick_once`, `_reap_orphaned_runs_if_due`, and `_prune_audit_if_due` with list appenders and asserts `called == ["tick", "reap", "prune"] * 2`. Contrary to its docstring, it replaces more than sleep. Correct refactoring can break it; broken maintenance implementations can pass it.

**Replace with:** user-visible maintenance outcomes using seeded orphaned runs and expired audit rows, the existing fake clock, and the real loop. Assert persisted run status and row retention, not helper names/order. Failure recovery needs its own scenario (A07).

### B02 · P2 · Lifespan transport test checks object identity, not safe egress

**Evidence:** `tests/integration/test_app.py:720-747`.

`transport._client is app.state.http_client` couples assertions to private fields. It does not establish that child HTTP works or that policy survives the boundary; A05 can coexist with this green assertion.

**Replace with:** a real sandboxed collector fetching a controlled origin through the host, plus redirect refusal, response-size, and compression scenarios. Fixture construction may change under refactoring; protocol outcomes should not.

### B03 · P2 · Idempotency mismatch test accepts four incompatible responses

**Evidence:** `tests/e2e/test_failure_paths.py:416-458`.

The stored-source assertions are useful, but `second.status_code in (200, 201, 409, 422)` leaves the client contract undefined. A caller cannot distinguish a deliberate replay from conflict or validation failure based on this test.

**Repair:** document one mismatch policy and pin its exact status/error body (409 is a reasonable default), original persisted data, and original replay result. Add the concurrent/crash cases in A12. Do not claim current sequential retries necessarily overwrite records.

### B04 · P2 · Redaction tests pass when nothing was audited

**Evidence:** `tests/integration/test_redaction_review.py:118-119,124-135`.

`if blob: assert "<redacted>" in blob` passes on an empty audit table. `test_rejected_write_is_still_audited` only asserts that a token is absent, not that a rejected-write row exists. The neighboring test at line 101 already demonstrates the required nonempty-evidence guard.

**Repair:** pin a matching audit row, its rejected outcome/correlation where applicable, and the redacted credential marker before checking secret absence. Distinguish authenticated validation rejection from deliberately unaudited perimeter refusals (C01).

### B05 · P2 · Marker guard counts decorators instead of checking effective test levels

**Evidence:** `tests/unit/test_structure_guards.py:511-529`; `justfile:46-58`.

The guard counts every `@pytest.mark.*` occurrence against top-level test definitions. Two decorators on one test (for example `integration` plus `asyncio`) can compensate for another test having none. Non-level decorators count too, and class-based tests are not represented by the top-level regex. `_test` additionally treats zero collected tests as success, a leftover skeleton policy.

**Repair:** inspect collected pytest items and require an effective recognized execution-level marker for each, including inherited module/class markers. Make required suites fail on zero selected tests. Add guard cases for mixed marking, asyncio/parametrize-only marking, and class-based tests.

### B06 · P2 · Coverage and fake-contract guarantees are weaker than the written methodology

**Evidence:** `justfile:99-119,232-238`; external `httpx.MockTransport` fixtures throughout the test suite.

`just ci` does not invoke coverage or coverage-diff. Both coverage recipes omit the E2E marker, so an E2E-only behavioral regression can be invisible to the changed-line coverage diagnostic. No fresh branch-coverage result was measured here; the skill's 90% branch target is not established by the 1,423 passing tests.

MockTransport is acceptable for narrowly scoped external protocol tests, but it is not real HTTP and is not automatically contract-validated. Recorded fixtures and real transport journeys must substantiate each fake's assumptions, especially redirects, compression, authentication, and streaming. The audit did not establish contract validation for every external fake.

**Repair:** include the relevant real E2E execution in coverage accounting, wire the chosen changed-line check into the actual merge workflow, and document fake provenance/contract evidence. Investigate uncovered failure paths before chasing a global percentage.

### B07 · P3 · Residual weak assertions and naming/process gaps

**Evidence:** `tests/integration/test_scheduler.py:184,839` (timestamp presence); scattered internal substitutions in scheduler/plugin-runtime tests.

Presence-only assertions can miss a wrong timestamp or wrong branch. Some internal substitutions are legitimate controlled fault injection, but should not become the default business-test setup. Many tests are descriptive without consistently using user-prefixed Given/When/Then scenarios. Static review cannot verify historical red-before-green execution.

**Repair:** pin exact values with injected clocks where meaningful; distinguish fault injection from mocking normal internal behavior. For each A-series fix, first write its user scenario and a test that fails for the diagnosed reason. For security/invariant repairs, perform the required injected-regression check and restore source afterward. Do not weaken assertions during pure refactors or rewrite every test name for cosmetic compliance.

## C. Contracts and maintainability risks

### C01 · P2 · Audit coverage has conflicting guarantees

**Evidence:** `src/app/main.py:367-388`; `src/app/middleware.py:114-118`; `tests/e2e/test_failure_paths.py` authentication-refusal scenarios; `docs/adr/0016-public-exposure-hardening.md:15-18,34-36`.

EarlyAuth/body/rate-limit middleware sit outside correlation/auditing. An unauthenticated POST returned 401 and produced zero audit rows in the reproduction; tests deliberately expect this. Other comments and the broad repository rules promise refused writes/authentication failures are audited.

This is **not simply an accidental middleware-order bug**. ADR 0016 identifies audit flooding as a resource-exhaustion issue. Moving synchronous audit writes outside every rejection can reintroduce that problem.

**Repair:** adopt an explicit bounded perimeter-security event policy (for example sampled/aggregated denial events), distinguish it from authenticated business auditing, and reconcile rules/comments/tests via a new ADR if semantics change. Test both bounded resource use and useful refusal evidence.

### C02 · P2 · Reader POST authentication contradicts the blanket rule

**Evidence:** `AGENTS.md:121`; `src/app/reader_routes.py:150-176,707-710`.

POST read endpoints accept header authentication because NetNewsWire uses it; actual state-changing handlers remain form-authenticated. The blanket rule says every POST must use form `T`. This is a deliberate compatibility exception, not an unauthenticated-write exploit.

**Repair:** formally document the narrow POST-read exception or revise compatibility intentionally. Keep exact negative tests for GET credentials, POST read credentials, and POST mutation credentials. Do not break NNW merely to satisfy the outdated blanket wording.

### C03 · P2 · Quality gates have no checked-in automatic workflow

**Evidence:** `TODO.md:379-382`; no `.forgejo`/`.github` workflow in this checkout; `justfile:232-238`.

The repository records Forgejo runner/workflow work as pending. Strong local gates cannot prevent merges if execution depends on memory. External host-side enforcement may exist, but was not verified by this review.

**Repair:** connect a thin required workflow to existing Nix/just commands when the runner is available; avoid copying gate logic into YAML. Explicitly decide where changed-line coverage runs.

### C04 · P2 · Plugin lifecycle ownership is hidden in module globals

**Evidence:** `src/app/collector_registry.py:330-384`; `src/app/plugins_routes.py:221`; `src/app/plugin_runtime.py:90-121,707,734`.

Registry, installed plugins, child processes, and transport ownership live in mutable globals; routes access private declarations. Generic runtime code also knows credential prefixes and Twitter walking behavior. This makes restart, reload, test isolation, and multiple app instances difficult to reason about; A03 and A06 are concrete consequences.

**Repair:** after fixing behavior, introduce an application-owned registry/runtime object with explicit public lifecycle operations. Express collector-specific capabilities through declared interfaces without weakening host credential policy. Preserve the new lifecycle E2E assertions during extraction.

### C05 · P2 · Reader operations have too many responsibilities in one module

**Evidence:** `src/app/reader_routes.py` (1,223 lines); `src/app/sources_routes.py` (907 lines); `src/app/api_conventions.py:232-283`.

Reader routes combine auth, subscriptions, folder behavior, stream selection, rendering, read-state changes, and credential configuration. Source routes also assemble several write workflows. Shared helpers exist, but correct transaction/replay composition remains each caller's responsibility.

**Repair:** extract cohesive application operations for subscriptions, stream selection/rendering, and lifecycle writes once their behavior is pinned. Keep Reader protocol forms at the compatibility boundary and domain decisions in their appropriate layer. Do not split files solely to hit a line-count limit or introduce a generic route framework.

### C06 · P2 · Apparently authoritative core helpers are not production enforcement points

**Evidence:** `TODO.md:330-352` records unused `is_content_frozen`, `is_terminal`, `is_duplicate_of`, and `next_run_status`.

A maintainer can update the apparent domain rule and its tests while real behavior elsewhere does not change. Surviving mutants in unused code then distract from missing assertions on executed paths.

**Repair:** decide per helper whether to wire it into enforcement, remove it, or identify a genuine public-library role. Prefer one real enforcement point over a tested but unused mirror.

### C07 · P2 · Architecture/status documentation no longer reliably maps implementation

**Evidence:** `architecture-spec.md:311,348,424,429`; `AGENTS.md:58`; `justfile:71-79,165-171,254-273`.

The architecture describes E2E as unwired/Compose-dependent, backup as unimplemented, and implemented commands as placeholders. It names an obsolete E2E command; the current recipe is `just test-e2e`. AGENTS also labels `just run` unwired despite its implementation. These are onboarding hazards, not merely cosmetic inaccuracies.

**Repair:** reconcile active status tables against code and `justfile`; retain historical plans in ADRs/history rather than presenting them as current behavior. Add command-reference validation for executable documentation examples.

### C08 · P3 · Deployment paths can drift

**Evidence:** `deploy/pearss.service:23,36-37`; `nix/package.nix`; `nix/module.nix`.

The user-service path hardcodes a checkout location and NixOS executable locations while a packaged NixOS deployment also exists. A different checkout layout needs edits, and both startup paths need maintenance.

**Repair:** identify the canonical deployment path and parameterize the alternate path's checkout location. Test configuration rendering/startup assumptions, not a developer's local home layout.

### C09 · P3 · Multi-tenancy is reserved in schema, not carried through execution

**Evidence:** `src/app/reader_routes.py:45`; `src/app/pipeline.py:54`; `src/app/scheduler.py:62`; `src/app/enrichment_worker.py:41`; `src/adapters/repository/enrichment.py:27`; `src/app/plugins_routes.py:69,97,158,259`.

Independent `default` tenant constants/arguments appear across requests, background work, and persistence. This is acceptable for the stated single-user scope, but a schema column is not an end-to-end isolation guarantee.

**Repair:** defer broad changes until multi-user scope is real. Before that expansion, propagate explicit request/job tenant context and add cross-tenant negative E2E scenarios. Replacing duplicates with one global constant would not solve isolation.

### C10 · P3 · Cancellation requires execution semantics, not another endpoint

**Evidence:** `TODO.md:398-408`; worker execution concerns in A07/A11.

The ledger already distinguishes a cancelled lock-acquisition attempt from interrupting running synchronous work. Longer collection/processing chains will make that distinction increasingly user-visible.

**Repair:** define run ownership, cooperative checkpoints or process cancellation, and terminal-state transitions before exposing cancellation controls. Keep queued-work removal distinct from stopping active work.

## What should be preserved

- Executable import boundaries, strict typing, SQL constraints, fixed-width timestamp helpers, and identity-before-content deduplication.
- Real migrated databases and client-level journeys rather than replacing them with faster internal mocks.
- Historical migrations with inlined values: 17 linear migrations are not, by themselves, migration rot. No live-source imports were found in that history.
- `uv.lock` for reproducible Python resolution and Nix for interpreter/system dependencies. Ranged manifest requirements plus frozen lock consumption are not inherently unpinned deployment.
- Existing shared API primitives and the comparatively cohesive collection pipeline. Refactoring should address demonstrated ownership problems, not apply uniform abstraction everywhere.
- The active TODO/ADR ledger and evidence of removing obsolete mechanisms. Consolidate contradictory status claims rather than adding another competing architecture narrative.

## Repair order and completion criteria

1. **Contain incorrect user-visible changes:** A01–A04, each with its failing behavioral regression first.
2. **Make plugin use and unattended operation dependable:** A05–A07, then A08–A11 with real boundary and deterministic concurrency tests.
3. **Define durable write/sync contracts:** A12–A15 plus B03 and C01–C02.
4. **Close deterministic boundary bugs:** A16–A18; keep the retired-renderer exposure qualification.
5. **Prevent recurrence:** B01–B06 and C03; then targeted lifecycle/Reader extraction (C04–C05), not a wholesale rewrite.

For each fix: record the user scenario, demonstrate a failure for the expected reason, implement the smallest layer-correct change, assert success/refusal/boundary outcomes, perform required security/invariant regression injection, and rerun `nix develop --command just ci`. Contract/schema/user-visible changes also require the appropriate snapshots, migrations, and guide/ADR updates. This review establishes a green baseline with actionable gaps; it does not mark any listed defect as fixed.
