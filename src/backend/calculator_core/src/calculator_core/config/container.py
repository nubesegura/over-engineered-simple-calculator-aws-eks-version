"""Composition root: the only place that wires adapters to use cases."""

from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config

from calculator_core.adapters.inbound.sqs_worker import SqsWorker
from calculator_core.adapters.outbound.postgres_repository import (
    PostgresCalculationRepository,
    create_pool,
)
from calculator_core.adapters.outbound.s3_object_store import S3ObjectStore
from calculator_core.adapters.outbound.secret_provider import (
    SecretsManagerCredentialsProvider,
    SecretsManagerCursorKeyProvider,
)
from calculator_core.adapters.outbound.sns_notifier import SnsErrorNotifier
from calculator_core.adapters.outbound.system import SystemClock, UuidGenerator
from calculator_core.adapters.outbound.throttled_notifier import ThrottledErrorNotifier
from calculator_core.application.ingest_csv import IngestCsv
from calculator_core.application.ports.credentials_provider import CredentialsProvider
from calculator_core.application.ports.error_notifier import ErrorNotifier
from calculator_core.application.use_cases.get_history import GetHistory
from calculator_core.application.use_cases.perform_calculation import PerformCalculation
from calculator_core.config.settings import (
    HISTORY_WORKLOAD,
    DatabaseTuning,
    IngestSettings,
    Settings,
)
from calculator_core.domain.history import CursorCodec

# Bounded waits for every AWS call: a slow dependency must not hold request threads for long.
AWS_CLIENT_CONFIG = Config(
    connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 2, "mode": "standard"}
)
# Long polling holds the connection for WaitTimeSeconds (20), so the read timeout must exceed it.
QUEUE_CLIENT_CONFIG = Config(
    connect_timeout=2, read_timeout=30, retries={"total_max_attempts": 2, "mode": "standard"}
)


def _client(service: str, region: str, config: Config = AWS_CLIENT_CONFIG) -> Any:
    return boto3.client(service, region_name=region, config=config)


def _pool(settings: Settings | IngestSettings, provider: CredentialsProvider) -> Any:
    tuning: DatabaseTuning = settings.database
    return create_pool(
        settings.db_host,
        settings.db_port,
        settings.db_name,
        settings.ssl_root_cert,
        provider,
        connect_timeout=tuning.connect_timeout_seconds,
        pool_timeout=tuning.pool_timeout_seconds,
        max_waiting=tuning.pool_max_waiting,
    )


def _notifier(settings: Settings | IngestSettings) -> ErrorNotifier:
    publisher = SnsErrorNotifier(_client("sns", settings.aws_region), settings.alerts_topic_arn)
    return ThrottledErrorNotifier(publisher, settings.alert_throttle_seconds)


@dataclass(frozen=True)
class Container:
    settings: Settings
    notifier: ErrorNotifier
    perform_calculation: PerformCalculation | None
    get_history: GetHistory | None


def build_container(settings: Settings) -> Container:
    """Wire the adapters; only the history workload reads the cursor key."""
    secrets = _client("secretsmanager", settings.aws_region)
    provider = SecretsManagerCredentialsProvider(settings.app_secret_arn, secrets)
    pool = _pool(settings, provider)
    repository = PostgresCalculationRepository(pool)
    notifier = _notifier(settings)
    if settings.workload != HISTORY_WORKLOAD or settings.cursor_key_secret_arn is None:
        calculation = PerformCalculation(repository, SystemClock(), UuidGenerator())
        return Container(settings, notifier, calculation, None)
    key = SecretsManagerCursorKeyProvider(settings.cursor_key_secret_arn, secrets).get()
    return Container(settings, notifier, None, GetHistory(repository, CursorCodec(key)))


def build_ingest_worker(settings: IngestSettings) -> SqsWorker:
    """Wire the ingestion worker: queue consumer, use case, bucket, database and alerts."""
    secrets = _client("secretsmanager", settings.aws_region)
    provider = SecretsManagerCredentialsProvider(settings.app_secret_arn, secrets)
    pool = _pool(settings, provider)
    notifier = _notifier(settings)
    use_case = IngestCsv(
        S3ObjectStore(_client("s3", settings.aws_region), settings.bucket),
        PostgresCalculationRepository(pool),
        notifier,
        settings.service_name,
        settings.environment,
        settings.batch_size,
    )
    sqs = _client("sqs", settings.aws_region, QUEUE_CLIENT_CONFIG)
    return SqsWorker(sqs, settings.queue_url, use_case.execute)
