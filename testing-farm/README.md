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

# The repo/ref must contain this testing-farm directory. Use your pushed
# branch or fork while developing it.
export GIT_URL="https://github.com/<user>/qe-rhel-jetson.git"
export GIT_REF="direct-ssh-testing-farm"

bash testing-farm/submit.sh
```

For the first connectivity check, submit only the SSH smoke test:

```bash
export SSH_SMOKE_ONLY=1
bash testing-farm/submit.sh
```

The VM will connect as `root` and print the Beaker hostname, user, RHEL
release, and kernel. Unset `SSH_SMOKE_ONLY` to run the full pytest suite.

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
