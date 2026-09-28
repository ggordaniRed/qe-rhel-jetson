from jumpstarter.common.utils import env
from jumpstarter.streams.encoding import Compression
from jumpstarter_driver_network.adapters import TcpPortforwardAdapter
from pexpect import EOF as PexpectEOF, TIMEOUT as PexpectTimeout
import collections
import concurrent.futures
import contextlib
import logging
import signal
import threading
import time
import sys
import os
import re
import shlex
import yaml
import subprocess
from datetime import datetime, timezone
from pathlib import Path

USERNAME = os.environ.get("JETSON_USERNAME")
PASSWORD = os.environ.get("JETSON_PASSWORD")
KEY_PATH = os.environ.get("JETSON_KEY_PATH")
DISK_IMAGE_PATH = os.environ.get("DISK_IMAGE_PATH", "") # path to the disk.raw.xz image to be flashed

EXPECTED_RHEL_MAJOR = os.environ.get("EXPECTED_RHEL_MAJOR", "9") # expected rhel version
MAX_WRONG_OS_RETRIES = 3 # max number of times to try to fix the wrong OS
CI_DEFAULT_PASSWORD = "redhat" # default password for the CI, which run for time to time and reflash to different version of the image

# Serial console resilience. The Jumpstarter serial stream can break mid-wait
# (BrokenResourceError / "watch channel closed"); without liveness probing a
# single blocking expect() burns its whole timeout against a dead transport.
LOGIN_TIMEOUT = int(os.environ.get("WRAPPER_LOGIN_TIMEOUT", "300"))
LIVENESS_CHUNK_SECONDS = int(os.environ.get("WRAPPER_LIVENESS_CHUNK", "60"))
MAX_SILENT_PROBES = int(os.environ.get("WRAPPER_MAX_SILENT_PROBES", "5"))
MAX_SERIAL_RECONNECTS = int(os.environ.get("WRAPPER_MAX_SERIAL_RECONNECTS", "3"))
SERIAL_RECONNECT_DELAY = int(os.environ.get("WRAPPER_SERIAL_RECONNECT_DELAY", "15"))

# Grace added on top of a call's own timeout before SIGALRM interrupts it.
HARD_TIMEOUT_GRACE = int(os.environ.get("WRAPPER_HARD_TIMEOUT_GRACE", "15"))
# Upper bound on everything from first power-off to a booted, SSH-ready DUT.
BOOT_DEADLINE = int(os.environ.get("WRAPPER_BOOT_DEADLINE", "1800"))

# How long to wait for a kernel to come up after launching a loader from the
# UEFI Shell (firmware handoff + GRUB + kernel + systemd on this board is ~2min).
UEFI_BOOT_TIMEOUT = int(os.environ.get("WRAPPER_UEFI_BOOT_TIMEOUT", "240"))
# The Shell prompt is "Shell>" at startup and "FSn:\>" once a filesystem is current.
UEFI_PROMPTS = ["Shell>", ":\\>"]
# Mapped filesystems to probe for a bootloader, and where RHEL puts one.
UEFI_FS_CANDIDATES = [f"FS{i}" for i in range(8)]
UEFI_LOADER_DIRS = [
    ("\\EFI\\redhat", ["shimaa64.efi", "grubaa64.efi"]),
    ("\\EFI\\BOOT", ["BOOTAA64.EFI"]),
]
# Description used for the boot entry we create, so repeat runs can recognise it.
UEFI_BOOT_ENTRY_LABEL = "WRAPPER-RHEL"

LOG_DIR = Path(os.environ.get("WRAPPER_LOG_DIR", "wrapper_logs")).resolve()
WRAPPER_LOG = LOG_DIR / "wrapper.log"
SERIAL_LOG = LOG_DIR / "serial-console.log"


class SerialStreamDead(Exception):
    """The serial console transport is no longer delivering data.

    Distinct from a plain timeout: the device may be fine, but this pexpect
    session is useless and the console must be reopened.
    """


def _setup_logging():
    """Send [wrapper] diagnostics to stdout and to a log file.

    Without this the module logger has no handler and the root logger defaults
    to WARNING, so every logger.info() in this file is discarded — which is what
    made past CI failures undiagnosable.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("wrapper")
    log.setLevel(logging.INFO)
    log.propagate = False
    log.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(WRAPPER_LOG, mode="w")):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    return log


logger = _setup_logging()


def _phase(name):
    """Emit a greppable phase banner: grep for 'PHASE:' to get a run timeline."""
    logger.info("")
    logger.info("=" * 72)
    logger.info("[wrapper] PHASE: %s", name)
    logger.info("=" * 72)


class ConsoleTee:
    """Fan serial bytes out to stdout, a log file, and an in-memory tail.

    The tail survives pexpect buffer resets, so OS detection still works after
    liveness probes have consumed bytes that expect() never saw.
    """

    def __init__(self, path, tail_bytes=262144):
        self._file = open(path, "wb")
        self._tail = collections.deque(maxlen=tail_bytes)

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        sys.stdout.buffer.write(data)
        self._file.write(data)
        self._tail.extend(data)

    def flush(self):
        sys.stdout.buffer.flush()
        self._file.flush()

    def tail(self):
        return bytes(self._tail)

    def reset_tail(self):
        """Forget earlier output so OS detection can't match a previous boot."""
        self._tail.clear()

    def close(self):
        self.flush()
        self._file.close()


def _is_dead_stream_error(exc):
    """True if exc indicates a broken transport rather than a normal timeout."""
    dead = {"BrokenResourceError", "ClosedResourceError", "EndOfStream",
            "ConnectionResetError", "BrokenPipeError"}
    seen = {type(exc).__name__}
    for sub in getattr(exc, "exceptions", None) or ():
        seen.add(type(sub).__name__)
    return bool(seen & dead) or any(name in str(exc) for name in dead)


@contextlib.contextmanager
def _hard_timeout(seconds, message):
    """Wall-clock guard for calls that can block past their own timeout.

    pexpect's timeout is not trustworthy on the Jumpstarter serial stream: an
    expect_exact(timeout=60) has been observed blocking for 16 minutes after the
    transport died, which is how a CI run burned 1h48m in silence. SIGALRM
    interrupts the stuck syscall so we fail fast and reconnect instead.

    Only arms in the main thread (signals can't be delivered elsewhere) and must
    not be nested — there is a single process interval timer.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _fire(signum, frame):
        raise SerialStreamDead(message)

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _start_boot_watchdog(seconds):
    """Abort the process if the DUT never becomes usable within `seconds`.

    Backstop for hangs that escape the per-call guards (e.g. a blocking stream
    teardown). Returns a callable that cancels the watchdog once boot succeeds.
    """
    def _expire():
        logger.error("[wrapper] FAIL: boot watchdog expired after %ds — the DUT never became "
                     "usable. See %s for the console transcript.", seconds, SERIAL_LOG)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(2)

    timer = threading.Timer(seconds, _expire)
    timer.daemon = True
    timer.start()
    logger.info("[wrapper] HEALTH: boot watchdog armed for %ds", seconds)
    return timer.cancel


if USERNAME is None:
    raise ValueError("JETSON_USERNAME must be set when running tests over jumpstarter")
if PASSWORD is None and KEY_PATH is None:
    raise ValueError(
        "JETSON_PASSWORD or JETSON_KEY_PATH must be set when running tests over jumpstarter"
    )

# Resolve key path
key_filename = os.path.expanduser(KEY_PATH) if KEY_PATH else None
if key_filename and not os.path.exists(key_filename):
    raise ValueError(f"SSH key file not found: {key_filename}")


def _expand_env_vars(text):
    """Expand ${VAR} patterns in text using os.environ."""
    return re.sub(r'\$\{([^}]+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), text)


def _pull_single_image(addr, username, password, key_filename, img_spec, index, total, min_disk_mb):
    """Pull a single container image. Returns (status, url) where status is 'pulled', 'cached', or 'failed'."""
    from infra_tests.ssh_client import SSHConnection

    url = _expand_env_vars(img_spec["url"])
    timeout = img_spec.get("timeout", 1800)
    required = img_spec.get("required", True)
    size_hint = img_spec.get("size_hint", "unknown size")

    logger.info("[wrapper] [%d/%d] Starting: %s (%s, timeout=%ds)...",
                index, total, url, size_hint, timeout)

    try:
        with SSHConnection(addr[0], username, password, addr[1],
                           key_filename=key_filename) as ssh:
            # Check disk space on /var (where podman stores images on bootc)
            result = ssh.sudo("df -m /var | tail -1 | awk '{print $4}'", fail_on_rc=False)
            try:
                avail_mb = int(result.stdout.strip())
                logger.info("[wrapper] [%d/%d]   Disk space: %dMB available on /var (min: %dMB)",
                            index, total, avail_mb, min_disk_mb)
                if avail_mb < min_disk_mb:
                    logger.warning("[wrapper] [%d/%d]   Low disk space: %dMB < %dMB — pull may fail",
                                   index, total, avail_mb, min_disk_mb)
            except (ValueError, AttributeError):
                logger.warning("[wrapper] [%d/%d]   Could not parse disk space, continuing anyway",
                               index, total)

            # Check if already cached
            check = ssh.sudo(f"podman image exists {url}", fail_on_rc=False)
            if check.exit_status == 0:
                logger.info("[wrapper] [%d/%d]   Image already cached, skipping", index, total)
                return ("cached", url)

            # Drop memory caches before pull
            logger.info("[wrapper] [%d/%d]   Dropping memory caches...", index, total)
            ssh.sudo("sync; sync; sync", fail_on_rc=False)
            ssh.sudo("echo 3 | tee /proc/sys/vm/drop_caches", fail_on_rc=False)

            # Pull the image
            start = time.time()
            ssh.sudo(f"podman pull {url}", timeout=timeout)
            elapsed = int(time.time() - start)
            logger.info("[wrapper] [%d/%d]   Pull complete in %ds", index, total, elapsed)
            return ("pulled", url)

    except Exception as e:
        if required:
            raise RuntimeError(f"[wrapper] Required image pull failed ({url}): {e}")
        logger.warning("[wrapper] [%d/%d]   Optional image pull failed: %s: %s",
                       index, total, url, e)
        return ("failed", url)


def _prepull_container_images(addr, username, password, key_filename):
    """Pre-pull NGC container images listed in container_images.yaml in parallel.

    Creates a fresh SSH session for each pull through the active
    TcpPortforwardAdapter tunnel. All images are pulled concurrently.
    """
    if os.environ.get("SKIP_PREPULL", "").strip() in ("1", "true", "yes"):
        logger.info("[wrapper] SKIP_PREPULL is set, skipping container image pre-pull")
        return

    config_path = Path(__file__).parent / "container_images.yaml"
    if not config_path.exists():
        logger.warning("[wrapper] No container_images.yaml found, skipping pre-pull")
        return

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
    except Exception as e:
        logger.warning("[wrapper] Failed to parse container_images.yaml: %s, skipping pre-pull", e)
        return

    images = config.get("images", [])
    min_disk_mb = config.get("min_disk_space_mb", 5000)
    max_workers = config.get("max_parallel_pulls", len(images))  # pull all at once by default

    if not images:
        raise RuntimeError("[wrapper] No images listed in container_images.yaml")

    logger.info("[wrapper] Starting NGC container image pre-pull (%d images in parallel, max_workers=%d)...",
                len(images), max_workers)

    pulled, cached, failed = [], [], []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_pull_single_image, addr, username, password, key_filename,
                            img, i + 1, len(images), min_disk_mb): img
            for i, img in enumerate(images)
        }

        for future in concurrent.futures.as_completed(futures):
            try:
                status, url = future.result()
                if status == "pulled":
                    pulled.append(url)
                elif status == "cached":
                    cached.append(url)
                elif status == "failed":
                    failed.append(url)
            except Exception as e:
                # Required images raise RuntimeError, which bubbles up here
                raise

    logger.info("[wrapper] Pre-pull complete. Pulled: %d, Cached: %d, Failed: %d",
                len(pulled), len(cached), len(failed))


def _serial_probe(p, timeout=5):
    """Poke the console with a newline and read whatever comes back.

    Returns the bytes read (empty if the console stayed silent).
    Raises SerialStreamDead if the transport itself is gone.
    """
    try:
        with _hard_timeout(timeout + HARD_TIMEOUT_GRACE, "serial console write blocked indefinitely"):
            p.sendline("")
    except SerialStreamDead:
        raise
    except Exception as e:
        if _is_dead_stream_error(e):
            raise SerialStreamDead(f"send failed on serial console: {e}") from e
        raise

    try:
        with _hard_timeout(timeout + HARD_TIMEOUT_GRACE, "serial console read blocked indefinitely"):
            data = p.read_nonblocking(size=8192, timeout=timeout)
        return data if isinstance(data, bytes) else data.encode("utf-8", errors="replace")
    except SerialStreamDead:
        raise
    except PexpectTimeout:
        return b""
    except PexpectEOF as e:
        raise SerialStreamDead("serial console reached EOF during liveness probe") from e
    except Exception as e:
        if _is_dead_stream_error(e):
            raise SerialStreamDead(f"serial stream broke during liveness probe: {e}") from e
        raise


def _expect_with_liveness(p, patterns, total_timeout, label):
    """expect_exact() in short slices, probing console liveness between them.

    Returns the matched pattern index, or None if total_timeout expired while
    the console was still responsive. Raises SerialStreamDead as soon as the
    transport looks broken, so the caller can reconnect instead of blocking for
    the full timeout against a dead stream.
    """
    deadline = time.monotonic() + total_timeout
    silent_probes = 0
    logger.info("[wrapper] WAIT: %s — up to %ds, probing console every %ds",
                label, total_timeout, LIVENESS_CHUNK_SECONDS)

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("[wrapper] WAIT: %s — timed out after %ds (console still responsive)",
                           label, total_timeout)
            return None

        chunk = min(LIVENESS_CHUNK_SECONDS, remaining)
        try:
            with _hard_timeout(chunk + HARD_TIMEOUT_GRACE,
                               f"serial stream stopped responding while waiting for {label} "
                               f"(pexpect ignored its own {int(chunk)}s timeout)"):
                idx = p.expect_exact(patterns, timeout=chunk)
            logger.info("[wrapper] WAIT: %s — matched %r", label, patterns[idx])
            return idx
        except SerialStreamDead:
            raise
        except PexpectTimeout:
            pass
        except PexpectEOF as e:
            raise SerialStreamDead(f"serial console reached EOF while waiting for {label}") from e
        except Exception as e:
            if _is_dead_stream_error(e):
                raise SerialStreamDead(f"serial stream broke while waiting for {label}: {e}") from e
            raise

        data = _serial_probe(p)
        elapsed = int(total_timeout - max(deadline - time.monotonic(), 0))

        if data:
            silent_probes = 0
            logger.info("[wrapper] WAIT: %s — %ds/%ds, console ALIVE (%d bytes)",
                        label, elapsed, total_timeout, len(data))
            # The probe may have swallowed the very bytes we were waiting for.
            text = data.decode("utf-8", errors="replace")
            for idx, pattern in enumerate(patterns):
                if pattern in text:
                    logger.info("[wrapper] WAIT: %s — matched %r inside probe output", label, pattern)
                    return idx
        else:
            silent_probes += 1
            logger.warning("[wrapper] WAIT: %s — %ds/%ds, console SILENT (%d/%d consecutive probes)",
                           label, elapsed, total_timeout, silent_probes, MAX_SILENT_PROBES)
            if silent_probes >= MAX_SILENT_PROBES:
                raise SerialStreamDead(
                    f"console did not echo anything across {silent_probes} probes "
                    f"({silent_probes * LIVENESS_CHUNK_SECONDS}s) while waiting for {label}"
                )


def _preflight_health_check(client):
    """Verify every driver we depend on is present before touching the DUT.

    Failing here costs seconds; failing later costs a full boot timeout.
    """
    _phase("preflight health check")
    required = ("power", "storage", "serial", "ssh")
    missing = []

    for name in required:
        driver = getattr(client, name, None)
        if driver is None:
            missing.append(name)
            logger.error("[wrapper] HEALTH: driver %-8s MISSING", name)
        else:
            logger.info("[wrapper] HEALTH: driver %-8s ok (%s)", name, type(driver).__name__)

    if missing:
        raise RuntimeError(
            f"[wrapper] Exporter is missing required driver(s): {', '.join(missing)}. "
            "Check the exporter config for this device."
        )

    logger.info("[wrapper] HEALTH: login timeout %ds, liveness chunk %ds, "
                "max silent probes %d, max serial reconnects %d",
                LOGIN_TIMEOUT, LIVENESS_CHUNK_SECONDS, MAX_SILENT_PROBES, MAX_SERIAL_RECONNECTS)
    logger.info("[wrapper] HEALTH: all preflight checks passed")


def _post_boot_health_check(ssh):
    """Log the booted system's identity so failures downstream have context."""
    _phase("post-boot health check")
    checks = (
        ("kernel", "uname -r"),
        ("os", "cat /etc/redhat-release"),
        ("uptime", "uptime -p"),
        ("disk /var", "df -h /var | tail -1"),
        ("failed units", "systemctl --failed --no-legend --plain | wc -l"),
    )
    for label, command in checks:
        try:
            result = ssh.run(command, timeout=30)
            logger.info("[wrapper] HEALTH: %-13s %s", label, result.stdout.strip())
        except Exception as e:
            logger.warning("[wrapper] HEALTH: %-13s check failed: %s", label, e)


# Shared by the serial and SSH pinning paths below. Runs as a SINGLE compound
# command — multiple dependent sendlines interleave on the shared serial console
# (console=ttyTCU0).
EFI_PIN_CMD = (
    "dmesg -n 1; "
    # Build the marker from a shell var so the literal 'WRAPPER_PIN_OK'
    # appears only in the command OUTPUT, never in its echo. Otherwise
    # p.expect_exact() matches the echoed command and returns BEFORE the
    # command finishes, letting the next sendline interleave with it and
    # corrupt the following SSH-config step (serial rule).
    "PMARK=WRAPPER_PIN; "
    "BC=$(efibootmgr | awk '/^BootCurrent:/{print $2}'); "
    "if [ -n \"$BC\" ]; then "
    # grep -E '^Boot[0-9A-Fa-f]{4}' (require 4 hex) so we match real boot
    # entries only, NOT the 'BootCurrent:'/'BootOrder:' info lines
    # ('BootCurrent' -> 'Curr' would otherwise slip through and yield a
    # bogus 'efibootmgr -b Curr -B'). See serial rules in memory.
    "for n in $(efibootmgr | grep -E '^Boot[0-9A-Fa-f]{4}' "
    "| grep -iE 'Red Hat|RHEL|Bootc|shim|redhat|PXE|Network|IPv4|IPv6|HTTP|EFI Network' "
    "| awk '{print substr($1,5,4)}'); do "
    "[ \"$n\" != \"$BC\" ] && efibootmgr -b \"$n\" -B >/dev/null 2>&1; "
    "done; "
    "O=$(efibootmgr | awk '/^BootOrder:/{print $2}'); "
    "R=$(echo \"$O\" | sed \"s/$BC,//g; s/,$BC//g; s/^$BC$//\"); "
    "efibootmgr -o \"$BC${R:+,$R}\" >/dev/null 2>&1; "
    "echo ${PMARK}_OK BC=$BC; "
    "else echo ${PMARK}_SKIP_NO_BOOTCURRENT; fi"
)


def _pin_usb_boot_first(p):
    """Make the firmware deterministically boot the flashed USB image on every
    subsequent power-on (the 'always USB' fix).

    Background (agx-orin-11, see .claude/memory/jumpstarter-errors.md 2026-09-06):
    every flash appends a new "Red Hat Enterprise Linux" UEFI boot entry and old
    ones are never cleaned up (80+ stale duplicates observed, all pointing at
    dead/old UUIDs or the internal eMMC). Which one the firmware puts first in
    BootOrder varies per flash, so the device intermittently boots the STALE
    internal eMMC RHEL — which hangs in early kernel init and never reaches
    login — instead of the freshly flashed USB image. (It also risks filling
    UEFI NVRAM -> the "Volume Full" boot-loop seen on nx-orin-01.)

    This must be called ONLY once we have a root shell on the correctly-booted
    USB image. Because the stale eMMC install hangs *before* login, simply
    reaching a login/root shell guarantees we are on the USB image, so
    `BootCurrent` is the good USB boot entry. We:
      1. pin BootCurrent first in BootOrder, and
      2. delete every other stale RHEL/PXE/Network entry (keeping BootCurrent),
    so the next boot (bootc firstboot reboots, later test phases, and subsequent
    runs until the next reflash) is deterministic.

    Assumes the caller is already logged in as root at a shell prompt (`#`).
    Runs as a SINGLE compound command — multiple dependent sendlines interleave
    on the shared serial console (console=ttyTCU0).
    """
    logger.info("[wrapper] Pinning USB boot entry first + pruning stale EFI entries...")
    p.sendline(EFI_PIN_CMD)
    try:
        idx = p.expect_exact(
            ["WRAPPER_PIN_OK", "WRAPPER_PIN_SKIP_NO_BOOTCURRENT"], timeout=60
        )
        if idx == 0:
            logger.info("[wrapper] USB boot entry pinned first; stale EFI entries pruned")
        else:
            logger.warning("[wrapper] Could not read BootCurrent — skipped EFI pin (non-fatal)")
    except Exception:
        # Non-fatal: the current boot already succeeded; pinning only helps
        # future boots. Don't fail the run if the serial marker is missed.
        logger.warning("[wrapper] EFI pin marker not seen — continuing (non-fatal)")


def _pin_usb_boot_first_ssh(ssh):
    """Same boot-order pin as _pin_usb_boot_first, over SSH instead of serial.

    The serial version only runs on the password path, and it has to dodge console
    interleaving. Running it again here means the boot order is always repaired
    before growpart and the container pulls — the long steps during which an
    unexpected reboot would otherwise drop the DUT back into PXE or the UEFI Shell.
    Idempotent: it recomputes BootCurrent and re-applies the same order.
    """
    logger.info("[wrapper] HEALTH: pinning boot order over SSH (pre-growpart)...")
    try:
        # Wrap in bash -c: Fabric's sudo() only elevates the first clause of a
        # compound command, so the efibootmgr calls after the first ';' would run
        # unprivileged. Harmless while we log in as root, wrong for anyone else.
        result = ssh.sudo(f"bash -c {shlex.quote(EFI_PIN_CMD)}", fail_on_rc=False)
        output = getattr(result, "stdout", "") or ""
        if "WRAPPER_PIN_OK" in output:
            logger.info("[wrapper] HEALTH: OK: boot order pinned — %s",
                        " ".join(output.split())[:120])
        elif "WRAPPER_PIN_SKIP_NO_BOOTCURRENT" in output:
            logger.warning("[wrapper] HEALTH: no BootCurrent — boot order left alone (non-fatal)")
        else:
            logger.warning("[wrapper] HEALTH: boot-order pin gave no marker (non-fatal): %s",
                           " ".join(output.split())[:120])
    except Exception as e:
        logger.warning("[wrapper] HEALTH: boot-order pin over SSH failed (non-fatal): %s", e)


def _configure_ssh_via_console(p):
    """Log in over serial and enable root SSH so the test tunnel can attach."""
    _phase("configure SSH over serial console")
    time.sleep(10)

    # Drop stale output so the login prompt we match is a fresh one.
    try:
        while True:
            p.read_nonblocking(size=4096, timeout=1)
    except Exception:
        pass

    # If the device rebooted after firstboot (SELinux relabel, growpart, ...)
    # the prompt won't come back until the second boot finishes.
    p.sendline("")
    try:
        p.expect_exact("login:", timeout=60)
    except Exception:
        logger.info("[wrapper] No login prompt — device may have rebooted (firstboot). Waiting for next boot...")
        if not _wait_for_login(p):
            raise RuntimeError("[wrapper] Failed to reach login prompt after device reboot")
        try:
            while True:
                p.read_nonblocking(size=4096, timeout=1)
        except Exception:
            pass
        p.sendline("")
        p.expect_exact("login:", timeout=60)

    p.sendline(USERNAME)
    p.expect("assword:", timeout=30)
    p.sendline(PASSWORD)
    p.expect(r"[#\$]", timeout=30)
    logger.info("[wrapper] OK: logged in over serial console as %s", USERNAME)

    # Reaching a root shell means we booted the flashed USB image (the stale
    # internal eMMC install hangs before login), so BootCurrent is the good
    # USB entry. Pin it first + prune stale duplicates so future boots are
    # deterministic ('always USB'). Non-fatal on failure.
    _pin_usb_boot_first(p)

    p.sendline(
        "echo 'PermitRootLogin yes' > /etc/ssh/sshd_config.d/01-permitrootlogin.conf"
        # Also enable password auth: reaching login proves the root password
        # is valid, but the image may ship PasswordAuthentication no, which
        # blocks the paramiko root-password SSH used by growpart/prepull steps.
        " && echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config.d/01-permitrootlogin.conf"
        " && chmod 644 /etc/ssh/sshd_config.d/01-permitrootlogin.conf"
        " && systemctl restart sshd"
        " && echo WRAPPER_SSH_CONFIG_OK"
    )
    p.expect_exact("WRAPPER_SSH_CONFIG_OK", timeout=30)
    logger.info("[wrapper] OK: SSH root login enabled and sshd restarted")

    p.sendline("exit")


def _detect_wrong_os(boot_output):
    """Check if device booted into wrong OS based on serial console output.

    Looks for RHEL version indicators in the text before the login: prompt.
    Returns (is_wrong, detected_version) tuple.
    """
    text = boot_output.decode("utf-8", errors="replace") if isinstance(boot_output, bytes) else str(boot_output)

    # Check for "Red Hat Enterprise Linux X.Y" in banner
    match = re.search(r'Enterprise Linux (\d+)', text)
    if match:
        booted_major = match.group(1)
        if booted_major != EXPECTED_RHEL_MAJOR:
            return True, booted_major

    # Check kernel version string for .elX pattern
    match = re.search(r'\.el(\d+)', text)
    if match:
        booted_major = match.group(1)
        if booted_major != EXPECTED_RHEL_MAJOR:
            return True, booted_major

    return False, None


def _fix_efi_via_serial(p):
    """Log into wrong OS and remove all OS-related EFI boot entries.

    Uses CI default password ("redhat") to log into the NVMe OS, removes all
    existing OS boot entries. Does NOT create new entries — relies on the
    hardware USB fallback (e.g. Boot0001 SanDisk) which doesn't use partition
    UUIDs and always works after a flash.
    """
    logger.info("[wrapper] Logging into wrong OS to fix EFI boot entries...")

    # Get a fresh login prompt and log in with CI default password
    p.sendline("")
    p.expect_exact("login:", timeout=30)
    p.sendline("root")
    p.expect("assword:", timeout=30)
    p.sendline(CI_DEFAULT_PASSWORD)
    p.expect(r"[#\$]", timeout=30)
    logger.info("[wrapper] Logged into wrong OS with CI default password")

    # Silence kernel console messages — they share the serial port (console=ttyTCU0)
    # and can split command output, causing pexpect markers to be unmatched
    p.sendline("dmesg -n 1 && echo WRAPPER_DMESG_OK")
    p.expect_exact("WRAPPER_DMESG_OK", timeout=15)
    logger.info("[wrapper] Kernel console messages silenced")

    # Show current EFI boot entries for debugging
    p.sendline("efibootmgr -v && echo WRAPPER_EFI_LIST_OK")
    p.expect_exact("WRAPPER_EFI_LIST_OK", timeout=30)
    logger.info("[wrapper] Current EFI entries:\n%s", p.before)

    # Remove ALL OS-related and PXE/network boot entries
    # Do NOT create any new entries — rely on hardware USB fallback
    # Filter with '^Boot[0-9]' first to exclude BootCurrent/BootOrder info lines
    # PXE/network entries cause the device to boot from Beaker PXE server
    # instead of USB, ending up at UEFI Shell
    remove_cmd = (
        "for num in $(efibootmgr | grep '^Boot[0-9]' "
        "| grep -iE 'Red Hat|RHEL|Bootc|Jumpstarter|shim|redhat|PXE|Network|IPv4|IPv6|HTTP|EFI Network' "
        "| awk '{print substr($1,5,4)}'); "
        "do echo \"Removing Boot$num\"; efibootmgr -b $num -B 2>/dev/null; done "
        "&& echo WRAPPER_EFI_REMOVE_OK"
    )
    p.sendline(remove_cmd)
    p.expect_exact("WRAPPER_EFI_REMOVE_OK", timeout=30)
    logger.info("[wrapper] Removed all OS-related EFI boot entries")

    # Reorder boot entries: put SanDisk USB first to avoid network boot timeouts
    # MUST be a single sendline — multiple sendlines interleave on serial console
    reorder_cmd = (
        "U=$(efibootmgr|grep -i SanDisk|head -1|awk '{print substr($1,5,4)}') && "
        "O=$(efibootmgr|grep ^BootOrder:|awk '{print $2}') && "
        "R=$(echo $O|sed \"s/$U,//;s/,$U//;s/$U//\") && "
        "efibootmgr -o $U,$R && "
        "echo WRAPPER_EFI_REORDER_OK || echo WRAPPER_EFI_REORDER_OK"
    )
    p.sendline(reorder_cmd)
    # expect_exact matches the echo first (harmless), then the verify step
    # waits for the actual command to complete before proceeding
    p.expect_exact("WRAPPER_EFI_REORDER_OK", timeout=30)
    logger.info("[wrapper] Boot order updated — SanDisk USB is first")

    # Show remaining entries for verification
    p.sendline("efibootmgr -v && echo WRAPPER_EFI_VERIFY_OK")
    p.expect_exact("WRAPPER_EFI_VERIFY_OK", timeout=30)
    logger.info("[wrapper] Remaining EFI entries:\n%s", p.before)

    p.sendline("exit")
    time.sleep(2)
    logger.info("[wrapper] EFI boot fix complete, will re-flash and retry boot from USB")


def _handle_emergency(p):
    """Handle emergency mode by trying password login + exit, repeating if needed.

    Each round: try CI_DEFAULT_PASSWORD ("redhat") then the user's PASSWORD.
    If a password works: logs in, sends "exit" to continue boot, waits for login prompt.
    If emergency reappears after "exit": repeats the password+exit cycle.

    Raises RuntimeError if no password works or emergency keeps reappearing.
    """
    MAX_EMERGENCY_ROUNDS = 3

    for round_num in range(MAX_EMERGENCY_ROUNDS):
        # Try each password
        logged_in = False
        for pwd_label, pwd in [("CI default (redhat)", CI_DEFAULT_PASSWORD), ("configured bootc", PASSWORD)]:
            if not pwd:
                continue
            logger.info("[wrapper] Emergency round %d: trying %s password...", round_num + 1, pwd_label)
            p.sendline(pwd)
            try:
                idx = p.expect([r"[#\$]", "Login incorrect", "Give root password"], timeout=15)
                if idx == 0:
                    logged_in = True
                    logger.info("[wrapper] Emergency login succeeded with %s password", pwd_label)
                    break
                logger.info("[wrapper] %s password rejected", pwd_label)
            except Exception:
                logger.info("[wrapper] %s password attempt failed (timeout/error)", pwd_label)
                continue

        if not logged_in:
            raise RuntimeError(
                "[wrapper] Emergency mode: neither the CI default password ('redhat') "
                "nor the configured root password for the bootc image worked. "
                "Cannot continue. Please verify the root password is correct in "
                "config.toml and that the image was built with the expected credentials."
            )

        # Got shell — silence kernel console messages first, then fix fstab
        p.sendline("dmesg -n 1")
        time.sleep(1)

        logger.info("[wrapper] Fixing /boot/efi fstab entry to prevent emergency mode loop...")
        p.sendline("sed -i '/boot\\/efi/s/^/#/' /etc/fstab && echo WRAPPER_FSTAB_FIX_OK")
        try:
            p.expect_exact("WRAPPER_FSTAB_FIX_OK", timeout=15)
            logger.info("[wrapper] /boot/efi commented out in fstab")
        except Exception:
            logger.info("[wrapper] fstab fix command did not confirm (may not have /boot/efi entry)")

        logger.info("[wrapper] Sending 'exit' to continue boot past emergency mode...")
        p.sendline("exit")
        time.sleep(5)

        # Wait for login prompt or another emergency
        idx2 = p.expect_exact(["login:", "Give root password"], timeout=120)
        if idx2 == 0:
            logger.info("[wrapper] Got login prompt after emergency recovery (round %d)", round_num + 1)
            return True
        else:
            logger.info("[wrapper] Emergency mode reappeared after exit (round %d/%d), retrying...",
                        round_num + 1, MAX_EMERGENCY_ROUNDS)

    # Password works but emergency keeps looping — signal caller to try NVMe boot fallback
    logger.info(
        "[wrapper] Emergency mode keeps reappearing after %d rounds of password login + exit. "
        "Will power cycle without USB to boot NVMe and fix EFI entries.",
        MAX_EMERGENCY_ROUNDS
    )
    return False


def _console_text(raw):
    """Decode whatever pexpect left in .before into printable text."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return raw


def _uefi_send(p, command, settle=0.0):
    """Send one command to the UEFI Shell.

    pexpect's sendline() terminates with LF, which EDK2's terminal driver drops:
    the command gets echoed but never runs, so the Shell looks like it has gone
    deaf ("Lost Shell> prompt"). The Shell's Enter key is a bare CR.
    """
    p.send(command + "\r")
    if settle:
        time.sleep(settle)


def _uefi_wait_prompt(p, timeout=20):
    """Wait for the Shell to come back to a prompt. Returns True if it did."""
    try:
        p.expect_exact(UEFI_PROMPTS, timeout=timeout)
        return True
    except SerialStreamDead:
        raise
    except Exception:
        return False


def _uefi_drain(p):
    """Discard prompts already sitting in the read buffer.

    Prompt accounting has to be exact: one stale prompt puts every later
    _uefi_run() a step behind, so each call returns the *previous* command's
    output and loader detection silently reports "nothing found".
    """
    for _ in range(20):
        try:
            p.expect_exact(UEFI_PROMPTS, timeout=0)
        except SerialStreamDead:
            raise
        except Exception:
            return


def _uefi_run(p, command, timeout=20):
    """Run a Shell command and return its output, or None if the Shell went quiet."""
    _uefi_drain(p)
    _uefi_send(p, command)
    if not _uefi_wait_prompt(p, timeout=timeout):
        return None
    return _console_text(p.before)


def _uefi_find_loader(p):
    """Return 'FSn:\\path\\loader.efi' for the first RHEL bootloader we can see."""
    logger.info("[wrapper] UEFI: refreshing device map...")
    _uefi_run(p, "map -r", timeout=30)

    for fs in UEFI_FS_CANDIDATES:
        for directory, loaders in UEFI_LOADER_DIRS:
            # List the directory rather than stat the file: the Shell echoes the
            # command back, so probing "ls FS0:\EFI\redhat\shimaa64.efi" would
            # match its own echo and report every path as present.
            listing = _uefi_run(p, f"ls {fs}:{directory}", timeout=15)
            if listing is None:
                logger.info("[wrapper] UEFI:   %s:%s — no response", fs, directory)
                continue
            lowered = listing.lower()
            for loader in loaders:
                if loader.lower() in lowered:
                    path = f"{fs}:{directory}\\{loader}"
                    logger.info("[wrapper] UEFI: OK: found bootloader at %s", path)
                    return path

    logger.warning("[wrapper] UEFI: no RHEL bootloader on any mapped filesystem")
    return None


def _uefi_persist_boot_entry(p, loader):
    """Put `loader` at the head of BootOrder with bcfg so the next reset just boots.

    Skips the add when a previous run already left the entry behind: bcfg appends a
    fresh Boot#### variable every time it is called and there is no point filling
    NVRAM with duplicates of the same loader.
    """
    dump = _uefi_run(p, "bcfg boot dump", timeout=30)
    if dump and UEFI_BOOT_ENTRY_LABEL in dump:
        logger.info("[wrapper] UEFI: boot entry %s already exists — leaving BootOrder alone",
                    UEFI_BOOT_ENTRY_LABEL)
        return

    logger.info("[wrapper] UEFI: adding %s as BootOrder #1 -> %s", UEFI_BOOT_ENTRY_LABEL, loader)
    result = _uefi_run(p, f'bcfg boot add 0 {loader} "{UEFI_BOOT_ENTRY_LABEL}"', timeout=30)
    if result is None:
        logger.warning("[wrapper] UEFI: bcfg did not return a prompt — BootOrder may be unchanged")
    else:
        logger.info("[wrapper] UEFI: bcfg said: %s", " ".join(result.split())[:200])


def _try_efi_shell_boot(p):
    """Repair the boot path from the UEFI Shell and get the DUT into RHEL.

    Landing at Shell> means the firmware walked the whole of BootOrder without
    booting anything — normally PXE entries ranked ahead of the disk, or a stale
    entry pointing at a device that is no longer attached. The Shell can fix both:
    bcfg puts a working loader at the head of BootOrder so future boots are
    unattended, and launching that loader by path gets this run moving now.

    Returns True if a login prompt was reached, False otherwise.
    """
    # Flush the Shell's input line before anything else. An earlier pexpect
    # sendline() left its text sitting there unsubmitted (the LF was dropped), and
    # our first CR would otherwise run that text glued to our own command —
    # "map -r" after a stale "exit" executes as "exitmap -r".
    _uefi_drain(p)
    _uefi_send(p, "")
    if not _uefi_wait_prompt(p, timeout=20):
        # Nothing came back; nudge once more before giving up on the Shell.
        _uefi_send(p, "")
        if not _uefi_wait_prompt(p, timeout=20):
            logger.warning("[wrapper] UEFI: Shell is not responding to input")
            return False

    loader = _uefi_find_loader(p)
    if loader is None:
        return False

    # Persist before launching: if the direct launch works we never come back here,
    # and if it does not, the reset below needs the repaired BootOrder already in place.
    _uefi_persist_boot_entry(p, loader)

    logger.info("[wrapper] UEFI: launching %s directly...", loader)
    _uefi_send(p, loader)
    try:
        idx = p.expect_exact(["login:", "Give root password", "Use the ^ and v keys"] + UEFI_PROMPTS,
                             timeout=UEFI_BOOT_TIMEOUT)
    except SerialStreamDead:
        raise
    except Exception:
        idx = None

    if idx == 0:
        logger.info("[wrapper] UEFI: OK: booted from %s — login prompt reached", loader)
        return True
    if idx == 1:
        logger.info("[wrapper] UEFI: booted from %s into emergency mode", loader)
        return _handle_emergency(p)
    if idx == 2:
        logger.info("[wrapper] UEFI: GRUB menu reached, sending ENTER to boot the default entry")
        p.sendline("")
        return _uefi_wait_for_login_after_boot(p)

    # Back at the Shell (or silence). Hand it to the firmware: BootOrder is repaired,
    # so a reset exercises the normal boot path instead of a bare loader launch.
    logger.info("[wrapper] UEFI: direct launch did not boot — resetting to use the "
                "repaired BootOrder")
    _uefi_send(p, "reset")
    return _uefi_wait_for_login_after_boot(p)


def _uefi_wait_for_login_after_boot(p):
    """Wait out a boot we just triggered from the Shell, stepping past a GRUB menu."""
    for _ in range(2):
        try:
            idx = p.expect_exact(["login:", "Give root password", "Use the ^ and v keys"],
                                 timeout=UEFI_BOOT_TIMEOUT)
        except SerialStreamDead:
            raise
        except Exception:
            logger.warning("[wrapper] UEFI: no login prompt after boot")
            return False
        if idx == 0:
            logger.info("[wrapper] UEFI: OK: login prompt reached")
            return True
        if idx == 1:
            logger.info("[wrapper] UEFI: emergency mode after boot")
            return _handle_emergency(p)
        logger.info("[wrapper] UEFI: GRUB menu, sending ENTER to boot the default entry")
        p.sendline("")
    return False


def _wait_for_login(p):
    """Wait for login: prompt, handling grub>, UEFI Shell, PXE GRUB menu, dutlink, and emergency mode.

    Returns True if login prompt was reached, False otherwise.
    Raises RuntimeError if emergency mode password login fails.
    """
    got_login = False
    for attempt in range(3):
        try:
            idx = _expect_with_liveness(
                p,
                ["login:", "grub>", "Give root password", "Shell>", "Use the ^ and v keys", "Enter to continue boot."],
                LOGIN_TIMEOUT,
                f"login prompt (attempt {attempt + 1}/3)",
            )
            if idx is None:
                raise PexpectTimeout("no recognizable prompt within login timeout")
            if idx == 0:
                got_login = True
                break
            elif idx == 1:
                logger.info(f"\n[wrapper] Device stuck at grub> (attempt {attempt + 1}/3), sending 'exit' to force reboot...")
                p.sendline("exit")
                time.sleep(10)
            elif idx == 2:
                logger.info(f"\n[wrapper] Emergency mode detected (attempt {attempt + 1}/3)")
                if _handle_emergency(p):
                    got_login = True
                    break
            elif idx == 3:
                # Reaching the Shell means the firmware exhausted BootOrder without
                # booting anything — nothing will happen if we just wait it out.
                logger.info("[wrapper] UEFI Shell detected (attempt %d/3) — no boot option "
                            "succeeded. Repairing BootOrder from the Shell...", attempt + 1)
                if _try_efi_shell_boot(p):
                    got_login = True
                    break
                logger.warning("[wrapper] UEFI Shell repair did not reach a login prompt")
            elif idx == 4:
                logger.info("[wrapper] GRUB boot menu detected, sending ENTER to boot default entry...")
                p.sendline("")
                time.sleep(5)
                continue
            elif idx == 5:
                logger.info("[wrapper] UEFI boot screen detected, sending ENTER to continue boot...")
                p.sendline("")
                time.sleep(5)
                continue
        except SerialStreamDead:
            raise  # caller reopens the console; retrying here would hit the same dead stream
        except RuntimeError:
            raise  # don't swallow RuntimeError from _handle_emergency
        except Exception:
            logger.info(f"\n[wrapper] Timeout waiting for login/grub (attempt {attempt + 1}/3), sending ENTER to probe for dutlink shell...")
            # LF for Linux consoles, CR for the UEFI Shell — it ignores LF entirely.
            p.sendline("")
            p.send("\r")
            try:
                idx = p.expect_exact(["#>", "login:", "grub>", "Shell>"], timeout=30)
                if idx == 0:
                    logger.info("[wrapper] Detected dutlink internal shell (#>), sending 'console' to re-enter serial console...")
                    p.sendline("console")
                    time.sleep(5)
                elif idx == 1:
                    got_login = True
                    break
                elif idx == 2:
                    logger.info("[wrapper] Got grub> after probe, sending 'exit'...")
                    p.sendline("exit")
                    time.sleep(10)
                elif idx == 3:
                    logger.info("[wrapper] Got Shell> after probe, trying direct EFI boot...")
                    if _try_efi_shell_boot(p):
                        got_login = True
                        break
            except Exception:
                logger.info("[wrapper] No recognizable prompt after probe, retrying...")

    return got_login


started_at = time.monotonic()
logger.info("[wrapper] Jetson test wrapper starting at %s",
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
logger.info("[wrapper] Expecting RHEL %s | wrapper log: %s | serial log: %s",
            EXPECTED_RHEL_MAJOR, WRAPPER_LOG, SERIAL_LOG)
logger.info("[wrapper] Grep tips: 'PHASE:' for the timeline, 'HEALTH:' for checks, "
            "'FAIL:' for errors, 'WAIT:' for boot progress")

console = ConsoleTee(SERIAL_LOG)

with env() as client:
    # NOTE: Do NOT wrap this in `client.log_stream()`. log_stream() opens a
    # second, passive consumer of the serial stream that races the interactive
    # `client.serial.pexpect()` reader opened below. The serial driver delivers
    # each byte to only one reader, so log_stream() intermittently swallows boot
    # output (including the `login:` prompt) before pexpect can match it —
    # causing _wait_for_login() to time out even though the device booted fine.
    # pexpect still mirrors live serial to stdout via `p.logfile`, so we lose no
    # visibility. See .claude/memory/jumpstarter-errors.md (2026-09-06).
    with contextlib.nullcontext():
        _preflight_health_check(client)
        cancel_boot_watchdog = _start_boot_watchdog(BOOT_DEADLINE)

        # When emergency mode can't be resolved via password+exit, skip storage.dut()
        # on the next attempt so the device boots from NVMe. The wrong OS detection
        # will then fix EFI entries and re-flash, allowing a clean USB boot after.
        force_nvme_boot = False
        booted_ok = False

        for boot_attempt in range(MAX_WRONG_OS_RETRIES + 1):
            _phase(f"boot attempt {boot_attempt + 1}/{MAX_WRONG_OS_RETRIES + 1}")
            wrong_os = False
            got_login = False

            client.power.off()
            logger.info("[wrapper] DUT powered off")

            if force_nvme_boot:
                logger.info("[wrapper] Skipping storage.dut() — forcing NVMe boot to fix EFI entries")
                force_nvme_boot = False
            else:
                client.storage.dut()
                logger.info("[wrapper] Storage connected to DUT")

            console.reset_tail()
            client.power.on()
            logger.info("[wrapper] DUT powered on")

            # A broken serial transport is not a boot failure — reopen the console
            # and keep watching the same boot instead of power cycling.
            for serial_attempt in range(MAX_SERIAL_RECONNECTS + 1):
                try:
                    with client.serial.pexpect() as p:
                        p.logfile = console
                        if serial_attempt == 0:
                            time.sleep(30)
                        else:
                            logger.info("[wrapper] Serial console reopened (reconnect %d/%d)",
                                        serial_attempt, MAX_SERIAL_RECONNECTS)

                        got_login = _wait_for_login(p)
                        if not got_login:
                            break

                        # Check if device booted into the wrong OS (e.g., RHEL 10 from NVMe).
                        # Include the tee tail: liveness probes may have consumed the
                        # banner bytes before expect() ever saw them.
                        before = p.before or b""
                        if isinstance(before, bytes):
                            before = before.decode("utf-8", errors="replace")
                        wrong_os, detected_version = _detect_wrong_os(
                            before + console.tail().decode("utf-8", errors="replace")
                        )

                        if wrong_os:
                            if boot_attempt >= MAX_WRONG_OS_RETRIES:
                                raise RuntimeError(
                                    f"[wrapper] Device keeps booting wrong OS (RHEL {detected_version}) "
                                    f"after {MAX_WRONG_OS_RETRIES} EFI fix attempts. "
                                    f"Expected RHEL {EXPECTED_RHEL_MAJOR}."
                                )
                            logger.warning(
                                "[wrapper] Wrong OS detected: RHEL %s (expected RHEL %s). "
                                "Fixing EFI boot entries (attempt %d/%d)...",
                                detected_version, EXPECTED_RHEL_MAJOR,
                                boot_attempt + 1, MAX_WRONG_OS_RETRIES,
                            )
                            _fix_efi_via_serial(p)
                            # exits serial context, then re-flash below before retrying
                        else:
                            logger.info("[wrapper] OK: reached login prompt with the expected OS")
                            if PASSWORD:
                                _configure_ssh_via_console(p)
                            booted_ok = True
                    break
                except SerialStreamDead as e:
                    if serial_attempt >= MAX_SERIAL_RECONNECTS:
                        logger.error("[wrapper] FAIL: serial console unusable after %d reconnects: %s",
                                     MAX_SERIAL_RECONNECTS, e)
                        got_login = False
                        break
                    logger.warning("[wrapper] Serial console died (%s) — reconnecting in %ds "
                                   "[reconnect %d/%d]",
                                   e, SERIAL_RECONNECT_DELAY, serial_attempt + 1, MAX_SERIAL_RECONNECTS)
                    time.sleep(SERIAL_RECONNECT_DELAY)

            if booted_ok:
                break

            if not got_login:
                # Could not reach login prompt. Possible causes:
                # - Emergency mode looping (password works but system can't boot)
                # - Timeout / grub stuck / serial transport gone
                # _handle_emergency raises RuntimeError if password fails,
                # so this path means either emergency looping or other failure.
                # Either way: power cycle without USB → boot NVMe → EFI fix.
                logger.error(
                    "[wrapper] FAIL: no login prompt (attempt %d/%d). Will boot NVMe next to fix EFI...",
                    boot_attempt + 1, MAX_WRONG_OS_RETRIES + 1,
                )
                if boot_attempt >= MAX_WRONG_OS_RETRIES:
                    raise RuntimeError(
                        f"[wrapper] Failed to reach login: prompt after all retries. "
                        f"See {SERIAL_LOG} for the full console transcript."
                    )
                force_nvme_boot = True
                continue

            # If wrong OS was detected, re-flash before retrying boot
            if wrong_os:
                if DISK_IMAGE_PATH:
                    logger.info("[wrapper] Re-flashing image: %s", DISK_IMAGE_PATH)
                    client.storage.flash(DISK_IMAGE_PATH, compression=Compression.XZ)
                    logger.info("[wrapper] OK: re-flash complete")
                else:
                    logger.warning(
                        "[wrapper] DISK_IMAGE_PATH not set — skipping re-flash. "
                        "Set DISK_IMAGE_PATH to the .raw.xz image path for automatic re-flash."
                    )

        cancel_boot_watchdog()

        if not booted_ok:
            raise RuntimeError(
                f"[wrapper] Failed to boot correct OS (RHEL {EXPECTED_RHEL_MAJOR}) "
                f"after {MAX_WRONG_OS_RETRIES + 1} attempts"
            )
        logger.info("[wrapper] OK: DUT booted and reachable after %ds", int(time.monotonic() - started_at))

        _phase("open SSH tunnel")
        # Wait for SSH service to be fully ready after sshd restart
        logger.info("[wrapper] Waiting for SSH service to start...")
        time.sleep(10)

        ssh_client = client.ssh.tcp if hasattr(client.ssh, 'tcp') else client.ssh
        with TcpPortforwardAdapter(client=ssh_client) as addr:
            os.environ["JETSON_HOST"] = addr[0]
            os.environ["JETSON_PORT"] = str(addr[1])
            os.environ["JUMPSTARTER_IN_USE"] = "1"
            logger.info("[wrapper] OK: SSH tunnel up at %s:%s", addr[0], addr[1])

            project_root = Path(__file__).parent.parent
            sys.path.insert(0, str(project_root))
            from infra_tests.ssh_client import SSHConnection

            with SSHConnection(
                addr[0],
                USERNAME,
                PASSWORD,
                addr[1],
                key_filename=key_filename,
            ) as ssh:
                _post_boot_health_check(ssh)
                # Repair the boot order BEFORE the long steps (growpart, image
                # pulls). If anything reboots the DUT during them, it has to come
                # back on this image rather than PXE or the UEFI Shell.
                _pin_usb_boot_first_ssh(ssh)
                ssh.sudo("/usr/libexec/bootc-generic-growpart")

            os.environ.setdefault("L4T_JETPACK_IMAGE", "nvcr.io/nvidia/l4t-jetpack:r36.4.0")
            _phase("pre-pull container images")
            logger.info("[wrapper] This may take a while — DO NOT force-exit the wrapper.")
            logger.info("[wrapper] To check progress: "
                        "'jmp shell --lease <LEASE> -- j serial start-console' then 'pgrep -fa podman'")
            _prepull_container_images(addr, USERNAME, PASSWORD, key_filename)

            _phase("run tests")
            logger.info("[wrapper] Setup took %ds", int(time.monotonic() - started_at))
            logger.info("[wrapper] JETSON_HOST=%s JETSON_PORT=%s JETSON_USERNAME=%s JETSON_KEY_PATH=%s",
                        os.environ["JETSON_HOST"], os.environ["JETSON_PORT"],
                        os.environ.get("JETSON_USERNAME"),
                        os.environ.get("JETSON_KEY_PATH", "(not set)"))
            logger.info("[wrapper] Command: %s", " ".join(sys.argv[1:]))

            result = subprocess.run(sys.argv[1:])

            _phase("done")
            if result.returncode == 0:
                logger.info("[wrapper] OK: tests passed (total runtime %ds)",
                            int(time.monotonic() - started_at))
            else:
                logger.error("[wrapper] FAIL: tests exited %d (total runtime %ds)",
                             result.returncode, int(time.monotonic() - started_at))
            logger.info("[wrapper] Logs: %s | %s", WRAPPER_LOG, SERIAL_LOG)
            console.close()
            sys.exit(result.returncode)
