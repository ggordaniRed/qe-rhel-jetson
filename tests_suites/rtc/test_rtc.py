"""
RTC (Real-Time Clock) tests for Jetson devices.

Validates the Tegra RTC hardware: device node presence, sysfs attributes,
clock ticking, alarm capability, and hwclock integration.

Known issue on Jetson AGX Orin with RHEL:
  The Tegra RTC registers as /dev/rtc1 instead of /dev/rtc0, which breaks
  hwclock defaults and boot-time hctosys sync.  Tests account for this by
  auto-detecting the correct device node.
"""

import re
import time
import pytest
from logging import getLogger

logger = getLogger(__name__)

# Tegra RTC driver name as it appears in sysfs
TEGRA_RTC_NAME = "tegra_rtc"


def _find_rtc_device(ssh):
    """Return (sysfs_name, dev_path) for the Tegra RTC, or (None, None)."""
    result = ssh.run("ls /sys/class/rtc/ 2>/dev/null", fail_on_rc=False)
    if result.exit_status != 0 or not result.stdout.strip():
        return None, None

    for rtc in sorted(result.stdout.strip().splitlines()):
        name = ssh.run(
            f"cat /sys/class/rtc/{rtc}/name 2>/dev/null", fail_on_rc=False
        )
        if name.exit_status == 0 and TEGRA_RTC_NAME in name.stdout:
            return rtc, f"/dev/{rtc}"

    # Fallback: return first available RTC
    first = result.stdout.strip().splitlines()[0]
    return first, f"/dev/{first}"


class TestRTCDevice:
    """Verify RTC device presence and kernel driver."""

    def test_rtc_device_exists(self, ssh):
        """At least one RTC device must be present in sysfs."""
        result = ssh.run("ls /sys/class/rtc/", fail_on_rc=False)
        assert result.exit_status == 0 and result.stdout.strip(), (
            "No RTC devices found in /sys/class/rtc/. "
            "The tegra_rtc module may not be loaded."
        )
        rtc_devices = result.stdout.strip().splitlines()
        logger.info("Found %d RTC device(s): %s", len(rtc_devices), ", ".join(rtc_devices))

    def test_rtc_dev_node_exists(self, ssh):
        """The RTC character device node must exist in /dev/."""
        result = ssh.run("ls /dev/rtc* 2>/dev/null", fail_on_rc=False)
        assert result.exit_status == 0 and result.stdout.strip(), (
            "No /dev/rtc* device nodes found"
        )
        nodes = result.stdout.strip().splitlines()
        logger.info("RTC device nodes: %s", ", ".join(nodes))

    def test_tegra_rtc_driver_loaded(self, ssh):
        """The tegra_rtc kernel module must be loaded or built-in."""
        lsmod = ssh.run("lsmod | grep rtc_tegra", fail_on_rc=False)
        if lsmod.exit_status == 0 and lsmod.stdout.strip():
            logger.info("rtc_tegra loaded as module: %s", lsmod.stdout.strip())
            return

        # May be built-in — check dmesg for registration
        dmesg = ssh.sudo("dmesg | grep tegra_rtc", fail_on_rc=False)
        assert dmesg.exit_status == 0 and "registered" in dmesg.stdout, (
            "tegra_rtc driver not loaded and not found in dmesg. "
            "Check kernel config for CONFIG_RTC_DRV_TEGRA."
        )
        logger.info("tegra_rtc registered (built-in): %s", dmesg.stdout.strip().splitlines()[0])

    def test_rtc_dmesg_no_errors(self, ssh):
        """dmesg should show RTC registration without errors."""
        result = ssh.sudo("dmesg | grep -iE 'rtc|tegra_rtc'", fail_on_rc=False)
        assert result.exit_status == 0 and result.stdout.strip(), (
            "No RTC-related messages in dmesg"
        )
        logger.info("RTC dmesg messages:\n%s", result.stdout)
        for line in result.stdout.splitlines():
            lower = line.lower()
            assert "error" not in lower and "fail" not in lower, (
                f"RTC error in dmesg: {line}"
            )


class TestRTCSysfs:
    """Validate RTC sysfs attributes."""

    def test_rtc_sysfs_name(self, ssh):
        """RTC sysfs name should identify the Tegra RTC."""
        rtc, _ = _find_rtc_device(ssh)
        assert rtc is not None, "No RTC device found in sysfs"

        name = ssh.run(f"cat /sys/class/rtc/{rtc}/name", fail_on_rc=False)
        assert name.exit_status == 0 and name.stdout.strip(), (
            f"Cannot read /sys/class/rtc/{rtc}/name"
        )
        logger.info("RTC name: %s", name.stdout.strip())

    def test_rtc_sysfs_date_time(self, ssh):
        """RTC date and time should be readable from sysfs."""
        rtc, _ = _find_rtc_device(ssh)
        assert rtc is not None, "No RTC device found"

        date_r = ssh.run(f"cat /sys/class/rtc/{rtc}/date", fail_on_rc=False)
        time_r = ssh.run(f"cat /sys/class/rtc/{rtc}/time", fail_on_rc=False)

        assert date_r.exit_status == 0 and date_r.stdout.strip(), "Cannot read RTC date"
        assert time_r.exit_status == 0 and time_r.stdout.strip(), "Cannot read RTC time"

        logger.info("RTC date: %s  time: %s", date_r.stdout.strip(), time_r.stdout.strip())

        # Validate date format YYYY-MM-DD
        assert re.match(r"\d{4}-\d{2}-\d{2}", date_r.stdout.strip()), (
            f"RTC date format unexpected: {date_r.stdout.strip()}"
        )

    def test_rtc_sysfs_since_epoch(self, ssh):
        """RTC since_epoch should return a positive integer."""
        rtc, _ = _find_rtc_device(ssh)
        assert rtc is not None, "No RTC device found"

        result = ssh.run(f"cat /sys/class/rtc/{rtc}/since_epoch", fail_on_rc=False)
        assert result.exit_status == 0 and result.stdout.strip(), (
            "Cannot read RTC since_epoch"
        )
        epoch = int(result.stdout.strip())
        assert epoch > 0, f"RTC epoch value is not positive: {epoch}"
        logger.info("RTC since_epoch: %d", epoch)


class TestRTCTick:
    """Verify the RTC is actively ticking."""

    def test_rtc_ticks_correctly(self, ssh):
        """RTC epoch counter must advance by ~2 seconds over a 2-second sleep."""
        rtc, _ = _find_rtc_device(ssh)
        assert rtc is not None, "No RTC device found"

        sysfs = f"/sys/class/rtc/{rtc}/since_epoch"
        before = ssh.run(f"cat {sysfs}")
        time.sleep(2)
        after = ssh.run(f"cat {sysfs}")

        t0 = int(before.stdout.strip())
        t1 = int(after.stdout.strip())
        diff = t1 - t0

        logger.info("RTC tick test: before=%d after=%d diff=%d", t0, t1, diff)
        assert 1 <= diff <= 4, (
            f"RTC drift out of range: expected ~2s, got {diff}s "
            f"(before={t0}, after={t1})"
        )


class TestRTCAlarm:
    """Test RTC alarm (wakealarm) functionality."""

    def test_rtc_alarm_set_read(self, ssh):
        """Setting wakealarm and reading it back should match."""
        rtc, _ = _find_rtc_device(ssh)
        assert rtc is not None, "No RTC device found"

        wakealarm = f"/sys/class/rtc/{rtc}/wakealarm"
        check = ssh.run(f"test -f {wakealarm} && echo yes", fail_on_rc=False)
        if "yes" not in check.stdout:
            pytest.skip(f"wakealarm not supported on {rtc}")

        epoch_r = ssh.run(f"cat /sys/class/rtc/{rtc}/since_epoch")
        epoch_now = int(epoch_r.stdout.strip())
        alarm_time = epoch_now + 10

        # Clear any existing alarm, set new one
        ssh.sudo(f"echo 0 > {wakealarm}", fail_on_rc=False)
        ssh.sudo(f"echo {alarm_time} > {wakealarm}")
        readback = ssh.run(f"cat {wakealarm}", fail_on_rc=False)

        logger.info("Alarm set=%d readback=%s", alarm_time, readback.stdout.strip())
        assert readback.stdout.strip() == str(alarm_time), (
            f"Alarm readback mismatch: set {alarm_time}, "
            f"got {readback.stdout.strip()}"
        )

        # Clean up
        ssh.sudo(f"echo 0 > {wakealarm}", fail_on_rc=False)


class TestHWClock:
    """Test hwclock integration with the RTC."""

    def test_hwclock_read(self, ssh):
        """hwclock should be able to read the hardware clock."""
        _, dev = _find_rtc_device(ssh)
        assert dev is not None, "No RTC device found"

        result = ssh.sudo(f"hwclock --rtc {dev} --show", fail_on_rc=False)
        assert result.exit_status == 0 and result.stdout.strip(), (
            f"hwclock --show failed for {dev}: {result.stderr}"
        )
        logger.info("hwclock reads: %s", result.stdout.strip())

    def test_hwclock_systohc(self, ssh):
        """hwclock --systohc should sync system time to the RTC."""
        _, dev = _find_rtc_device(ssh)
        assert dev is not None, "No RTC device found"

        # Write system time to RTC
        result = ssh.sudo(f"hwclock --rtc {dev} --systohc", fail_on_rc=False)
        assert result.exit_status == 0, (
            f"hwclock --systohc failed: {result.stderr}"
        )

        # Read back and verify it's recent (not stuck at 1970)
        readback = ssh.sudo(f"hwclock --rtc {dev} --show", fail_on_rc=False)
        assert readback.exit_status == 0, f"hwclock --show failed after systohc: {readback.stderr}"
        logger.info("hwclock after systohc: %s", readback.stdout.strip())
        assert "1970" not in readback.stdout, (
            f"RTC still at epoch 1970 after systohc: {readback.stdout.strip()}"
        )

    def test_hwclock_hctosys(self, ssh):
        """hwclock --hctosys should sync RTC time to system clock."""
        _, dev = _find_rtc_device(ssh)
        assert dev is not None, "No RTC device found"

        # First ensure RTC has valid time
        ssh.sudo(f"hwclock --rtc {dev} --systohc", fail_on_rc=False)

        result = ssh.sudo(f"hwclock --rtc {dev} --hctosys", fail_on_rc=False)
        assert result.exit_status == 0, (
            f"hwclock --hctosys failed: {result.stderr}"
        )
        logger.info("hwclock --hctosys succeeded")


class TestRTCEnumeration:
    """Check RTC device enumeration (rtc0 vs rtc1 issue)."""

    def test_rtc_enumeration(self, ssh):
        """
        Report whether the Tegra RTC is rtc0 or rtc1.

        On Jetson AGX Orin with RHEL, tegra_rtc often registers as rtc1
        instead of rtc0. This breaks default hwclock behavior and
        boot-time hctosys sync. This test documents the current state.
        """
        rtc, dev = _find_rtc_device(ssh)
        assert rtc is not None, "No RTC device found"

        logger.info("Tegra RTC registered as: %s (%s)", rtc, dev)

        # Check for /dev/rtc and /dev/rtc0 symlinks
        rtc_link = ssh.run("ls -la /dev/rtc 2>/dev/null", fail_on_rc=False)
        rtc0_link = ssh.run("ls -la /dev/rtc0 2>/dev/null", fail_on_rc=False)

        has_rtc = rtc_link.exit_status == 0 and rtc_link.stdout.strip()
        has_rtc0 = rtc0_link.exit_status == 0 and rtc0_link.stdout.strip()

        logger.info("/dev/rtc exists: %s", has_rtc)
        logger.info("/dev/rtc0 exists: %s", has_rtc0)

        if rtc != "rtc0":
            logger.warning(
                "Tegra RTC is %s (not rtc0). hwclock without --rtc will fail. "
                "Consider a udev rule to create /dev/rtc0 -> %s",
                rtc, dev,
            )

    def test_rtc_hctosys_flag(self, ssh):
        """
        Check the hctosys flag — it should be 1 for the system RTC
        to auto-sync time at boot.
        """
        rtc, _ = _find_rtc_device(ssh)
        assert rtc is not None, "No RTC device found"

        result = ssh.run(f"cat /sys/class/rtc/{rtc}/hctosys", fail_on_rc=False)
        if result.exit_status != 0:
            pytest.skip("hctosys attribute not available")

        hctosys = result.stdout.strip()
        logger.info("RTC %s hctosys: %s", rtc, hctosys)

        if hctosys != "1":
            logger.warning(
                "hctosys=0 for %s — RTC time is NOT synced to system clock at boot. "
                "This is expected when tegra_rtc registers as rtc1 instead of rtc0.",
                rtc,
            )


class TestTimedatectl:
    """Validate timedatectl reports on RTC status."""

    def test_timedatectl_status(self, ssh):
        """timedatectl should run and report clock status."""
        result = ssh.run("timedatectl", fail_on_rc=False)
        assert result.exit_status == 0 and result.stdout.strip(), (
            "timedatectl command failed or returned empty output"
        )
        logger.info("timedatectl output:\n%s", result.stdout)

    def test_ntp_service_active(self, ssh):
        """NTP service should be active for time synchronization."""
        result = ssh.run("timedatectl", fail_on_rc=False)
        assert result.exit_status == 0, "timedatectl failed"

        output = result.stdout.lower()
        if "ntp service: active" in output or "ntp synchronized: yes" in output \
                or "system clock synchronized: yes" in output:
            logger.info("NTP synchronization is active")
        else:
            logger.warning(
                "NTP may not be active. timedatectl output:\n%s",
                result.stdout,
            )
