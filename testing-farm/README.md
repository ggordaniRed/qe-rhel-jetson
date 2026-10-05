# Testing Farm direct SSH runner

This directory runs the existing `qe-rhel-jetson` pytest suite against a
separately reserved Beaker Jetson. It does not build a disk image, flash a
device, acquire a Jumpstarter lease, or invoke `jumpstarter/wrapper.py`.

The Beaker machine must already be provisioned and reachable from the machine
running the tests. For Testing Farm, the provisioned VM must also have network
access to the BOS2 lab.

If the Testing Farm VM reaches the lab through Jumpstarter, use the
Jumpstarter transport mode below. It still runs the normal pytest SSH fixtures;
Jumpstarter only supplies the temporary SSH port-forward.

## Test locally from the workstation

Install the QE dependencies once:

```bash
python3 -m pip install -r requirements.txt
```

Set the reserved Beaker host and SSH credentials:

```bash
export JETSON_HOST="nvidia-jetson-agx-orin-05.khw.eng.bos2.dc.redhat.com"
export JETSON_USERNAME="root"
export JETSON_KEY_PATH="$HOME/.ssh/id_ed25519"
# Alternatively: unset JETSON_KEY_PATH and export JETSON_PASSWORD='...'
```

Run all tests or a subset:

```bash
bash testing-farm/run-local.sh
bash testing-farm/run-local.sh tests_suites/sanity/ -v
```

## Test locally through Jumpstarter

This mode uses an existing Jumpstarter lease and avoids the wrapper's flash,
boot, and recovery logic:

```bash
export JMP_LEASE="<active-lease-name>"
export JETSON_USERNAME="root"
export JETSON_PASSWORD="..."

bash testing-farm/run-via-jumpstarter.sh tests_suites/sanity/ -v
```

The pytest process connects to the temporary forwarded host and port created by
Jumpstarter. It does not connect to the Beaker FQDN directly.

## Submit a Testing Farm request

This requires `tft-cli` and a Red Hat Testing Farm API token:

```bash
python3 -m pip install tft-cli
export TESTING_FARM_API_TOKEN="..."
export JETSON_HOST="nvidia-jetson-agx-orin-05.khw.eng.bos2.dc.redhat.com"
export JETSON_USERNAME="root"
export JETSON_PASSWORD="..."

# The GitLab project is private. Use a GitLab PAT with repository read access
# so Testing Farm can clone it.
export GIT_USERNAME="ggordani"
export GIT_PASSWORD="<gitlab-read-token>"

# The repo/ref must contain this testing-farm directory. Use your pushed
# branch or fork while developing it.
export GIT_URL="https://gitlab.cee.redhat.com/ggordani/qe-rhel-jetson.git"
export GIT_REF="direct-ssh-testing-farm"

# The actual pytest source is pulled separately from the upstream GitHub repo.
export QE_REPO_URL="https://github.com/rh-ecosystem-edge/qe-rhel-jetson.git"
export QE_REPO_REF="rhel-9.8-latest"

bash testing-farm/submit.sh
```

For the first connectivity check, submit only the SSH smoke test:

```bash
export SSH_SMOKE_ONLY=1
bash testing-farm/submit.sh
```

The VM will connect as `root` and print the Beaker hostname, user, RHEL
release, and kernel. Unset `SSH_SMOKE_ONLY` to run the full pytest suite.

## Investigate a submitted request

Use the request ID printed by `submit.sh`:

```bash
bash testing-farm/investigate.sh cc6bb08f-15d7-4f1d-b14c-af598bf21842
```

The script waits for completion, prints the final state/result, and fetches the
test output. It uses `curl -k` for the internal artifact endpoint when Red Hat
CA certificates are not installed.

## Optional Ansible bootc deployment

Set `ANSIBLE_BOOTC=1` to switch an already booted bootc system to the requested
image with the existing Beaker Ansible playbook before the SSH smoke test or
pytest suite. This is opt-in; without it, the runner only connects to the
existing OS.

```bash
export ANSIBLE_BOOTC=1
export BOOTC_IMAGE_BASE="quay.io/<quay-namespace>/<bootc-image>"
export BOOTC_IMAGE_TAG="411ed591"
export REGISTRY_URL="quay.io"
export ANSIBLE_AUTO_REBOOT=true
export ANSIBLE_RESTORE_BOOT_ORDER=true
export ANSIBLE_RESERVATION_HOURS=24

export SSH_SMOKE_ONLY=1
bash testing-farm/submit.sh
```

For this public image, no registry credentials are required. For a private
image, additionally set `REGISTRY_USER` and `REGISTRY_PASSWORD`; they are sent
as Testing Farm secrets. The playbook installs the image, reboots when enabled,
waits for SSH, and then runs the smoke test or pytest. Do not enable this while
another job is using the same Beaker machine.

For key authentication, pass the private key as a runtime secret:

```bash
export SSH_PRIVATE_KEY="$(< "$HOME/.ssh/id_ed25519")"
unset JETSON_PASSWORD
bash testing-farm/submit.sh
```

The credentials are sent with `--secret` and are not stored in the repository.
The Beaker reservation and the Testing Farm request are separate; release or
cancel the Beaker reservation after the run.

## Important local limitation

`run-local.sh` can be tested immediately from a workstation with Red Hat VPN
and Beaker access. `submit.sh` cannot use uncommitted local files because
Testing Farm clones `GIT_URL`/`GIT_REF`; push the branch first, then submit it.
