"""
Bootc image switch lifecycle tests.

Verifies that the system can successfully switch to a new bootc image,
survive a reboot, and come back healthy — then refreshes all hardware-info
globals so every subsequent test in the session runs with accurate data from
the new image.

Run the full upgrade-then-test pipeline in one go:

    pytest tests_suites/bootc/ tests_suites/ \\
        --bootc-switch-image=registry.gitlab.com/.../rhel-9.7:<new_tag>

The bootc/ directory is listed first so the switch and reboot complete before
the rest of the suite starts.  All subsequent test classes open fresh SSH
connections (class-scoped fixture) and therefore hit the new image automatically.
"""
import logging
import os
import pytest
import tests_suites.conftest as root_conftest
from tests_resources.device_ops import reboot_and_reconnect

logger = logging.getLogger(__name__)

# On Jumpstarter the wrapper already performed the switch + reboot before
# opening the SSH tunnel, so tests 02 and 03 are a no-op.
_JUMPSTARTER = bool(os.environ.get("JUMPSTARTER_IN_USE"))


class TestBootcSwitch:
    """
    End-to-end bootc image switch test.

    Tests run in definition order and share state through class variables:
      _pre_switch_image  — image URL active before the switch (for reporting)
      _post_reboot_ssh   — fresh SSHConnection after the reboot

    If test_03 (reboot) does not complete, tests 04 and 05 fail immediately
    with a clear message rather than an obscure connection error.
    """

    _pre_switch_image: str = ""
    _post_reboot_ssh = None

    # ------------------------------------------------------------------
    # 01 — baseline: confirm the system is already running bootc
    # ------------------------------------------------------------------
    def test_01_current_bootc_status(self, ssh, bootc_new_image):
        """System must be running bootc before a switch can be attempted."""
        result = ssh.run("bootc status", fail_on_rc=False)
        assert result.exit_status == 0, (
            f"bootc status failed — is this system running bootc?\n{result.stderr}"
        )
        logger.info("Pre-switch bootc status:\n%s", result.stdout)

        for line in result.stdout.splitlines():
            if "image:" in line.lower():
                TestBootcSwitch._pre_switch_image = line.strip()
                break

        logger.info("Switching FROM: %s", TestBootcSwitch._pre_switch_image)
        logger.info("Switching TO:   %s", bootc_new_image)

    # ------------------------------------------------------------------
    # 02 — queue the new image for next boot
    # ------------------------------------------------------------------
    def test_02_bootc_switch_initiated(self, ssh, bootc_new_image):
        """bootc switch must queue the new image without error.

        On Jumpstarter the wrapper already ran bootc switch before opening the
        SSH tunnel, so this test is skipped — the outcome is verified in test_04.
        """
        if _JUMPSTARTER:
            pytest.skip(
                "Jumpstarter: bootc switch was performed by wrapper.py before the "
                "SSH tunnel opened — see test_04 for verification"
            )

        result = ssh.run(f"bootc switch {bootc_new_image}", fail_on_rc=False)
        assert result.exit_status == 0, (
            f"bootc switch failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        logger.info("bootc switch output:\n%s", result.stdout)

        assert any(
            kw in result.stdout
            for kw in ("Queued", "Staged", "staged", "queued", "booted")
        ), (
            f"No staging confirmation in bootc switch output:\n{result.stdout}"
        )

    # ------------------------------------------------------------------
    # 03 — reboot and wait for the device to come back on the new image
    # ------------------------------------------------------------------
    def test_03_reboot_and_reconnect(self, ssh, bootc_new_image):
        """Reboot the device to apply the bootc switch.

        On Jumpstarter the reboot was handled by wrapper.py (serial console
        wait + fresh tunnel) — we simply promote the current ssh connection
        to post-reboot status so test_04 and test_05 can verify the outcome.
        """
        if _JUMPSTARTER:
            pytest.skip(
                "Jumpstarter: reboot was handled by wrapper.py before the "
                "SSH tunnel opened — device is already on the new image"
            )
            return  # unreachable; keeps type checkers happy

        logger.info("Rebooting to apply bootc switch to %s ...", bootc_new_image)
        new_ssh = reboot_and_reconnect(ssh, timeout=600, poll_interval=15)
        assert new_ssh is not None, "reboot_and_reconnect returned None"
        TestBootcSwitch._post_reboot_ssh = new_ssh
        logger.info("Device is back after reboot")

    # ------------------------------------------------------------------
    # 04 — verify the new image is active; refresh session-wide globals
    # ------------------------------------------------------------------
    def test_04_new_image_is_active(self, ssh, bootc_new_image):
        """
        bootc status must show the new image as the booted image.

        Also refreshes all hardware-info globals in the root conftest so that
        every subsequent test class in this pytest session reads accurate data
        from the new image (kernel version, JetPack version, etc.).
        """
        # On Jumpstarter test_03 is skipped; use the class-scoped ssh directly.
        active_ssh = TestBootcSwitch._post_reboot_ssh or ssh

        result = active_ssh.run("bootc status", fail_on_rc=False)
        assert result.exit_status == 0, (
            f"bootc status failed after reboot:\n{result.stderr}"
        )
        logger.info("Post-switch bootc status:\n%s", result.stdout)

        image_tag = bootc_new_image.split(":")[-1]
        assert image_tag in result.stdout, (
            f"Expected image tag '{image_tag}' not found in bootc status.\n"
            f"Full output:\n{result.stdout}"
        )

        # Refresh session globals so subsequent tests see the new image state
        root_conftest.refresh_hardware_info_globals(active_ssh)
        logger.info(
            "Session hardware-info globals refreshed — kernel: %s  image: %s",
            root_conftest.KERNEL_VERSION,
            root_conftest.BOOTC_IMAGE_URL,
        )

    # ------------------------------------------------------------------
    # 05 — verify bootc is healthy (no unexpected pending update)
    # ------------------------------------------------------------------
    def test_05_bootc_healthy_no_rollback(self, ssh, bootc_new_image):
        """
        bootc status must show the system is fully settled on the new image
        with no additional staged update waiting on top.
        """
        active_ssh = TestBootcSwitch._post_reboot_ssh or ssh

        result = active_ssh.run("bootc status", fail_on_rc=False)
        assert result.exit_status == 0, f"bootc status failed:\n{result.stderr}"

        image_tag = bootc_new_image.split(":")[-1]
        # A healthy post-switch system either has no staged update, or the
        # staged entry is the same image we just switched to.
        if "staged" in result.stdout.lower():
            assert image_tag in result.stdout, (
                "A different staged update is queued after the switch — "
                f"check bootc status:\n{result.stdout}"
            )

        logger.info(
            "Bootc switch complete and healthy.\n"
            "  From: %s\n"
            "  To:   %s\n"
            "  Kernel: %s",
            TestBootcSwitch._pre_switch_image,
            bootc_new_image,
            root_conftest.KERNEL_VERSION,
        )

        # Print a refreshed setup summary so the report shows the new image
        print("\n" + "=" * 80)
        print("POST-SWITCH SUMMARY")
        print("=" * 80)
        print(f"New image:     {root_conftest.BOOTC_IMAGE_URL}")
        print(f"Kernel:        {root_conftest.KERNEL_VERSION}")
        print(f"RHEL version:  {root_conftest.RHEL_VERSION}")
        print(f"JetPack:       {root_conftest.JETPACK_VERSION}")
        print("=" * 80)
