"""Console entrypoint `calculator <command>`: one image, one command per workload."""

import sys
from collections.abc import Sequence

from calculator_core.config.http_service import run_http_service
from calculator_core.config.migrate_job import run_migrate
from calculator_core.config.settings import WORKLOADS, SettingsError

MIGRATE = "migrate"
INGEST = "ingest"
COMMANDS = (*WORKLOADS, MIGRATE)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        return _usage()
    command = args[0]
    if command == MIGRATE:
        return run_migrate()
    if command == INGEST:
        sys.stderr.write("The ingest command is not available in this version.\n")
        return 2
    if command not in WORKLOADS:
        return _usage()
    try:
        run_http_service(command)
    except SettingsError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    return 0


def _usage() -> int:
    sys.stderr.write(f"Usage: calculator <{'|'.join(COMMANDS)}>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
