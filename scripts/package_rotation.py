"""Build the deployment zip of the database rotation Lambda function.

The handler is `app.lambda_handler`. The folder content (`app.py`, pinned arm64 dependencies and the RDS CA
bundle fetched at package time) is assembled by src/rotation/package_bundle.py, the same code `sam build` runs
through src/rotation/Makefile. This script zips that folder deterministically (sorted entries, fixed timestamps
and permissions) into `.build/rotation/db-rotation.zip`. The bundle is never versioned.

Usage:

    python scripts/package_rotation.py
"""

import shutil
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO_ROOT / "src" / "rotation"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

import package_bundle  # noqa: E402

BUILD_DIR = REPO_ROOT / ".build" / "rotation"
PACKAGE_DIR = BUILD_DIR / "package"
ZIP_PATH = BUILD_DIR / "db-rotation.zip"

FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o644 << 16  # regular file, rw-r--r--
IGNORED_PARTS = {"__pycache__"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def write_zip() -> None:
    """Zip the package folder in a deterministic way."""
    files = sorted(
        path
        for path in PACKAGE_DIR.rglob("*")
        if path.is_file() and not IGNORED_PARTS.intersection(path.parts) and path.suffix not in IGNORED_SUFFIXES
    )
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(PACKAGE_DIR).as_posix(), FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = FILE_MODE
            info.create_system = 3  # Unix, so the mode above is honored
            zf.writestr(info, path.read_bytes())


def package() -> Path:
    """Assemble the package folder and zip it."""
    shutil.rmtree(BUILD_DIR, ignore_errors=True)
    package_bundle.assemble(PACKAGE_DIR)
    write_zip()
    return ZIP_PATH


if __name__ == "__main__":
    print(f"Rotation package ready: {package()}")
