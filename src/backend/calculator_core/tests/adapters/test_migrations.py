# ruff: noqa: S105 - throwaway test passwords
"""Migrations against a real PostgreSQL (`TEST_DATABASE_URL`, marker `postgres`)."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from calculator_core.adapters.outbound.migrations import (
    GROUP_ROLE,
    apply_migrations,
    ensure_application_user,
)

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not set"),
]

# tests/adapters -> tests -> calculator_core -> backend
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"
APP_USER = "test_calc_app"
CLONE_USER = "test_calc_app_clone"


def _reset(admin: psycopg.Connection[Any]) -> None:
    roles = [r[0] for r in admin.execute("SELECT rolname FROM pg_roles").fetchall()]
    existing = [r for r in (GROUP_ROLE, APP_USER, CLONE_USER) if r in roles]
    for role in existing:
        admin.execute(f'DROP OWNED BY "{role}"')
    admin.execute("DROP TABLE IF EXISTS calculations, schema_migrations")
    for role in existing:
        admin.execute(f'DROP ROLE "{role}"')


@pytest.fixture
def admin() -> Iterator[psycopg.Connection[Any]]:
    assert DATABASE_URL
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        _reset(connection)
        yield connection
        _reset(connection)


def _snapshot(admin: psycopg.Connection[Any]) -> list[Any]:
    return admin.execute(
        "SELECT (SELECT array_agg(version ORDER BY version) FROM schema_migrations), "
        "(SELECT array_agg(indexname ORDER BY indexname) FROM pg_indexes "
        " WHERE tablename = 'calculations'), "
        "(SELECT array_agg(privilege_type ORDER BY privilege_type) FROM "
        " information_schema.role_table_grants WHERE grantee = %s AND table_name = 'calculations')",
        (GROUP_ROLE,),
    ).fetchall()


def test_running_twice_applies_once_and_changes_nothing(admin: psycopg.Connection[Any]) -> None:
    first = apply_migrations(admin, MIGRATIONS_DIR)
    created = ensure_application_user(admin, APP_USER, "first-password")
    before = _snapshot(admin)

    second = apply_migrations(admin, MIGRATIONS_DIR)
    created_again = ensure_application_user(admin, APP_USER, "first-password")

    assert first == ["0001_create_calculations", "0002_group_role"]
    assert second == []
    assert created is True
    assert created_again is False
    assert _snapshot(admin) == before
    assert before[0][1] == ["calculations_history_idx", "calculations_pkey"]
    assert before[0][2] == ["INSERT", "SELECT"]


def test_an_existing_user_keeps_its_password(admin: psycopg.Connection[Any]) -> None:
    apply_migrations(admin, MIGRATIONS_DIR)
    ensure_application_user(admin, APP_USER, "first-password")
    verifier = admin.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s", (APP_USER,))
    before = verifier.fetchone()

    ensure_application_user(admin, APP_USER, "rotated-by-someone-else")

    after = admin.execute(
        "SELECT rolpassword FROM pg_authid WHERE rolname = %s", (APP_USER,)
    ).fetchone()
    assert before == after
    assert before is not None
    assert str(before[0]).startswith("SCRAM-SHA-256$")


def test_the_application_user_can_read_and_insert_but_cannot_change_the_schema(
    admin: psycopg.Connection[Any],
) -> None:
    apply_migrations(admin, MIGRATIONS_DIR)
    ensure_application_user(admin, APP_USER, "first-password")
    ensure_application_user(admin, CLONE_USER, "clone-password")
    insert = (
        "INSERT INTO calculations VALUES "
        "('00000000-0000-0000-0000-000000000001', 'add', 1, 2, 3, now(), NULL)"
    )

    for user in (APP_USER, CLONE_USER):
        admin.execute(f'SET ROLE "{user}"')
        try:
            admin.execute("SELECT count(*) FROM calculations").fetchone()
            for forbidden in (
                "CREATE TABLE intruder (id int)",
                "DROP TABLE calculations",
                "UPDATE calculations SET result = 0",
                "DELETE FROM calculations",
                "CREATE ROLE intruder",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    admin.execute(forbidden)
            if user == APP_USER:
                admin.execute(insert)
        finally:
            admin.execute("RESET ROLE")


def test_the_user_is_a_login_member_of_the_group_role(admin: psycopg.Connection[Any]) -> None:
    apply_migrations(admin, MIGRATIONS_DIR)
    ensure_application_user(admin, APP_USER, "first-password")

    row = admin.execute(
        "SELECT u.rolcanlogin, u.rolsuper, g.rolcanlogin FROM pg_roles u "
        "JOIN pg_auth_members m ON m.member = u.oid JOIN pg_roles g ON g.oid = m.roleid "
        "WHERE u.rolname = %s AND g.rolname = %s",
        (APP_USER, GROUP_ROLE),
    ).fetchone()

    assert row == (True, False, False)
