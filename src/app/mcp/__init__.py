"""The MCP server package: the agent-facing control plane.

Split from a single 1490-line module so each concern has a home: the Core API
caller (core_client), the presentation rules (rendering), the elicitation
machinery (confirmation), and the tools themselves (tools/). The public entry
points stay importable from app.mcp_server, which main.py uses.
"""
