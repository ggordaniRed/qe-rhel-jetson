"""
SSH client infrastructure for Jetson RPM tests using fabric.
Based on test_basic_locally.py from edge-ai-image-pipelines.
"""

import logging
import socket
import time
import paramiko
from fabric import Connection, Config
from typing import Optional
logger = logging.getLogger(__name__)


class SSHConnection(Connection):
    """Simple SSH wrapper that mimics FabricAdapter interface for compatibility with existing tests."""

    def __init__(
        self,
        hostname: str,
        username: str,
        password: Optional[str],
        port: int = 22,
        timeout: int = 30,
        key_filename: Optional[str] = None,
    ):
        """
        Initialize SSH connection.

        Auth priority (handled by paramiko internally):
          1. key_filename (if provided) — tried first
          2. password (if provided) — fallback if key auth fails, or primary if no key

        Args:
            hostname: Hostname or IP address of the Jetson device
            username: SSH username
            password: SSH password (used for password auth; also used as key passphrase
                      if key_filename points to an encrypted key)
            port: SSH port (default: 22)
            timeout: Connection timeout in seconds
            key_filename: Path to private key file (e.g. ~/.ssh/id_rsa).
                          Has priority over password — tried first.
        """
        auth_methods = []
        if key_filename:
            auth_methods.append(f"key({key_filename})")
        if password:
            auth_methods.append("password")
        logger.info(
            "[SSH debug] Connecting: host=%s port=%s user=%s timeout=%ss auth=%s",
            hostname,
            port,
            username,
            timeout,
            " -> ".join(auth_methods) or "none",
        )

        # Step 1: quick TCP check (fails here if host unreachable or you need ProxyJump)
        # also, saving the socket to pre_connected_sock so we can use it for the paramiko connection.
        pre_connected_sock = None
        try:
            logger.info("[SSH debug] Step 1: TCP connect to %s:%s ...", hostname, port)
            sock = socket.create_connection((hostname, port), timeout=timeout)
            pre_connected_sock = sock
            logger.info("[SSH debug] Step 1: TCP connect OK, and saved the socket")
        except OSError as e:
            logger.error("[SSH debug] Step 1 FAILED (TCP): %s", e)
            raise

        # Step 2: initialize fabric.Connection
        # Auth priority: key_filename first, password fallback.
        # Disable agent/look_for_keys so only explicitly provided creds are used
        # (avoids random ~/.ssh/ keys exhausting MaxAuthTries on the server).
        if not key_filename and not password:
            raise ValueError("one of key_filename or password must be set")

        connect_kwargs: dict = {
            "allow_agent": False,
            "look_for_keys": False,
            "sock": pre_connected_sock, # use the pre-connected socket to avoid the paramiko's own address resolution (which handles IPv6→IPv4 fallback).
        }
        if key_filename:
            connect_kwargs["key_filename"] = key_filename
            connect_kwargs["passphrase"] = password  # decrypt key if encrypted, or None
        if password:
            connect_kwargs["password"] = password  # password auth (primary or fallback)

        config = Config(overrides={"sudo": {"password": password}}) if password else Config()

        super().__init__(
            host=hostname,
            user=username,
            port=port,
            config=config,
            connect_timeout=timeout,
            connect_kwargs=connect_kwargs,
        )

        #This is needed because Beaker reprovisioning gives each Jetson a new host key every time, so it's never in known_hosts.
        self.client.set_missing_host_key_policy(paramiko.WarningPolicy())

        # Step 3: Fabric SSH connect (retry on failed handshake, or timeout- lab links can be flaky)
        last_error = None
        for attempt in range(1, 4):  # up to 3 attempts
            try:
                logger.info(
                    "[SSH debug] Step 3: Fabric connect (attempt %s/3) ...", attempt
                )
                self.open()
                logger.info("[SSH debug] Step 3: Fabric connect OK")
                last_error = None
                break
            except (TimeoutError, OSError) as e:
                last_error = e
                logger.warning(
                    "[SSH debug] Step 3: Attempt %s/3 failed: %s", attempt, e
                )
                if attempt < 3:
                    time.sleep(2)
                    # The previous socket is consumed/closed after a failed
                    # handshake, so create a fresh one for the next attempt.
                    try:
                        new_sock = socket.create_connection((hostname, port), timeout=timeout)
                        self.connect_kwargs["sock"] = new_sock
                    except OSError as sock_err:
                        logger.warning(
                            "[SSH debug] Step 3: Socket reconnect failed: %s",
                            sock_err,
                        )
        if last_error is not None:
            logger.error(
                "[SSH debug] Step 3: FAILED (Fabric) after 3 attempts: %s",
                last_error,
                exc_info=True,
            )
            raise last_error

        # Step 4: Fabric SSH sftp
        try:
            logger.info("[SSH debug] Step 4: sftp() ...")
            self.sftp()
            logger.info("[SSH debug] Step 4: sftp() OK")
        except Exception as e:
            logger.error("[SSH debug] Step 4: FAILED (SFTP): %s", e, exc_info=True)
            raise

    def _mutate_command(self, command: str) -> str:
        MUTATING_DNF_COMMANDS = [
            "install",
            "remove",
            "update",
            "upgrade",
            "dist-sync",
            "group",
            "config-manager",
        ]
        # Import here to avoid circular import (conftest imports ssh_client)
        from tests_suites import conftest as _conftest
        # if bootc is available, add --transient
        # and --nogpgcheck because the image does not have the GPG key for internal Red Hat repositories
        if _conftest.BOOTC_AVAILABLE:
            cmd_parts = command.split()
            if cmd_parts and cmd_parts[0] == "dnf":
                if any(sub_cmd in cmd_parts for sub_cmd in MUTATING_DNF_COMMANDS):
                    if "--transient" not in command:
                        command += " --transient --nogpgcheck"
        return command

    def run(
        self,
        command: str,
        timeout: Optional[int] = None,
        fail_on_rc: bool = True,
        expect_rc: Optional[int] = 0,
        print_output: bool = True,
    ):
        """
        Run a command and return result with stdout attribute.

        Args:
            command: Command to execute
            timeout: Optional timeout in seconds

        Returns:
            Result object with stdout and exit_status attributes
        """
        command = self._mutate_command(command)

        if print_output:
            string_logger_formatted = f"\t\t[Fabric] Running command: {command}"
            logger.info(string_logger_formatted)

        result = super().run(command, timeout=timeout, warn=True, hide=True)
        # Create a result-like object similar to Fabric's result
        result = type(
            "Result",
            (),
            {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_status": result.exited,
                "ok": result.exited == 0,
            },
        )()

        if print_output:
            string_logger_formatted = f"\t\t[Fabric] stdout: \n{result.stdout}"
            logger.info(string_logger_formatted)

        if fail_on_rc and result.exit_status != expect_rc:
            raise RuntimeError(
                f"Command '{command}' failed with exit status {result.exit_status}. Expected {expect_rc}. Error: {result.stderr}. \n\t\tOutput: \n{result.stdout}"
            )

        return result

    def sudo(
        self,
        command: str,
        timeout: Optional[int] = None,
        fail_on_rc: bool = True,
        expect_rc: Optional[int] = 0,
        print_output: bool = True,
    ):
        """
        Run a command with sudo.

        Args:
            command: Command to execute with sudo
            timeout: Optional timeout in seconds
            fail_on_rc: Whether to raise an exception if the command fails
            expect_rc: The expected exit status of the command
        Returns:
            Result object with stdout and exit_status attributes
        """
        command = self._mutate_command(command)

        if print_output:
            string_logger_formatted = f"\t\t[Fabric] Running command: {command}"
            logger.info(string_logger_formatted)

        result = super().sudo(command, timeout=timeout, warn=True, hide=True)
        # Create a result-like object similar to Fabric's result
        result = type(
            "Result",
            (),
            {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_status": result.exited,
                "ok": result.exited == 0,
            },
        )()

        if print_output:
            string_logger_formatted = f"\t\t[Fabric] stdout: \n{result.stdout}"
            logger.info(string_logger_formatted)

        if fail_on_rc and result.exit_status != expect_rc:
            raise RuntimeError(
                f"Command '{command}' failed with exit status {result.exit_status}. Expected {expect_rc}. Error: {result.stderr}. \n\t\tOutput: \n{result.stdout}"
            )

        return result
