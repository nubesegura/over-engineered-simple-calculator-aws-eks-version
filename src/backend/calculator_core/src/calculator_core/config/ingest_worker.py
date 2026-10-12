"""Runs the ingestion worker until the pod receives SIGTERM or SIGINT."""

import logging
import os
import signal
import threading
from collections.abc import Mapping

from calculator_core.adapters.outbound.aws_errors import describe_error
from calculator_core.config.container import build_ingest_worker
from calculator_core.config.observability import configure_logging
from calculator_core.config.settings import SettingsError, load_ingest_settings

logger = logging.getLogger(__name__)


def run_ingest_worker(env: Mapping[str, str] | None = None) -> int:
    """Entry of the `ingest` command; returns the process exit code."""
    values = os.environ if env is None else env
    try:
        settings = load_ingest_settings(values)
    except SettingsError as error:
        configure_logging("calculator-ingest", values.get("ENVIRONMENT", "unknown"))
        logger.error("%s", error)
        return 1
    configure_logging(settings.service_name, settings.environment, settings.log_level)
    stop = threading.Event()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signal_number, lambda *_: stop.set())
    try:
        build_ingest_worker(settings).run_forever(stop)
    except Exception as error:
        logger.error("The ingestion worker could not start (%s).", describe_error(error))
        return 1
    return 0
