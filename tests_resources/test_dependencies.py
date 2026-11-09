"""Resolve target-side RPM dependencies declared by the selected test suites."""

from pathlib import Path
import logging
import re
import shlex
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent
    / "tests_suites"
    / "dependencies"
    / "host-rpms.yaml"
)
_RPM_NAME = re.compile(r"^[A-Za-z0-9+_.:-]+$")


def load_host_rpm_manifest() -> dict:
    """Load and validate the host RPM manifest."""
    with MANIFEST_PATH.open(encoding="utf-8") as manifest_file:
        manifest = yaml.safe_load(manifest_file)
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError(f"Unsupported host RPM manifest: {MANIFEST_PATH}")
    if not isinstance(manifest.get("rhel"), dict):
        raise ValueError(f"Missing RHEL package definitions in {MANIFEST_PATH}")
    return manifest


def dependency_group_names() -> set[str]:
    """Return group names accepted by the pytest ``requires_test_rpms`` marker."""
    names = set()
    for release in load_host_rpm_manifest()["rhel"].values():
        names.update(release.get("groups", {}))
    return names


def _capability_value(hardware_spec: dict, capability: str):
    value = hardware_spec
    for name in capability.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(name)
    return value


def resolve_host_rpms(
    rhel_major: str,
    groups: set[str],
    hardware_spec: Optional[dict] = None,
) -> tuple[list[str], list[str]]:
    """Return de-duplicated (required, optional) RPM names for a test run."""
    manifest = load_host_rpm_manifest()
    release = manifest["rhel"].get(str(rhel_major))
    if release is None:
        raise ValueError(
            f"No Jetson test RPM manifest for RHEL {rhel_major}; "
            f"supported releases: {', '.join(sorted(manifest['rhel']))}"
        )

    definitions = release.get("groups", {})
    unknown = groups - definitions.keys()
    if unknown:
        raise ValueError(f"Unknown test RPM dependency groups: {', '.join(sorted(unknown))}")

    required = []
    optional = []
    for group in sorted(groups):
        definition = definitions[group]
        required.extend(definition.get("required", []))
        optional.extend(definition.get("optional", []))

    hardware_spec = hardware_spec or {}
    for condition in release.get("conditional", []):
        if condition.get("group") not in groups:
            continue
        actual = _capability_value(hardware_spec, condition.get("capability", ""))
        if actual == condition.get("equals"):
            required.extend(condition.get("required", []))
            optional.extend(condition.get("optional", []))

    for package in required + optional:
        if not _RPM_NAME.fullmatch(package):
            raise ValueError(f"Invalid RPM name in {MANIFEST_PATH}: {package!r}")
    return list(dict.fromkeys(required)), list(dict.fromkeys(optional))


def install_host_rpms(ssh, required: list[str], optional: list[str]) -> None:
    """Install required packages before tests and report unavailable optional RPMs."""
    if required:
        command = "dnf install -y " + shlex.join(required)
        result = ssh.sudo(command, timeout=900, fail_on_rc=False)
        if result.exit_status != 0:
            raise RuntimeError(
                "Failed to install required Jetson test RPMs: "
                f"{', '.join(required)}\n{result.stderr or result.stdout}"
            )

    for package in optional:
        result = ssh.sudo(
            "dnf install -y " + shlex.quote(package),
            timeout=900,
            fail_on_rc=False,
        )
        if result.exit_status != 0:
            logger.warning(
                f"Optional test RPM {package!r} is unavailable; tests that need "
                "its binaries may skip. "
                f"{result.stderr or result.stdout}"
            )
