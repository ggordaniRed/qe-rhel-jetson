import pytest
from tests_resources.device_ops import reboot_and_reconnect, set_kernel_arg, get_systemd_target
from tests_suites import conftest as _conftest
import os
import warnings
import logging

logger = logging.getLogger(__name__)


@pytest.fixture(scope="class")
def ensure_pd_ignore_unused(ssh):
    """Ensure pd_ignore_unused is in the kernel cmdline before display tests.

    Only needed on RHEL 9.7 (Tech Preview) + multi-user.target (the confirmed
    default base image target) as a safety measure to prevent kernel hang.
    RHEL 9.8+ (GA) does not require this workaround.

    - RHEL 9.8+ or graphical.target -> skip, just yield ssh
    - RHEL 9.7 + multi-user.target + already present -> proceed normally
    - RHEL 9.7 + multi-user.target + missing + Beaker -> add via grubby, reboot
    - RHEL 9.7 + multi-user.target + missing + Jumpstarter -> skip (reboot kills tunnel)
    """
    # Only needed on RHEL 9.7; skip when version unknown or not 9.7
    if _conftest.RHEL_VERSION is None or _conftest.RHEL_VERSION != "9.7":
        yield ssh
        return

    # graphical.target loads nvidia_drm safely - no need to add pd_ignore_unused
    if get_systemd_target(ssh) == "graphical.target":
        yield ssh
        logger.info("graphical.target - skipping pd_ignore_unused")
        return

    logger.info("setting pd_ignore_unused is needed (RHEL 9.7 + non graphical.target)")
    needs_reboot = set_kernel_arg(ssh, "pd_ignore_unused")
    if not needs_reboot:
        yield ssh
        return

    # Needs reboot to apply. On Jumpstarter this will pytest.skip().
    if os.environ.get("JUMPSTARTER_IN_USE"):
        logger.info("Reboot was needed to set pd_ignore_unused, but not supported through Jumpstarter SSH tunnel")
        warnings.warn(
            "Reboot was needed to set pd_ignore_unused (see jumpstarter/README.md), "
            "but not supported through Jumpstarter SSH tunnel — the TCP port-forward breaks when the device goes down",
            UserWarning,
        )
    new_ssh = reboot_and_reconnect(ssh)
    # Verify the arg took effect
    check = new_ssh.run("grep -i pd_ignore_unused /proc/cmdline", fail_on_rc=False)
    assert check.exit_status == 0, "pd_ignore_unused not in cmdline after reboot"
    yield new_ssh
