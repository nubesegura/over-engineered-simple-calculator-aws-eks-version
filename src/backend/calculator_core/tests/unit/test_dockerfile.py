"""Static checks of the image definition (the build itself runs in CI)."""

import re
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[3] / "Dockerfile"
SECRET_WORDS = re.compile(r"password|secret|token|aws_access_key", re.IGNORECASE)


@pytest.fixture
def lines() -> list[str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if not line.strip().startswith("#")]


def test_base_image_is_pinned_by_digest_and_never_latest(lines: list[str]) -> None:
    base = next(line for line in lines if line.startswith("ARG PYTHON_IMAGE="))

    assert "latest" not in "\n".join(lines).lower()
    assert re.search(r"@sha256:[0-9a-f]{64}$", base)
    assert all("${PYTHON_IMAGE}" in line for line in lines if line.startswith("FROM"))


def test_the_image_runs_as_the_non_root_uid_of_the_chart(lines: list[str]) -> None:
    users = [line for line in lines if line.startswith("USER ")]

    assert users[-1] == "USER 10001:10001"
    assert "--uid 10001" in "\n".join(lines)
    assert "--gid 10001" in "\n".join(lines)


def test_entrypoint_is_the_dispatcher_without_a_shell(lines: list[str]) -> None:
    assert 'ENTRYPOINT ["calculator"]' in lines
    assert not any(line.startswith("CMD") for line in lines)


def test_nothing_is_added_from_the_network_and_no_secret_enters_a_layer(lines: list[str]) -> None:
    assert not any(line.startswith("ADD ") for line in lines)
    assert not any(line.startswith(("ENV", "ARG")) and SECRET_WORDS.search(line) for line in lines)


def test_only_runtime_requirements_are_installed_and_the_ca_bundle_is_in_place(
    lines: list[str],
) -> None:
    text = "\n".join(lines)

    assert "requirements-dev" not in text
    assert "COPY calculator_core/requirements.txt" in text
    assert "/app/db-trust-bundle.pem" in text
    assert "WORKDIR /app" in lines


def test_the_trust_file_is_built_by_the_helper_and_is_root_owned_and_read_only(
    lines: list[str],
) -> None:
    text = "\n".join(lines)

    assert "RUN python /helper/package_bundle.py --trust-bundle /db-trust-bundle.pem" in lines
    assert (
        "COPY --from=bundle --chown=root:root --chmod=0444 "
        "/db-trust-bundle.pem /app/db-trust-bundle.pem" in lines
    )
    assert "rds-global-bundle" not in text
