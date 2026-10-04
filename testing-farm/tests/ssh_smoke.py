#!/usr/bin/env python3
"""Minimal SSH connectivity check for a Beaker Jetson."""

import os
import sys

import paramiko


host = os.environ["JETSON_HOST"]
port = int(os.environ.get("JETSON_PORT", "22"))
username = os.environ["JETSON_USERNAME"]
password = os.environ.get("JETSON_PASSWORD") or None
key_filename = os.environ.get("JETSON_KEY_PATH") or None

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.WarningPolicy())
try:
    client.connect(
        hostname=host,
        port=port,
        username=username,
        password=password,
        key_filename=key_filename,
        allow_agent=False,
        look_for_keys=False,
        timeout=30,
        auth_timeout=30,
        banner_timeout=30,
    )
    _, stdout, stderr = client.exec_command(
        "hostname -f; id -un; cat /etc/redhat-release 2>/dev/null || true; uname -r"
    )
    output = stdout.read().decode().strip()
    error = stderr.read().decode().strip()
    if error:
        print(error, file=sys.stderr)
    print(output)
finally:
    client.close()
