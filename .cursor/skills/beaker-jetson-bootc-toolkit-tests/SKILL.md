---
name: beaker-jetson-bootc-toolkit-tests
description: Build a toolkit-layered arm64 bootc image, deploy it to a Beaker Jetson with Ansible, and run the QE pytest suite. Use when validating a supplied JetPack bootc image on Beaker lab hardware.
---

# Beaker Jetson Bootc Toolkit Tests

Use this workflow for Beaker hardware. Read the `jetson-context` skill first. This path layers NVIDIA Container Toolkit onto the supplied bootc image, publishes the derived image to a registry the device can pull from, deploys it with Ansible, and runs pytest directly over SSH. It does not flash a disk through Jumpstarter.

## Prepare the image reference

Collect the target FQDN, full base image, writable Quay destination, and desired reservation duration. Remove an optional `http://` prefix from an OCI image reference. Split the source reference into its registry, repository, and tag. Infer the RHEL release and kernel from the tag, then verify them in `tests_suites/jetson_hardware_specs.yaml`.

The Beaker host cannot pull `localhost/...`. Ask for a writable destination repository if one has not been given. Do not push into the source image's namespace unless the user confirms they have write access. Never request or print registry passwords or tokens; have the user authenticate with `podman login` in their own terminal.

## Build the toolkit layer

From the repository root, build for Jetson's arm64 architecture using the exact supplied base image:

```bash
podman build --platform linux/arm64 \
  --build-arg BASE_IMAGE="$BASE_IMAGE" \
  --tag localhost/jetson-nvidia-toolkit:testing \
  --file jumpstarter/Containerfile-toolkit \
  jumpstarter/
```

`jumpstarter/Containerfile-toolkit` installs the pinned `nvidia-container-toolkit-base` package, adds the Jetson `libcuda` links when present, removes a stale CDI spec, and enables `nvidia-ctk.service`. Read the Containerfile for the current package version. Check the build output for a successful package transaction and image commit before continuing.

Tag and push the resulting image to the user-provided Quay destination:

```bash
podman tag localhost/jetson-nvidia-toolkit:testing "$PUSH_IMAGE"
podman push "$PUSH_IMAGE"
```

Set `PUSH_IMAGE` to the complete registry reference, including a unique tag. Pass its repository path without the tag as `bootc_image_base` and the tag separately as `bootc_image_tag` to Ansible.

## Reserve and deploy

Check Beaker access and machine availability before scheduling the reservation. If `klist` has no valid ticket, ask the user to run `kinit <user>@IPA.REDHAT.COM` in their terminal; do not run `kinit` or request the password. Use a reservation duration appropriate for the build and test run, and provision a Beaker distro matching the image's RHEL major and minor version.

When adding internal nightly repositories, use the `major.minor` RHEL version in `latest-RHEL-<version>` without appending `.0`. For example, the Jetson returned HTTP 200 for `latest-RHEL-10.2` and HTTP 404 for `latest-RHEL-10.2.0`.

The reservation command is:

```bash
cd beaker
python3 scripts/reserve_jetson.py \
  --target "$JETSON_HOST" \
  --hours "$RESERVATION_HOURS" \
  --distro "$BEAKER_DISTRO"
```

The Ansible playbook requires `beaker/ansible/vars/secrets.yml` and logs into the image registry using its vaulted `registry_user` and `registry_pass`. Check that those credentials can pull from the destination registry. If the vault needs an update, have the user edit it locally; never place a vault password or registry secret in tool arguments or chat. If the vault password cannot be supplied through an existing secure mechanism, hand the interactive command to the user.

Run the bootc install from `beaker/ansible/`:

```bash
ansible-playbook -i inventory.yml install_bootc.yml --ask-vault-pass \
  -e "target_host=$JETSON_HOST" \
  -e "registry_url=quay.io" \
  -e "bootc_image_base=$PUSH_IMAGE_BASE" \
  -e "bootc_image_tag=$PUSH_IMAGE_TAG" \
  -e "reservation_hours=$RESERVATION_HOURS"
```

Do not retry a failed reservation or image deployment without confirming the machine's state. After deployment, verify `bootc status`, `uname -r`, and the RHEL release remotely before testing.

## Run pytest

For a Beaker deployment, run pytest directly from the QE repository; do not use `jumpstarter/wrapper.py`. Create or activate `.venv`, install `requirements.txt` if needed, and use the exact kernel version encoded by the deployed image when it differs from the suite's general default:

```bash
JETSON_HOST="$JETSON_HOST" JETSON_USERNAME=root \
  pytest tests_suites/ --target-kernel-version="$KERNEL_VERSION"
```

Use the existing `JETSON_KEY_PATH` or `JETSON_PASSWORD` configuration without printing those values. Summarize the pytest result and distinguish failures from expected skips. Do not rerun a failed deployment or test workflow without user confirmation.
