"""Scenario register (normative source: SCENARIOS.md).

AC-275..277: the logging bootstrap injects the active correlation id into
every record, renders it in the line, and prints one redacted startup banner.
AC-295: a network database URL with no path must not print its authority -- the
last path segment is the whole authority there, so the old fallback leaked the
password into the startup banner.

These assert on the observable artifacts -- the rendered line and the captured
``LogRecord`` -- never on "the filter ran N times".
"""

from __future__ import annotations

import io
import logging
import threading
from collections.abc import Iterator

import pytest

from adapters import correlation
from app import log_setup

pytestmark = pytest.mark.unit


class _CollectingHandler(logging.Handler):
    """A handler that keeps the records it is handed, for assertions on them."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    """Put the root logger back the way configure_logging found it.

    The bootstrap mutates process-global state (root handlers and level), so a
    test that installs it must not leak into the rest of the suite.
    """
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


def test_the_format_pins_the_correlation_id_placeholder() -> None:
    """AC-275: the format string names the field it depends on."""
    assert "%(correlation_id)s" in log_setup.LOG_FORMAT


def test_every_application_log_line_carries_the_correlation_id(
    restore_root_logger: None,
) -> None:
    """AC-275: a request id set on the context appears in the rendered line."""
    stream = io.StringIO()
    log_setup.configure_logging(level="INFO", stream=stream)

    token = correlation.set_correlation_id("abc123")
    try:
        logging.getLogger("app.example").info("hello from the app")
    finally:
        correlation.reset(token)

    rendered = stream.getvalue()
    assert "abc123" in rendered
    assert "hello from the app" in rendered


def test_the_record_carries_the_correlation_id_field(
    restore_root_logger: None,
) -> None:
    """AC-275: the injected attribute exists on the record, not only the text."""
    log_setup.configure_logging(level="INFO")
    capture = _CollectingHandler()
    logging.getLogger().addHandler(capture)

    token = correlation.set_correlation_id("abc123")
    try:
        logging.getLogger("app.example").info("hello")
    finally:
        correlation.reset(token)

    assert capture.records, "the log line was not captured at all"
    assert capture.records[-1].correlation_id == "abc123"  # type: ignore[attr-defined]


def test_concurrent_correlation_ids_do_not_cross(
    restore_root_logger: None,
) -> None:
    """AC-276: two concurrent requests keep their own ids."""
    stream = io.StringIO()
    log_setup.configure_logging(level="INFO", stream=stream)
    barrier = threading.Barrier(2)

    def worker(correlation_id: str, message: str) -> None:
        token = correlation.set_correlation_id(correlation_id)
        try:
            barrier.wait(timeout=5)
            logging.getLogger("app.example").info(message)
        finally:
            correlation.reset(token)

    threads = [
        threading.Thread(target=worker, args=("alpha-1", "from alpha")),
        threading.Thread(target=worker, args=("beta-2", "from beta")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    lines = stream.getvalue().splitlines()
    alpha = [line for line in lines if "from alpha" in line]
    beta = [line for line in lines if "from beta" in line]
    assert alpha and "alpha-1" in alpha[0]
    assert beta and "beta-2" in beta[0]


def test_repeated_configure_logging_does_not_duplicate_lines(
    restore_root_logger: None,
) -> None:
    """The bootstrap is idempotent: a second call does not stack a handler."""
    stream = io.StringIO()
    log_setup.configure_logging(level="INFO", stream=stream)
    log_setup.configure_logging(level="INFO", stream=stream)

    logging.getLogger("app.example").info("once")

    assert stream.getvalue().count("once") == 1


def test_startup_banner_reports_effective_config_without_credentials(
    monkeypatch: pytest.MonkeyPatch, restore_root_logger: None
) -> None:
    """AC-277: the banner names the level and a redacted database location."""
    monkeypatch.setenv("PEARSS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv(
        "PEARSS_DATABASE_URL",
        "postgresql://operator:s3cr3t-pw@db.internal:5432/pearss?sslmode=require",
    )
    stream = io.StringIO()
    log_setup.configure_logging(stream=stream)

    log_setup.log_startup_banner()

    rendered = stream.getvalue()
    assert "level=DEBUG" in rendered
    assert "postgresql:///pearss" in rendered
    assert "s3cr3t-pw" not in rendered
    assert "db.internal" not in rendered
    assert "operator" not in rendered


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # No path at all: the authority must not be mistaken for the path.
        ("postgresql://operator:s3cr3t@db.internal", "postgresql:///"),
        ("mysql://root:hunter2@localhost", "mysql:///"),
        ("postgresql://operator:s3cr3t@db.internal:5432", "postgresql:///"),
        # A path is still reported; the authority and query are still dropped.
        (
            "postgresql://operator:s3cr3t@db.internal:5432/pearss?sslmode=require",
            "postgresql:///pearss",
        ),
        # SQLite has no authority; the path is the location.
        ("sqlite:////var/lib/pearss.db", "sqlite:////var/lib/pearss.db"),
        ("sqlite+aiosqlite:///./pearss.db?mode=ro", "sqlite+aiosqlite:///./pearss.db"),
    ],
)
def test_describe_database_url_never_reports_credentials(
    url: str, expected: str
) -> None:
    """AC-295: only scheme and path survive, whatever shape the URL has."""
    assert log_setup.describe_database_url(url) == expected


def test_describe_database_url_flags_a_url_it_cannot_parse() -> None:
    """AC-295: a string with no scheme is reported, not echoed."""
    assert log_setup.describe_database_url("pearss.db") == "<unparseable>"


def test_startup_banner_hides_a_pathless_network_credential(
    monkeypatch: pytest.MonkeyPatch, restore_root_logger: None
) -> None:
    """AC-295: the banner is the user-visible path; it must not leak either."""
    monkeypatch.delenv("PEARSS_LOG_LEVEL", raising=False)
    monkeypatch.setenv(
        "PEARSS_DATABASE_URL", "postgresql://operator:s3cr3t-pw@db.internal"
    )
    stream = io.StringIO()
    log_setup.configure_logging(stream=stream)

    log_setup.log_startup_banner()

    rendered = stream.getvalue()
    assert "postgresql:///" in rendered
    assert "s3cr3t-pw" not in rendered
    assert "db.internal" not in rendered
    assert "operator" not in rendered
