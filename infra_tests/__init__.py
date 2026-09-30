"""SSH infrastructure for Jetson RPM tests."""

from .ssh_client import CommandResult, SSHConnection

__all__ = ["CommandResult", "SSHConnection"]
