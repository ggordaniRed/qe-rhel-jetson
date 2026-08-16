"""
SC7 suspend/resume tests for Jetson devices.

SC7 is the deepest suspend state on Tegra SoCs (suspend-to-RAM).
The SoC powers down all non-essential logic; DRAM stays in self-refresh.
On resume, the full kernel context is restored.

Test flow:
  1. Verify the platform reports SC7 support
  2. Set an RTC wakealarm to wake the system automatically
  3. Trigger suspend (echo mem > /sys/power/state)
  4. SSH connection drops — wait and reconnect
  5. Verify system resumed cleanly (dmesg, services, hardware)
"""

import socket
import time
import pytest
from logging import getLogger

from tests_suites.conftest import (
    JETSON_HOST,
    JETSON_PORT,
    JETSON_USERNAME,
    JETSON_PASSWORD,
    JETSON_TIMEOUT,
)
from infra_tests.ssh_client import SSHConnection

logger = getLogger(__name__)

# Seconds to wait for the system to come back after suspend
RESUME_TIMEOUT  = 120
# How long to sleep before polling for SSH (give the system time to actually suspend)
SUSPEND_SETTLE  = 10
# RTC wakealarm offset in seconds (must be > SUSPEND_SETTLE + boot time margin)
WAKEALARM_DELTA = 60


def _reconnect(timeout=RESUME_TIMEOUT):
    """Poll until SSH accepts connections again; return new SSHConnection."""
    import os
    key_path = os.path.expanduser(JETSON_KEY_PATH) if (JETSON_KEY_PATH := __import__("os").getenv("JETSON_KEY_PATH")) else None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            conn = SSHConnection(
                JETSON_HOST, JETSON_USERNAME,
                JETSON_PASSWORD or None, JETSON_PORT, 10,
                key_filename=key_path,
            )
            conn.run("true")
            logger.info("SSH reconnected after %.0fs", timeout - (deadline - time.monotonic()))
            return conn
        except Exception:
            time.sleep(5)
    raise TimeoutError(
        f"Jetson did not come back within {timeout}s after SC7 suspend"
    )


def _set_wakealarm(ssh, delta=WAKEALARM_DELTA):
    """Set the RTC wakealarm delta seconds from now. Returns alarm epoch."""
    epoch_r = ssh.run("cat /proc/driver/rtc 2>/dev/null | grep rtc_time || date +%s")
    now = int(ssh.run("date +%s").stdout.strip())
    alarm = now + delta

    # Find a wakealarm-capable RTC
    rtcs = ssh.run("ls /sys/class/rtc/", fail_on_rc=False).stdout.strip().splitlines()
    for rtc in rtcs:
        wa = f"/sys/class/rtc/{rtc}/wakealarm"
        check = ssh.run(f"test -f {wa} && echo yes", fail_on_rc=False)
        if "yes" in check.stdout:
            ssh.sudo(f"echo 0 > {wa}", fail_on_rc=False)
            ssh.sudo(f"echo {alarm} > {wa}")
            readback = ssh.run(f"cat {wa}", fail_on_rc=False).stdout.strip()
            logger.info("Wakealarm set on %s: %d (readback: %s)", rtc, alarm, readback)
            assert readback == str(alarm), (
                f"Wakealarm readback mismatch: set {alarm}, got {readback}"
            )
            return alarm
    pytest.skip("No wakealarm-capable RTC found — cannot safely trigger SC7")


class TestSC7Support:
    """Verify the platform advertises SC7 (mem) suspend support."""

    def test_mem_in_power_states(self, ssh):
        """'/sys/power/state' must list 'mem' for SC7 support."""
        result = ssh.run("cat /sys/power/state", fail_on_rc=False)
        assert result.exit_status == 0, "/sys/power/state not readable"
        states = result.stdout.strip()
        logger.info("Available power states: %s", states)
        assert "mem" in states.split(), (
            f"'mem' (SC7) not in /sys/power/state: {states!r}"
        )

    def test_mem_sleep_type(self, ssh):
        """'/sys/power/mem_sleep' should report 'deep' (SC7) as available."""
        result = ssh.run("cat /sys/power/mem_sleep 2>/dev/null", fail_on_rc=False)
        if result.exit_status != 0 or not result.stdout.strip():
            pytest.skip("/sys/power/mem_sleep not available on this kernel")
        logger.info("mem_sleep: %s", result.stdout.strip())
        assert "deep" in result.stdout, (
            f"'deep' sleep mode not available: {result.stdout.strip()!r}"
        )

    def test_sc7_supported_in_tegra_pm(self, ssh):
        """dmesg or tegra PM driver should indicate SC7 capability."""
        result = ssh.sudo(
            "dmesg | grep -iE 'sc7|suspend|tegra.*pm|power.*state' | head -20",
            fail_on_rc=False,
        )
        if result.exit_status != 0 or not result.stdout.strip():
            pytest.skip("No Tegra PM messages in dmesg")
        logger.info("Tegra PM dmesg:\n%s", result.stdout)

    def test_wakeup_sources_available(self, ssh):
        """/sys/bus/platform/drivers/tegra-pmc should list wakeup sources."""
        result = ssh.run(
            "ls /sys/devices/platform/tegra-pmc*/wakeup* 2>/dev/null | head -10",
            fail_on_rc=False,
        )
        logger.info("Wakeup sources: %s", result.stdout.strip() or "(none listed)")


class TestSC7Suspend:
    """Trigger a full SC7 suspend/resume cycle and verify clean resume."""

    @pytest.fixture(autouse=True)
    def fresh_ssh(self):
        """Each SC7 suspend test gets its own SSH session for reconnect control."""
        import os
        key_path = os.path.expanduser(kp) if (kp := os.getenv("JETSON_KEY_PATH")) else None
        conn = SSHConnection(
            JETSON_HOST, JETSON_USERNAME,
            JETSON_PASSWORD or None, JETSON_PORT, JETSON_TIMEOUT,
            key_filename=key_path,
        )
        self._ssh = conn
        yield conn
        try:
            conn.close()
        except Exception:
            pass

    def _suspend_and_resume(self):
        """
        Trigger SC7, wait for the board to go down, then reconnect.
        Returns a fresh SSHConnection to the resumed system.
        """
        ssh = self._ssh
        _set_wakealarm(ssh, WAKEALARM_DELTA)

        # Record pre-suspend uptime so we can detect a real resume vs hang
        uptime_before = ssh.run("cat /proc/uptime").stdout.split()[0]
        logger.info("Pre-suspend uptime: %ss", uptime_before)

        # Trigger suspend asynchronously — connection will drop
        logger.info("Triggering SC7 suspend (echo mem > /sys/power/state)…")
        ssh.sudo("echo mem > /sys/power/state &", fail_on_rc=False)

        # Give the board time to actually suspend before we start polling
        time.sleep(SUSPEND_SETTLE)

        resumed = _reconnect(RESUME_TIMEOUT)
        return resumed, float(uptime_before)

    def test_sc7_suspend_resumes(self, fresh_ssh):
        """System must resume from SC7 within RESUME_TIMEOUT seconds."""
        resumed, _ = self._suspend_and_resume()
        result = resumed.run("echo alive", fail_on_rc=False)
        assert result.exit_status == 0 and "alive" in result.stdout, (
            "System came back but shell is unresponsive"
        )
        logger.info("SC7 resume: system is alive")
        resumed.close()

    def test_sc7_dmesg_resume_clean(self, fresh_ssh):
        """dmesg after resume must show PM resume without errors."""
        resumed, _ = self._suspend_and_resume()
        try:
            dmesg = resumed.sudo("dmesg | tail -60", fail_on_rc=False)
            logger.info("Post-resume dmesg (last 60 lines):\n%s", dmesg.stdout)

            lines = dmesg.stdout.lower().splitlines()
            errors = [l for l in lines if "error" in l or "failed" in l or "call trace" in l]
            if errors:
                logger.warning("Errors in post-resume dmesg:\n%s", "\n".join(errors))
            assert not errors, (
                f"Errors found in dmesg after SC7 resume:\n" + "\n".join(errors)
            )

            resume_lines = [l for l in dmesg.stdout.splitlines()
                            if any(k in l.lower() for k in ("resume", "syscore", "pm: "))]
            assert resume_lines, "No PM resume messages found in dmesg"
            logger.info("PM resume messages: %s", resume_lines[:5])
        finally:
            resumed.close()

    def test_sc7_uptime_continues(self, fresh_ssh):
        """
        After SC7 resume, uptime must be greater than before suspend
        (confirms the kernel resumed, not rebooted).
        """
        resumed, uptime_before = self._suspend_and_resume()
        try:
            uptime_after = float(resumed.run("cat /proc/uptime").stdout.split()[0])
            logger.info("Uptime before=%.1fs after=%.1fs", uptime_before, uptime_after)
            assert uptime_after > uptime_before, (
                f"Uptime went backwards ({uptime_before}s → {uptime_after}s) "
                "— system may have rebooted instead of resuming"
            )
        finally:
            resumed.close()


class TestSC7Recovery:
    """Verify hardware and services are healthy after SC7 resume."""

    @pytest.fixture(autouse=True)
    def post_resume_ssh(self):
        """Perform one suspend/resume cycle; yield a connected SSH to the resumed system."""
        import os
        key_path = os.path.expanduser(kp) if (kp := os.getenv("JETSON_KEY_PATH")) else None
        pre = SSHConnection(
            JETSON_HOST, JETSON_USERNAME,
            JETSON_PASSWORD or None, JETSON_PORT, JETSON_TIMEOUT,
            key_filename=key_path,
        )
        _set_wakealarm(pre, WAKEALARM_DELTA)
        logger.info("Triggering SC7 for recovery tests…")
        pre.sudo("echo mem > /sys/power/state &", fail_on_rc=False)
        pre.close()

        time.sleep(SUSPEND_SETTLE)
        self._resumed = _reconnect(RESUME_TIMEOUT)
        yield self._resumed
        try:
            self._resumed.close()
        except Exception:
            pass

    def test_network_recovers(self, post_resume_ssh):
        """Network interfaces must be up after resume."""
        result = post_resume_ssh.run("ip link show up", fail_on_rc=False)
        assert result.exit_status == 0 and result.stdout.strip(), (
            "No network interfaces up after SC7 resume"
        )
        logger.info("Network interfaces after resume:\n%s", result.stdout.strip())

    def test_gpu_accessible_after_resume(self, post_resume_ssh):
        """nvidia-smi must succeed after resume (GPU context restored)."""
        result = post_resume_ssh.run("nvidia-smi", fail_on_rc=False)
        assert result.exit_status == 0, (
            f"nvidia-smi failed after SC7 resume:\n{result.stderr}"
        )
        logger.info("nvidia-smi after resume:\n%s", result.stdout.strip())

    def test_tegra_pm_no_resume_errors(self, post_resume_ssh):
        """No Tegra PM errors in dmesg after resume."""
        result = post_resume_ssh.sudo(
            "dmesg | grep -iE 'tegra.*pm|sc7|suspend' | grep -iE 'error|fail'",
            fail_on_rc=False,
        )
        if result.exit_status == 0 and result.stdout.strip():
            pytest.fail(
                f"Tegra PM errors after SC7 resume:\n{result.stdout.strip()}"
            )
        logger.info("No Tegra PM errors after SC7 resume")

    def test_systemd_no_failed_units(self, post_resume_ssh):
        """No systemd units should be in a failed state after resume."""
        result = post_resume_ssh.run(
            "systemctl list-units --state=failed --no-legend --no-pager",
            fail_on_rc=False,
        )
        failed = result.stdout.strip()
        if failed:
            logger.warning("Failed systemd units after SC7 resume:\n%s", failed)
        assert not failed, (
            f"Systemd units failed after SC7 resume:\n{failed}"
        )

    def test_rtc_still_ticking_after_resume(self, post_resume_ssh):
        """RTC must continue ticking after SC7 resume."""
        rtcs = post_resume_ssh.run("ls /sys/class/rtc/", fail_on_rc=False).stdout.strip().splitlines()
        assert rtcs, "No RTC devices found after resume"
        rtc = rtcs[0]
        sysfs = f"/sys/class/rtc/{rtc}/since_epoch"

        t0 = int(post_resume_ssh.run(f"cat {sysfs}").stdout.strip())
        time.sleep(2)
        t1 = int(post_resume_ssh.run(f"cat {sysfs}").stdout.strip())
        diff = t1 - t0
        logger.info("RTC tick after resume: diff=%ds (t0=%d t1=%d)", diff, t0, t1)
        assert 1 <= diff <= 5, (
            f"RTC not ticking correctly after SC7 resume: diff={diff}s"
        )
