"""Plugins: collectors and processors.

P1 ships quasi-plugins — hardcoded registry, no manifest or dynamic install.
Collectors produce ItemDraft and never touch the database, RSS output or
scheduler directly; everything flows through core's normalisation entry point.
"""
