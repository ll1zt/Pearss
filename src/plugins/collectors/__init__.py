"""Collector plugins.

P1 quasi-plugins: they follow the collector interface from
docs/guides/collector-guide.md but are registered explicitly in the app layer
rather than discovered dynamically. The hookspec is frozen in P2 once there
are three real collectors to generalise from (Rule of Three).
"""
