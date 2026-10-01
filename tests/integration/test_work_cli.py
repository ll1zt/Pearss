"""The CLI: a file in, a command value out, `/v1` underneath.

Scenario register (normative source: SCENARIOS.md, AC-209..210; issue #198).

The measured need: a 568-address list had two paths -- through a model's context
(~52KB, twice, and a transcript risk) or a hand-written curl with a credential
the agent cannot read. A *file* is the third, and it is the one an operator
already has.

What is asserted here is the part a shell script depends on: stdout is the
handle and nothing else, the list is not echoed, and the re-send acts on the
ledger rather than on the whole list again.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Item as ItemRow
from adapters.db_models import ItemEnrichment
from adapters.repository import create_source, units_for_work, work_by_id
from adapters.timestamps import fixed_width_utc as timestamp
from app.main import create_app
from app.work_cli import main as cli_main
from app.work_worker import WorkWorker
from tests.integration.conftest import ServerInThread, free_port
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

API_TOKEN = "work-cli-api-token"
SITE = "https://cli.example"
FEED_URL = f"{SITE}/feed"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """A deployment per test.

    Not shared: the worker claims the *oldest* queued work, so a work another
    test left behind would be the one a sweep picked up -- and the assertion
    about "my" work's ledger would be about somebody else's.
    """
    return {
        "PEARSS_DATABASE_URL": migrated_database(tmp_path, "work_cli.db"),
        "PEARSS_STORAGE_DIR": str(tmp_path / "storage"),
        "PEARSS_API_TOKEN": API_TOKEN,
        "PEARSS_SCHEDULER_ENABLED": "0",
    }


@pytest.fixture
def served(env: dict[str, str]) -> Iterator[tuple[str, FastAPI]]:
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        app = create_app()
        with ServerInThread(app, free_port()) as server:
            yield server.base_url(""), app
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def cli(served: tuple[str, FastAPI], monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A callable that runs the CLI against the served deployment."""
    base_url, app = served
    monkeypatch.setenv("PEARSS_API_URL", base_url)
    monkeypatch.setenv("PEARSS_API_TOKEN", API_TOKEN)

    del app, base_url

    def run(*argv: str) -> int:
        return cli_main(list(argv))

    yield run


def _session_factory(app: FastAPI) -> sessionmaker[Session]:
    return app.state.session_factory  # type: ignore[no-any-return]


def _a_source(session_factory: sessionmaker[Session]) -> str:
    with session_factory() as session:
        row = create_source(
            session,
            source_type="rss",
            url=FEED_URL,
            canonical_url=FEED_URL,
            schedule_policy='{"type": "fixed", "interval": "30m"}',
            tenant_id="default",
            at=timestamp(datetime(2026, 10, 3, 2, 0, tzinfo=UTC)),
        )
        session.commit()
        return str(row.id)


def _sweep(app: FastAPI) -> dict[str, int]:
    """One pass of the real worker, in-process (the deployment's loop is off)."""
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(599, text="no request expected here")
        )
    )
    worker = WorkWorker(_session_factory(app), client=client)
    try:
        import asyncio

        return asyncio.run(worker.sweep_once())
    finally:
        client.close()


# --------------------------------------------------------------------------
# AC-209: a file becomes a command value
# --------------------------------------------------------------------------


def test_ac209_a_file_of_targets_becomes_one_work(
    cli: Any,
    served: tuple[str, FastAPI],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-209: the list is read locally, and stdout is the handle alone.

    The file is the shape an operator actually has: a column pasted out of a
    spreadsheet, with blank lines and a comment. The assertion that matters for
    scripting is the negative one -- the list is not echoed, so a captured
    stdout is an id rather than 568 URLs.

    The addresses are off the source's host so the work finishes without a
    network, which is what lets the last assertion be about the *key* rather
    than about live de-duplication: once the work is terminal, only a key
    derived from the command value makes the same file answer with the same
    handle (the API replays the stored response). A random key would create a
    second, identical work -- and that is the difference between "the same
    command" and "another command that happens to look the same".
    """
    app = served[1]
    source_id = _a_source(_session_factory(app))
    listing = tmp_path / "urls.txt"
    elsewhere = "https://elsewhere.example"
    listing.write_text(
        "# addresses for this backfill\n"
        "\n"
        f"{elsewhere}/blog/1\n"
        f"{elsewhere}/blog/2\n"
        "   \n",
        encoding="utf-8",
    )

    code = cli(
        "submit",
        "--kind",
        "fetch",
        "--source",
        source_id,
        "--targets-file",
        str(listing),
    )

    captured = capsys.readouterr()
    assert code == 0
    work_id = captured.out.strip()
    assert work_id.startswith("work-")
    assert SITE not in captured.out, "the target list must not be echoed to stdout"
    with _session_factory(app)() as session:
        work = work_by_id(session, work_id)
    assert work is not None
    assert work.kind == "fetch"
    assert work.targets == [f"{elsewhere}/blog/1", f"{elsewhere}/blog/2"]
    assert work.params["source_id"] == source_id
    # The commentary is on stderr, so `WORK=$(just work submit ...)` works.
    assert "kind=fetch" in captured.err

    # Off the source's host: the work finishes (every target `rejected`) without
    # a request, so what follows is about the key rather than about a work that
    # is still in flight.
    assert _sweep(app) == {"works": 1, "units": 2}

    assert (
        cli(
            "submit",
            "--kind",
            "fetch",
            "--source",
            source_id,
            "--targets-file",
            str(listing),
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == work_id, (
        "the same file must be the same command: a re-submit replays the stored "
        "response instead of creating a second identical work"
    )


def test_the_cli_says_what_is_wrong_when_it_cannot_authenticate(
    served: tuple[str, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing token is a sentence, not a traceback.

    The CLI authenticates the way every caller does, so this is the failure an
    operator will hit first -- and a stack trace would send them looking at the
    wrong thing.
    """
    base_url, _ = served
    monkeypatch.setenv("PEARSS_API_URL", base_url)
    monkeypatch.delenv("PEARSS_API_TOKEN", raising=False)

    code = cli_main(["status", "work-whatever"])

    captured = capsys.readouterr()
    assert code == 1
    assert "PEARSS_API_TOKEN" in captured.err
    assert "Traceback" not in captured.err


# --------------------------------------------------------------------------
# AC-210: re-send what failed, and only that
# --------------------------------------------------------------------------


def test_ac210_retry_failed_re_sends_only_the_failed_targets(
    cli: Any,
    served: tuple[str, FastAPI],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-210: the ledger answers "what do I need to re-send?" and the CLI acts on it.

    A `retry` work with one real row (which becomes `ok`) and one id that names
    nothing (which becomes `failed`) is the smallest thing that can tell the two
    apart. `units --outcome failed` must list exactly the missing one, and
    `retry-failed` must submit a work for exactly that target: re-running the row
    that worked is the cost this whole mechanism exists to avoid.
    """
    app = served[1]
    session_factory = _session_factory(app)
    source_id = _a_source(session_factory)
    now = timestamp(datetime(2026, 10, 3, 2, 0, tzinfo=UTC))
    with session_factory() as session:
        session.add(
            ItemRow(
                id="cli-item",
                source_id=source_id,
                guid="cli-guid",
                content_hash="cli-hash",
                title="Row to retry",
                url=f"{SITE}/blog/item",
                publish_status="ready",
                tenant_id="default",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ItemEnrichment(
                id="cli-enrichment",
                item_id="cli-item",
                plugin_id="translate",
                plugin_version="0.1.0",
                kind="translation",
                lang="en",
                input_hash="cli-hash",
                status="failed",
                error_class="auth",
                detail="provider said no",
                attempt_count=3,
                tenant_id="default",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    assert (
        cli("submit", "--kind", "retry", "--targets", "cli-enrichment,no-such-row") == 0
    )
    work_id = capsys.readouterr().out.strip()
    assert _sweep(app) == {"works": 1, "units": 2}

    assert cli("units", work_id) == 0
    every = [line.split("\t") for line in capsys.readouterr().out.strip().splitlines()]
    assert {line[0]: line[1] for line in every} == {
        "cli-enrichment": "ok",
        "no-such-row": "failed",
    }

    assert cli("units", work_id, "--outcome", "failed") == 0
    failed_lines = capsys.readouterr().out.strip().splitlines()
    assert failed_lines == [
        "no-such-row\tfailed\tstructure_changed\tno enrichment row 'no-such-row'"
    ]

    # Re-send: the failed targets, with the same kind and params, and not the
    # one that already worked.
    assert cli("retry-failed", work_id) == 0
    retried = capsys.readouterr().out.strip()
    assert retried.startswith("work-") and retried != work_id
    with session_factory() as session:
        work = work_by_id(session, retried)
        assert work is not None
        assert work.kind == "retry"
        assert work.targets == ["no-such-row"]


def test_a_work_with_no_failures_re_sends_nothing(
    cli: Any,
    served: tuple[str, FastAPI],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing failed is an answer, not an empty re-send.

    Submitting a work with zero targets would be a 422 from the API, so the CLI
    has to recognise the case rather than pass it on -- and say so where a shell
    can see it.
    """
    app = served[1]
    session_factory = _session_factory(app)
    source_id = _a_source(session_factory)
    now = timestamp(datetime(2026, 10, 3, 2, 0, tzinfo=UTC))
    with session_factory() as session:
        session.add(
            ItemRow(
                id="cli-clean-item",
                source_id=source_id,
                guid="cli-clean-guid",
                content_hash="cli-clean-hash",
                title="Nothing to retry",
                url=f"{SITE}/blog/clean",
                publish_status="ready",
                tenant_id="default",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ItemEnrichment(
                id="cli-clean-enrichment",
                item_id="cli-clean-item",
                plugin_id="translate",
                plugin_version="0.1.0",
                kind="translation",
                lang="en",
                input_hash="cli-clean-hash",
                status="failed",
                error_class="auth",
                detail="provider said no",
                attempt_count=3,
                tenant_id="default",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    assert cli("submit", "--kind", "retry", "--targets", "cli-clean-enrichment") == 0
    work_id = capsys.readouterr().out.strip()
    assert _sweep(app) == {"works": 1, "units": 1}
    with session_factory() as session:
        assert [row.outcome.value for row in units_for_work(session, work_id)] == ["ok"]

    assert cli("retry-failed", work_id) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "nothing failed" in captured.err
