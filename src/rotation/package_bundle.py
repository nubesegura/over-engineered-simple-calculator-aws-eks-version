"""Assemble the rotation function package folder. Build tooling: it is not part of the function package.

Used by `make` (src/rotation/Makefile, run by `sam build`) and by scripts/package_rotation.py. It fills a
folder with `app.py`, the pinned dependencies of requirements.txt (pure Python wheels installed for Linux
arm64 and Python 3.14 whatever the machine that builds) and the RDS global CA bundle, downloaded over verified
HTTPS and checked before it is packaged. Everything it needs lives in this directory.

Usage:

    python package_bundle.py --output-dir DIR      # rotation function package folder
    python package_bundle.py --trust-bundle FILE   # RDS bundle + Amazon roots for the application image
"""

import hashlib
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

# Trust file of the application image (src/backend/Dockerfile): the RDS bundle (direct connections to the
# instance, used by the migration job) plus the Amazon Trust Services roots, because RDS Proxy presents an
# ACM certificate that chains to them and not to the RDS CAs. The roots are self-signed, valid until 2038
# (CA 1) and 2040 (CA 2 to 4); each file is pinned by the SHA-256 of the PEM published at amazontrust.com.
AMAZON_ROOTS_BASE_URL = "https://www.amazontrust.com/repository/"
AMAZON_ROOTS_SHA256 = {
    "AmazonRootCA1.pem": "2c43952ee9e000ff2acc4e2ed0897c0a72ad5fa72c3d934e81741cbd54f05bd1",
    "AmazonRootCA2.pem": "a3a7fe25439d9a9b50f60af43684444d798a4c869305bf615881e5c84a44c1a2",
    "AmazonRootCA3.pem": "3eb7c3258f4af9222033dc1bb3dd2c7cfa0982b98e39fb8e9dc095cfeb38126c",
    "AmazonRootCA4.pem": "b0b7961120481e33670315b2f843e643c42f693c7a1010eb9555e06ddc730214",
}
AMAZON_ROOT_MAX_BYTES = 10_000


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


def _download(url: str, max_bytes: int) -> bytes:
    """Download over HTTPS with certificate verification (never another scheme)."""
    if not url.startswith("https://"):
        raise SystemExit("The CA bundle must be fetched over HTTPS")
    context = ssl.create_default_context()
    with urllib.request.urlopen(url, timeout=BUNDLE_TIMEOUT_SECONDS, context=context) as response:  # noqa: S310
        data: bytes = response.read(max_bytes + 1)
    return data


def fetch_bundle() -> bytes:
    """Download the RDS global CA bundle over HTTPS with certificate verification."""
    return _download(BUNDLE_URL, BUNDLE_MAX_BYTES)


def fetch_amazon_roots() -> list[bytes]:
    """Download the four Amazon root CA files over verified HTTPS; each must match its pinned SHA-256."""
    roots: list[bytes] = []
    for name, expected in AMAZON_ROOTS_SHA256.items():
        data = _download(AMAZON_ROOTS_BASE_URL + name, AMAZON_ROOT_MAX_BYTES)
        if hashlib.sha256(data).hexdigest() != expected:
            raise SystemExit(f"{name} does not match its pinned SHA-256")
        roots.append(data)
    return roots


def validate_bundle(data: bytes) -> None:
    """Fail when the download is not a loadable bundle of PEM certificates."""
    if len(data) > BUNDLE_MAX_BYTES or PEM_BEGIN not in data:
        raise SystemExit("The downloaded CA bundle is not a PEM certificate bundle")
    try:
        ssl.create_default_context().load_verify_locations(cadata=data.decode("ascii"))
    except ssl.SSLError, UnicodeDecodeError:
        raise SystemExit("The downloaded CA bundle cannot be loaded") from None


def build_trust_bundle(rds_bundle: bytes, roots: list[bytes]) -> bytes:
    """One PEM file with the RDS bundle followed by the Amazon roots; both parts are validated first."""
    validate_bundle(rds_bundle)
    if len(roots) != len(AMAZON_ROOTS_SHA256):
        raise SystemExit("The four Amazon root CA files are required")
    for root in roots:
        validate_bundle(root)
        if root.count(PEM_BEGIN) != 1:
            raise SystemExit("Each Amazon root CA file must hold exactly one certificate")
    parts = [rds_bundle, *roots]
    data = b"".join(part.rstrip(b"\r\n") + b"\n" for part in parts)
    validate_bundle(data)
    return data


def write_trust_bundle(path: Path) -> None:
    """Download, pin-check, validate and write the trust file of the application image."""
    path.write_bytes(build_trust_bundle(fetch_bundle(), fetch_amazon_roots()))


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
    usage = "Usage: package_bundle.py --output-dir DIR | --trust-bundle FILE"
    if len(argv) != 2 or argv[0] not in ("--output-dir", "--trust-bundle"):
        raise SystemExit(usage)
    if argv[0] == "--trust-bundle":
        write_trust_bundle(Path(argv[1]))
        print(f"Trust bundle ready: {argv[1]}")
        return
    assemble(Path(argv[1]))
    print(f"Rotation package folder ready: {argv[1]}")


if __name__ == "__main__":
    main(sys.argv[1:])
