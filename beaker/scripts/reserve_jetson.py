#!/usr/bin/env python3
"""
Reserve a Jetson system via Beaker.

Usage:
    python scripts/reserve_jetson.py
    python scripts/reserve_jetson.py --distro RHEL-9.6.0-updates-20260101.0
    python scripts/reserve_jetson.py --target nvidia-jetson-agx-orin-03.khw.eng.bos2.dc.redhat.com
    python scripts/reserve_jetson.py --hours 48

Environment variables:
    BEAKER_HUB_URL: Beaker server URL (required)
    BEAKER_SSL_VERIFY: Set to "false" to disable SSL verification
    
    Authentication (choose one method):
    
    Option 1 - Kerberos (default):
        BEAKER_AUTH_METHOD: Set to "krbv" (or leave unset)
        BEAKER_KRB_REALM: Kerberos realm (optional)
        Requires: valid Kerberos ticket (run 'kinit' first)
    
    Option 2 - Username/Password:
        BEAKER_AUTH_METHOD: Set to "password"
        BEAKER_USERNAME: Your Beaker username
        BEAKER_PASSWORD: Your Beaker password
"""

import argparse
import os
import subprocess
import sys
import time

from _common import get_beaker_client, get_hub_url
from pybeaker import BeakerClient


# Default values
DEFAULT_TARGET = "nvidia-jetson-agx-orin-05.khw.eng.bos2.dc.redhat.com"
DEFAULT_DISTRO = "RHEL-9.7.0"
DEFAULT_HOURS = 24
SSH_TIMEOUT_MINUTES = 30
SSH_RETRY_INTERVAL = 30  # seconds

# Job XML template
JOB_XML_TEMPLATE = '''<job retention_tag="scratch">
  <whiteboard>Jetson Bootc Testing - {target_short}</whiteboard>
  <recipeSet priority="High">
    <recipe whiteboard="" role="RECIPE_MEMBERS" ks_meta="" kernel_options="" kernel_options_post="">
      <autopick random="false"/>
      <watchdog panic="ignore"/>
      <packages/>
      <ks_appends/>
      <repos/>
      <distroRequires>
        <and>
          <distro_family op="=" value="RedHatEnterpriseLinux9"/>
          <distro_variant op="=" value="BaseOS"/>
          <distro_name op="=" value="{distro}"/>
          <distro_arch op="=" value="aarch64"/>
        </and>
      </distroRequires>
      <hostRequires force="{target}"/>
      <partitions/>
      <task name="/distribution/check-install" role="STANDALONE"/>
      <task name="/distribution/reservesys" role="STANDALONE">
        <params>
          <param name="RESERVETIME" value="{reserve_seconds}"/>
        </params>
      </task>
    </recipe>
  </recipeSet>
</job>'''


def run_ssh_command(host: str, command: str, user: str = "root", timeout: int = 15) -> tuple[bool, str]:
    """Run a command over SSH and return (success, stdout).

    Args:
        host: Hostname to connect to
        command: Remote command to execute
        user: SSH username
        timeout: Connection timeout in seconds

    Returns:
        (True, stdout) on success, (False, "") on failure
    """
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={timeout}",
                "-o", "LogLevel=ERROR",
                f"{user}@{host}",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 10,
        )
        return result.returncode == 0, result.stdout.strip()
    except (subprocess.TimeoutExpired, Exception):
        return False, ""


def check_ssh_connectivity(host: str, user: str = "root", timeout: int = 10) -> bool:
    """Check if SSH connection can be established.

    Args:
        host: Hostname to connect to
        user: SSH username
        timeout: Connection timeout in seconds

    Returns:
        True if SSH connection succeeds, False otherwise
    """
    ok, out = run_ssh_command(host, "echo ok", user, timeout)
    return ok and "ok" in out


def wait_for_ssh(host: str, timeout_minutes: int = 30, user: str = "root") -> bool:
    """Wait for SSH to become available on the host.

    Args:
        host: Hostname to connect to
        timeout_minutes: Maximum time to wait in minutes
        user: SSH username

    Returns:
        True if SSH becomes available, False if timeout
    """
    start_time = time.time()
    timeout_seconds = timeout_minutes * 60
    attempt = 0

    print(f"\n⏳ Waiting for SSH connectivity (timeout: {timeout_minutes} minutes)...")

    while (time.time() - start_time) < timeout_seconds:
        attempt += 1
        elapsed = int(time.time() - start_time)
        remaining = int(timeout_seconds - elapsed)

        print(f"   Attempt {attempt}: Trying SSH to {host}... ", end="", flush=True)

        if check_ssh_connectivity(host, user):
            print("✅ SUCCESS")
            return True

        print(f"❌ (retrying in {SSH_RETRY_INTERVAL}s, {remaining}s remaining)")

        if (time.time() - start_time + SSH_RETRY_INTERVAL) < timeout_seconds:
            time.sleep(SSH_RETRY_INTERVAL)

    return False


def verify_system_ready(host: str, user: str = "root") -> bool:
    """SSH into the reserved machine, collect basic system info, and confirm
    the provisioning actually completed.

    Returns:
        True if the system responds with valid info, False otherwise.
    """
    print(f"\n🔍 Verifying system is fully provisioned on {host}...")

    checks = [
        ("hostname",   "hostname -f"),
        ("kernel",     "uname -r"),
        ("os-release", "cat /etc/redhat-release 2>/dev/null || cat /etc/os-release | head -1"),
        ("uptime",     "uptime"),
        ("disk",       "df -h / | tail -1"),
    ]

    all_ok = True
    for label, cmd in checks:
        ok, out = run_ssh_command(host, cmd, user, timeout=15)
        if ok and out:
            print(f"   ✅ {label}: {out}")
        else:
            print(f"   ❌ {label}: no response")
            all_ok = False

    return all_ok


def find_existing_job(client: BeakerClient, target: str, user: str) -> dict | None:
    """Find an existing running or queued job for the target machine.
    
    Args:
        client: BeakerClient instance
        target: Target machine FQDN
        user: Current username
        
    Returns:
        Job info dict if found, None otherwise
    """
    target_short = target.split(".")[0]
    expected_whiteboard = f"Jetson Bootc Testing - {target_short}"
    
    print(f"🔍 Checking for existing jobs for {target_short}...")
    
    try:
        jobs = client.list_jobs(limit=20)
        
        for job in jobs:
            job_id = job.get("id") if isinstance(job, dict) else job.job_id
            status = job.get("status") if isinstance(job, dict) else job.status
            whiteboard = job.get("whiteboard") if isinstance(job, dict) else job.whiteboard
            
            # Check if job is active and matches our target
            if status in ("New", "Queued", "Scheduled", "Waiting", "Installing", "Running", "Reserved"):
                if whiteboard and target_short in whiteboard:
                    print(f"   Found existing job: {job_id} (status: {status})")
                    return {"id": job_id, "status": status, "whiteboard": whiteboard}
    except Exception as e:
        print(f"   Warning: Could not list jobs: {e}")
    
    return None


def wait_for_job_running(
    client: BeakerClient,
    job_id: str,
    target_host: str,
    timeout_minutes: int = 60,
) -> bool:
    """Wait for a job to reach Running or Reserved status.

    Polls the Beaker API every 30 s but *also* probes SSH on each
    iteration.  If the API becomes unreachable (VPN drop, timeout)
    the function falls back to SSH: once the host answers, the job
    is considered ready regardless of what Beaker reports.

    Args:
        client: BeakerClient instance
        job_id: Job ID to monitor
        target_host: FQDN of the target machine (for SSH fallback)
        timeout_minutes: Maximum time to wait

    Returns:
        True if job reaches Running/Reserved or host is SSH-reachable,
        False on timeout or terminal failure.
    """
    start_time = time.time()
    timeout_seconds = timeout_minutes * 60
    poll_interval = 30
    api_failures = 0

    print(f"\n⏳ Waiting for job {job_id} to reach Running status...")

    terminal_failed = {"Cancelled", "Aborted", "Completed"}
    target_status = {"Running", "Reserved"}

    while (time.time() - start_time) < timeout_seconds:
        elapsed = int(time.time() - start_time)

        # -- Beaker API check --
        try:
            status = client.get_job_status(job_id)
            api_failures = 0

            print(f"   [{elapsed}s] Job {job_id}: {status.status}")

            if status.status in target_status:
                return True

            if status.status in terminal_failed:
                print(f"❌ Job ended with status: {status.status}")
                return False

        except Exception as e:
            api_failures += 1
            print(f"   [{elapsed}s] Beaker API unreachable ({api_failures}x): {type(e).__name__}")

        # -- SSH fallback (try after first few minutes or when API is flaky) --
        if elapsed > 120 or api_failures >= 2:
            if check_ssh_connectivity(target_host):
                print(f"   [{elapsed}s] ✅ SSH to {target_host} succeeded — machine is ready")
                return True

        time.sleep(poll_interval)

    print(f"❌ Timeout waiting for job to reach Running status")
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Reserve a Jetson system via Beaker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    
    parser.add_argument(
        "--target", "-t",
        default=DEFAULT_TARGET,
        help=f"Target Jetson FQDN (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--distro", "-d",
        default=DEFAULT_DISTRO,
        help=f"Distro name (default: {DEFAULT_DISTRO})",
    )
    parser.add_argument(
        "--hours", "-H",
        type=int,
        default=DEFAULT_HOURS,
        help=f"Reservation hours (default: {DEFAULT_HOURS})",
    )
    parser.add_argument(
        "--ssh-timeout",
        type=int,
        default=SSH_TIMEOUT_MINUTES,
        help=f"SSH connectivity timeout in minutes (default: {SSH_TIMEOUT_MINUTES})",
    )
    parser.add_argument(
        "--initial-wait",
        type=int,
        default=15,
        help="Minutes to wait after job reaches Running before first SSH attempt (default: 15)",
    )
    parser.add_argument(
        "--skip-ssh-wait",
        action="store_true",
        help="Skip waiting for SSH connectivity",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show job XML without submitting",
    )
    
    args = parser.parse_args()
    
    # Build job XML
    target_short = args.target.split(".")[0]
    job_xml = JOB_XML_TEMPLATE.format(
        target=args.target,
        target_short=target_short,
        distro=args.distro,
        reserve_seconds=args.hours * 3600,
    )
    
    if args.dry_run:
        print("=== Job XML (dry run) ===")
        print(job_xml)
        return

    # ---- Beaker phase (best-effort) ----
    hub_url = os.environ.get("BEAKER_HUB_URL", "")
    beaker_ok = False
    job_id = None

    try:
        hub_url = get_hub_url()
        print(f"🔗 Connecting to Beaker...")
        client = get_beaker_client()

        user = client.whoami()
        print(f"✅ Authenticated as: {user}")

        existing_job = find_existing_job(client, args.target, user)

        if existing_job:
            job_id = existing_job["id"]
            job_status = existing_job["status"]
            print(f"\n✅ Using existing job: {job_id} (status: {job_status})")

            if job_status in ("Running", "Reserved"):
                print(f"   Job is already in {job_status} state")
            else:
                if not wait_for_job_running(client, job_id, args.target):
                    print(f"\n⚠️  Job {job_id} did not reach Running status via Beaker API")
                    print(f"   Falling back to SSH verification...")
        else:
            print(f"\n📋 Submitting new job:")
            print(f"   Target: {args.target}")
            print(f"   Distro: {args.distro}")
            print(f"   Hours:  {args.hours}")
            print()

            job_id = client.submit_job(job_xml)
            print(f"✅ Job submitted: {job_id}")
            print(f"   View: {hub_url}/jobs/{job_id.replace('J:', '')}")

            if not wait_for_job_running(client, job_id, args.target):
                print(f"\n⚠️  Job {job_id} did not reach Running status via Beaker API")
                print(f"   Falling back to SSH verification...")

        beaker_ok = True
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n⚠️  Beaker API unreachable: {type(e).__name__}")
        print(f"   Skipping Beaker phase — falling back to SSH verification...")

    # ---- SSH verification phase (always runs) ----
    if args.skip_ssh_wait:
        print(f"\n⏭️  Skipping SSH wait (--skip-ssh-wait)")
        print(f"\n📝 Next steps:")
        print(f"   1. Wait for machine to be accessible via SSH")
        print(f"   2. Run Ansible playbook to install bootc:")
        print(f"      cd ansible && ansible-playbook -i inventory.yml install_bootc.yml --ask-vault-pass")
        return

    if args.initial_wait > 0:
        print(f"\n⏳ Waiting {args.initial_wait} minutes for provisioning to complete...")
        for remaining in range(args.initial_wait, 0, -1):
            print(f"   {remaining} min remaining...", end="\r", flush=True)
            time.sleep(60)
        print()

    if wait_for_ssh(args.target, args.ssh_timeout):
        if verify_system_ready(args.target):
            print(f"\n✅ Machine {args.target} is ready and verified!")
            print(f"\n📝 Next steps:")
            print(f"   Run Ansible playbook to install bootc:")
            print(f"      GO TO qe-rhel-jetson/beaker/README.md step 3: Set Up Ansible Vault ")
        else:
            print(f"\n⚠️  SSH works but system verification incomplete on {args.target}")
            print(f"   The machine may still be finalizing provisioning.")
            print(f"   You can still try connecting manually.")
            sys.exit(1)
    else:
        print(f"\n❌ FAILED: Could not establish SSH connection to {args.target}")
        print(f"   after waiting {args.ssh_timeout} minutes.")
        print(f"\n   Possible causes:")
        print(f"   - Machine is still being provisioned")
        print(f"   - Network/firewall issues")
        print(f"   - Installation failed")
        if hub_url and job_id:
            print(f"\n   Check job status: {hub_url}/jobs/{job_id.replace('J:', '')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
