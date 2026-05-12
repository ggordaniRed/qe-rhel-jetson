"""
Shared pytest configuration and fixtures for all tests.
This file imports SSHConnection from infra_tests/ssh_client.py and
collects hardware info from tests_resources/hardware_info.py for use in all tests.

Test tiers:
  - Basic tests (default): run with `pytest tests_suites/`
  - Extra tests: marked with @pytest.mark.extra, run with `pytest tests_suites/ --run-extra`
"""
import pytest
import os
import re
import time
from datetime import datetime
from pathlib import Path
import sys
from typing import Optional, Union, Dict, Any
import yaml
from infra_tests.ssh_client import SSHConnection
from tests_resources.hardware_info import (
    collect as collect_hardware_info,
    compare_versions,
)
import logging
logger = logging.getLogger(__name__)

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Configuration - can be overridden via environment variables
JETSON_HOST = os.getenv("JETSON_HOST")
JETSON_USERNAME = os.getenv("JETSON_USERNAME")
JETSON_PASSWORD = os.getenv("JETSON_PASSWORD")
JETSON_KEY_PATH = os.getenv("JETSON_KEY_PATH")  # e.g. ~/.ssh/id_rsa or ~/.ssh/id_ed25519
JETSON_PORT = int(os.getenv("JETSON_PORT", "22"))
JETSON_TIMEOUT = int(os.getenv("JETSON_TIMEOUT", "60"))  # Connection timeout per attempt (seconds)

# Validate required environment variables (need either password or key path)
missing_vars = []
if not JETSON_HOST:
    missing_vars.append("JETSON_HOST")
if not JETSON_USERNAME:
    missing_vars.append("JETSON_USERNAME")
if not JETSON_PASSWORD and not JETSON_KEY_PATH:
    missing_vars.append("JETSON_PASSWORD or JETSON_KEY_PATH")

if missing_vars:
    error_msg = (f"Missing required environment variables: {', '.join(missing_vars)}\n")
    logger.error(error_msg)
    raise ValueError(error_msg)

# ---------------------------------------------------------------------------
# Hardware info variables (set by hardware_info_session fixture; use in tests)
# All are None by default if value not found.
# ---------------------------------------------------------------------------
RHEL_VERSION: Optional[str] = None  # str to avoid float('9.10') == 9.1
L4T_VERSION: Optional[Union[float, str]] = None  # L4T from /etc/nv_tegra_release
JETPACK_VERSION: Optional[str] = None  # userspace RPM version (e.g. '6.2.2')
JETPACK_KMOD_VERSION: Optional[str] = None  # kmod RPM version (e.g. '6.2.2')
FIRMWARE_VERSION: Optional[Union[float, str]] = None  # str if X.Y.Z, float if X.Y
FIRMWARE_TYPE: Optional[str] = None
HARDWARE_MODEL_NAME: Optional[str] = None
KERNEL_VERSION: Optional[str] = None
CPU_ARCH: Optional[str] = None
BOOTC_AVAILABLE: bool = False
BOOTC_VERSION: Optional[Union[float, str]] = None  # str if X.Y.Z, float if X.Y
BOOTC_IMAGE_VERSION: Optional[Union[str]] = None
BOOTC_IMAGE_URL: Optional[str] = None
SECURE_BOOT_STATE: Optional[str] = None

if JETSON_KEY_PATH: # key path provided, use key-based authentication
    key_path = os.path.expanduser(JETSON_KEY_PATH)
    if not os.path.exists(key_path):
        raise ValueError(
            f"SSH key file not found: {key_path}\n"
            f"Set JETSON_KEY_PATH to a valid SSH private key path."
        )
else: # no key path provided, use password authentication
    key_path = None

# ---------------------------------------------------------------------------
# Internal Functions
# --------------------------------------------------------------------------- 

def _load_hardware_specs() -> Dict[str, Any]:
    """Load jetson_hardware_specs.yaml once."""
    path = Path(__file__).parent / "jetson_hardware_specs.yaml"
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Hardware specs must be a dictionary, got {type(data)}")
        return data
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in {path}: {e}")
    except FileNotFoundError:
        raise ValueError(f"Hardware specs file not found: {path}")

def _install_beaker_repo(ssh, rhel_version: Optional[str]):
    """
    Ensure nightly repos (AppStream/BaseOS) and EPEL exist on the Jetson.
    On Beaker deployments the Ansible playbook handles this; 
    on Jumpstarter deployments there is no playbook, so this function installs them as fallback.
    
    download.devel.redhat.com uses a Red Hat internal self-signed cert chain -
    bootc images don't include the internal CA, so sslverify=0 is required.
    """
    logger.info("[Setup] Verifying repositories exist on the Jetson...")
    if rhel_version is None:
        raise ValueError("RHEL version not found, The environment is not a RHEL machine")

    main_rhel_version = str(rhel_version).split(".")[0]
    nightly_base = f"https://download.devel.redhat.com/rhel-{main_rhel_version}/nightly/RHEL-{main_rhel_version}/latest-RHEL-{rhel_version}/compose"
    cmd_appstream = f"dnf config-manager --add-repo {nightly_base}/AppStream/aarch64/os/"
    cmd_baseos = f"dnf config-manager --add-repo {nightly_base}/BaseOS/aarch64/os/"
    # sslverify=0: download.devel.redhat.com uses Red Hat internal CA not in bootc trust store
    cmd_sslverify = (
        'for f in /etc/yum.repos.d/download.devel.redhat.com_*.repo; do '
        '[ -f "$f" ] && grep -q "^sslverify" "$f" '
        '&& sed -i "s/^sslverify.*/sslverify=0/" "$f" '
        '|| echo "sslverify=0" >> "$f"; done'
    )
    # --nogpgcheck: bootc images don't ship pre-imported GPG keys
    cmd_epel = f"dnf install https://dl.fedoraproject.org/pub/epel/epel-release-latest-{main_rhel_version}.noarch.rpm -y"

    result = ssh.sudo("dnf repolist")
    repos = result.stdout.lower()

    if "appstream" not in repos or "baseos" not in repos:
        logger.info("[Setup] Nightly repos missing, installing for RHEL %s", rhel_version)
        for attempt in range(1, 4):
            try:
                ssh.sudo(cmd_appstream)
                ssh.sudo(cmd_baseos)
                ssh.sudo(cmd_sslverify)
                break
            except Exception as e:
                if attempt < 3:
                    logger.warning("Repo setup failed (attempt %s/3): %s, retrying...", attempt, e)
                    time.sleep(5)
                else:
                    raise

    if "epel" not in repos:
        logger.info("[Setup] EPEL missing, installing for RHEL %s", rhel_version)
        ssh.sudo(cmd_epel)

def _get_target_versions(jetpack_userspace_version: Optional[str]) -> Optional[Dict[str, str]]:
    """Return target version dict for the given Jetpack version, or None."""
    specs = _load_hardware_specs()
    targets = specs.get("_target_versions", {})
    return targets.get(str(jetpack_userspace_version)) if jetpack_userspace_version else None

def _verify_target_versions() -> list[str]:
    """Verify detected versions match targets. Returns list of mismatch messages."""
    target = _get_target_versions(JETPACK_VERSION)
    if target is None:
        return []
    mismatches = []
    checks = [
        (RHEL_VERSION, "rhel_version", "RHEL"),
        (FIRMWARE_VERSION, "uefi_firmware_version", "UEFI firmware"),
        (L4T_VERSION, "l4t_version", "L4T"),
        (KERNEL_VERSION, "kernel_version", "Kernel"),
        # kmod version is not checked here, it is checked in sanity/test_version_check.py
    ]
    for actual, key, label in checks:
        expected = target.get(key)
        if not compare_versions(actual, expected):
            mismatches.append(f"{label}: actual={actual}, target={expected}")
    return mismatches

# ---------------------------------------------------------------------------
# Public Functions
# ---------------------------------------------------------------------------

def get_hardware_spec(hardware_model_name: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Return the expected-spec dict for the given hardware model name, or None if unknown.
    hardware_model_name comes from the device (dmidecode Product Name or devicetree model).
    """
    if not hardware_model_name:
        return None
    specs = _load_hardware_specs()
    name_lower = hardware_model_name.lower()
    for key in specs:
        if key.lower() in name_lower:
            spec = specs.get(key)
            if spec and not str(key).startswith("_"):
                return spec
    return None

def fetch_hardware_logs(ssh):
    """
    Fetch hardware logs and outputs from the Jetson.
    Logs and outputs are saved to qe-rhel-jetson/device_logs/ as a tar.gz archive.
    """
    from tests_resources.device_logs_collector import save_device_logs
    output_dir = Path(__file__).parent.parent / "device_logs"
    archive_path = save_device_logs(ssh, BOOTC_IMAGE_URL, output_dir)
    logger.info("[Teardown] Device logs saved to: %s", archive_path)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# ! FYI:
# ! session fixture run once at the beginning of the test session
# ! class fixture run once for each test class

@pytest.fixture(scope="session", autouse=True)
def hardware_info_session():
    """
    Collect hardware and system info from the Jetson via SSH at session start.
    Sets global variables and prints SETUP summary for each pytest run.
    """
    with SSHConnection(
        JETSON_HOST,
        JETSON_USERNAME,
        JETSON_PASSWORD or None,
        JETSON_PORT,
        JETSON_TIMEOUT,
        key_filename=key_path,
    ) as ssh:
        info = collect_hardware_info(ssh)

    # Set module-level variables so all tests can import them
    global RHEL_VERSION, L4T_VERSION, JETPACK_VERSION, JETPACK_KMOD_VERSION
    global FIRMWARE_VERSION, FIRMWARE_TYPE
    global HARDWARE_MODEL_NAME, KERNEL_VERSION, CPU_ARCH
    global BOOTC_AVAILABLE, BOOTC_VERSION, BOOTC_IMAGE_URL, BOOTC_IMAGE_VERSION
    global SECURE_BOOT_STATE
    RHEL_VERSION = info.get("rhel_version")
    L4T_VERSION = info.get("l4t_version")
    JETPACK_VERSION = info.get("jetpack_version")
    JETPACK_KMOD_VERSION = info.get("jetpack_kmod_version")
    FIRMWARE_VERSION = info.get("firmware_version")
    FIRMWARE_TYPE = info.get("firmware_type")
    HARDWARE_MODEL_NAME = info.get("hardware_model_name")
    KERNEL_VERSION = info.get("kernel_version")
    CPU_ARCH = info.get("cpu_arch")
    BOOTC_AVAILABLE = info.get("bootc_available", False)
    BOOTC_VERSION = info.get("bootc_version")
    BOOTC_IMAGE_VERSION = info.get("bootc_image_version")
    BOOTC_IMAGE_URL = info.get("bootc_image_url")
    SECURE_BOOT_STATE = info.get("secure_boot_state")
    # Skip entire session if hardware model is not in Testing Matrix (jetson_hardware_specs.yaml)
    if get_hardware_spec(HARDWARE_MODEL_NAME) is None:
        pytest.skip(
            f"Hardware model not included in Testing Matrix: {HARDWARE_MODEL_NAME!r}. "
            "Add the device to tests_suites/jetson_hardware_specs.yaml to run tests."
        )

    # Skip if RHEL is not installed
    if RHEL_VERSION is None:
        pytest.skip(
            f"No RHEL installation detected on {JETSON_HOST}. "
            "These tests require a RHEL-based OS."
        )

    # Skip if JetPack RPMs are not installed
    if JETPACK_VERSION is None:
        pytest.skip(
            f"JetPack RPMs not installed on {JETSON_HOST}. "
            "Install nvidia-jetpack-for-rhel RPMs before running tests."
        )

    # Skip if no target specs defined for this JetPack version
    target = _get_target_versions(JETPACK_VERSION)
    if target is None:
        pytest.skip(
            f"No target specs defined for JetPack {JETPACK_VERSION}. "
            "Add an entry to _target_versions in "
            "tests_suites/jetson_hardware_specs.yaml."
        )

    # Skip entire session if detected versions don't match targets
    mismatches = _verify_target_versions()
    if mismatches:
        pytest.skip("Version mismatch — " + "; ".join(mismatches))

    # Print SETUP summary for each pytest run (values may be None if not found)
    fw_ver = f" {FIRMWARE_VERSION}" if FIRMWARE_VERSION is not None else ""
    print("\n" + "=" * 80)
    print("SETUP SUMMARY")
    print("=" * 80)
    print(f"Hardware model name:         {HARDWARE_MODEL_NAME}")
    print("=" * 80)
    print(f"1. RHEL version:             {RHEL_VERSION}")
    print(f"2. Kernel version:           {KERNEL_VERSION}")
    print(f"3. L4T version:              {L4T_VERSION}")
    print(f"4. JetPack version (RPM):    {JETPACK_VERSION}")
    print(f"5. JetPack kmod (RPM):       {JETPACK_KMOD_VERSION}")
    print(f"6. Firmware type/version:    {FIRMWARE_TYPE}{fw_ver}")
    print(f"7. Secure boot state:        {SECURE_BOOT_STATE}")
    print("=" * 80)
    print(f"8. Bootc available:          {BOOTC_AVAILABLE}")
    print(f"9. Bootc image version:      {BOOTC_IMAGE_VERSION}")
    print(f"10. Bootc image URL:          {BOOTC_IMAGE_URL}")
    print("=" * 80 + "\n")
    yield

@pytest.fixture(scope="session", autouse=True)
def l4t_image_pulled(hardware_info_session):
    """Pre-pull L4T JetPack container image once per session.
    Podman caches the base layer — subsequent podman build FROM this image
    only downloads the test-specific layers on top."""
    from tests_resources.container_ops import L4T_JETPACK_IMAGE
    with SSHConnection(
        JETSON_HOST,
        JETSON_USERNAME,
        JETSON_PASSWORD or None,
        JETSON_PORT,
        JETSON_TIMEOUT,
        key_filename=key_path,
    ) as ssh:
        ssh.sudo(f"podman pull {L4T_JETPACK_IMAGE}", timeout=300, fail_on_rc=False)
    yield

@pytest.fixture(scope="session", autouse=True)
def beaker_repo_session(hardware_info_session):
    """
    Install Beaker repositories on the Jetson after hardware info is collected.
    Depends on hardware_info_session to ensure RHEL_VERSION is available.
    """
    with SSHConnection(
        JETSON_HOST,
        JETSON_USERNAME,
        JETSON_PASSWORD or None,
        JETSON_PORT,
        JETSON_TIMEOUT,
        key_filename=key_path,
    ) as ssh:
        _install_beaker_repo(ssh, RHEL_VERSION)
    yield

@pytest.fixture(scope="session", autouse=True)
def fetch_hardware_logs_session(hardware_info_session):
    """
    Fetch hardware logs and outputs from the Jetson at session teardown.
    Depends on hardware_info_session to ensure BOOTC_IMAGE_URL is available.
    Logs are saved to qe-rhel-jetson/device_logs/ as a tar.gz archive.
    """
    yield
    logger.info("[Teardown] Fetching hardware logs from the Jetson...")
    with SSHConnection(
        JETSON_HOST,
        JETSON_USERNAME,
        JETSON_PASSWORD or None,
        JETSON_PORT,
        JETSON_TIMEOUT,
        key_filename=key_path,
    ) as ssh:
        fetch_hardware_logs(ssh)
    logger.info("[Teardown] Hardware logs fetched successfully")

@pytest.fixture(scope="class")
def ssh():
    """
    SSH fixture that connects directly to Jetson.
    Can be configured via environment variables:
    - JETSON_HOST: Hostname or IP address
    - JETSON_USERNAME: SSH username
    - JETSON_PASSWORD: SSH password (optional if JETSON_KEY_PATH is set)
    - JETSON_KEY_PATH: Path to private key, e.g. ~/.ssh/id_rsa (use when auth is key-based)
    - JETSON_PORT: SSH port (default: 22)
    
    Note: This fixture depends on hardware_info fixture which collects
    hardware information at the beginning of the test session.
    """
    logger.info(
        "[SSH Fixture] Using JETSON_HOST=%s JETSON_PORT=%s JETSON_USERNAME=%s JETSON_TIMEOUT=%s",
        JETSON_HOST, JETSON_PORT, JETSON_USERNAME, JETSON_TIMEOUT,
    )
    key_path = os.path.expanduser(JETSON_KEY_PATH) if JETSON_KEY_PATH else None
    try:
        with SSHConnection(
            JETSON_HOST,
            JETSON_USERNAME,
            JETSON_PASSWORD or None,
            JETSON_PORT,
            JETSON_TIMEOUT,
            key_filename=key_path,
        ) as ssh:
            yield ssh
    except Exception as e:
        error_msg = (
            f"Failed to establish SSH connection to {JETSON_HOST}:{JETSON_PORT}\n"
            f"Error: {str(e)}\n"
            f"Please verify:\n"
            f"  - Environment variables are set correctly (JETSON_HOST, JETSON_USERNAME, JETSON_PASSWORD or JETSON_KEY_PATH)\n"
            f"  - For key-based auth, set JETSON_KEY_PATH=~/.ssh/id_rsa (or your private key path)\n"
            f"  - Host is reachable (try: ping {JETSON_HOST} or telnet {JETSON_HOST} {JETSON_PORT})\n"
            f"  - SSH service is running on the target\n"
            f"  - Credentials are correct\n"
            f"  - Firewall allows SSH connections\n"
            f"  - Network connectivity is available"
        )
        logger.error(f"\n[SSH Fixture] {error_msg}\n")
        raise

# ---------------------------------------------------------------------------
# Session File Logger — mirrors all terminal output to a timestamped file
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

class _TeeWriter:
    """Wraps a stream to duplicate writes to a file, stripping ANSI codes from the log."""

    def __init__(self, original, log_file):
        self._original = original
        self._log_file = log_file

    def write(self, data):
        self._original.write(data)
        try:
            self._log_file.write(_ANSI_RE.sub('', data))
        except (ValueError, OSError):
            pass

    def flush(self):
        self._original.flush()
        try:
            self._log_file.flush()
        except (ValueError, OSError):
            pass

    def __getattr__(self, name):
        return getattr(self._original, name)

class SessionFileLogger:

    def __init__(self):
        self._log_file = None
        self._report_path = None

    def pytest_configure(self, config):
        host = os.getenv("JETSON_HOST", "unknown-host").split(".")[0]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_dir = Path(__file__).parent.parent / "test-results"
        results_dir.mkdir(exist_ok=True)
        self._report_path = results_dir / f"report_{host}_{timestamp}.txt"
        self._log_file = open(self._report_path, "w", buffering=1)
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        sys.stdout = _TeeWriter(self._orig_stdout, self._log_file)
        sys.stderr = _TeeWriter(self._orig_stderr, self._log_file)

    def pytest_unconfigure(self, config):
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        if self._log_file and not self._log_file.closed:
            self._log_file.close()
        if self._report_path and self._report_path.exists():
            print(f"\n📄 Session report saved to: {self._report_path}")

def pytest_configure(config):
    config.pluginmanager.register(SessionFileLogger(), "session_file_logger")

# ---------------------------------------------------------------------------
# Extra-tests support: @pytest.mark.extra tests are excluded by default.
# Run them with: pytest tests_suites/ --run-extra
# Or run ONLY extras: pytest tests_suites/ -m extra --run-extra
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--run-extra",
        action="store_true",
        default=False,
        help="Run tests marked with @pytest.mark.extra (skipped by default)",
    )

def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-extra"):
        return  # --run-extra given: run everything
    skip_extra = pytest.mark.skip(reason="Extra test — use --run-extra to run")
    for item in items:
        if "extra" in item.keywords:
            item.add_marker(skip_extra)
