"""Run Jetson tests through a Jumpstarter lease.

The module keeps hardware-specific operations behind small classes.  That
makes the boot state machine readable and leaves the third-party Jumpstarter
objects at the edges of the program.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import contextlib
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Iterator, Optional, Protocol, Sequence, Tuple

import yaml
from pexpect import EOF as PexpectEof
from pexpect import TIMEOUT as PexpectTimeout


class SerialSession(Protocol):
    """Operations used from a Jumpstarter pexpect session."""

    before: Any
    logfile: Any

    def send(self, data: str) -> Any: ...

    def sendline(self, data: str = "") -> Any: ...

    def expect(self, patterns: Any, timeout: Optional[float] = None) -> int: ...

    def expect_exact(self, patterns: Any, timeout: Optional[float] = None) -> int: ...

    def read_nonblocking(self, size: int, timeout: float) -> Any: ...


class PowerController(Protocol):
    def on(self) -> Any: ...
    def off(self) -> Any: ...


class StorageController(Protocol):
    def dut(self) -> Any: ...
    def flash(self, image_path: str, compression: Any) -> Any: ...


class SerialController(Protocol):
    def pexpect(self) -> contextlib.AbstractContextManager[SerialSession]: ...


class ExporterClient(Protocol):
    power: PowerController
    storage: StorageController
    serial: SerialController
    ssh: Any


class SerialStreamDead(RuntimeError):
    """Raised when the serial transport is unusable and must be reopened."""


@contextlib.contextmanager
def suspend_serial_logging(console: SerialSession) -> Iterator[None]:
    """Prevent credentials sent to a serial session from reaching its log."""
    previous_log = console.logfile
    console.logfile = None
    try:
        yield
    finally:
        console.logfile = previous_log


@dataclass(frozen=True)
class WrapperSettings:
    """Runtime configuration read from environment variables."""

    username: str
    password: Optional[str]
    key_path: Optional[str]
    expected_rhel_major: str
    expected_rhel_version: Optional[str]
    expected_bootc_image: Optional[str]
    disk_image_path: Optional[str]
    login_timeout: int
    liveness_chunk_seconds: int
    max_serial_reconnects: int
    serial_reconnect_delay: int
    boot_deadline: int
    uefi_boot_timeout: int
    max_boot_screen_steps: int
    max_wrong_os_retries: int = 3

    @classmethod
    def from_environment(cls) -> "WrapperSettings":
        username = os.environ.get("JETSON_USERNAME")
        password = os.environ.get("JETSON_PASSWORD")
        key_path = os.environ.get("JETSON_KEY_PATH")
        if not username:
            raise ValueError("JETSON_USERNAME must be set when using Jumpstarter")
        if not password and not key_path:
            raise ValueError("JETSON_PASSWORD or JETSON_KEY_PATH must be set")

        expanded_key = os.path.expanduser(key_path) if key_path else None
        if expanded_key and not os.path.isfile(expanded_key):
            raise ValueError(f"SSH key file not found: {expanded_key}")

        return cls(
            username=username,
            password=password,
            key_path=expanded_key,
            expected_rhel_major=os.environ.get("EXPECTED_RHEL_MAJOR", "9"),
            expected_rhel_version=os.environ.get("EXPECTED_RHEL_VERSION") or None,
            expected_bootc_image=os.environ.get("EXPECTED_BOOTC_IMAGE") or None,
            disk_image_path=os.environ.get("DISK_IMAGE_PATH") or None,
            login_timeout=environment_int("WRAPPER_LOGIN_TIMEOUT", 300),
            liveness_chunk_seconds=environment_int("WRAPPER_LIVENESS_CHUNK", 60),
            max_serial_reconnects=environment_int("WRAPPER_MAX_SERIAL_RECONNECTS", 3),
            serial_reconnect_delay=environment_int(
                "WRAPPER_SERIAL_RECONNECT_DELAY", 15
            ),
            boot_deadline=environment_int("WRAPPER_BOOT_DEADLINE", 1800),
            uefi_boot_timeout=environment_int("WRAPPER_UEFI_BOOT_TIMEOUT", 240),
            max_boot_screen_steps=environment_int("WRAPPER_MAX_BOOT_SCREEN_STEPS", 6),
        )


@dataclass(frozen=True)
class BootStatus:
    """Identity reported by the operating system after serial login."""

    rhel_major: Optional[str]
    rhel_version: Optional[str]
    bootc_status: str
    raw_output: str

    def mismatch_reason(self, settings: WrapperSettings) -> Optional[str]:
        if self.rhel_major is None:
            return "the logged-in system did not report its RHEL major version"
        if self.rhel_major != settings.expected_rhel_major:
            return (
                f"logged-in system is RHEL {self.rhel_major}; "
                f"expected RHEL {settings.expected_rhel_major}"
            )
        expected_version = settings.expected_rhel_version
        if expected_version and self.rhel_version != expected_version:
            return (
                f"logged-in system is RHEL {self.rhel_version or 'unknown'}; "
                f"expected exactly RHEL {expected_version}"
            )
        expected_image = settings.expected_bootc_image
        if expected_image and expected_image.lower() not in self.bootc_status.lower():
            return f"bootc status does not contain expected image {expected_image!r}"
        return None


def environment_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer, got {value!r}") from error


class ConsoleLog:
    """Mirror serial bytes to the terminal, a file, and a bounded memory tail."""

    def __init__(self, path: Path, tail_size: int = 262_144) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("wb")
        self._tail: Deque[int] = collections.deque(maxlen=tail_size)

    def write(self, data: Any) -> None:
        raw = (
            data.encode("utf-8", errors="replace")
            if isinstance(data, str)
            else bytes(data)
        )
        sys.stdout.buffer.write(raw)
        self._file.write(raw)
        self._tail.extend(raw)

    def flush(self) -> None:
        sys.stdout.buffer.flush()
        self._file.flush()

    def text_tail(self) -> str:
        return bytes(self._tail).decode("utf-8", errors="replace")

    def clear(self) -> None:
        self._tail.clear()

    def close(self) -> None:
        self.flush()
        self._file.close()


class RunLogger:
    """Provide consistent console and file logging for one wrapper run."""

    def __init__(self, log_directory: Path) -> None:
        log_directory.mkdir(parents=True, exist_ok=True)
        self.serial_log = log_directory / "serial-console.log"
        self._logger = logging.getLogger("jumpstarter.wrapper")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.handlers.clear()
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"
        )
        for handler in (
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_directory / "wrapper.log", mode="w"),
        ):
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)

    @property
    def value(self) -> logging.Logger:
        return self._logger

    def phase(self, name: str) -> None:
        self._logger.info("")
        self._logger.info("=" * 72)
        self._logger.info("PHASE: %s", name)
        self._logger.info("=" * 72)


@contextlib.contextmanager
def hard_timeout(seconds: float, message: str) -> Iterator[None]:
    """Interrupt a stuck serial syscall when running on the main thread."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def interrupt(_: int, __: Any) -> None:
        raise SerialStreamDead(message)

    previous_handler = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def exception_contains_serial_failure(error: BaseException) -> Optional[BaseException]:
    if isinstance(error, SerialStreamDead):
        return error
    for nested in getattr(error, "exceptions", ()) or ():
        found = exception_contains_serial_failure(nested)
        if found:
            return found
    return None


def unwrap_single_exception(error: BaseException) -> BaseException:
    """Return the useful error nested in single-item task-group wrappers."""
    current = error
    while True:
        nested = getattr(current, "exceptions", ()) or ()
        if len(nested) != 1:
            return current
        current = nested[0]


def is_dead_transport_error(error: BaseException) -> bool:
    names = {
        "BrokenResourceError",
        "ClosedResourceError",
        "EndOfStream",
        "ConnectionResetError",
        "BrokenPipeError",
    }
    return type(error).__name__ in names or any(name in str(error) for name in names)


class SerialReader:
    """Read a console in bounded chunks and reconnect on dead transports."""

    def __init__(self, settings: WrapperSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def probe(self, console: SerialSession) -> bytes:
        try:
            with hard_timeout(20, "serial console write blocked"):
                console.sendline("")
            with hard_timeout(20, "serial console read blocked"):
                result = console.read_nonblocking(8192, timeout=5)
            return result if isinstance(result, bytes) else str(result).encode()
        except PexpectTimeout:
            return b""
        except PexpectEof as error:
            raise SerialStreamDead("serial console reached EOF") from error
        except SerialStreamDead:
            raise
        except Exception as error:
            if is_dead_transport_error(error):
                raise SerialStreamDead(f"serial probe failed: {error}") from error
            raise

    def expect(
        self, console: SerialSession, patterns: Sequence[str], timeout: int, label: str
    ) -> int:
        deadline = time.monotonic() + timeout
        silent_probes = 0
        self.logger.info(
            "waiting up to %ds for %s; sending ENTER every %ds",
            timeout,
            label,
            self.settings.liveness_chunk_seconds,
        )
        while time.monotonic() < deadline:
            chunk = min(
                self.settings.liveness_chunk_seconds, int(deadline - time.monotonic())
            )
            if chunk <= 0:
                break
            try:
                with hard_timeout(
                    chunk + 15, f"serial stream stopped while waiting for {label}"
                ):
                    return console.expect_exact(patterns, timeout=chunk)
            except PexpectTimeout:
                data = self.probe(console)
                if data:
                    silent_probes = 0
                    text = data.decode("utf-8", errors="replace")
                    for index, pattern in enumerate(patterns):
                        if pattern in text:
                            return index
                else:
                    silent_probes += 1
                    self.logger.info(
                        "console is quiet while waiting for %s; ENTER probe %d sent",
                        label,
                        silent_probes,
                    )
            except PexpectEof as error:
                raise SerialStreamDead(
                    f"console reached EOF while waiting for {label}"
                ) from error
            except SerialStreamDead:
                raise
            except Exception as error:
                if is_dead_transport_error(error):
                    raise SerialStreamDead(
                        f"console failed while waiting for {label}: {error}"
                    ) from error
                raise
        raise PexpectTimeout(f"timed out waiting for {label}")


class UefiShell:
    """Run commands and repair the boot path from an EDK2 UEFI shell."""

    PROMPTS = ("Shell>", r":\>")
    FILESYSTEMS = tuple(f"FS{index}" for index in range(8))
    LOADER_LOCATIONS = (
        (r"\EFI\redhat", ("shimaa64.efi", "grubaa64.efi")),
        (r"\EFI\BOOT", ("BOOTAA64.EFI",)),
    )

    def __init__(
        self, console: SerialSession, settings: WrapperSettings, logger: logging.Logger
    ) -> None:
        self.console = console
        self.settings = settings
        self.logger = logger

    def send(self, command: str) -> None:
        self.console.send(command + "\r")

    def wait_prompt(self, timeout: int = 20) -> bool:
        try:
            self.console.expect_exact(self.PROMPTS, timeout=timeout)
            return True
        except SerialStreamDead:
            raise
        except Exception:
            return False

    def drain(self) -> None:
        for _ in range(20):
            try:
                self.console.expect_exact(self.PROMPTS, timeout=0)
            except Exception:
                return

    def run(self, command: str, timeout: int = 20) -> Optional[str]:
        self.drain()
        self.send(command)
        if not self.wait_prompt(timeout):
            return None
        output = self.console.before
        return (
            output.decode("utf-8", errors="replace")
            if isinstance(output, bytes)
            else str(output)
        )

    def find_loader(self) -> Optional[str]:
        self.run("map -r", timeout=30)
        for filesystem in self.FILESYSTEMS:
            for directory, loader_names in self.LOADER_LOCATIONS:
                listing = self.run(f"ls {filesystem}:{directory}", timeout=15)
                if not listing:
                    continue
                for loader_name in loader_names:
                    if loader_name.lower() in listing.lower():
                        return f"{filesystem}:{directory}\\{loader_name}"
        return None

    def boot(self) -> bool:
        self.drain()
        self.send("")
        if not self.wait_prompt():
            self.send("")
            if not self.wait_prompt():
                return False
        loader = self.find_loader()
        if not loader:
            self.logger.warning("UEFI shell has no RHEL loader")
            return False
        existing_entries = self.run("bcfg boot dump", timeout=30) or ""
        if "WRAPPER-RHEL" not in existing_entries:
            self.run(f'bcfg boot add 0 {loader} "WRAPPER-RHEL"', timeout=30)
        self.send(loader)
        try:
            result = self.console.expect_exact(
                ("login:", "Give root password", "Use the ^ and v keys") + self.PROMPTS,
                timeout=self.settings.uefi_boot_timeout,
            )
        except Exception:
            result = -1
        if result == 0:
            return True
        if result == 1:
            return EmergencyRecovery(
                self.console, self.logger, self.settings.password
            ).recover()
        if result == 2:
            self.console.sendline("")
        else:
            self.send("reset")
        return wait_for_login_after_boot(self.console, self.settings, self.logger)


class EmergencyRecovery:
    """Leave emergency mode and repair the fstab entry that caused it."""

    def __init__(
        self,
        console: SerialSession,
        logger: logging.Logger,
        password: Optional[str],
    ) -> None:
        self.console = console
        self.logger = logger
        self.password = password

    def recover(self) -> bool:
        if not self.password:
            raise RuntimeError("emergency recovery requires JETSON_PASSWORD")
        for _ in range(3):
            logged_in = False
            with suspend_serial_logging(self.console):
                self.console.sendline(self.password)
                try:
                    if (
                        self.console.expect(
                            (r"[#\$]", "Login incorrect", "Give root password"),
                            timeout=15,
                        )
                        == 0
                    ):
                        logged_in = True
                except Exception:
                    pass
            if not logged_in:
                raise RuntimeError("emergency mode login failed")
            self.console.sendline("dmesg -n 1")
            self.console.sendline("sed -i '/boot\\/efi/s/^/#/' /etc/fstab")
            self.console.sendline("exit")
            try:
                result = self.console.expect_exact(
                    ("login:", "Give root password"), timeout=120
                )
            except Exception:
                return False
            if result == 0:
                return True
        return False


def wait_for_login_after_boot(
    console: SerialSession, settings: WrapperSettings, logger: logging.Logger
) -> bool:
    for _ in range(2):
        try:
            result = console.expect_exact(
                ("login:", "Give root password", "Use the ^ and v keys"),
                timeout=settings.uefi_boot_timeout,
            )
        except Exception:
            return False
        if result == 0:
            return True
        if result == 1:
            return EmergencyRecovery(console, logger, settings.password).recover()
        console.sendline("")
    return False


def detect_wrong_os(output: str, expected_major: str) -> Tuple[bool, Optional[str]]:
    for pattern in (r"Enterprise Linux (\d+)", r"\.el(\d+)"):
        match = re.search(pattern, output)
        if match and match.group(1) != expected_major:
            return True, match.group(1)
    return False, None


def parse_boot_status(output: str) -> BootStatus:
    """Parse the marked output produced by the serial status command."""
    major_match = re.search(r"WRAPPER_STATUS_OS_MAJOR=([^\r\n ]+)", output)
    version_match = re.search(r"WRAPPER_STATUS_OS_VERSION=([^\r\n ]+)", output)
    bootc_match = re.search(
        r"WRAPPER_STATUS_BOOTC_BEGIN\s*(.*?)\s*WRAPPER_STATUS_END",
        output,
        flags=re.DOTALL,
    )
    return BootStatus(
        rhel_major=major_match.group(1) if major_match else None,
        rhel_version=version_match.group(1) if version_match else None,
        bootc_status=bootc_match.group(1).strip() if bootc_match else "",
        raw_output=output,
    )


def boot_screen_step(
    console: SerialSession, settings: WrapperSettings, logger: logging.Logger
) -> bool:
    patterns = (
        "login:",
        "Give root password",
        "Use the ^ and v keys",
        "Enter to continue boot.",
    )
    for step in range(settings.max_boot_screen_steps):
        try:
            result = console.expect_exact(patterns, timeout=settings.uefi_boot_timeout)
        except Exception:
            return False
        if result == 0:
            return True
        if result == 1:
            return EmergencyRecovery(console, logger, settings.password).recover()
        console.sendline("")
        console.send("\r")
        logger.info(
            "boot screen %d/%d advanced", step + 1, settings.max_boot_screen_steps
        )
    return False


class BootCoordinator:
    """Power-cycle, recover, and validate the expected operating system."""

    def __init__(
        self,
        client: ExporterClient,
        settings: WrapperSettings,
        logger: logging.Logger,
        console_log: ConsoleLog,
    ) -> None:
        self.client = client
        self.settings = settings
        self.logger = logger
        self.console_log = console_log
        self.serial_reader = SerialReader(settings, logger)
        self._boot_deadline: Optional[float] = None

    def remaining_boot_time(self) -> int:
        """Return seconds left before the complete boot operation must stop."""
        if self._boot_deadline is None:
            return self.settings.boot_deadline
        remaining = int(self._boot_deadline - time.monotonic())
        if remaining <= 0:
            raise RuntimeError(
                f"device did not become usable within {self.settings.boot_deadline}s"
            )
        return remaining

    def wait_for_login(self, console: SerialSession) -> bool:
        patterns = (
            "login:",
            "grub>",
            "Give root password",
            "Shell>",
            "Use the ^ and v keys",
            "Enter to continue boot.",
        )
        # Wake a device that is already sitting at a quiet Linux, GRUB, or
        # firmware prompt. EDK2 needs CR while the Linux console accepts LF.
        console.sendline("")
        console.send("\r")
        for _ in range(3):
            try:
                timeout = min(
                    self.settings.login_timeout,
                    self.remaining_boot_time(),
                )
                result = self.serial_reader.expect(
                    console, patterns, timeout, "login prompt"
                )
                if result == 0:
                    return True
                if result == 1:
                    console.sendline("exit")
                    time.sleep(10)
                elif result == 2:
                    if EmergencyRecovery(
                        console, self.logger, self.settings.password
                    ).recover():
                        return True
                elif result == 3:
                    if UefiShell(console, self.settings, self.logger).boot():
                        return True
                else:
                    console.sendline("")
                    console.send("\r")
                    if boot_screen_step(console, self.settings, self.logger):
                        return True
            except SerialStreamDead:
                raise
            except PexpectTimeout:
                self.logger.warning("login prompt did not appear; sending ENTER again")
                console.sendline("")
                console.send("\r")
        return False

    def login_and_read_status(self, console: SerialSession) -> BootStatus:
        """Log in at the matched prompt and query the running system directly."""
        if not self.settings.password:
            raise RuntimeError("serial status inspection requires JETSON_PASSWORD")
        with suspend_serial_logging(console):
            console.sendline(self.settings.username)
            console.expect("assword:", timeout=30)
            console.sendline(self.settings.password)
            console.expect(r"[#\$]", timeout=30)
        console.sendline(
            "CONSOLE_MARK=WRAPPER_CONSOLE; dmesg -n 1; echo ${CONSOLE_MARK}_QUIET"
        )
        console.expect_exact("WRAPPER_CONSOLE_QUIET", timeout=15)
        status_command = (
            "WRAPPER_MARK=WRAPPER_STATUS; "
            ". /etc/os-release; "
            "echo ${WRAPPER_MARK}_OS_MAJOR=${VERSION_ID%%.*}; "
            "echo ${WRAPPER_MARK}_OS_VERSION=${VERSION_ID}; "
            "echo ${WRAPPER_MARK}_BOOTC_BEGIN; "
            "bootc status 2>&1 || true; "
            "echo ${WRAPPER_MARK}_END"
        )
        console.sendline(status_command)
        console.expect_exact("WRAPPER_STATUS_END", timeout=60)
        output = console.before
        text = (
            output.decode("utf-8", errors="replace")
            if isinstance(output, bytes)
            else str(output)
        )
        status = parse_boot_status(text + "\nWRAPPER_STATUS_END")
        self.logger.info(
            "logged-in system reports RHEL %s",
            status.rhel_version or status.rhel_major or "unknown",
        )
        if status.bootc_status:
            self.logger.info("bootc status:\n%s", status.bootc_status)
        return status

    def configure_ssh(self, console: SerialSession) -> None:
        """Enable the SSH authentication needed by the forwarded test session."""
        command = (
            "SSH_MARK=WRAPPER_SSH; "
            "echo 'PermitRootLogin yes' > /etc/ssh/sshd_config.d/01-permitrootlogin.conf && "
            "echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config.d/01-permitrootlogin.conf && "
            "chmod 644 /etc/ssh/sshd_config.d/01-permitrootlogin.conf && "
            "systemctl restart sshd && echo ${SSH_MARK}_OK"
        )
        console.sendline(command)
        console.expect_exact("WRAPPER_SSH_OK", timeout=30)

    def run(self) -> None:
        reflash_attempted = False
        self._boot_deadline = time.monotonic() + self.settings.boot_deadline
        for attempt in range(self.settings.max_wrong_os_retries + 1):
            self.remaining_boot_time()
            self.logger.info("boot attempt %d", attempt + 1)
            self.client.power.off()
            self.client.power.off()
            self.client.storage.dut()
            self.console_log.clear()
            self.client.power.on()
            got_login = False
            wrong_os = False
            for reconnect in range(self.settings.max_serial_reconnects + 1):
                self.remaining_boot_time()
                try:
                    with self.client.serial.pexpect() as console:
                        console.logfile = self.console_log
                        time.sleep(30 if reconnect == 0 else 0)
                        got_login = self.wait_for_login(console)
                        if not got_login:
                            break
                        if self.settings.password:
                            status = self.login_and_read_status(console)
                            mismatch = status.mismatch_reason(self.settings)
                            wrong_os = mismatch is not None
                            if mismatch:
                                self.logger.warning("wrong boot detected: %s", mismatch)
                            else:
                                self.configure_ssh(console)
                            console.sendline("exit")
                        else:
                            observed = self.console_log.text_tail()
                            wrong_os, version = detect_wrong_os(
                                observed, self.settings.expected_rhel_major
                            )
                            if wrong_os:
                                self.logger.warning(
                                    "boot output reports RHEL %s; expected RHEL %s",
                                    version,
                                    self.settings.expected_rhel_major,
                                )
                    break
                except BaseException as error:
                    dead_error = exception_contains_serial_failure(error)
                    if dead_error is None:
                        raise
                    if reconnect >= self.settings.max_serial_reconnects:
                        raise SerialStreamDead(
                            "serial transport remained unusable after "
                            f"{self.settings.max_serial_reconnects} reconnects: "
                            f"{dead_error}"
                        ) from dead_error
                    self.logger.warning(
                        "serial transport failed; reconnecting: %s", dead_error
                    )
                    time.sleep(self.settings.serial_reconnect_delay)
            if got_login and not wrong_os:
                return
            if wrong_os:
                if not self.settings.disk_image_path:
                    raise RuntimeError(
                        "the logged-in system is not the expected image and "
                        "DISK_IMAGE_PATH is not set; refusing to reboot the same "
                        "image repeatedly"
                    )
                if reflash_attempted:
                    raise RuntimeError(
                        "the logged-in system is still not the expected image after "
                        "one automatic reflash; refusing to enter a reflash loop"
                    )
                from jumpstarter.streams.encoding import Compression

                self.logger.info("reflashing %s", self.settings.disk_image_path)
                self.client.storage.flash(
                    self.settings.disk_image_path, compression=Compression.XZ
                )
                reflash_attempted = True
            if attempt == self.settings.max_wrong_os_retries:
                raise RuntimeError("failed to boot the expected RHEL image")
        raise RuntimeError("boot coordinator ended without a usable device")


class ImagePreparer:
    """Pull configured container images over the forwarded SSH connection."""

    def __init__(self, config_path: Path, logger: logging.Logger) -> None:
        self.config_path = config_path
        self.logger = logger

    def prepare(self, address: Tuple[str, int], settings: WrapperSettings) -> None:
        if os.environ.get("SKIP_PREPULL", "").lower() in {"1", "true", "yes"}:
            return
        if not self.config_path.exists():
            self.logger.warning("image configuration is missing: %s", self.config_path)
            return
        config = yaml.safe_load(self.config_path.read_text()) or {}
        images = config.get("images", [])
        if not images:
            raise RuntimeError("image configuration contains no images")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(images)) as executor:
            futures = [
                executor.submit(
                    self._pull, address, settings, image, index, len(images)
                )
                for index, image in enumerate(images, 1)
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()

    def _pull(
        self,
        address: Tuple[str, int],
        settings: WrapperSettings,
        image: dict,
        index: int,
        total: int,
    ) -> None:
        from infra_tests.ssh_client import SSHConnection

        image_url = re.sub(
            r"\$\{([^}]+)\}",
            lambda match: os.environ.get(match.group(1), match.group(0)),
            image["url"],
        )
        timeout = int(image.get("timeout", 1800))
        required = bool(image.get("required", True))
        try:
            with SSHConnection(
                address[0],
                settings.username,
                settings.password,
                address[1],
                key_filename=settings.key_path,
            ) as ssh:
                if (
                    ssh.sudo(
                        f"podman image exists {shlex.quote(image_url)}",
                        fail_on_rc=False,
                    ).exit_status
                    == 0
                ):
                    self.logger.info(
                        "[%d/%d] image cached: %s", index, total, image_url
                    )
                    return
                ssh.sudo("sync; sync; sync", fail_on_rc=False)
                ssh.sudo("echo 3 | tee /proc/sys/vm/drop_caches", fail_on_rc=False)
                ssh.sudo(f"podman pull {shlex.quote(image_url)}", timeout=timeout)
                self.logger.info("[%d/%d] image pulled: %s", index, total, image_url)
        except Exception as error:
            if required:
                raise RuntimeError(
                    f"required image pull failed: {image_url}"
                ) from error
            self.logger.warning("optional image pull failed: %s: %s", image_url, error)


class JumpstarterRunner:
    """Own the complete setup, tunnel, and pytest lifecycle."""

    def __init__(self, settings: WrapperSettings, logger: RunLogger) -> None:
        self.settings = settings
        self.run_logger = logger
        self.log = logger.value
        self.started_at = time.monotonic()

    def run(self, test_command: Sequence[str]) -> int:
        from jumpstarter.common.utils import env
        from jumpstarter_driver_network.adapters import TcpPortforwardAdapter

        project_root = Path(__file__).resolve().parent.parent
        project_root_text = str(project_root)
        if project_root_text not in sys.path:
            sys.path.insert(0, project_root_text)

        with env() as client:
            self._validate_client(client)
            console_log = ConsoleLog(self.run_logger.serial_log)
            try:
                BootCoordinator(client, self.settings, self.log, console_log).run()
                time.sleep(10)
                ssh_client = getattr(client.ssh, "tcp", client.ssh)
                try:
                    with TcpPortforwardAdapter(client=ssh_client) as address:
                        os.environ["JETSON_HOST"] = address[0]
                        os.environ["JETSON_PORT"] = str(address[1])
                        os.environ["JUMPSTARTER_IN_USE"] = "1"
                        self._grow_partition(address)
                        os.environ.setdefault(
                            "L4T_JETPACK_IMAGE",
                            "nvcr.io/nvidia/l4t-jetpack:r36.4.0",
                        )
                        ImagePreparer(
                            Path(__file__).with_name("container_images.yaml"), self.log
                        ).prepare(address, self.settings)
                        return subprocess.run(
                            list(test_command), check=False
                        ).returncode
                except BaseException as error:
                    useful_error = unwrap_single_exception(error)
                    if useful_error is error:
                        raise
                    raise useful_error from None
            finally:
                console_log.close()

    def _validate_client(self, client: ExporterClient) -> None:
        missing = [
            name
            for name in ("power", "storage", "serial", "ssh")
            if getattr(client, name, None) is None
        ]
        if missing:
            raise RuntimeError(
                f"exporter is missing required drivers: {', '.join(missing)}"
            )

    def _grow_partition(self, address: Tuple[str, int]) -> None:
        from infra_tests.ssh_client import SSHConnection

        with SSHConnection(
            address[0],
            self.settings.username,
            self.settings.password,
            address[1],
            key_filename=self.settings.key_path,
        ) as ssh:
            ssh.sudo("/usr/libexec/bootc-generic-growpart")


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", nargs=argparse.REMAINDER, help="pytest command and arguments"
    )
    arguments = parser.parse_args(argv)
    if not arguments.command:
        parser.error("provide a command, for example: pytest tests_suites/")
    return arguments


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = parse_arguments(argv if argv is not None else sys.argv[1:])
    settings = WrapperSettings.from_environment()
    log_directory = Path(os.environ.get("WRAPPER_LOG_DIR", "wrapper_logs")).resolve()
    run_logger = RunLogger(log_directory)
    run_logger.value.info(
        "Jumpstarter wrapper started at %s", datetime.now(timezone.utc).isoformat()
    )
    run_logger.value.info(
        "expected RHEL major=%s exact version=%s expected bootc image=%s",
        settings.expected_rhel_major,
        settings.expected_rhel_version or "not specified",
        settings.expected_bootc_image or "not specified",
    )
    return JumpstarterRunner(settings, run_logger).run(arguments.command)


if __name__ == "__main__":
    raise SystemExit(main())
