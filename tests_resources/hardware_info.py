"""
Hardware and system information collection from a Jetson device via SSH.

Provides individual getter functions (e.g. get_rhel_version, get_l4t_version,
get_jetpack_userspace_version) that any test can call directly, plus a collect()
convenience function that gathers everything at once (used by tests_suites/conftest.py
to set global variables).

Usage:
    from tests_resources.hardware_info import get_rhel_version, get_l4t_version
    rhel = get_rhel_version(ssh)

    # Or collect everything at once:
    from tests_resources.hardware_info import collect
    info = collect(ssh)
"""
import re
import logging
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)


def _run(ssh, command: str, timeout: Optional[int] = 30) -> str:
    """Run command via SSH and return stripped stdout, or empty string on failure."""
    try:
        result = ssh.run(command, timeout=timeout, print_output=False)
        return (result.stdout or "").strip() if result.exit_status == 0 else ""
    except (OSError, TimeoutError, RuntimeError, EOFError) as e:
        logger.debug("Command %r failed: %s", command, e)
        return ""


def _run_sudo(ssh, command: str, timeout: Optional[int] = 30) -> str:
    """Run command with sudo via SSH and return stripped stdout, or empty string on failure."""
    try:
        result = ssh.run(f"sudo {command}", timeout=timeout, print_output=False)
        return (result.stdout or "").strip() if result.exit_status == 0 else ""
    except (OSError, TimeoutError, RuntimeError, EOFError) as e:
        logger.debug("Command sudo %r failed: %s", command, e)
        return ""


def _parse_decimal(s: str) -> Optional[Union[float, str]]:
    """
    Extract a decimal from string.
    - If 3 numbers with 2 dots exist (e.g. 1.2.3), returns that string.
    - If only 1 dot exists (e.g. 1.2), returns a one-dot decimal as float.
    """
    if not s:
        return None
    # Prefer X.Y.Z (three numbers, two dots)
    match_three = re.search(r"(\d+\.\d+\.\d+)", s)
    if match_three:
        return match_three.group(1)
    # Else one dot: X.Y (two numbers)
    match_two = re.search(r"(\d+\.\d+)", s)
    if match_two:
        try:
            return float(match_two.group(1))
        except ValueError:
            pass
    # Else single integer
    match_one = re.search(r"(\d+)", s)
    if match_one:
        try:
            return float(match_one.group(1))
        except ValueError:
            pass
    return None


def get_rhel_version(ssh) -> Optional[str]:
    """Return RHEL version as string (e.g. '9.7', '9.10'), or None if not found.
    Always returns a string to avoid float precision issues (e.g. float('9.10') == 9.1)."""
    rhel = _run(ssh, "cat /etc/redhat-release")
    if not rhel:
        return None
    match = re.search(r"(\d+\.\d+)", rhel)
    return match.group(1) if match else None


def get_tegra_release(ssh) -> str:
    return _run(ssh, "cat /etc/nv_tegra_release")


def get_l4t_version(ssh) -> Optional[Union[float, str]]:
    """Return L4T version from /etc/nv_tegra_release (e.g. '36.5.0').
    Returns str for X.Y.Z, float for X.Y, or None."""
    jetpack_raw = get_tegra_release(ssh).splitlines()[0]
    if not jetpack_raw:
        return None

    r_match = re.search(r"R(\d+)(?:\.(\d+))?", jetpack_raw)
    rev_match = re.search(r"REVISION:\s*([\d.]+)", jetpack_raw)
    if r_match and rev_match:
        r_major = int(r_match.group(1))
        rev_str = rev_match.group(1).strip()
        rev_parts = rev_str.split(".")
        rev_first = int(rev_parts[0]) if rev_parts and rev_parts[0].isdigit() else 0
        rev_second = int(rev_parts[1]) if len(rev_parts) > 1 and rev_parts[1].isdigit() else 0
        return f"{r_major}.{rev_first}.{rev_second}"
    if rev_match:
        try:
            return float(rev_match.group(1).strip())
        except ValueError:
            pass
    if r_match:
        try:
            major = int(r_match.group(1))
            minor = int(r_match.group(2) or 0)
            return float(f"{major}.{minor}")
        except ValueError:
            pass
    return _parse_decimal(jetpack_raw)


def get_jetpack_userspace_version(ssh) -> Optional[str]:
    """Return JetPack userspace RPM version (e.g. '6.2.2') from nvidia-jetpack-*-core RPM."""
    rpm_output = _run(ssh, "rpm -qa | grep 'nvidia-jetpack.*core'")
    if not rpm_output:
        return None
    match = re.search(r"core-(\d+\.\d+\.\d+)", rpm_output)
    return match.group(1) if match else None


# Backward-compat alias: get_jetpack_version now returns userspace RPM version
get_jetpack_version = get_jetpack_userspace_version


def get_jetpack_kmod_version(ssh) -> Optional[str]:
    """Return JetPack kmod RPM version (e.g. '6.2.2') from nvidia-jetpack-*-kmod RPM."""
    rpm_output = _run(ssh, "rpm -qa | grep 'nvidia-jetpack.*kmod'")
    if not rpm_output:
        return None
    match = re.search(r"kmod-(\d+\.\d+\.\d+)", rpm_output)
    return match.group(1) if match else None


def get_all_jetpack_rpm_versions(ssh) -> dict[str, str]:
    """Return dict of {component: version} for all nvidia-jetpack RPMs."""
    output = _run(ssh, "rpm -qa | grep nvidia-jetpack | sort")
    if not output:
        return {}
    versions = {}
    for line in output.strip().splitlines():
        match = re.search(r"nvidia-jetpack-for-rhel-[\d.]+-(.+?)-(\d+\.\d+\.\d+)", line)
        if match:
            versions[match.group(1)] = match.group(2)
    return versions


def compare_versions(actual: Optional[Union[float, str]], target) -> bool:
    """Exact version comparison. Converts both to strings.
    For kernel versions (target contains '-'): uses prefix match.
    If target is a list, returns True if actual matches any entry.
    Returns True if they match."""
    if actual is None or target is None:
        return False
    if isinstance(target, list):
        return any(compare_versions(actual, t) for t in target)
    actual_str, target_str = str(actual), str(target)
    if "-" in target_str:
        return actual_str.startswith(target_str)
    return actual_str == target_str


def compare_versions_gte(actual: Optional[str], target: Optional[str]) -> bool:
    """Return True if actual version >= target version.
    Handles X.Y and X.Y.Z formats."""
    if actual is None or target is None:
        return False
    actual_parts = [int(x) for x in str(actual).split(".")]
    target_parts = [int(x) for x in str(target).split(".")]
    max_len = max(len(actual_parts), len(target_parts))
    actual_parts.extend([0] * (max_len - len(actual_parts)))
    target_parts.extend([0] * (max_len - len(target_parts)))
    return actual_parts >= target_parts


def get_firmware_info(ssh) -> dict[str, Any]:
    """Return firmware version and type from dmidecode.
    Returns dict with keys: firmware_version (float|str|None), firmware_type (str|None)."""
    firmware_version = None
    firmware_type = None

    dmidecode = _run_sudo(ssh, "dmidecode -t bios")
    if dmidecode:
        ver_match = re.search(r"Version:\s*(.+)", dmidecode, re.MULTILINE | re.IGNORECASE)
        if ver_match:
            firmware_version = _parse_decimal(ver_match.group(1).strip())
        if "UEFI" in dmidecode.upper() or "EFI" in dmidecode.upper():
            firmware_type = "UEFI"
        else:
            firmware_type = "BIOS"
    if firmware_type is None:
        efi_check = _run(ssh, "test -d /sys/firmware/efi && echo UEFI || echo BIOS").strip()
        firmware_type = efi_check if efi_check else None

    return {"firmware_version": firmware_version, "firmware_type": firmware_type}


def get_hardware_model_name(ssh) -> Optional[str]:
    """Return hardware model name from devicetree or dmidecode."""
    model = _run(ssh, "cat /sys/firmware/devicetree/base/model | tr -d '\\0'")
    if not model:
        sysinfo = _run_sudo(ssh, "dmidecode -t system")
        if sysinfo:
            m = re.search(r"Product Name:\s*(.+)", sysinfo, re.MULTILINE | re.IGNORECASE)
            if m:
                model = m.group(1).strip()
    return model if model else None


def get_kernel_version(ssh) -> Optional[str]:
    """Return kernel version string (uname -r), or None."""
    kernel = _run(ssh, "uname -r")
    return kernel if kernel else None


def get_cpu_arch(ssh) -> Optional[str]:
    """Return CPU architecture (uname -m), or None."""
    arch = _run(ssh, "uname -m")
    return arch if arch else None


def get_bootc_info(ssh) -> dict[str, Any]:
    """Return bootc/rpm-ostree information.
    Returns dict with keys: bootc_available (bool), bootc_version (float|str|None),
    bootc_image_url (str|None), bootc_image_version (str|None)."""
    bootc_which = _run(ssh, "which bootc")
    rpm_ostree_which = _run(ssh, "which rpm-ostree")
    available = bool(bootc_which or rpm_ostree_which)

    info = {
        "bootc_available": available,
        "bootc_version": None,
        "bootc_image_url": None,
        "bootc_image_version": None,
    }
    if not available:
        return info

    ver_out = _run(ssh, "bootc --version")
    if ver_out:
        info["bootc_version"] = _parse_decimal(ver_out)

    status_out = _run(ssh, "rpm-ostree status")
    if status_out:
        img_ver_match = re.search(r"(?:Version)\s*[:\s]+\s*(\S+.*?)(?:\s*$|\n)", status_out, re.MULTILINE | re.IGNORECASE)
        if img_ver_match:
            info["bootc_image_version"] = img_ver_match.group(1).strip() if img_ver_match.group(1).strip() else None
        # Image URL: extract from "ostree-unverified-registry:<url>" or "ostree-image-signed:docker://<url>"
        registry_match = re.search(r"registry:(?:docker://)?(\S+)", status_out)
        if registry_match:
            info["bootc_image_url"] = registry_match.group(1).strip()

    return info


def get_secure_boot_state(ssh) -> str:
    state = _run(ssh, "mokutil --sb-state").splitlines()[0].split(" ")[1].strip()
    if state == "":
        raise ValueError("Failed to get secure boot state")
    return state


def collect(ssh) -> dict[str, Any]:
    """
    Collect all hardware and system info from the remote host via SSH.
    Calls individual getter functions and merges results into a single dict.

    Args:
        ssh: An SSHConnection instance (from infra_tests.ssh_client).

    Returns:
        Dict with keys (all None if value not found):
        - rhel_version: str | None (e.g. '9.7', '9.10')
        - l4t_version: float | str | None (L4T from /etc/nv_tegra_release)
        - jetpack_version: str | None (userspace RPM version, e.g. '6.2.2')
        - jetpack_userspace_version: str | None (same as jetpack_version)
        - jetpack_kmod_version: str | None (kmod RPM version)
        - firmware_version: float | str | None (str if X.Y.Z, float if X.Y)
        - firmware_type: str | None (e.g. "UEFI", "BIOS")
        - hardware_model_name: str | None
        - kernel_version: str | None
        - cpu_arch: str | None
        - bootc_available: bool (default False if not found)
        - bootc_version: float | str | None (str if X.Y.Z, float if X.Y)
        - bootc_image_url: str | None
        - bootc_image_version: str | None
    """
    firmware = get_firmware_info(ssh)
    bootc = get_bootc_info(ssh)
    l4t = get_l4t_version(ssh)
    userspace = get_jetpack_userspace_version(ssh)
    kmod = get_jetpack_kmod_version(ssh)
    secure_boot_state = get_secure_boot_state(ssh)

    return {
        "rhel_version": get_rhel_version(ssh),
        "l4t_version": l4t,
        "jetpack_version": userspace,
        "jetpack_userspace_version": userspace,
        "jetpack_kmod_version": kmod,
        "firmware_version": firmware["firmware_version"],
        "firmware_type": firmware["firmware_type"],
        "hardware_model_name": get_hardware_model_name(ssh),
        "kernel_version": get_kernel_version(ssh),
        "cpu_arch": get_cpu_arch(ssh),
        "bootc_available": bootc["bootc_available"],
        "bootc_version": bootc["bootc_version"],
        "bootc_image_url": bootc["bootc_image_url"],
        "bootc_image_version": bootc["bootc_image_version"],
        "secure_boot_state": secure_boot_state,
    }
    