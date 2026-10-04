#!/usr/bin/env python3
"""Run pytest over the Jumpstarter SSH port-forward, without wrapper.py."""

import os
import subprocess
import sys

from jumpstarter.common.utils import env
from jumpstarter_driver_network.adapters import TcpPortforwardAdapter


def main() -> int:
    username = os.environ.get("JETSON_USERNAME")
    if not username:
        raise SystemExit("JETSON_USERNAME is required")
    if not os.environ.get("JETSON_PASSWORD") and not os.environ.get("JETSON_KEY_PATH"):
        raise SystemExit("JETSON_PASSWORD or JETSON_KEY_PATH is required")

    pytest_args = sys.argv[1:] or ["tests_suites/", "-v"]
    command = [sys.executable, "-m", "pytest", *pytest_args]

    # env() uses the active Jumpstarter client/lease. The adapter exposes the
    # device SSH service as a temporary local TCP endpoint.
    with env() as client:
        ssh_client = client.ssh.tcp if hasattr(client.ssh, "tcp") else client.ssh
        with TcpPortforwardAdapter(client=ssh_client) as address:
            os.environ["JETSON_HOST"] = address[0]
            os.environ["JETSON_PORT"] = str(address[1])
            os.environ["JUMPSTARTER_IN_USE"] = "1"
            print(
                f"Running pytest over Jumpstarter SSH port-forward "
                f"{address[0]}:{address[1]}",
                flush=True,
            )
            result = subprocess.run(command, check=False)
            return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
