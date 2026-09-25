# Pearss command entry point.
#
# Section 10.3: every command used by humans, agents and CI is declared here.
# README, AGENTS.md and CI reference only these names. If a step cannot be
# expressed as a recipe, it does not belong in the workflow.

set shell := ["bash", "-uc"]

# Surface the environment early: these fail with clear messages instead of
# letting a later step die on a missing binary.
python := "uv run python"
project := "pearss"

# Show the available recipes.
default:
    @just --list

# Print the toolchain the environment resolved to.
shell:
    @echo "python: $(uv run python -V 2>&1)"
    @echo "uv:     $(uv --version)"
    @echo "just:   $(just --version)"
    @echo "ruff:   $(uv run ruff --version)"
    @echo "mypy:   $(uv run mypy --version)"

# Install or update Python dependencies from uv.lock.
sync:
    uv sync

# ---------------------------------------------------------------------------
# Quality gates
# ---------------------------------------------------------------------------

# Format, lint, type check and enforce the layer contract.
lint: 
    uv run ruff format --check .
    uv run ruff check .
    uv run mypy
    uv run lint-imports

# Apply fixes for what can be fixed automatically.
fix:
    uv run ruff format .
    uv run ruff check --fix .

# Required suites must fail when selection is empty (pytest exit 5).
_test marker:
    uv run pytest -m "{{marker}}" -q

# Unit and fixture tests (offline and deterministic).
test:
    just _test unit\ or\ fixture

# Tests that touch SQLite or temporary local services, but never the network.
test-integration:
    just _test integration

# Contract snapshots: the MCP tool surface and the OpenAPI document.
# A diff means a client-visible surface changed; review it, then regenerate
# with MCP_SNAPSHOT_UPDATE=1 / OPENAPI_SNAPSHOT_UPDATE=1 and commit the file.
contract:
    @echo "contract: MCP tool surface (tests/contract/mcp_tools.json)"
    @echo "contract: OpenAPI document  (tests/contract/openapi.json)"
    @uv run pytest -m contract -q

# The end-to-end loop: one process, real server + MCP client + fixture origin.
# Needs no Docker; see docs/guides/testing.md for the topology.
test-e2e:
    just _test e2e

# Mutation testing over src/core. Slow on purpose (minutes, not seconds), so
# it is a tool you run when touching core or reviewing assertion strength,
# not a per-commit gate. `mutmut results` lists survivors; `mutmut show <id>`
# prints the mutant. Survivors are either missing assertions (add a test) or
# equivalent mutants (record and move on).
mutate:
    uv run mutmut run
    uv run mutmut results

# ---------------------------------------------------------------------------
# Coverage
#
# A percentage over this whole codebase is a diagnostic, not a target. The
# number that protects new work is the coverage of the lines a change *added*,
# which is what `coverage-diff` reports. See docs/guides/testing.md for why
# there is no global fail_under.
# ---------------------------------------------------------------------------

# Measure production coverage (test modules and conftest are excluded).
coverage:
    #!/usr/bin/env bash
    set -euo pipefail
    rm -f .coverage
    uv run pytest -q -m "unit or fixture or integration" -p no:cacheprovider --cov --cov-report=term-missing

# Coverage of the lines this branch added or changed, against a base ref.
# Usage: just coverage-diff [base] [threshold]   (default: origin/main 20)
#
# The gate that protects new work: a global percentage can be met by testing
# whatever is cheapest to reach, but "did the lines you just wrote get run?"
# has a direct answer. Runs the suite with instrumentation, then compares.
# E2E is part of the measured run: a behavior exercised only through the real
# server counts toward the change that introduced it. See
# scripts/coverage_diff.py and docs/guides/testing.md.
coverage-diff base="origin/main" threshold="20":
    #!/usr/bin/env bash
    set -euo pipefail
    rm -f .coverage
    uv run pytest -q -m "unit or fixture or integration or e2e" -p no:cacheprovider \
        --cov --cov-report= >/dev/null
    uv run python scripts/coverage_diff.py "{{base}}" "{{threshold}}"

# The changed-line gate, as CI runs it: same measurement as coverage-diff.
#
# `COVERAGE_BASE` and `COVERAGE_THRESHOLD` override the defaults, so a merge
# workflow can pin them without editing this file (B06); a positional argument
# still wins, so `just coverage-gate HEAD~3 5` works from a shell.
#
#   COVERAGE_BASE=origin/main COVERAGE_THRESHOLD=25 just coverage-gate
#
# The comment used to claim this parameterization while the recipe took plain
# positional defaults, which meant setting the variables did nothing at all --
# a gate described as configurable that silently ignored its configuration.
coverage-gate base=env_var_or_default("COVERAGE_BASE", "origin/main") threshold=env_var_or_default("COVERAGE_THRESHOLD", "20"):
    #!/usr/bin/env bash
    set -euo pipefail
    rm -f .coverage
    uv run pytest -q -m "unit or fixture or integration or e2e" -p no:cacheprovider \
        --cov --cov-report= >/dev/null
    uv run python scripts/coverage_diff.py "{{base}}" "{{threshold}}"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

# Apply migrations up to head.
migrate:
    uv run alembic upgrade head

# Verify every migration upgrades and downgrades cleanly.
# Verify every migration upgrades and downgrades cleanly, against a throwaway
# database. This must never touch the configured one: `downgrade base` drops
# every table, and item and read_state are fact sources. Running this recipe
# against ./data/pearss.db destroyed a populated database once, which is why
# the target is pinned to a temporary file and the environment variable is
# overridden rather than inherited.
migrate-check:
    #!/usr/bin/env bash
    set -euo pipefail
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    echo "migrate-check: verifying on a throwaway database (configured one was ${PEARSS_DATABASE_URL:-<default>})"
    export PEARSS_DATABASE_URL="sqlite+pysqlite:///$tmp/migrate_check.db"
    uv run alembic upgrade head
    uv run alembic downgrade base
    uv run alembic upgrade head

# Create a new migration with an autogenerated revision id.
revision message:
    uv run alembic revision --autogenerate -m "{{message}}"

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

# Start the API server on 127.0.0.1 by default: this is a single-user
# service, so it should not be reachable from the network without an explicit
# decision to expose it. Pass a different host to expose it, e.g.
#   just run 100.64.0.1   # a Tailscale address
#   just run 192.168.1.10    # the LAN address
#
# A gitignored .env is sourced first when present. The app itself only ever
# reads os.environ, deliberately: keeping the loading here means there is one
# visible place where configuration enters the process, rather than an import
# that silently pulls a file in from wherever it happens to be.
run host="127.0.0.1" port="8000":
    #!/usr/bin/env bash
    set -a
    # shellcheck disable=SC1091
    [ -f .env ] && source .env
    set +a
    exec uv run uvicorn app.main:app --host {{host}} --port {{port}}

# Start the service using configuration from the environment, so a deployment
# changes .env rather than the command line. This is what the systemd unit
# calls; keeping it argument-free is what lets one unit file work for any host
# and port without editing it.
serve:
    #!/usr/bin/env bash
    set -a
    # shellcheck disable=SC1091
    [ -f .env ] && source .env
    set +a
    exec uv run uvicorn app.main:app \
        --host "${PEARSS_BIND_HOST:-127.0.0.1}" \
        --port "${PEARSS_PORT:-8000}" \
        --proxy-headers --forwarded-allow-ips="127.0.0.1"

# ---------------------------------------------------------------------------
# Service (systemd user unit; see deploy/pearss.service)
# ---------------------------------------------------------------------------

# Install the user unit and enable it. Requires no root; run `just linger`
# first if the service must survive logout and start at boot.
install-unit:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p ~/.config/systemd/user
    cp deploy/pearss.service ~/.config/systemd/user/pearss.service
    cp deploy/pearss-backup.service ~/.config/systemd/user/pearss-backup.service
    cp deploy/pearss-backup.timer ~/.config/systemd/user/pearss-backup.timer
    systemctl --user daemon-reload
    systemctl --user enable --now pearss.service
    systemctl --user enable --now pearss-backup.timer
    echo "installed and started. check with: just service-status"

# Stop and disable the service and its backup timer.
uninstall-unit:
    systemctl --user disable --now pearss.service pearss-backup.timer || true
    rm -f ~/.config/systemd/user/pearss.service \
        ~/.config/systemd/user/pearss-backup.service \
        ~/.config/systemd/user/pearss-backup.timer
    systemctl --user daemon-reload

# Keep the service running across logout and boot.
linger:
    loginctl enable-linger "$(whoami)"

# Recent log lines from the service.
# Uses --no-pager so it is usable from an agent or a script.
service-status:
    systemctl --user status pearss.service --no-pager || true
    journalctl --user -u pearss.service -n 30 --no-pager || true

# Restart after changing .env or the code.
service-restart:
    systemctl --user restart pearss.service

# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

# Everything CI runs, in the order architecture section 7.6 prescribes.
# coverage-gate is the changed-line diagnostic wired into the merge workflow
# (B06): it re-runs the suite instrumented, so it is the slowest step and
# therefore last.
ci: lint test test-integration test-e2e migrate-check contract coverage-gate

# The exact command CI executes, including the Nix environment entry point.
ci-nix:
    nix flake check
    nix develop --command just ci


# Backfill processor products for items stored before a chain was enabled.
# Usage: just enrichment-backfill <source_id> [limit]
enrichment-backfill source_id limit="":
    #!/usr/bin/env bash
    set -a
    # shellcheck disable=SC1091
    [ -f .env ] && source .env
    set +a
    export PEARSS_BACKFILL_SOURCE_ID="{{source_id}}"
    export PEARSS_BACKFILL_LIMIT="{{limit}}"
    exec uv run python -m app.enrichment_backfill


# Back up the fact-source data (SQLite snapshot + file store) into PEARSS_BACKUP_DIR (default backups/).
backup:
    #!/usr/bin/env bash
    set -a
    # shellcheck disable=SC1091
    [ -f .env ] && source .env
    set +a
    exec uv run python -m app.backup create

# Restore an archive into an empty target directory and verify it.
# Usage: just restore <archive> <target-dir>
restore archive target_dir:
    #!/usr/bin/env bash
    set -a
    # shellcheck disable=SC1091
    [ -f .env ] && source .env
    set +a
    export PEARSS_RESTORE_ARCHIVE="{{archive}}"
    export PEARSS_RESTORE_TARGET="{{target_dir}}"
    exec uv run python -m app.backup restore
