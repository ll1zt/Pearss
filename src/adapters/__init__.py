"""Adapters: outward-facing implementations of the ports core defines.

Database, HTTP, RSS, MCP and file storage live here. Adapters may import core,
and plugins may import adapters, but nothing in this layer may import app.
"""
