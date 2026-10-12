"""Schema migrations and the application database user, run by the migrate command.

SQL files are applied in name order, once each, under an advisory lock so two jobs never
interleave. The application user is created with the password of its secret only when it
does not exist: afterwards the rotation function owns the password.
"""

import logging
from pathlib import Path
from typing import Any

from psycopg import Connection, sql

logger = logging.getLogger(__name__)

GROUP_ROLE = "calc_app_rw"
_LOCK_KEY = 7_302_003
_CREATE_TABLE = sql.SQL(
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
)


def apply_migrations(connection: Connection[Any], directory: Path) -> list[str]:
    """Apply the pending files of `directory`; return the versions applied by this call."""
    with connection.transaction():
        connection.execute(_CREATE_TABLE)
    applied: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        with connection.transaction():
            # Serializes concurrent migration jobs until their transaction ends.
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            found = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = %s", (path.stem,)
            ).fetchone()
            if found is not None:
                continue
            connection.execute(sql.SQL(path.read_text(encoding="utf-8")))
            connection.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
            applied.append(path.stem)
            logger.info("Applied migration %s.", path.stem)
    return applied


def ensure_application_user(connection: Connection[Any], username: str, password: str) -> bool:
    """Create the login user in the group role; keep the password of an existing user.

    Returns True when the user was created. The password travels as a SCRAM verifier, never
    in clear text, because the server may log a failed statement.
    """
    with connection.transaction():
        exists = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (username,)
        ).fetchone()
        if exists is None:
            verifier = connection.pgconn.encrypt_password(
                password.encode(), username.encode(), b"scram-sha-256"
            ).decode()
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
                ).format(sql.Identifier(username), sql.Literal(verifier))
            )
        connection.execute(
            sql.SQL("GRANT {} TO {}").format(sql.Identifier(GROUP_ROLE), sql.Identifier(username))
        )
    return exists is None
