"""Typed SSH helpers used by the Jetson tests.

Fabric's native result exposes the return code as ``exited``.  The test suite
uses the clearer name ``exit_status``.  This module makes that translation with
a named dataclass instead of creating an anonymous type at runtime.
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, cast

import paramiko
from fabric import Config, Connection
from invoke.runners import Result as InvokeResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    """Stable result returned by :class:`SSHConnection`."""

    stdout: str
    stderr: str
    exit_status: int
    ok: bool

    @property
    def failed(self) -> bool:
        """Whether the remote command returned a non-zero status."""
        return not self.ok


class SSHConnection(Connection):
    """Fabric connection with explicit authentication and test-friendly results."""

    _MAX_CONNECT_ATTEMPTS = 3
    _RETRY_DELAY_SECONDS = 2
    _MUTATING_DNF_COMMANDS = frozenset(
        {
            "install",
            "remove",
            "update",
            "upgrade",
            "dist-sync",
            "group",
            "config-manager",
        }
    )

    def __init__(
        self,
        hostname: str,
        username: str,
        password: Optional[str],
        port: int = 22,
        timeout: int = 30,
        key_filename: Optional[str] = None,
    ) -> None:
        self._validate_credentials(password, key_filename)
        logger.info(
            "SSH connection: host=%s port=%s timeout=%ss auth=%s",
            hostname,
            port,
            timeout,
            self._authentication_description(password, key_filename),
        )

        connected_socket = self._open_socket(hostname, port, timeout)
        connect_kwargs: Dict[str, Any] = {
            "allow_agent": False,
            "look_for_keys": False,
            "sock": connected_socket,
        }
        if key_filename:
            connect_kwargs["key_filename"] = key_filename
            connect_kwargs["passphrase"] = password
        if password:
            connect_kwargs["password"] = password

        fabric_config = (
            Config(overrides={"sudo": {"password": password}}) if password else Config()
        )
        super().__init__(
            host=hostname,
            user=username,
            port=port,
            config=fabric_config,
            connect_timeout=timeout,
            connect_kwargs=connect_kwargs,
        )

        # Jetson devices are frequently reprovisioned and therefore receive a
        # new host key. WarningPolicy preserves visibility without blocking CI.
        self.client.set_missing_host_key_policy(paramiko.WarningPolicy())
        self._connect_with_retries(hostname, port, timeout)
        self._open_sftp()

    @staticmethod
    def _validate_credentials(
        password: Optional[str], key_filename: Optional[str]
    ) -> None:
        if not password and not key_filename:
            raise ValueError("one of password or key_filename must be set")

    @staticmethod
    def _authentication_description(
        password: Optional[str], key_filename: Optional[str]
    ) -> str:
        methods = []
        if key_filename:
            methods.append("key")
        if password:
            methods.append("password")
        return " -> ".join(methods)

    @staticmethod
    def _open_socket(hostname: str, port: int, timeout: int) -> socket.socket:
        logger.info("Opening TCP connection to %s:%s", hostname, port)
        return socket.create_connection((hostname, port), timeout=timeout)

    def _connect_with_retries(self, hostname: str, port: int, timeout: int) -> None:
        last_error: Optional[BaseException] = None
        for attempt in range(1, self._MAX_CONNECT_ATTEMPTS + 1):
            try:
                logger.info(
                    "Fabric SSH handshake attempt %d/%d",
                    attempt,
                    self._MAX_CONNECT_ATTEMPTS,
                )
                self.open()
                return
            except (TimeoutError, OSError) as error:
                last_error = error
                logger.warning("SSH handshake attempt %d failed: %s", attempt, error)
                if attempt < self._MAX_CONNECT_ATTEMPTS:
                    time.sleep(self._RETRY_DELAY_SECONDS)
                    self.connect_kwargs["sock"] = self._open_socket(
                        hostname, port, timeout
                    )
        assert last_error is not None
        raise last_error

    def _open_sftp(self) -> None:
        try:
            self.sftp()
        except Exception:
            logger.exception("Unable to open SFTP connection")
            raise

    def _mutate_command(self, command: str) -> str:
        """Add bootc's transient DNF flags when the test environment needs them."""
        from tests_suites import conftest

        if not bool(getattr(conftest, "BOOTC_AVAILABLE", False)):
            return command
        words = command.split()
        if not words or words[0] != "dnf":
            return command
        if not self._MUTATING_DNF_COMMANDS.intersection(words[1:]):
            return command
        if "--transient" in words:
            return command
        return f"{command} --transient --nogpgcheck"

    def run(
        self,
        command: str,
        timeout: Optional[int] = None,
        fail_on_rc: bool = True,
        expect_rc: Optional[int] = 0,
        print_output: bool = True,
        stream_output: bool = False,
    ) -> CommandResult:
        """Run a command and return a typed result."""
        return self._execute(
            command=command,
            timeout=timeout,
            fail_on_rc=fail_on_rc,
            expect_rc=expect_rc,
            print_output=print_output,
            stream_output=stream_output,
            use_sudo=False,
        )

    def sudo(
        self,
        command: str,
        timeout: Optional[int] = None,
        fail_on_rc: bool = True,
        expect_rc: Optional[int] = 0,
        print_output: bool = True,
        stream_output: bool = False,
    ) -> CommandResult:
        """Run a command through sudo and return a typed result."""
        return self._execute(
            command=command,
            timeout=timeout,
            fail_on_rc=fail_on_rc,
            expect_rc=expect_rc,
            print_output=print_output,
            stream_output=stream_output,
            use_sudo=True,
        )

    def _execute(
        self,
        command: str,
        timeout: Optional[int],
        fail_on_rc: bool,
        expect_rc: Optional[int],
        print_output: bool,
        stream_output: bool,
        use_sudo: bool,
    ) -> CommandResult:
        prepared_command = self._mutate_command(command)
        if print_output:
            logger.info("[Fabric] Running command: %s", prepared_command)

        if use_sudo:
            fabric_result = super().sudo(
                prepared_command,
                timeout=timeout,
                warn=True,
                hide=not stream_output,
            )
        else:
            fabric_result = super().run(
                prepared_command,
                timeout=timeout,
                warn=True,
                hide=not stream_output,
            )

        typed_result = self._to_command_result(fabric_result)
        if print_output and not stream_output:
            logger.info("[Fabric] stdout:\n%s", typed_result.stdout)
        if fail_on_rc and typed_result.exit_status != expect_rc:
            raise RuntimeError(
                f"Command {prepared_command!r} failed with exit status "
                f"{typed_result.exit_status}; expected {expect_rc}. "
                f"Error: {typed_result.stderr}\nOutput:\n{typed_result.stdout}"
            )
        return typed_result

    @staticmethod
    def _to_command_result(result: Any) -> CommandResult:
        """Convert Fabric's dynamically typed return value once at the boundary."""
        fabric_result = cast(InvokeResult, result)
        exit_status = int(fabric_result.exited)
        return CommandResult(
            stdout=str(fabric_result.stdout),
            stderr=str(fabric_result.stderr),
            exit_status=exit_status,
            ok=exit_status == 0,
        )


__all__ = ["CommandResult", "SSHConnection"]
