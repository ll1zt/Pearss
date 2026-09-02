"""App: composition, dependency injection, route registration, startup.

The top layer: it may import core, adapters and plugins. It carries no domain
rules — those belong in core, and moving one here is an architecture violation.
"""
