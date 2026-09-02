"""Core: pure domain logic, zero framework imports.

This layer holds the domain model, state machines, normalisation, dedupe and
schedule statistics. It must not import FastAPI, SQLAlchemy, feedparser,
httpx, FastMCP or any other I/O or framework dependency — import-linter
enforces this, and AGENTS.md explains why: keeping core pure is what makes the
domain invariants unit-testable without a database or network.
"""
