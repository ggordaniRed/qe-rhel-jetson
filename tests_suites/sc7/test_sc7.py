"""
SC7 suspend/resume tests for Jetson devices.

SC7 is the deepest suspend state on Tegra SoCs (suspend-to-RAM).
The SoC powers down all non-essential logic; DRAM stays in self-refresh.
On resume, the full kernel context is restored.

Test flow:
  1. Verify the platform reports SC7 support
  2. Select mem_sleep=deep (SC7), not s2idle
  3. Set an RTC wakealarm from the Tegra RTC epoch (not system time)
  4. Trigger suspend from a process detached from SSH
  5. SSH connection drops — wait and reconnect
  6. Verify a real SC7 cycle (suspend_stats + dmesg), then services/hardware
"""

import os
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
RESUME_TIMEOUT  = 240
# How long to sleep before polling for SSH (give the system time to actually suspend)
SUSPEND_SETTLE  = 15
# RTC wakealarm offset in seconds (must be > SUSPEND_SETTLE + boot time margin)
WAKEALARM_DELTA = 90
# Seconds to wait after reconnect before issuing commands (let the board stabilize)
POST_RESUME_SETTLE = 10
# Delay before the detached process writes /sys/power/state (must be < SUSPEND_SETTLE)
_SUSPEND_DELAY_SEC = 3

# Tegra RTC driver name as it appears in sysfs (often rtc1, not rtc0, on AGX Orin)
TEGRA_RTC_NAME = "tegra_rtc"

# dmesg patterns that are known-benign on Jetson and should not fail the test
_DMESG_ALLOWLIST = [
    "dce: dce_admin_setup_clients_ipc",  # DCE firmware IPC warning, harmless on resume
    # ENXIO (-6) on BPMP I2C is boot/idle noise. Also, grep 'tegra.*pm' false-matches 'bpmp'.
    "tegra-bpmp-i2c",
]


def _skip_suspend_over_jumpstarter():
    """Do not drop the DUT behind Jumpstarter's non-resilient SSH tunnel."""
    if os.environ.get("JUMPSTARTER_IN_USE"):
        pytest.skip(
            "SC7 suspend is handled by the wrapper-controlled fresh-tunnel phase — "
            "an individual TcpPortforwardAdapter cannot reconnect after the DUT "
            "goes down"
        )


def _key_path():
    kp = os.getenv("JETSON_KEY_PATH")
    return os.path.expanduser(kp) if kp else None


def _close_quietly(ssh):
    try:
        ssh.close()
    except Exception:
        pass


def _xfail_if_boot_already_has_nvrng_resume_failure(ssh):
    """Avoid repeating a destructive SC7 cycle after this boot proved broken."""
    result = ssh.sudo(
        "dmesg | grep -F "
        "'tegra_se_sc7_check_error:234 SE HW is not idle, timeout' | tail -1",
        fail_on_rc=False,
    )
    if result.exit_status == 0 and result.stdout.strip():
        _close_quietly(ssh)
        pytest.xfail(
            "This boot already hit the L4T 39 SC7 NVRNG resume timeout; "
            "repeating suspend risks another SSH outage"
        )


def _reconnect(timeout=RESUME_TIMEOUT):
    """Poll until SSH accepts connections again; return new SSHConnection."""
    key_path = _key_path()
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


def _sysfs_write(ssh, path, value):
    """Write *value* to a sysfs node as root (Fabric sudo does not wrap redirects)."""
    ssh.sudo(f"sh -c 'echo {value} > {path}'")


def _find_wakeup_rtc(ssh):
    """Return (rtc_name, wakealarm_path) preferring tegra_rtc.

    On Jetson AGX Orin with RHEL, tegra_rtc often registers as rtc1 while rtc0
    is a PMIC clock that cannot wake the SoC from SC7.
    """
    listing = ssh.run("ls /sys/class/rtc/ 2>/dev/null", fail_on_rc=False)
    rtcs = [r.strip() for r in listing.stdout.splitlines() if r.strip()]
    candidates = []
    for rtc in rtcs:
        wa = f"/sys/class/rtc/{rtc}/wakealarm"
        has_wa = ssh.run(f"test -f {wa} && echo yes", fail_on_rc=False)
        if "yes" not in has_wa.stdout:
            continue
        name = ssh.run(
            f"cat /sys/class/rtc/{rtc}/name 2>/dev/null", fail_on_rc=False
        ).stdout.strip()
        candidates.append((rtc, name, wa))

    for rtc, name, wa in candidates:
        if TEGRA_RTC_NAME in name:
            logger.info("Using wakeup RTC %s (%s)", rtc, name)
            return rtc, wa
    if candidates:
        rtc, name, wa = candidates[0]
        logger.warning(
            "No %s with wakealarm; falling back to %s (%s)",
            TEGRA_RTC_NAME, rtc, name or "unknown",
        )
        return rtc, wa
    pytest.skip("No wakealarm-capable RTC found — cannot safely trigger SC7")


def _set_wakealarm(ssh, delta=WAKEALARM_DELTA):
    """Set wakealarm *delta* seconds from the RTC's own epoch. Returns alarm epoch.

    /sys/class/rtc/*/wakealarm is compared against RTC hardware time, not
    date +%s. Using system time on a board where tegra_rtc is not hctosys
    fires the alarm too early (wakeup consumed before SC7) or too late.
    """
    rtc, wa = _find_wakeup_rtc(ssh)
    epoch_r = ssh.run(f"cat /sys/class/rtc/{rtc}/since_epoch")
    now = int(epoch_r.stdout.strip())
    alarm = now + delta

    # echo 0 clears a pending alarm; ignore failure if none is armed
    try:
        _sysfs_write(ssh, wa, 0)
    except Exception as e:
        logger.debug("Clearing wakealarm on %s: %s", rtc, e)

    _sysfs_write(ssh, wa, alarm)
    readback = ssh.run(f"cat {wa}", fail_on_rc=False).stdout.strip()
    logger.info("Wakealarm set on %s: %d (readback: %s)", rtc, alarm, readback)
    assert readback == str(alarm), (
        f"Wakealarm readback mismatch on {rtc}: set {alarm}, got {readback}"
    )
    return alarm


def _enforce_deep_sleep(ssh):
    """Select mem_sleep=deep so that echo mem actually enters SC7, not s2idle."""
    result = ssh.run("cat /sys/power/mem_sleep 2>/dev/null", fail_on_rc=False)
    current = result.stdout.strip()
    if result.exit_status != 0 or not current:
        pytest.skip("/sys/power/mem_sleep not available on this kernel")
    if "deep" not in current:
        pytest.skip(f"'deep' sleep mode not available: {current!r}")
    if "[deep]" in current:
        logger.info("mem_sleep already [deep]: %s", current)
        return
    logger.info("Selecting SC7: echo deep > /sys/power/mem_sleep (was %s)", current)
    _sysfs_write(ssh, "/sys/power/mem_sleep", "deep")
    after = ssh.run("cat /sys/power/mem_sleep").stdout.strip()
    assert "[deep]" in after, (
        f"Failed to select SC7 (deep) sleep; mem_sleep={after!r}"
    )


def _trigger_suspend(ssh):
    """Flush filesystems, force deep sleep, then suspend outside the SSH session.

    Writing /sys/power/state from the SSH process tree races the freezer against
    sshd flushing sockets on a NIC that is going down. Schedule the write from a
    detached job, then close SSH before the delay elapses.
    """
    _enforce_deep_sleep(ssh)
    logger.info("Syncing filesystems before SC7 suspend…")
    ssh.sudo("sync")
    time.sleep(2)

    # systemd-run fully leaves the SSH cgroup; nohup is the fallback.
    logger.info(
        "Scheduling detached SC7 suspend in %ss (echo mem > /sys/power/state)…",
        _SUSPEND_DELAY_SEC,
    )
    scheduled = ssh.sudo(
        "systemd-run --collect --quiet "
        f"--on-active={_SUSPEND_DELAY_SEC}s --timer-property=AccuracySec=1s "
        "/bin/sh -c 'echo mem > /sys/power/state'",
        fail_on_rc=False,
        timeout=20,
    )
    if scheduled.exit_status != 0:
        logger.warning(
            "systemd-run failed (rc=%s); falling back to nohup: %s",
            scheduled.exit_status, (scheduled.stderr or scheduled.stdout).strip(),
        )
        ssh.sudo(
            "sh -c 'nohup sh -c \""
            f"sleep {_SUSPEND_DELAY_SEC} && echo mem > /sys/power/state"
            "\" >/dev/null 2>&1 < /dev/null &'",
            fail_on_rc=False,
        )


def _read_suspend_success(ssh):
    """Return kernel successful-suspend count, or None if unavailable."""
    result = ssh.run("cat /sys/power/suspend_stats/success", fail_on_rc=False)
    if result.exit_status == 0 and result.stdout.strip().isdigit():
        return int(result.stdout.strip())
    result = ssh.sudo(
        "awk '/^success:/ {print $2; exit}' /sys/kernel/debug/suspend_stats 2>/dev/null",
        fail_on_rc=False,
    )
    if result.exit_status == 0 and result.stdout.strip().isdigit():
        return int(result.stdout.strip())
    return None


def _read_last_failed_dev(ssh):
    result = ssh.run("cat /sys/power/suspend_stats/last_failed_dev", fail_on_rc=False)
    if result.exit_status == 0:
        return result.stdout.strip()
    return ""


def _dmesg_since_last_suspend(ssh):
    """Return dmesg lines from the last 'PM: suspend entry' through now.

    Full-log greps treat hours-old boot noise (e.g. tegra-bpmp-i2c at t=2737s)
    as resume failures.
    """
    result = ssh.sudo("dmesg", fail_on_rc=False)
    lines = result.stdout.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "PM: suspend entry" in line:
            start = i
    if start is None:
        logger.warning("No 'PM: suspend entry' in dmesg; using last 80 lines")
        return lines[-80:]
    return lines[start:]


def _dmesg_errors(lines):
    """Error/fail lines in *lines* that are not on the Jetson allowlist."""
    return [
        l for l in lines
        if any(k in l.lower() for k in ("error", "failed", "call trace"))
        and not any(a in l for a in _DMESG_ALLOWLIST)
    ]


def _assert_sc7_cycle(ssh, success_before):
    """Fail if the board came back without a completed deep (SC7) suspend."""
    success_after = _read_suspend_success(ssh)
    logger.info(
        "suspend_stats/success before=%s after=%s", success_before, success_after
    )

    entry = ssh.sudo(
        "dmesg | grep -E 'PM: suspend entry' | tail -5",
        fail_on_rc=False,
    )
    lines = [l for l in entry.stdout.splitlines() if l.strip()]
    if lines:
        last = lines[-1]
        logger.info("Last PM suspend entry: %s", last)
        if "s2idle" in last and "deep" not in last:
            pytest.fail(
                f"Last suspend was s2idle, not SC7 (deep):\n" + "\n".join(lines)
            )
    else:
        logger.warning("No 'PM: suspend entry' lines in dmesg")

    stats_ok = success_before is not None and success_after is not None
    if stats_ok:
        failed_dev = _read_last_failed_dev(ssh)
        extra = f" last_failed_dev={failed_dev!r}" if failed_dev else ""
        assert success_after >= success_before + 1, (
            f"suspend_stats/success did not increment "
            f"({success_before} → {success_after}){extra}. "
            "The board is up but never completed SC7 (freeze abort, s2idle skip, "
            "or SSH never dropped)."
        )
    elif not lines:
        pytest.fail(
            "Cannot prove SC7: suspend_stats unavailable and no "
            "'PM: suspend entry' lines in dmesg"
        )
    else:
        logger.warning("suspend_stats not available; accepted dmesg PM entry as SC7 proof")
    return success_after


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
        """'/sys/power/mem_sleep' must list 'deep' (SC7). Active mode is bracketed."""
        result = ssh.run("cat /sys/power/mem_sleep 2>/dev/null", fail_on_rc=False)
        if result.exit_status != 0 or not result.stdout.strip():
            pytest.skip("/sys/power/mem_sleep not available on this kernel")
        current = result.stdout.strip()
        logger.info("mem_sleep: %s", current)
        assert "deep" in current, (
            f"'deep' sleep mode not available: {current!r}"
        )
        if "[deep]" not in current:
            logger.warning(
                "deep is available but not selected (%s); "
                "suspend tests will switch to deep before triggering SC7",
                current,
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
        _skip_suspend_over_jumpstarter()
        conn = SSHConnection(
            JETSON_HOST, JETSON_USERNAME,
            JETSON_PASSWORD or None, JETSON_PORT, JETSON_TIMEOUT,
            key_filename=_key_path(),
        )
        _xfail_if_boot_already_has_nvrng_resume_failure(conn)
        self._ssh = conn
        yield conn
        _close_quietly(conn)

    def _suspend_and_resume(self):
        """
        Trigger SC7, wait for the board to go down, then reconnect.
        Returns (SSHConnection, uptime_before, success_before, success_after).
        """
        ssh = self._ssh
        _set_wakealarm(ssh, WAKEALARM_DELTA)
        success_before = _read_suspend_success(ssh)

        uptime_before = ssh.run("cat /proc/uptime").stdout.split()[0]
        logger.info(
            "Pre-suspend uptime: %ss  suspend_stats/success: %s",
            uptime_before, success_before,
        )

        _trigger_suspend(ssh)
        _close_quietly(ssh)

        # Give the board time to actually suspend before we start polling
        time.sleep(SUSPEND_SETTLE)

        resumed = _reconnect(RESUME_TIMEOUT)
        time.sleep(POST_RESUME_SETTLE)  # let sshd and systemd fully stabilize
        success_after = _assert_sc7_cycle(resumed, success_before)
        return resumed, float(uptime_before), success_before, success_after

    def test_sc7_suspend_resumes(self, fresh_ssh):
        """System must resume from SC7 within RESUME_TIMEOUT seconds."""
        resumed, *_ = self._suspend_and_resume()
        result = resumed.run("echo alive", fail_on_rc=False)
        assert result.exit_status == 0 and "alive" in result.stdout, (
            "System came back but shell is unresponsive"
        )
        logger.info("SC7 resume: system is alive")
        resumed.close()

    def test_sc7_dmesg_resume_clean(self, fresh_ssh):
        """dmesg after resume must show PM resume without errors."""
        resumed, *_ = self._suspend_and_resume()
        try:
            lines = _dmesg_since_last_suspend(resumed)
            logger.info("Post-resume dmesg (this SC7 cycle):\n%s", "\n".join(lines))

            errors = _dmesg_errors(lines)
            if errors:
                logger.warning("Errors in post-resume dmesg:\n%s", "\n".join(errors))
            assert not errors, (
                f"Errors found in dmesg after SC7 resume:\n" + "\n".join(errors)
            )

            resume_lines = [
                l for l in lines
                if any(k in l.lower() for k in ("resume", "syscore", "pm: "))
            ]
            assert resume_lines, "No PM resume messages found in dmesg"
            logger.info("PM resume messages: %s", resume_lines[:5])
        finally:
            resumed.close()

    def test_sc7_uptime_continues(self, fresh_ssh):
        """
        After SC7 resume, uptime must be greater than before suspend
        (confirms the kernel resumed, not rebooted).
        """
        resumed, uptime_before, *_ = self._suspend_and_resume()
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
        _skip_suspend_over_jumpstarter()
        pre = SSHConnection(
            JETSON_HOST, JETSON_USERNAME,
            JETSON_PASSWORD or None, JETSON_PORT, JETSON_TIMEOUT,
            key_filename=_key_path(),
        )
        _xfail_if_boot_already_has_nvrng_resume_failure(pre)
        _set_wakealarm(pre, WAKEALARM_DELTA)
        self._success_before = _read_suspend_success(pre)
        _trigger_suspend(pre)
        _close_quietly(pre)

        time.sleep(SUSPEND_SETTLE)
        self._resumed = _reconnect(RESUME_TIMEOUT)
        time.sleep(POST_RESUME_SETTLE)
        self._success_after = _assert_sc7_cycle(self._resumed, self._success_before)
        yield self._resumed
        _close_quietly(self._resumed)

    def test_sc7_counter_incremented(self, post_resume_ssh):
        """Kernel suspend_stats success count must increment (true mem/SC7 cycle)."""
        logger.info(
            "suspend_stats/success before=%s after=%s",
            self._success_before, self._success_after,
        )
        if self._success_before is None or self._success_after is None:
            pytest.skip(
                "suspend_stats not available "
                "(/sys/power/suspend_stats/success and debugfs fallback)"
            )
        assert self._success_after >= self._success_before + 1, (
            f"suspend_stats/success did not increment "
            f"({self._success_before} → {self._success_after})"
        )

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
        """No Tegra PM errors in dmesg from this SC7 cycle (not historical boot noise)."""
        cycle = _dmesg_since_last_suspend(post_resume_ssh)
        # Do not use 'tegra.*pm' — it matches tegra-bpmp (the 'pm' in bpmp).
        pm_lines = [
            l for l in cycle
            if any(k in l.lower() for k in ("pm: ", "tegra-pmc", "sc7", "suspend"))
        ]
        real_errors = _dmesg_errors(pm_lines)
        if real_errors:
            pytest.fail(
                f"Tegra PM errors after SC7 resume:\n" + "\n".join(real_errors)
            )
        logger.info(
            "No Tegra PM errors after SC7 resume (%d cycle lines)", len(cycle)
        )

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
        """Tegra RTC must continue ticking after SC7 resume."""
        rtc, _ = _find_wakeup_rtc(post_resume_ssh)
        sysfs = f"/sys/class/rtc/{rtc}/since_epoch"

        t0 = int(post_resume_ssh.run(f"cat {sysfs}").stdout.strip())
        time.sleep(2)
        t1 = int(post_resume_ssh.run(f"cat {sysfs}").stdout.strip())
        diff = t1 - t0
        logger.info("RTC %s tick after resume: diff=%ds (t0=%d t1=%d)", rtc, diff, t0, t1)
        assert 1 <= diff <= 5, (
            f"RTC {rtc} not ticking correctly after SC7 resume: diff={diff}s"
        )
