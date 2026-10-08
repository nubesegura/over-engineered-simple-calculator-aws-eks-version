"""Tests of the rotation steps with moto (Secrets Manager) and an in-memory fake PostgreSQL."""

import json
import logging
import re
import ssl
from collections.abc import Iterator
from typing import Any

import boto3
import pg8000.native
import pytest
from moto import mock_aws
from moto.secretsmanager.models import secretsmanager_backends

import app

DEFAULT_ACCOUNT_ID = "123456789012"
TOKEN = "11111111-2222-3333-4444-555555555555"
STEPS = ("createSecret", "setSecret", "testSecret", "finishSecret")
MASTER: dict[str, Any] = {"username": "master", "password": "master-pw-0001"}
CURRENT: dict[str, Any] = {"username": "calc_app", "password": "current-pw-0001", "dbname": "calc"}


class FakeServer:
    """In-memory PostgreSQL: roles with passwords and group memberships."""

    def __init__(self) -> None:
        self.passwords = {"master": MASTER["password"], "calc_app": CURRENT["password"]}
        self.members: dict[str, set[str]] = {"calc_app": {"calc_app_rw"}}
        self.statements: list[str] = []
        self.connections: list[tuple[str, str]] = []
        self.fail_after_alter = False

    def connect(self, settings: app.Settings, user: str, password: str) -> FakeConnection:
        assert settings.host == "db.internal"
        self.connections.append((settings.dbname, user))
        if self.passwords.get(user) != password:
            raise ConnectionRefusedError(f"password authentication failed for {user} with {password}")
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.closed = False

    def run(self, sql: str, **params: Any) -> list[list[Any]] | None:
        self.server.statements.append(sql)
        if sql == "SELECT 1":
            return [[1]]
        if sql.startswith("SELECT 1 FROM pg_roles"):
            return [[1]] if params["name"] in self.server.passwords else []
        if sql.startswith("SELECT g.rolname"):
            return [[g] for g in sorted(self.server.members.get(params["base"], set()))]
        if match := re.fullmatch(r'CREATE ROLE "(\w+)" WITH LOGIN', sql):
            self.server.passwords[match[1]] = ""
            return None
        if match := re.fullmatch(r'GRANT "(\w+)" TO "(\w+)"', sql):
            self.server.members.setdefault(match[2], set()).add(match[1])
            return None
        if match := re.fullmatch(r"ALTER ROLE \"(\w+)\" WITH PASSWORD '(.*)'", sql):
            self.server.passwords[match[1]] = match[2]
            if self.server.fail_after_alter:
                self.server.fail_after_alter = False
                raise TimeoutError("connection lost after the role change")
            return None
        raise AssertionError(f"unexpected SQL: {sql}")

    def close(self) -> None:
        self.closed = True


class Env:
    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.sm: Any = boto3.client("secretsmanager", region_name="us-east-1")
        self.arn: str = self.sm.create_secret(Name="app", SecretString=json.dumps(CURRENT))["ARN"]
        self.master_arn: str = self.sm.create_secret(Name="master", SecretString=json.dumps(MASTER))["ARN"]
        secret = self.backend_secret("app")
        secret.rotation_requested = True
        secret.rotation_enabled = True
        secret.auto_rotate_after_days = 3
        self.start_rotation(TOKEN)

    @staticmethod
    def backend_secret(name: str) -> Any:
        """Reach into moto: it cannot enable rotation without invoking a real function."""
        return secretsmanager_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].secrets[name]

    def start_rotation(self, token: str) -> None:
        """Stage an empty AWSPENDING version, as Secrets Manager does when a rotation starts."""
        versions = self.backend_secret("app").versions
        for version in versions.values():
            version["version_stages"] = [s for s in version["version_stages"] if s != "AWSPENDING"]
        versions[token] = {"createdate": 0, "version_id": token, "version_stages": ["AWSPENDING"]}

    def step(self, name: str, token: str = TOKEN) -> None:
        app.lambda_handler({"SecretId": self.arn, "ClientRequestToken": token, "Step": name}, None)

    def cycle(self, token: str = TOKEN) -> None:
        for name in STEPS:
            self.step(name, token)

    def stages(self) -> dict[str, list[str]]:
        versions: dict[str, list[str]] = self.sm.describe_secret(SecretId=self.arn)["VersionIdsToStages"]
        return versions

    def secret(self, **kwargs: str) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.sm.get_secret_value(SecretId=self.arn, **kwargs)["SecretString"])
        return value


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    for name, value in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "DB_HOST": "db.internal",
        "DB_NAME": "calc",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("DB_PORT", raising=False)
    monkeypatch.delenv("SSL_ROOT_CERT", raising=False)
    server = FakeServer()
    monkeypatch.setattr(app, "connect", server.connect)
    with mock_aws():
        e = Env(server)
        monkeypatch.setenv("MASTER_SECRET_ARN", e.master_arn)
        yield e


def test_full_cycle_moves_the_inactive_user_to_current(env: Env) -> None:
    env.cycle()
    current = env.secret(VersionStage="AWSCURRENT")
    assert current["username"] == "calc_app_clone"
    assert current["dbname"] == "calc"
    assert "host" not in current
    assert env.server.passwords["calc_app_clone"] == current["password"]
    assert env.server.members["calc_app_clone"] == {"calc_app_rw"}
    assert env.server.passwords["calc_app"] == CURRENT["password"]
    assert ("calc", "calc_app_clone") in env.server.connections
    assert env.secret(VersionStage="AWSPREVIOUS") == CURRENT


def test_users_alternate_back_to_the_base_user(env: Env) -> None:
    env.cycle()
    second = "99999999-2222-3333-4444-555555555555"
    env.start_rotation(second)
    env.step("createSecret", second)
    assert env.secret(VersionId=second, VersionStage="AWSPENDING")["username"] == "calc_app"


def test_repeated_steps_are_idempotent(env: Env) -> None:
    env.step("createSecret")
    pending = env.secret(VersionId=TOKEN, VersionStage="AWSPENDING")
    env.step("createSecret")
    assert env.secret(VersionId=TOKEN, VersionStage="AWSPENDING") == pending
    env.step("setSecret")
    env.step("setSecret")
    assert len([s for s in env.server.statements if s.startswith("CREATE ROLE")]) == 1
    env.step("testSecret")
    env.step("finishSecret")
    current = env.secret(VersionStage="AWSCURRENT")
    env.step("finishSecret")
    assert env.secret(VersionStage="AWSCURRENT") == current


def test_interruption_after_alter_role_is_recovered_by_retrying_set_secret(env: Env) -> None:
    env.step("createSecret")
    env.server.fail_after_alter = True
    with pytest.raises(app.RotationError, match="TimeoutError"):
        env.step("setSecret")
    pending = env.secret(VersionId=TOKEN, VersionStage="AWSPENDING")
    assert env.server.passwords["calc_app_clone"] == pending["password"]
    env.step("setSecret")
    env.step("testSecret")
    env.step("finishSecret")
    assert env.secret(VersionStage="AWSCURRENT")["password"] == pending["password"]
    assert len([s for s in env.server.statements if s.startswith("CREATE ROLE")]) == 1


def test_failed_verification_keeps_the_old_version_current(env: Env) -> None:
    env.step("createSecret")
    before = env.stages()
    with pytest.raises(app.RotationError, match="login failed"):
        env.step("testSecret")
    assert env.stages() == before
    assert env.secret(VersionStage="AWSCURRENT") == CURRENT
    assert env.server.passwords["calc_app"] == CURRENT["password"]


def test_database_errors_are_sanitized(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.step("createSecret")
    pending = env.secret(VersionId=TOKEN, VersionStage="AWSPENDING")

    def failing(_settings: app.Settings, _user: str, password: str) -> Any:
        raise OSError(f"cannot connect with {password}")

    monkeypatch.setattr(app, "connect", failing)
    with pytest.raises(app.RotationError) as caught:
        env.step("setSecret")
    assert pending["password"] not in str(caught.value)
    assert caught.value.__cause__ is None


def test_passwords_never_appear_in_logs_even_on_failure(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    env.step("createSecret")
    pending = env.secret(VersionId=TOKEN, VersionStage="AWSPENDING")
    with pytest.raises(app.RotationError):
        env.step("testSecret")
    env.step("setSecret")
    env.step("testSecret")
    env.step("finishSecret")
    for password in (pending["password"], CURRENT["password"], MASTER["password"]):
        assert password not in caplog.text
        assert all(password not in repr(record.__dict__) for record in caplog.records)
    assert any(r.levelno == logging.ERROR and "testSecret" in r.getMessage() for r in caplog.records)


def test_rotation_disabled_and_unknown_token_are_rejected(env: Env) -> None:
    other = env.sm.create_secret(Name="plain", SecretString=json.dumps(CURRENT))["ARN"]
    event: dict[str, Any] = {"SecretId": other, "ClientRequestToken": TOKEN, "Step": "createSecret"}
    with pytest.raises(app.RotationError, match="rotation disabled"):
        app.lambda_handler(event, None)
    with pytest.raises(app.RotationError, match="no stage"):
        app.lambda_handler({**event, "SecretId": env.arn, "ClientRequestToken": "x" * 36}, None)


def test_unknown_step_is_rejected(env: Env) -> None:
    with pytest.raises(app.RotationError, match="Unknown"):
        env.step("noSuchStep")


def test_clone_without_base_group_role_is_rejected(env: Env) -> None:
    env.server.members["calc_app"] = set()
    env.step("createSecret")
    with pytest.raises(app.RotationError, match="no group role"):
        env.step("setSecret")


def test_settings_defaults_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DB_HOST", "DB_NAME", "MASTER_SECRET_ARN"):
        monkeypatch.setenv(name, "x")
    monkeypatch.delenv("DB_PORT", raising=False)
    monkeypatch.delenv("SSL_ROOT_CERT", raising=False)
    settings = app.load_settings()
    assert (settings.port, settings.ssl_root_cert) == (5432, "/var/task/rds-global-bundle.pem")
    monkeypatch.setenv("DB_PORT", "abc")
    with pytest.raises(app.RotationError, match="DB_PORT"):
        app.load_settings()
    monkeypatch.delenv("DB_PORT")
    monkeypatch.delenv("DB_HOST")
    with pytest.raises(app.RotationError, match="DB_HOST"):
        app.load_settings()


def test_tls_context_is_verify_full_against_the_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    real_default = ssl.create_default_context

    def fake_default(cafile: str | None = None) -> ssl.SSLContext:
        seen["cafile"] = cafile
        return real_default()

    monkeypatch.setattr(ssl, "create_default_context", fake_default)
    context = app.tls_context("/var/task/rds-global-bundle.pem")
    assert seen["cafile"] == "/var/task/rds-global-bundle.pem"
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_connect_uses_the_instance_host_and_the_verified_context(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(pg8000.native, "Connection", lambda *a, **kw: captured.update(kw, user=a[0]))
    monkeypatch.setattr(app, "tls_context", lambda _path: ssl.create_default_context())
    app.connect(app.Settings("db.internal", 5433, "calc", "arn", "/bundle.pem"), "calc_app", "x")
    assert (captured["host"], captured["port"], captured["database"]) == ("db.internal", 5433, "calc")
    assert captured["ssl_context"].check_hostname
