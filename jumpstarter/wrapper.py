from jumpstarter.common.utils import env
from jumpstarter.streams.encoding import Compression
from jumpstarter_driver_network.adapters import TcpPortforwardAdapter
import time
import sys
import os
import re
import subprocess
from pathlib import Path
from logging import getLogger

logger = getLogger(__name__)
USERNAME = os.environ.get("JETSON_USERNAME")
PASSWORD = os.environ.get("JETSON_PASSWORD")
KEY_PATH = os.environ.get("JETSON_KEY_PATH")
DISK_IMAGE_PATH = os.environ.get("DISK_IMAGE_PATH", "") # path to the disk.raw.xz image to be flashed

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


def _wait_for_login(p):
    """Wait for login: prompt, handling grub>, dutlink, and emergency mode recovery.

    Returns True if login prompt was reached, False otherwise.
    Raises RuntimeError if emergency mode password login fails.
    """
    got_login = False
    for attempt in range(3):
        try:
            idx = p.expect_exact(["login:", "grub>", "Give root password"], timeout=600)
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
        except RuntimeError:
            raise  # don't swallow RuntimeError from _handle_emergency
        except Exception:
            logger.info(f"\n[wrapper] Timeout waiting for login/grub (attempt {attempt + 1}/3), sending ENTER to probe for dutlink shell...")
            p.sendline("")
            try:
                idx = p.expect_exact(["#>", "login:", "grub>"], timeout=30)
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
            except Exception:
                logger.info("[wrapper] No recognizable prompt after probe, retrying...")

    return got_login


with env() as client:
    with client.log_stream():

        # When emergency mode can't be resolved via password+exit, skip storage.dut()
        # on the next attempt so the device boots from NVMe. The wrong OS detection
        # will then fix EFI entries and re-flash, allowing a clean USB boot after.
        force_nvme_boot = False

        for boot_attempt in range(MAX_WRONG_OS_RETRIES + 1):
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

                    if PASSWORD:
                        logger.info("[wrapper] Configuring SSH root password login via serial console...")
                        time.sleep(5)
                        # Flush any stale login prompts from buffer
                        try:
                            while True:
                                p.read_nonblocking(size=4096, timeout=1)
                        except Exception:
                            pass
                        # Send Enter to get a single clean login prompt
                        p.sendline("")
                        p.expect_exact("login:", timeout=30)
                        p.sendline(USERNAME)
                        p.expect("assword:", timeout=30)
                        p.sendline(PASSWORD)
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

        ssh_client = client.ssh.tcp if hasattr(client.ssh, 'tcp') else client.ssh
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
                ssh.sudo("/usr/libexec/bootc-generic-growpart")

            logger.info(f"[wrapper] Launching pytest with JETSON_HOST={os.environ['JETSON_HOST']} "
                  f"JETSON_PORT={os.environ['JETSON_PORT']} "
                  f"JETSON_USERNAME={os.environ.get('JETSON_USERNAME')} "
                  f"JETSON_KEY_PATH={os.environ.get('JETSON_KEY_PATH', '(not set)')}")
            subprocess.run(sys.argv[1:])
