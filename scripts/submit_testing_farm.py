#!/usr/bin/env python3
"""Submit the existing private GitLab Beaker plan to the Testing Farm API."""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_URL = os.environ.get("TESTING_FARM_API_URL", "https://api.dev.testing-farm.io/v0.1").rstrip("/")
DEFAULT_REPO = "https://gitlab.cee.redhat.com/ggordani/qe-rhel-jetson.git"
ARTIFACTS_BASE = "https://artifacts.osci.redhat.com/testing-farm"


def required(name):
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"Missing required GitHub secret or workflow input: {name}")
    return value


def main():
    api_token = required("TESTING_FARM_API_TOKEN")
    beaker_user = required("BEAKER_USERNAME")
    beaker_password = required("BEAKER_PASSWORD")
    ssh_key = required("JETSON_SSH_KEY")
    gitlab_user = required("GITLAB_USERNAME")
    gitlab_token = required("GITLAB_TOKEN")
    git_ref = required("TF_GIT_REF")
    firmware = required("JETSON_FIRMWARE")
    image = required("BOOTC_IMAGE")
    reservation_hours = required("RESERVATION_HOURS")

    repo = os.environ.get("TESTING_FARM_REPO_URL", DEFAULT_REPO)
    parsed = urllib.parse.urlsplit(repo)
    if parsed.scheme != "https" or parsed.hostname != "gitlab.cee.redhat.com":
        raise SystemExit("TESTING_FARM_REPO_URL must point to gitlab.cee.redhat.com over HTTPS")
    if not parsed.path.endswith(".git"):
        raise SystemExit("TESTING_FARM_REPO_URL must be a GitLab .git URL")
    auth_netloc = (
        urllib.parse.quote(gitlab_user, safe="") + ":" + urllib.parse.quote(gitlab_token, safe="")
        + "@" + parsed.netloc
    )
    authenticated_repo = urllib.parse.urlunsplit((parsed.scheme, auth_netloc, parsed.path, "", ""))
    image_base, separator, image_tag = image.rpartition(":")
    if not separator or not image_base or not image_tag:
        raise SystemExit("BOOTC_IMAGE must include a tag, for example quay.io/org/image:tag")

    secrets = {
        "BEAKER_USERNAME": beaker_user,
        "BEAKER_PASSWORD": beaker_password,
        "JETSON_SSH_KEY": ssh_key,
        # This token is included in the private clone URL and also passed as a
        # Testing Farm secret so it can redact the value from stored artifacts.
        "GIT_PASSWORD": gitlab_token,
    }
    registry_user = os.environ.get("REGISTRY_USER", "")
    registry_password = os.environ.get("REGISTRY_PASS", "")
    if bool(registry_user) != bool(registry_password):
        raise SystemExit("Set both REGISTRY_USER and REGISTRY_PASS, or neither")
    if registry_user:
        secrets["REGISTRY_USER"] = registry_user
        secrets["REGISTRY_PASS"] = registry_password

    payload = {
        "test": {"fmf": {
            "url": authenticated_repo,
            "ref": git_ref,
            "name": "/testing-farm/beaker-e2e",
        }},
        "environments": [{
            "arch": "x86_64",
            "os": {"compose": "RHEL-9-Nightly"},
            "variables": {
                "BEAKER_HUB_URL": "https://beaker.engineering.redhat.com",
                "BEAKER_AUTH_METHOD": "password",
                "JETSON_FIRMWARE": firmware,
                "JETSON_USERNAME": "root",
                "BOOTC_IMAGE_BASEURL": image_base,
                "BOOTC_IMAGE_TAG": image_tag,
                "RESERVATION_HOURS": reservation_hours,
            },
            "secrets": secrets,
        }],
        "settings": {"pipeline": {"timeout": 720}},
    }
    request = urllib.request.Request(
        f"{API_URL}/requests",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        for secret in (api_token, beaker_password, ssh_key, gitlab_token, registry_password):
            if secret:
                body = body.replace(secret, "[redacted]")
        body = body.replace(authenticated_repo, repo)
        print(f"Testing Farm rejected the request (HTTP {error.code}): {body[:2000]}", file=sys.stderr)
        raise SystemExit(1)
    except urllib.error.URLError as error:
        print(f"Could not reach the Testing Farm API: {error.reason}", file=sys.stderr)
        raise SystemExit(1)

    request_id = result.get("id") or result.get("request_id")
    if not request_id:
        raise SystemExit("Testing Farm accepted a response without a request ID")
    request_url = f"{API_URL}/requests/{request_id}"
    artifacts_url = f"{ARTIFACTS_BASE}/{request_id}"
    print(f"Testing Farm request submitted: {request_id}")
    print(f"Request: {request_url}")
    print(f"Artifacts: {artifacts_url}")

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"request_id={request_id}\n")
            stream.write(f"request_url={request_url}\n")
            stream.write(f"artifacts_url={artifacts_url}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        safe_repo = repo
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("## Testing Farm request submitted\n\n")
            stream.write("| Setting | Value |\n| --- | --- |\n")
            stream.write(f"| Test repository | `{safe_repo}` |\n")
            stream.write(f"| Ref | `{git_ref}` |\n")
            stream.write(f"| Plan | `/testing-farm/beaker-e2e` |\n")
            stream.write(f"| Firmware | `{firmware}` |\n")
            stream.write(f"| Bootc image | `{image}` |\n")
            stream.write(f"| Reservation | `{reservation_hours}h` |\n\n")
            stream.write(f"[Request details]({request_url}) · [Artifacts]({artifacts_url})\n\n")
            stream.write("The request has been accepted by Testing Farm. Its result will appear in the dashboard after the plan finishes.\n")


if __name__ == "__main__":
    main()
