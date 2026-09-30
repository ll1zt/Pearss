"""Reader API: the Google Reader compatible surface (ADR 0004).

Split out of the single `reader_routes.py` module (issue #86) by concern:

- `auth` owns the credential model, the GET-header / POST-form asymmetry and
  the startup credential sync
- `operations` owns each operation's session, transaction and repository
  sequence, in domain terms
- `routes` is HTTP: parse the form, translate the client's stream ids, call an
  operation, render the answer

`main.py` imports `router`, `ReaderAuth` and `configure_reader_credentials`
from here, so the package presents the same three names the module did.
"""

from __future__ import annotations

from app.reader.auth import ReaderAuth, configure_reader_credentials
from app.reader.routes import router

__all__ = ["ReaderAuth", "configure_reader_credentials", "router"]
