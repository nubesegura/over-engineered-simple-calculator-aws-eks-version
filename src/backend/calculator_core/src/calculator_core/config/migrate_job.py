"""Migrate command: schema, group role and application user, with the master credential.

It connects straight to the database instance (not the proxy) over TLS verify-full. Both
secrets are read at run time; no password is printed or logged, and a failure is reported by
its type only.
"""

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import psycopg

from calculator_core.adapters.outbound.aws_errors import describe_error
from calculator_core.adapters.outbound.migrations import apply_migrations, ensure_application_user
from calculator_core.config.observability import configure_logging
from calculator_core.config.settings import SettingsError

logger = logging.getLogger(__name__)

DEFAULT_MIGRATIONS_DIRECTORY = "/app/migrations"
_REQUIRED = (
    "AWS_REGION",
    "MASTER_SECRET_ARN",
    "APP_SECRET_ARN",
    "DB_INSTANCE_HOST",
    "DB_NAME",
    "SSL_ROOT_CERT",
)


@dataclass(frozen=True)
class MigrateSettings:
    aws_region: str
    master_secret_arn: str
    app_secret_arn: str
    db_host: str
    db_port: int
    db_name: str
    ssl_root_cert: str
    migrations_directory: Path


def load_migrate_settings(env: Mapping[str, str]) -> MigrateSettings:
    problems = [f"{name} is required" for name in _REQUIRED if not env.get(name, "").strip()]
    port = env.get("DB_PORT", "").strip() or "5432"
    if not (port.isdigit() and 0 < int(port) < 65536):
        problems.append("DB_PORT must be a number between 1 and 65535")
    if problems:
        raise SettingsError("Invalid configuration: " + "; ".join(problems) + ".")
    return MigrateSettings(
        aws_region=env["AWS_REGION"].strip(),
        master_secret_arn=env["MASTER_SECRET_ARN"].strip(),
        app_secret_arn=env["APP_SECRET_ARN"].strip(),
        db_host=env["DB_INSTANCE_HOST"].strip(),
        db_port=int(port),
        db_name=env["DB_NAME"].strip(),
        ssl_root_cert=env["SSL_ROOT_CERT"].strip(),
        migrations_directory=Path(env.get("MIGRATIONS_DIRECTORY", DEFAULT_MIGRATIONS_DIRECTORY)),
    )


def run_migrations(settings: MigrateSettings, secrets_client: Any) -> list[str]:
    """Apply the pending files and make sure the application user exists."""
    master = _read_secret(secrets_client, settings.master_secret_arn)
    application = _read_secret(secrets_client, settings.app_secret_arn)
    with psycopg.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=master["username"],
        password=master["password"],
        sslmode="verify-full",
        sslrootcert=settings.ssl_root_cert,
        autocommit=True,
    ) as connection:
        applied = apply_migrations(connection, settings.migrations_directory)
        created = ensure_application_user(
            connection, application["username"], application["password"]
        )
    logger.info("Application user %s.", "created" if created else "already present")
    return applied


def run_migrate(env: Mapping[str, str] | None = None) -> int:
    """Entry of the `migrate` command; returns the process exit code."""
    values = os.environ if env is None else env
    configure_logging("calculator-migrate", values.get("ENVIRONMENT", "unknown"))
    try:
        settings = load_migrate_settings(values)
        client = boto3.client("secretsmanager", region_name=settings.aws_region)
        applied = run_migrations(settings, client)
    except SettingsError as error:
        logger.error("%s", error)
        return 1
    except Exception as error:
        # Type, AWS code and operation only: messages are not guaranteed to be free of secrets.
        logger.error("Migration failed (%s).", describe_error(error))
        return 1
    logger.info("Migration finished; %d file(s) applied.", len(applied))
    return 0


def _read_secret(client: Any, secret_id: str) -> dict[str, str]:
    secret = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])
    return {"username": secret["username"], "password": secret["password"]}
