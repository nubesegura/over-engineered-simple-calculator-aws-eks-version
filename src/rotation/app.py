"""Secrets Manager rotation function for the PostgreSQL application credential.

Strategy: alternating users. The application secret holds the credential of one of two database
users, "<base>" and "<base>_clone". Each rotation sets a new password on the inactive user, tests
a login with it and only then makes it the current version, so the current credential stays valid
until the very last step. The clone is created as a member of the same group roles as the base
user, which keep the grants.

Environment variables: DB_HOST (the RDS instance endpoint, not the proxy), DB_PORT (default 5432),
DB_NAME, MASTER_SECRET_ARN (RDS-managed master secret with username and password),
SSL_ROOT_CERT (packaged RDS CA bundle, default /var/task/rds-global-bundle.pem) and LOG_LEVEL.
The connection is TLS with certificate and host name verification (sslmode verify-full).

Secret material is never logged: database errors are replaced by their class name and the log
only carries the step, the version id and the error class.
"""

import json
import logging
import os
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import boto3
import pg8000.native
from botocore.config import Config

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
# Library debug logs carry request and response bodies (secret values): keep them off whatever LOG_LEVEL says.
for _noisy in ("boto3", "botocore", "urllib3", "pg8000"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

CLONE_SUFFIX = "_clone"
PASSWORD_LENGTH = 32
EXCLUDED_CHARACTERS = "/@\"'\\`"
DB_TIMEOUT_SECONDS = 10
DEFAULT_PORT = 5432
DEFAULT_SSL_ROOT_CERT = "/var/task/rds-global-bundle.pem"
CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=15,
    retries={"max_attempts": 5, "mode": "standard"},
)


class RotationError(Exception):
    """Raised when a rotation step cannot be completed."""


@dataclass(frozen=True)
class Settings:
    """Validated configuration read from the environment."""

    host: str
    port: int
    dbname: str
    master_secret_arn: str
    ssl_root_cert: str


def load_settings() -> Settings:
    """Read and validate the environment, failing with the name of the offending variable."""
    missing = [name for name in ("DB_HOST", "DB_NAME", "MASTER_SECRET_ARN") if not os.environ.get(name)]
    if missing:
        raise RotationError(f"Missing environment variable {missing[0]}")
    try:
        port = int(os.environ.get("DB_PORT", DEFAULT_PORT))
    except ValueError:
        raise RotationError("Environment variable DB_PORT is not a number") from None
    return Settings(
        host=os.environ["DB_HOST"],
        port=port,
        dbname=os.environ["DB_NAME"],
        master_secret_arn=os.environ["MASTER_SECRET_ARN"],
        ssl_root_cert=os.environ.get("SSL_ROOT_CERT", DEFAULT_SSL_ROOT_CERT),
    )


class Database(Protocol):
    """The part of a database connection used by the function."""

    def run(self, sql: str, **params: Any) -> list[list[Any]] | None:
        """Run one statement with named parameters (:name) and return the rows, if any."""

    def close(self) -> None:
        """Close the connection."""


def tls_context(ca_file: str) -> ssl.SSLContext:
    """TLS context that verifies the server certificate chain and host name (verify-full)."""
    context = ssl.create_default_context(cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def connect(settings: Settings, user: str, password: str) -> Database:
    """Open a verified TLS connection to the database instance."""
    return pg8000.native.Connection(  # type: ignore[no-any-return]
        user,
        host=settings.host,
        port=settings.port,
        database=settings.dbname,
        password=password,
        ssl_context=tls_context(settings.ssl_root_cert),
        timeout=DB_TIMEOUT_SECONDS,
    )


@contextmanager
def _database(settings: Settings, user: str, password: str) -> Iterator[Database]:
    """Connect and hide driver error details, which could echo statements or credentials."""
    try:
        db = connect(settings, user, password)
    except Exception as exc:
        raise RotationError(f"Database login failed ({type(exc).__name__})") from None
    try:
        yield db
    except RotationError:
        raise
    except Exception as exc:
        raise RotationError(f"Database operation failed ({type(exc).__name__})") from None
    finally:
        db.close()


def _base_user(username: str) -> str:
    return username.removesuffix(CLONE_SUFFIX)


def _other_user(username: str) -> str:
    base = _base_user(username)
    return base if username.endswith(CLONE_SUFFIX) else base + CLONE_SUFFIX


def _secret_value(client: Any, arn: str, **kwargs: str) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(client.get_secret_value(SecretId=arn, **kwargs)["SecretString"])
    return value


def create_secret(client: Any, arn: str, token: str) -> None:
    """Store a new password for the inactive user as AWSPENDING, once."""
    current = _secret_value(client, arn, VersionStage="AWSCURRENT")
    try:
        client.get_secret_value(SecretId=arn, VersionId=token, VersionStage="AWSPENDING")
        LOGGER.info("createSecret: pending version %s already exists", token)
        return
    except client.exceptions.ResourceNotFoundException:
        pass
    password = client.get_random_password(PasswordLength=PASSWORD_LENGTH, ExcludeCharacters=EXCLUDED_CHARACTERS)[
        "RandomPassword"
    ]
    pending = {**current, "username": _other_user(current["username"]), "password": password}
    client.put_secret_value(
        SecretId=arn,
        ClientRequestToken=token,
        SecretString=json.dumps(pending),
        VersionStages=["AWSPENDING"],
    )
    LOGGER.info("createSecret: stored pending version %s for the inactive user", token)


def _ensure_clone(db: Database, user: str) -> None:
    """Create the clone with the group roles of the base user when it does not exist."""
    quoted = pg8000.native.identifier(user)
    if not db.run("SELECT 1 FROM pg_roles WHERE rolname = :name", name=user):
        db.run(f"CREATE ROLE {quoted} WITH LOGIN")  # noqa: S608
        LOGGER.info("setSecret: created the clone database user")
    groups = db.run(
        "SELECT g.rolname FROM pg_auth_members m"
        " JOIN pg_roles g ON g.oid = m.roleid"
        " JOIN pg_roles u ON u.oid = m.member"
        " WHERE u.rolname = :base",
        base=_base_user(user),
    )
    if not groups:
        raise RotationError("The base database user has no group role to copy")
    for (group,) in groups:
        db.run(f"GRANT {pg8000.native.identifier(group)} TO {quoted}")


def set_secret(client: Any, arn: str, token: str, settings: Settings) -> None:
    """Create the clone when needed and set the pending password on the inactive user."""
    pending = _secret_value(client, arn, VersionId=token, VersionStage="AWSPENDING")
    master = _secret_value(client, settings.master_secret_arn)
    with _database(settings, master["username"], master["password"]) as db:
        if pending["username"].endswith(CLONE_SUFFIX):
            _ensure_clone(db, pending["username"])
        quoted = pg8000.native.identifier(pending["username"])
        password = pg8000.native.literal(pending["password"])
        db.run(f"ALTER ROLE {quoted} WITH PASSWORD {password}")
    LOGGER.info("setSecret: password set for the pending version %s", token)


def verify_secret(client: Any, arn: str, token: str, settings: Settings) -> None:
    """Log in with the pending credential and run a trivial query."""
    pending = _secret_value(client, arn, VersionId=token, VersionStage="AWSPENDING")
    with _database(settings, pending["username"], pending["password"]) as db:
        if db.run("SELECT 1") != [[1]]:
            raise RotationError("Test query returned an unexpected result")
    LOGGER.info("testSecret: login with the pending version %s succeeded", token)


def finish_secret(client: Any, arn: str, token: str) -> None:
    """Make the pending version the current one."""
    metadata = client.describe_secret(SecretId=arn)
    current = None
    for version, stages in metadata["VersionIdsToStages"].items():
        if "AWSCURRENT" in stages:
            if version == token:
                LOGGER.info("finishSecret: version %s already current", token)
                return
            current = version
    client.update_secret_version_stage(
        SecretId=arn,
        VersionStage="AWSCURRENT",
        MoveToVersionId=token,
        **({"RemoveFromVersionId": current} if current else {}),
    )
    LOGGER.info("finishSecret: version %s moved to AWSCURRENT", token)


def _validate(client: Any, arn: str, token: str) -> bool:
    """Check rotation is enabled and the token is staged; return True when already finished."""
    metadata = client.describe_secret(SecretId=arn)
    if not metadata.get("RotationEnabled"):
        raise RotationError("Secret has rotation disabled")
    versions = metadata.get("VersionIdsToStages", {})
    if token not in versions:
        raise RotationError("Secret version has no stage for this rotation")
    if "AWSCURRENT" in versions[token]:
        LOGGER.info("Version %s is already AWSCURRENT, nothing to do", token)
        return True
    if "AWSPENDING" not in versions[token]:
        raise RotationError("Secret version is not staged AWSPENDING")
    return False


def lambda_handler(event: dict[str, Any], _context: object) -> None:
    """Entrypoint: Secrets Manager rotation event (SecretId, ClientRequestToken, Step)."""
    arn, token, step = event["SecretId"], event["ClientRequestToken"], event["Step"]
    try:
        settings = load_settings()
        client = boto3.client("secretsmanager", config=CLIENT_CONFIG)
        if _validate(client, arn, token):
            return
        if step == "createSecret":
            create_secret(client, arn, token)
        elif step == "setSecret":
            set_secret(client, arn, token, settings)
        elif step == "testSecret":
            verify_secret(client, arn, token, settings)
        elif step == "finishSecret":
            finish_secret(client, arn, token)
        else:
            raise RotationError(f"Unknown rotation step {step}")
    except Exception as exc:
        LOGGER.error("Rotation step %s failed for version %s: %s", step, token, type(exc).__name__)
        raise
