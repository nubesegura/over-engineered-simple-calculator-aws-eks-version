"""Builds and starts one HTTPS workload: container, ASGI app and uvicorn with self-signed TLS.

The command-line dispatcher (task 4.5) calls `run_http_service(workload)`.
"""

import os
from collections.abc import Mapping
from pathlib import Path

import uvicorn
from starlette.types import ASGIApp

from calculator_core.adapters.inbound.http_app import CalculationRoute, HttpAppConfig, create_app
from calculator_core.adapters.inbound.self_signed_tls import self_signed_files
from calculator_core.config.container import Container, build_container
from calculator_core.config.observability import configure_logging, set_request_context
from calculator_core.config.settings import BACKEND_NAME, load_settings
from calculator_core.domain.operation import Operation

PORT = 8443
# Longer than the load balancer idle timeout (60 s by default), or it would reuse closed sockets.
KEEP_ALIVE_SECONDS = 65
TLS_DIRECTORY = "/tmp"  # noqa: S108 - the writable volume the chart mounts at /tmp


def build_service_app(container: Container) -> ASGIApp:
    """Mount only the routes of the container's workload."""
    settings = container.settings
    config = HttpAppConfig(
        service_name=settings.service_name,
        environment=settings.environment,
        cors_allowed_origin=settings.allowed_origin,
        backend_name=BACKEND_NAME,
    )
    calculation = None
    if container.perform_calculation is not None:
        calculation = CalculationRoute(Operation(settings.workload), container.perform_calculation)
    return create_app(
        config,
        container.notifier,
        set_request_context,
        calculation=calculation,
        history=container.get_history,
    )


def run_http_service(workload: str, env: Mapping[str, str] | None = None) -> None:
    settings = load_settings(workload, os.environ if env is None else env)
    configure_logging(settings.service_name, settings.environment, settings.log_level)
    app = build_service_app(build_container(settings))
    with self_signed_files(settings.service_name, Path(TLS_DIRECTORY)) as files:
        uvicorn.run(
            app,
            host="0.0.0.0",  # noqa: S104 - pod interface; network policies limit access
            port=PORT,
            ssl_certfile=str(files.certificate),
            ssl_keyfile=str(files.key),
            timeout_keep_alive=KEEP_ALIVE_SECONDS,
            log_config=None,
            access_log=False,
        )
