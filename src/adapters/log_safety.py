"""Making untrusted text safe to put in one log line.

A log line is a record boundary. Request targets, article titles, feed names
and error bodies are caller- or third-party-controlled, and a control
character inside one of them ends the physical line and starts a new one: the
journal then carries an entry the application never emitted, wearing whatever
timestamp and severity the attacker chose. That is log forging, and ``logging``
will not prevent it -- it renders ``%s`` arguments verbatim.

Escaping rather than dropping, because an operator investigating an attack
needs to see what was sent, and a silently truncated string would also make
two different requests indistinguishable.
"""

from __future__ import annotations


def escape_control_characters(text: str) -> str:
    """Replace every non-printable character with an escaped ``\\xNN`` form.

    Space and all printable Unicode survive: a path with a space in it is
    ordinary. Newlines, tabs, NUL and the C1 range do not, because each of them
    can break a line or hide content from a terminal.
    """
    return "".join(
        character if character.isprintable() else f"\\x{ord(character):02x}"
        for character in text
    )
