"""
Device operations for Jetson RPM tests.
Provides reboot, reconnection, and kernel configuration utilities
that any test module can use.

Usage:
    from tests_resources.device_ops import reboot_and_reconnect, set_kernel_arg
"""
import re
import time
import logging
import os
import pytest

logger = logging.getLogger(__name__)


def _get_efi_boot_info(ssh):
    """
    Query EFI boot order from the device. Returns (original_boot_order, rhel_entry)
    or (None, None) if not a Beaker/EFI machine.

    Beaker machines have PXE boot entries and a RHEL entry. Before rebooting,
    RHEL must be set first in boot order, otherwise the machine PXE boots
    and is lost. After reboot, the original order is restored so Beaker
    can reclaim the machine when the reservation ends.
    """
    result = ssh.sudo("efibootmgr", fail_on_rc=False, print_output=False)
    if result.exit_status != 0 or not result.stdout:
        return None, None

    output = result.stdout

    # Extract current boot order (e.g. "BootOrder: 0003,0000,0001")
    order_match = re.search(r"BootOrder:\s*(.+)", output)
    if not order_match:
        return None, None
    original_boot_order = order_match.group(1).strip()

    # Find RHEL boot entry (e.g. "Boot0003* Red Hat Enterprise Linux")
    rhel_match = re.search(r"Boot([0-9A-Fa-f]{4})\*\s*Red Hat Enterprise Linux", output)
    if not rhel_match:
        return None, None

    return original_boot_order, rhel_match.group(1)


def reboot_and_reconnect(ssh, timeout=300, poll_interval=10):
    """
    Reboot the device via SSH and return a new SSHConnection once it's back up.

    On Beaker machines (detected by EFI boot entries with RHEL + PXE), sets RHEL
    first in boot order before rebooting and restores the original order after.
    On Jumpstarter, reboots without touching boot order.

    Args:
        ssh: Current SSHConnection instance (will be closed after reboot command)
        timeout: Max seconds to wait for the device to come back (default: 300)
        poll_interval: Seconds between reconnection attempts (default: 10)

    Returns:
        A new SSHConnection instance to the rebooted device.

    Raises:
        TimeoutError: If the device does not come back within the timeout.
    """
    # Jumpstarter SSH tunnel (TcpPortforwardAdapter) breaks on reboot — the
    # exporter can't reach the device while it's down, killing the tunnel
    # permanently.  All subsequent tests would fail with AuthenticationException.
    if os.environ.get("JUMPSTARTER_IN_USE"):
        pytest.skip(
            "Reboot not supported through Jumpstarter SSH tunnel — "
            "the TCP port-forward breaks when the device goes down"
        )

    # Import here to avoid circular imports (conftest imports ssh_client)
    from tests_suites import conftest as _conftest
    SSHConnection = _conftest.SSHConnection
    JETSON_HOST = _conftest.JETSON_HOST
    JETSON_USERNAME = _conftest.JETSON_USERNAME
    JETSON_PASSWORD = _conftest.JETSON_PASSWORD
    JETSON_PORT = _conftest.JETSON_PORT
    JETSON_TIMEOUT = _conftest.JETSON_TIMEOUT
    JETSON_KEY_PATH = _conftest.JETSON_KEY_PATH

    key_path = os.path.expanduser(JETSON_KEY_PATH) if JETSON_KEY_PATH else None

    # Check if this is a Beaker machine (EFI with RHEL + PXE boot entries)
    original_boot_order, rhel_entry = _get_efi_boot_info(ssh)
    if original_boot_order is None or rhel_entry is None:
        raise ValueError("Failed to get EFI boot info")

    logger.info(f"Original boot order: {original_boot_order}")
    # Set RHEL first in boot order so the machine boots RHEL, not PXE
    other_entries = [e for e in original_boot_order.split(",") if e != rhel_entry]
    new_order = ",".join([rhel_entry] + other_entries)
    logger.info("Beaker machine detected — setting RHEL first in boot order: %s", new_order)
    ssh.sudo(f"efibootmgr -o {new_order}", print_output=False)

    logger.info("Rebooting device %s ...", JETSON_HOST)
    ssh.sudo("reboot", fail_on_rc=False)

    # Wait for the device to go down
    logger.info("Waiting for device to go down ...")
    time.sleep(15)

    # Poll until SSH is back
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            logger.info("Attempting reconnect to %s:%s ...", JETSON_HOST, JETSON_PORT)
            new_ssh = SSHConnection(
                JETSON_HOST,
                JETSON_USERNAME,
                JETSON_PASSWORD or None,
                JETSON_PORT,
                JETSON_TIMEOUT,
                key_filename=key_path,
            )
            logger.info("Reconnected to %s after reboot", JETSON_HOST)

            # Restore original boot order so Beaker can reclaim via PXE
            logger.info("Restoring original boot order: %s", original_boot_order)
            new_ssh.sudo(f"efibootmgr -o {original_boot_order}", print_output=False)

            return new_ssh
        except Exception:
            logger.info("Device not ready yet, retrying in %ss ...", poll_interval)
            time.sleep(poll_interval)

    raise TimeoutError(
        f"Device {JETSON_HOST} did not come back after reboot within {timeout}s"
    )


def get_systemd_target(ssh):
    """Return the current systemd default target (e.g. 'multi-user.target', 'graphical.target')."""
    result = ssh.run("systemctl get-default", fail_on_rc=False)
    if result.exit_status != 0:
        raise RuntimeError(f"Failed to get systemd target: {result.stderr}")
    return result.stdout.strip()


def set_kernel_arg(ssh, arg):
    """
    Add a kernel boot argument if not already present.
    Tries grubby first, then falls back to ostree kargs for bootc systems.
    Returns True if the argument was added (reboot needed), False if already set.

    Args:
        ssh: SSHConnection instance
        arg: Kernel argument string (e.g. 'pd_ignore_unused')

    Returns:
        bool: True if argument was added (reboot required), False if already present.
    """
    check = ssh.run(f"grep -i '{arg}' /proc/cmdline", fail_on_rc=False)
    if check.exit_status == 0:
        logger.info("Kernel argument '%s' already set", arg)
        return False

    # Try grubby first
    logger.info("Adding kernel argument '%s' via grubby", arg)
    ssh.sudo(f"grubby --update-kernel=ALL --args={arg}")

    # Verify grubby actually added it to the default boot entry
    verify = ssh.sudo("grubby --info=DEFAULT", fail_on_rc=False, print_output=False)
    if verify.exit_status == 0 and arg in verify.stdout:
        logger.info("Verified '%s' in default boot entry via grubby", arg)
        return True

    # grubby didn't apply — try ostree kargs on bootc systems
    from tests_suites import conftest as _conftest
    if _conftest.BOOTC_AVAILABLE:
        logger.warning("grubby did not add '%s' to boot entry, trying ostree kargs", arg)
        ostree_result = ssh.sudo(
            f"ostree admin kargs edit-in-place --append-if-missing={arg}",
            fail_on_rc=False,
        )
        if ostree_result.exit_status == 0:
            logger.info("Added '%s' via ostree admin kargs", arg)
            return True

    raise RuntimeError(
        f"Failed to add kernel argument '{arg}' via grubby or ostree (in case of bootc system). "
        f"grubby --info=DEFAULT output: {verify.stdout}"
    )
