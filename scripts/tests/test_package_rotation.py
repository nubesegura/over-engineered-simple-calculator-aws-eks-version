"""Tests of the rotation packaging script: CA bundle checks and the content of the zip (no network)."""

from __future__ import annotations

import datetime
import hashlib
import io
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import package_rotation as pr
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_pr: Any = pr  # package_rotation puts src/rotation on sys.path and exposes package_bundle at run time
pb = _pr.package_bundle


def make_pem(common_name: str = "test-ca") -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text("def lambda_handler(event, context): ...\n", encoding="utf-8")
    (source / "requirements.txt").write_text("pg8000==1.31.5\n", encoding="utf-8")
    build_dir = tmp_path / "build"
    monkeypatch.setattr(pb, "SOURCE_DIR", source)
    monkeypatch.setattr(pr, "BUILD_DIR", build_dir)
    monkeypatch.setattr(pr, "PACKAGE_DIR", build_dir / "package")
    monkeypatch.setattr(pr, "ZIP_PATH", build_dir / "db-rotation.zip")
    return build_dir


def fake_pip(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    target = Path(command[command.index("--target") + 1])
    (target / "pg8000").mkdir(parents=True)
    (target / "pg8000" / "__init__.py").write_text("", encoding="utf-8")
    (target / "pg8000" / "stale.pyc").write_bytes(b"x")
    return subprocess.CompletedProcess(command, 0)


def test_validate_bundle_accepts_pem_certificates() -> None:
    pb.validate_bundle(make_pem() + make_pem())


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"<html>error</html>", id="not-pem"),
        pytest.param(b"-----BEGIN CERTIFICATE-----\nnot base64\n", id="broken-pem"),
        pytest.param(b"x" * (pb.BUNDLE_MAX_BYTES + 1), id="too-big"),
    ],
)
def test_validate_bundle_rejects_anything_else(data: bytes) -> None:
    with pytest.raises(SystemExit):
        pb.validate_bundle(data)


def test_fetch_bundle_uses_verified_https(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(url: str, timeout: int, context: Any) -> io.BytesIO:
        seen.update(url=url, timeout=timeout, verify=context.verify_mode, host=context.check_hostname)
        return io.BytesIO(b"pem")

    monkeypatch.setattr(pb.urllib.request, "urlopen", fake_urlopen)
    assert pb.fetch_bundle() == b"pem"
    assert seen["url"].startswith("https://")
    assert seen["verify"].name == "CERT_REQUIRED"
    assert seen["host"]


def test_package_contains_handler_dependencies_and_bundle_deterministically(
    build: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem = make_pem()
    monkeypatch.setattr(pb, "fetch_bundle", lambda: pem)
    monkeypatch.setattr(subprocess, "run", fake_pip)
    first = pr.package().read_bytes()
    with zipfile.ZipFile(pr.ZIP_PATH) as zf:
        assert zf.namelist() == ["app.py", "pg8000/__init__.py", "rds-global-bundle.pem"]
        assert zf.read("rds-global-bundle.pem") == pem
    assert pr.package().read_bytes() == first


def test_package_stops_on_an_invalid_bundle_before_building(build: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pb, "fetch_bundle", lambda: b"<html>")
    with pytest.raises(SystemExit):
        pr.package()
    assert not build.exists()


def test_package_stops_when_the_source_is_missing(build: Path) -> None:
    (pb.SOURCE_DIR / "app.py").unlink()
    with pytest.raises(SystemExit, match="Missing source file"):
        pr.package()


def test_output_dir_mode_assembles_the_folder_without_a_zip(
    build: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem = make_pem()
    monkeypatch.setattr(pb, "fetch_bundle", lambda: pem)
    monkeypatch.setattr(subprocess, "run", fake_pip)
    artifacts = tmp_path / "artifacts"
    pb.main(["--output-dir", str(artifacts)])
    assert sorted(p.name for p in artifacts.iterdir()) == ["app.py", "pg8000", "rds-global-bundle.pem"]
    assert not pr.ZIP_PATH.exists()


def test_main_rejects_unknown_arguments() -> None:
    with pytest.raises(SystemExit, match="Usage"):
        pb.main(["--bogus"])


def test_makefile_target_matches_the_function_and_calls_the_script() -> None:
    makefile = (pr.REPO_ROOT / "src" / "rotation" / "Makefile").read_text(encoding="utf-8")
    assert "build-RotationFunction:" in makefile
    assert 'package_bundle.py --output-dir "$(ARTIFACTS_DIR)"' in makefile


# ---- trust file of the application image: RDS bundle + Amazon Trust Services roots ----

ROOT_NAMES = [f"Amazon Root CA {number}" for number in range(1, 5)]


@pytest.fixture
def fake_roots(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """Four self-signed stand-ins named like the Amazon roots, pinned by their own hashes; no network."""
    files = {f"AmazonRootCA{n}.pem": make_pem(f"Amazon Root CA {n}") for n in range(1, 5)}
    monkeypatch.setattr(pb, "AMAZON_ROOTS_SHA256", {k: hashlib.sha256(v).hexdigest() for k, v in files.items()})
    served = {pb.AMAZON_ROOTS_BASE_URL + name: data for name, data in files.items()}
    monkeypatch.setattr(pb, "_download", lambda url, max_bytes: served[url])
    return files


def subjects(bundle: bytes) -> list[str]:
    return [c.subject.rfc4514_string() for c in x509.load_pem_x509_certificates(bundle)]


def test_the_real_pins_are_four_sha256_values_of_the_amazon_root_files() -> None:
    assert list(pb.AMAZON_ROOTS_SHA256) == [f"AmazonRootCA{n}.pem" for n in range(1, 5)]
    for digest in pb.AMAZON_ROOTS_SHA256.values():
        assert len(digest) == 64 and int(digest, 16) >= 0
    assert pb.AMAZON_ROOTS_BASE_URL == "https://www.amazontrust.com/repository/"


def test_trust_bundle_holds_the_rds_certificates_and_the_four_amazon_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_roots: dict[str, bytes]
) -> None:
    rds = make_pem("Amazon RDS us-east-2 Root CA") + make_pem("Amazon RDS Root 2019 CA")
    monkeypatch.setattr(pb, "fetch_bundle", lambda: rds)
    target = tmp_path / "db-trust-bundle.pem"

    pb.write_trust_bundle(target)

    found = subjects(target.read_bytes())
    assert len(found) == 6
    assert found[:2] == ["CN=Amazon RDS us-east-2 Root CA", "CN=Amazon RDS Root 2019 CA"]
    assert found[2:] == [f"CN={name}" for name in ROOT_NAMES]
    assert target.read_bytes().endswith(b"-----END CERTIFICATE-----\n")
    ssl_context = pb.ssl.create_default_context()
    ssl_context.load_verify_locations(cadata=target.read_text(encoding="ascii"))


def test_a_root_that_does_not_match_its_pin_stops_the_build(
    monkeypatch: pytest.MonkeyPatch, fake_roots: dict[str, bytes]
) -> None:
    monkeypatch.setattr(pb, "_download", lambda url, max_bytes: make_pem("Amazon Root CA 1"))

    with pytest.raises(SystemExit, match="pinned SHA-256"):
        pb.fetch_amazon_roots()


def test_the_trust_bundle_needs_all_four_roots_each_with_one_certificate(fake_roots: dict[str, bytes]) -> None:
    roots = list(fake_roots.values())
    with pytest.raises(SystemExit, match="four"):
        pb.build_trust_bundle(make_pem(), roots[:3])
    with pytest.raises(SystemExit, match="exactly one"):
        pb.build_trust_bundle(make_pem(), [roots[0] + roots[0], *roots[1:]])
    with pytest.raises(SystemExit):
        pb.build_trust_bundle(b"<html></html>", roots)


def test_roots_are_downloaded_over_verified_https(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="HTTPS"):
        pb._download("http://www.amazontrust.com/repository/AmazonRootCA1.pem", 10)


def test_main_accepts_the_trust_bundle_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []
    monkeypatch.setattr(pb, "write_trust_bundle", seen.append)

    pb.main(["--trust-bundle", str(tmp_path / "x.pem")])

    assert seen == [tmp_path / "x.pem"]
    with pytest.raises(SystemExit):
        pb.main(["--other", "x"])
