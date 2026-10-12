"""Composition root: the only place that wires adapters to use cases."""

from dataclasses import dataclass

import boto3

from calculator_core.adapters.outbound.postgres_repository import (
    PostgresCalculationRepository,
    create_pool,
)
from calculator_core.adapters.outbound.secret_provider import (
    SecretsManagerCredentialsProvider,
    SecretsManagerCursorKeyProvider,
)
from calculator_core.adapters.outbound.sns_notifier import SnsErrorNotifier
from calculator_core.adapters.outbound.system import SystemClock, UuidGenerator
from calculator_core.application.ports.error_notifier import ErrorNotifier
from calculator_core.application.use_cases.get_history import GetHistory
from calculator_core.application.use_cases.perform_calculation import PerformCalculation
from calculator_core.config.settings import HISTORY_WORKLOAD, Settings
from calculator_core.domain.history import CursorCodec


@dataclass(frozen=True)
class Container:
    settings: Settings
    notifier: ErrorNotifier
    perform_calculation: PerformCalculation | None
    get_history: GetHistory | None


def build_container(settings: Settings) -> Container:
    """Wire the adapters; only the history workload reads the cursor key."""
    secrets = boto3.client("secretsmanager", region_name=settings.aws_region)
    provider = SecretsManagerCredentialsProvider(settings.app_secret_arn, secrets)
    pool = create_pool(
        settings.db_host, settings.db_port, settings.db_name, settings.ssl_root_cert, provider
    )
    repository = PostgresCalculationRepository(pool)
    notifier = SnsErrorNotifier(
        boto3.client("sns", region_name=settings.aws_region), settings.alerts_topic_arn
    )
    if settings.workload != HISTORY_WORKLOAD or settings.cursor_key_secret_arn is None:
        calculation = PerformCalculation(repository, SystemClock(), UuidGenerator())
        return Container(settings, notifier, calculation, None)
    key = SecretsManagerCursorKeyProvider(settings.cursor_key_secret_arn, secrets).get()
    return Container(settings, notifier, None, GetHistory(repository, CursorCodec(key)))
