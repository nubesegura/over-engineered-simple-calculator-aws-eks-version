"""Assemble the rotation function package folder. Build tooling: it is not part of the function package.

Used by `make` (src/rotation/Makefile, run by `sam build`) and by scripts/package_rotation.py. It fills a
folder with `app.py`, the pinned dependencies of requirements.txt (pure Python wheels installed for Linux
arm64 and Python 3.14 whatever the machine that builds) and the RDS global CA bundle, downloaded over verified
HTTPS and checked before it is packaged. Everything it needs lives in this directory.

Usage:

    python package_bundle.py --output-dir DIR
"""

import shutil
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent
PLATFORM = "manylinux2014_aarch64"
PYTHON_VERSION = "3.14"
SOURCE_FILES = ["app.py"]
BUNDLE_NAME = "rds-global-bundle.pem"
BUNDLE_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
BUNDLE_TIMEOUT_SECONDS = 30
BUNDLE_MAX_BYTES = 2_000_000
PEM_BEGIN = b"-----BEGIN CERTIFICATE-----"


def install_dependencies(target: Path) -> None:
    """Install the pinned dependencies as wheels for the Lambda platform."""
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--no-compile",
            "--disable-pip-version-check",
            "--target",
            str(target),
            "--platform",
            PLATFORM,
            "--python-version",
            PYTHON_VERSION,
            "--implementation",
            "cp",
            "--only-binary=:all:",
            "--requirement",
            str(SOURCE_DIR / "requirements.txt"),
        ],
        check=True,
    )


def fetch_bundle() -> bytes:
    """Download the RDS global CA bundle over HTTPS with certificate verification."""
    if not BUNDLE_URL.startswith("https://"):
        raise SystemExit("The CA bundle must be fetched over HTTPS")
    context = ssl.create_default_context()
    with urllib.request.urlopen(BUNDLE_URL, timeout=BUNDLE_TIMEOUT_SECONDS, context=context) as response:  # noqa: S310
        data: bytes = response.read(BUNDLE_MAX_BYTES + 1)
    return data


def validate_bundle(data: bytes) -> None:
    """Fail when the download is not a loadable bundle of PEM certificates."""
    if len(data) > BUNDLE_MAX_BYTES or PEM_BEGIN not in data:
        raise SystemExit("The downloaded CA bundle is not a PEM certificate bundle")
    try:
        ssl.create_default_context().load_verify_locations(cadata=data.decode("ascii"))
    except ssl.SSLError, UnicodeDecodeError:
        raise SystemExit("The downloaded CA bundle cannot be loaded") from None


def assemble(target: Path) -> None:
    """Fill `target` with the sources, the pinned dependencies and the validated CA bundle."""
    for name in SOURCE_FILES:
        if not (SOURCE_DIR / name).is_file():
            raise SystemExit(f"Missing source file: {SOURCE_DIR / name}")
    bundle = fetch_bundle()
    validate_bundle(bundle)
    target.mkdir(parents=True, exist_ok=True)
    install_dependencies(target)
    for name in SOURCE_FILES:
        shutil.copyfile(SOURCE_DIR / name, target / name)
    (target / BUNDLE_NAME).write_bytes(bundle)


def main(argv: list[str]) -> None:
    if len(argv) != 2 or argv[0] != "--output-dir":
        raise SystemExit("Usage: package_bundle.py --output-dir DIR")
    assemble(Path(argv[1]))
    print(f"Rotation package folder ready: {argv[1]}")


if __name__ == "__main__":
    main(sys.argv[1:])
