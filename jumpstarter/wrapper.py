from jumpstarter.common.utils import env
from jumpstarter.streams.encoding import Compression
from jumpstarter_driver_network.adapters import TcpPortforwardAdapter
import time
import sys
import os
import re
import yaml
import subprocess
from pathlib import Path
from logging import getLogger

logger = getLogger(__name__)
USERNAME = os.environ.get("JETSON_USERNAME")
PASSWORD = os.environ.get("JETSON_PASSWORD")
KEY_PATH = os.environ.get("JETSON_KEY_PATH")
DISK_IMAGE_PATH = os.environ.get("DISK_IMAGE_PATH", "") # path to the disk.raw.xz image to be flashed
# Read from env OR from --bootc-switch-image=... in the pytest args forwarded via sys.argv
BOOTC_SWITCH_IMAGE = os.environ.get("BOOTC_SWITCH_IMAGE") or next(
    (a.split("=", 1)[1] for a in sys.argv if a.startswith("--bootc-switch-image=")), None
)

EXPECTED_RHEL_MAJOR = os.environ.get("EXPECTED_RHEL_MAJOR", "9") # expected rhel version
MAX_WRONG_OS_RETRIES = 3 # max number of times to try to fix the wrong OS
CI_DEFAULT_PASSWORD = "redhat" # default password for the CI, which run for time to time and reflash to different version of the image

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


def _configure_ssh_via_serial(p, username, password):
    """Enable SSH root login via an already-open serial console pexpect session.

    Expects the login: prompt to be on screen (or appearing shortly).
    Logs in, writes PermitRootLogin yes, restarts sshd, then exits.
    No-op when password is None (key-only auth — sshd already accepts keys).
    """
    if not password:
        return
    logger.info("[wrapper] Configuring SSH root login via serial console...")
    time.sleep(10)
    # Flush stale output
    try:
        while True:
            p.read_nonblocking(size=4096, timeout=1)
    except Exception:
        pass
    p.sendline("")
    try:
        p.expect_exact("login:", timeout=60)
    except Exception:
        logger.info("[wrapper] No login prompt — device may have rebooted (firstboot). Waiting...")
        if not _wait_for_login(p):
            raise RuntimeError("[wrapper] Failed to reach login prompt")
        try:
            while True:
                p.read_nonblocking(size=4096, timeout=1)
        except Exception:
            pass
        p.sendline("")
        p.expect_exact("login:", timeout=60)
    p.sendline(username)
    p.expect("assword:", timeout=30)
    p.sendline(password)
    p.expect(r"[#\$]", timeout=30)
    p.sendline(
        "echo 'PermitRootLogin yes' > /etc/ssh/sshd_config.d/01-permitrootlogin.conf"
        " && chmod 644 /etc/ssh/sshd_config.d/01-permitrootlogin.conf"
        " && systemctl restart sshd"
        " && echo WRAPPER_SSH_CONFIG_OK"
    )
    p.expect_exact("WRAPPER_SSH_CONFIG_OK", timeout=30)
    logger.info("[wrapper] SSH root login enabled and sshd restarted")
    p.sendline("exit")


def _bootc_switch_and_reboot(client, ssh_client, image):
    """Switch to a new bootc image via Jumpstarter, handling the reboot cleanly.

    Because TcpPortforwardAdapter breaks when the device reboots, the flow is:
      1. Open a temporary SSH tunnel
      2. Run `bootc switch <image>` and reboot
      3. Close the tunnel (it's dead anyway)
      4. Wait for the device to come back via serial console (serial survives reboots)
      5. Re-enable SSH root login via serial console
    After this function returns, the caller can open a fresh TcpPortforwardAdapter.
    """
    from infra_tests.ssh_client import SSHConnection

    print(f"\n[wrapper] bootc switch: switching to {image}", flush=True)

    # Step 1 & 2 — switch + reboot via temporary SSH tunnel
    with TcpPortforwardAdapter(client=ssh_client) as addr:
        with SSHConnection(addr[0], USERNAME, PASSWORD, addr[1],
                           key_filename=key_filename) as ssh:
            result = ssh.run(f"bootc switch {image}")
            if result.exit_status != 0:
                raise RuntimeError(
                    f"[wrapper] bootc switch failed:\n"
                    f"stdout: {result.stdout}\nstderr: {result.stderr}"
                )
            print(f"[wrapper] bootc switch output:\n{result.stdout}", flush=True)
            print("[wrapper] Rebooting device to apply bootc switch...", flush=True)
            try:
                ssh.sudo("reboot", fail_on_rc=False)
            except Exception:
                pass  # SSH drops when device reboots — expected

    # Step 3 — tunnel is closed; device is rebooting
    print("[wrapper] Waiting for device to come back after bootc switch reboot...", flush=True)
    time.sleep(30)  # Give device time to go down before probing

    # Step 4 & 5 — wait for device to come back and re-enable SSH.
    # The device boots from USB via the bcfg entry set earlier; USB RHEL GRUB/kernel
    # produce no serial output on Jetson, so we probe SSH directly instead of waiting
    # for a login: prompt on the serial console.
    if not _try_ssh_and_configure(ssh_client):
        # Fallback: try serial in case the device unexpectedly booted from NVMe/eMMC.
        with client.serial.pexpect() as p:
            p.logfile = sys.stdout.buffer
            if not _wait_for_login(p):
                raise RuntimeError("[wrapper] Device did not come back after bootc switch reboot")
            _configure_ssh_via_serial(p, USERNAME, PASSWORD)

    time.sleep(10)
    print("[wrapper] Device is back on new bootc image. Proceeding with test session...\n", flush=True)


def _expand_env_vars(text):
    """Expand ${VAR} patterns in text using os.environ."""
    return re.sub(r'\$\{([^}]+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), text)


def _prepull_container_images(addr, username, password, key_filename):
    """Pre-pull NGC container images listed in container_images.yaml.

    Creates a fresh SSH session for each pull through the active
    TcpPortforwardAdapter tunnel.  Performs health checks (disk space,
    memory cache drop) and rest periods between pulls to avoid
    overloading the device.
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
    rest_period = config.get("rest_between_pulls", 30)
    min_disk_mb = config.get("min_disk_space_mb", 5000)

    if not images:
        raise RuntimeError("[wrapper] No images listed in container_images.yaml")

    from infra_tests.ssh_client import SSHConnection

    pulled, cached, failed = [], [], []
    logger.info("[wrapper] Starting NGC container image pre-pull (%d images)...", len(images))

    for i, img in enumerate(images):
        url = _expand_env_vars(img["url"])
        timeout = img.get("timeout", 1800)
        required = img.get("required", True)
        size_hint = img.get("size_hint", "unknown size")

        print(f"[wrapper] Pre-pulling image {i + 1}/{len(images)}: {url} ({size_hint}, timeout={timeout}s)...", flush=True)
        logger.info("[wrapper] Pre-pulling image %d/%d: %s (%s, timeout=%ds)...",
                    i + 1, len(images), url, size_hint, timeout)

        try:
            with SSHConnection(addr[0], username, password, addr[1],
                               key_filename=key_filename) as ssh:
                # Check disk space on /var (where podman stores images on bootc)
                result = ssh.sudo("df -m /var | tail -1 | awk '{print $4}'", fail_on_rc=False)
                try:
                    avail_mb = int(result.stdout.strip())
                    logger.info("[wrapper]   Disk space: %dMB available on /var (min: %dMB)", avail_mb, min_disk_mb)
                    if avail_mb < min_disk_mb:
                        logger.warning("[wrapper]   Low disk space: %dMB < %dMB — pull may fail", avail_mb, min_disk_mb)
                except (ValueError, AttributeError):
                    logger.warning("[wrapper]   Could not parse disk space, continuing anyway")

                # Check if already cached
                check = ssh.sudo(f"podman image exists {url}", fail_on_rc=False)
                if check.exit_status == 0:
                    print(f"[wrapper]   Image already cached, skipping", flush=True)
                    logger.info("[wrapper]   Image already cached, skipping")
                    cached.append(url)
                    continue

                # Drop memory caches before pull
                logger.info("[wrapper]   Dropping memory caches...")
                ssh.sudo("sync; sync; sync", fail_on_rc=False)
                ssh.sudo("echo 3 | tee /proc/sys/vm/drop_caches", fail_on_rc=False)

                # Pull the image
                start = time.time()
                ssh.sudo(f"podman pull {url}", timeout=timeout)
                elapsed = int(time.time() - start)
                print(f"[wrapper]   Pull complete in {elapsed}s", flush=True)
                logger.info("[wrapper]   Pull complete in %ds", elapsed)
                pulled.append(url)

        except Exception as e:
            if required:
                raise RuntimeError(f"[wrapper] Required image pull failed ({url}): {e}")
            logger.warning("[wrapper]   Optional image pull failed: %s: %s", url, e)
            failed.append(url)

        # Rest between pulls (skip after the last one)
        if i < len(images) - 1:
            logger.info("[wrapper]   Resting %ds before next pull...", rest_period)
            time.sleep(rest_period)
            # SSH liveness probe
            try:
                with SSHConnection(addr[0], username, password, addr[1],
                                   key_filename=key_filename) as probe:
                    probe.run("echo alive", timeout=30)
                logger.info("[wrapper]   SSH liveness check: OK")
            except Exception:
                logger.warning("[wrapper]   SSH liveness check failed — tunnel may be degraded")

    print(f"\n[wrapper] Pre-pull complete. Pulled: {len(pulled)}, Cached: {len(cached)}, Failed: {len(failed)}", flush=True)
    logger.info("[wrapper] Pre-pull complete. Pulled: %d, Cached: %d, Failed: %d",
                len(pulled), len(cached), len(failed))


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

    # Remove ALL OS-related boot entries (Red Hat, RHEL, Bootc, Jumpstarter, shim)
    # Do NOT create any new entries — rely on hardware USB fallback
    # Filter with '^Boot[0-9]' first to exclude BootCurrent/BootOrder info lines
    remove_cmd = (
        "for num in $(efibootmgr | grep '^Boot[0-9]' "
        "| grep -iE 'Red Hat|RHEL|Bootc|Jumpstarter|shim|redhat' "
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


# Tracks which EFI filesystems have already been tried in the current boot cycle.
# Cleared at the start of each _wait_for_login() call (i.e. each power-on attempt).
# Prevents re-launching a filesystem that loaded silently but didn't produce a
# login prompt (e.g. eMMC GRUB with graphical-only output).
_efi_boot_tried: set = set()

# Set to True when _boot_from_efi_shell() successfully adds a bcfg entry and sends
# reset. The USB RHEL GRUB/kernel produce no serial output on Jetson, so the outer
# boot loop uses this flag to skip waiting for serial login and probe SSH instead.
_did_bcfg_reset: bool = False


def _boot_from_efi_shell(p):
    """Try to launch GRUB from the EFI shell by scanning filesystems for BOOTAA64.EFI.

    On Jetson, FS0 and FS1 are UEFI firmware volumes — BOOTAA64.EFI there is the
    EFI shell binary itself, so launching it just re-enters the shell. Real disk
    partitions (eMMC, USB) start at FS2. We try FS2+ first, skip filesystems that
    have already been tried in this boot cycle, verify each launch actually leaves
    the shell (i.e. Shell> does not immediately reappear), and fall back to FS0/FS1
    only if nothing else has BOOTAA64.EFI.
    """
    global _efi_boot_tried, _did_bcfg_reset
    # Prioritise real disk partitions; firmware volumes last as a last-resort probe.
    all_candidates = list(range(2, 10)) + [0, 1]
    remaining = [fs for fs in all_candidates if fs not in _efi_boot_tried]

    if not remaining:
        logger.warning("[wrapper] All EFI filesystems already tried — no boot candidates left")
        return False

    logger.info(
        "[wrapper] EFI Shell detected — candidates to try: fs%s (already tried: fs%s)",
        ", fs".join(str(f) for f in remaining),
        ", fs".join(str(f) for f in _efi_boot_tried) if _efi_boot_tried else "(none)",
    )

    for fs in remaining:
        efi_path = f"fs{fs}:\\EFI\\BOOT\\BOOTAA64.EFI"
        check_cmd = f"if exist {efi_path} then echo WRAPPER_EFI_FOUND_{fs} endif"
        p.sendline(check_cmd)
        try:
            p.expect_exact(f"WRAPPER_EFI_FOUND_{fs}", timeout=10)
        except Exception:
            continue  # file not found on this filesystem

        # Mark as tried BEFORE rebooting so the next EFI shell call skips this FS.
        _efi_boot_tried.add(fs)
        logger.info("[wrapper] Found BOOTAA64.EFI on fs%d — adding to UEFI boot order and rebooting...", fs)

        # The EFI shell prints a new Shell> prompt right after the check echo.
        # Consume it before issuing the next command.
        try:
            p.expect_exact("Shell>", timeout=5)
        except Exception:
            pass

        # Use bcfg to add the EFI binary as the FIRST boot option and reboot.
        # Launching BOOTAA64.EFI directly from EFI shell skips UEFI firmware
        # initialization (including serial console setup), so GRUB produces no
        # serial output. Adding it as a proper UEFI boot entry and using `reset`
        # lets the UEFI boot manager invoke GRUB with full firmware initialization.
        #
        # NOTE: path must NOT be quoted in bcfg; only the description string is quoted.
        add_cmd = f'bcfg boot add 0 {efi_path} "USB RHEL"'
        logger.info("[wrapper] Running: %s", add_cmd)
        p.sendline(add_cmd)
        try:
            p.expect_exact("Shell>", timeout=10)
        except Exception:
            pass

        # Dump boot entries so we can verify in the log that the add succeeded
        p.sendline("bcfg boot dump")
        try:
            p.expect_exact("Shell>", timeout=10)
        except Exception:
            pass

        logger.info("[wrapper] Sending 'reset' to reboot into USB RHEL boot entry...")
        p.sendline("reset")
        _did_bcfg_reset = True  # USB RHEL GRUB/kernel have no serial output — SSH probe needed
        return True  # device is rebooting; UEFI will boot from the new entry

    logger.warning("[wrapper] BOOTAA64.EFI not found (or all launches returned to Shell>) on remaining filesystems")
    return False


def _wait_for_login(p):
    """Wait for login: prompt, handling grub>, EFI Shell, dutlink, and emergency mode.

    Returns True if login prompt was reached, False otherwise.
    Raises RuntimeError if emergency mode password login fails.
    """
    global _efi_boot_tried
    _efi_boot_tried = set()  # fresh boot attempt — allow all filesystems again

    got_login = False
    for attempt in range(3):
        try:
            idx = p.expect_exact(["login:", "grub>", "Give root password", "Shell>"], timeout=600)
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
                logger.info(f"\n[wrapper] EFI Shell detected (attempt {attempt + 1}/3), trying to boot from USB...")
                if _boot_from_efi_shell(p):
                    # bcfg+reset sent — USB RHEL boots silently (no serial from GRUB/kernel).
                    # Stop waiting on serial and let the outer loop do an SSH probe instead.
                    logger.info("[wrapper] bcfg+reset done — exiting serial wait, will probe SSH...")
                    break
                time.sleep(5)
        except RuntimeError:
            raise  # don't swallow RuntimeError from _handle_emergency
        except Exception:
            logger.info(f"\n[wrapper] Timeout waiting for login/grub (attempt {attempt + 1}/3), sending ENTER to probe for dutlink shell...")
            p.sendline("")
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
                    logger.info("[wrapper] Got EFI Shell> after probe, trying to boot from USB...")
                    if _boot_from_efi_shell(p):
                        logger.info("[wrapper] bcfg+reset done — exiting serial wait, will probe SSH...")
                        break
                    time.sleep(5)
            except Exception:
                logger.info("[wrapper] No recognizable prompt after probe, retrying...")

    return got_login


def _try_ssh_and_configure(ssh_client, retries=12, delay=30):
    """Probe SSH after a silent USB RHEL boot (GRUB/kernel produce no serial output).

    Tries to connect via Jumpstarter's TCP tunnel and enables PermitRootLogin if
    needed.  Returns True when SSH is confirmed working.
    """
    from infra_tests.ssh_client import SSHConnection

    logger.info("[wrapper] SSH probe: up to %d attempts × %ds = %dmin", retries, delay, retries * delay // 60)
    for attempt in range(retries):
        try:
            with TcpPortforwardAdapter(client=ssh_client) as addr:
                with SSHConnection(
                    addr[0], USERNAME, PASSWORD, addr[1], key_filename=key_filename
                ) as ssh:
                    ssh.run(
                        "mkdir -p /etc/ssh/sshd_config.d"
                        " && echo 'PermitRootLogin yes'"
                        " > /etc/ssh/sshd_config.d/01-permitrootlogin.conf"
                        " && chmod 644 /etc/ssh/sshd_config.d/01-permitrootlogin.conf"
                        " && systemctl restart sshd"
                    )
                logger.info("[wrapper] SSH probe succeeded on attempt %d/%d", attempt + 1, retries)
                return True
        except Exception as exc:
            logger.info("[wrapper] SSH probe attempt %d/%d failed: %s", attempt + 1, retries, exc)
            if attempt < retries - 1:
                time.sleep(delay)

    logger.warning("[wrapper] SSH probe exhausted all %d attempts", retries)
    return False


with env() as client:

    # Flash USB storage with the target image before the first boot attempt.
    # This ensures the Jumpstarter-controlled USB always has a known-good bootable
    # image regardless of what was there before.  The boot retry loop calls
    # client.storage.dut() at the top of each attempt, so the freshly-flashed
    # USB will be connected to the DUT on the very first power-on.
    if DISK_IMAGE_PATH:
        logger.info("[wrapper] Pre-flashing USB storage with: %s", DISK_IMAGE_PATH)
        print(f"\n[wrapper] Pre-flashing USB storage — this may take a few minutes...", flush=True)
        client.storage.flash(DISK_IMAGE_PATH, compression=Compression.XZ)
        logger.info("[wrapper] USB pre-flash complete — storage ready with target image")
        print("[wrapper] USB pre-flash complete.\n", flush=True)
    else:
        logger.info("[wrapper] DISK_IMAGE_PATH not set — skipping USB pre-flash")

    # When emergency mode can't be resolved via password+exit, skip storage.dut()
    # on the next attempt so the device boots from NVMe. The wrong OS detection
    # will then fix EFI entries and re-flash, allowing a clean USB boot after.
    force_nvme_boot = False

    # Available early so the SSH probe inside the boot loop can use it.
    ssh_client = client.ssh.tcp if hasattr(client.ssh, 'tcp') else client.ssh

    for boot_attempt in range(MAX_WRONG_OS_RETRIES + 1):
        _did_bcfg_reset = False  # reset per-attempt flag
        wrong_os = False

        client.power.off()
        logger.info("[wrapper] DUT powered off")

        if force_nvme_boot:
            logger.info("[wrapper] Skipping storage.dut() — forcing NVMe boot to fix EFI entries")
            force_nvme_boot = False
        else:
            client.storage.dut()
            logger.info("[wrapper] Storage connected to DUT")

        client.power.on()
        logger.info("[wrapper] DUT powered on")

        with client.serial.pexpect() as p:
            p.logfile = sys.stdout.buffer
            time.sleep(30)

            if not _wait_for_login(p):
                # USB RHEL GRUB/kernel produce no serial output — if a bcfg+reset was
                # issued the device has likely booted silently. Try SSH probe first.
                if _did_bcfg_reset:
                    logger.info(
                        "[wrapper] Serial login timed out after bcfg+reset — probing SSH "
                        "(USB RHEL boots silently on Jetson)..."
                    )
                    print("\n[wrapper] bcfg+reset done — probing SSH (USB RHEL boots silently)...", flush=True)
                    if _try_ssh_and_configure(ssh_client):
                        print("[wrapper] SSH probe succeeded — device is up and SSH is configured.", flush=True)
                        break  # exit boot loop; ssh_client already set above
                    logger.warning("[wrapper] SSH probe failed after bcfg+reset — falling through to retry")

                # Could not reach login prompt. Possible causes:
                # - Emergency mode looping (password works but system can't boot)
                # - Timeout / grub stuck
                # _handle_emergency raises RuntimeError if password fails,
                # so this path means either emergency looping or other failure.
                # Either way: power cycle without USB → boot NVMe → EFI fix.
                logger.info(
                    f"[wrapper] Failed to reach login prompt (attempt {boot_attempt + 1}/"
                    f"{MAX_WRONG_OS_RETRIES + 1}). Will boot NVMe next to fix EFI..."
                )
                if boot_attempt >= MAX_WRONG_OS_RETRIES:
                    raise RuntimeError("[wrapper] Failed to reach login: prompt after all retries")
                force_nvme_boot = True
                continue

            # Check if device booted into the wrong OS (e.g., RHEL 10 from NVMe)
            wrong_os, detected_version = _detect_wrong_os(p.before)

            if wrong_os:
                if boot_attempt >= MAX_WRONG_OS_RETRIES:
                    raise RuntimeError(
                        f"[wrapper] Device keeps booting wrong OS (RHEL {detected_version}) "
                        f"after {MAX_WRONG_OS_RETRIES} EFI fix attempts. "
                        f"Expected RHEL {EXPECTED_RHEL_MAJOR}."
                    )
                logger.info(
                    f"[wrapper] Wrong OS detected: RHEL {detected_version} "
                    f"(expected RHEL {EXPECTED_RHEL_MAJOR}). "
                    f"Fixing EFI boot entries (attempt {boot_attempt + 1}/{MAX_WRONG_OS_RETRIES})..."
                )
                _fix_efi_via_serial(p)
                # exits serial context, then re-flash below before retrying

            else:
                # Correct OS — proceed with SSH configuration
                logger.info("[wrapper] Successfully showing login prompt via console")

                _configure_ssh_via_serial(p, USERNAME, PASSWORD)

                break  # correct OS booted and SSH configured

        # If wrong OS was detected, re-flash before retrying boot
        if wrong_os:
            if DISK_IMAGE_PATH:
                logger.info(f"[wrapper] Re-flashing image: {DISK_IMAGE_PATH}")
                client.storage.flash(DISK_IMAGE_PATH, compression=Compression.XZ)
                logger.info("[wrapper] Re-flash complete")
            else:
                logger.warning(
                    "[wrapper] DISK_IMAGE_PATH not set — skipping re-flash. "
                    "Set DISK_IMAGE_PATH to the .raw.xz image path for automatic re-flash."
                )
            continue  # retry boot
    else:
        raise RuntimeError(
            f"[wrapper] Failed to boot correct OS (RHEL {EXPECTED_RHEL_MAJOR}) "
            f"after {MAX_WRONG_OS_RETRIES + 1} attempts"
        )

    # Wait for SSH service to be fully ready after sshd restart
    logger.info("[wrapper] Waiting for SSH service to start...")
    time.sleep(10)

    # If a new bootc image was requested, switch + reboot now — before the main
    # tunnel opens, because TcpPortforwardAdapter breaks on device reboot.
    if BOOTC_SWITCH_IMAGE:
        _bootc_switch_and_reboot(client, ssh_client, BOOTC_SWITCH_IMAGE)

    with TcpPortforwardAdapter(client=ssh_client) as addr:
        os.environ["JETSON_HOST"] = addr[0]
        os.environ["JETSON_PORT"] = str(addr[1])
        os.environ["JUMPSTARTER_IN_USE"] = "1"

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
            ssh.sudo("/usr/libexec/bootc-generic-growpart", timeout=120, fail_on_rc=False)

        os.environ.setdefault("L4T_JETPACK_IMAGE", "nvcr.io/nvidia/l4t-jetpack:r36.4.0")
        print("\n" + "=" * 80)
        print("[wrapper] Pre-pulling container images — this may take a while...")
        print("[wrapper] DO NOT force-exit the wrapper while images are being pulled.")
        print("[wrapper] To check progress, open another terminal and run:")
        print("[wrapper] 'jmp shell --lease <LEASE> -- j serial start-console' and then 'pgrep -fa podman'")
        print("=" * 80 + "\n", flush=True)
        _prepull_container_images(addr, USERNAME, PASSWORD, key_filename)

        logger.info(f"[wrapper] Launching pytest with JETSON_HOST={os.environ['JETSON_HOST']} "
              f"JETSON_PORT={os.environ['JETSON_PORT']} "
              f"JETSON_USERNAME={os.environ.get('JETSON_USERNAME')} "
              f"JETSON_KEY_PATH={os.environ.get('JETSON_KEY_PATH', '(not set)')}")
        subprocess.run(sys.argv[1:])
