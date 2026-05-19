# Flash & Test Jetson via Jumpstarter

Manual guide for building a bootc disk image, flashing it to a Jetson via Jumpstarter, and running tests.

## Prerequisites

- `podman` installed (with rootful machine configured)
- `registry.redhat.io` registry access (for pulling official bootc-image-builder)
- `quay.io` registry access
- Jumpstarter client access ([check your name here](https://gitlab.cee.redhat.com/automotive/jumpstarter/ecosystems-edge/-/tree/main/configuration/clients), if missing contact Nick Cao)

## Environment Variables

```bash
export JETSON_USERNAME=root
export JETSON_PASSWORD="your-password"
# Optional: for key-based SSH auth (key must be added to config.toml too)
export JETSON_KEY_PATH="~/.ssh/id_ed25519"
```

---

## 1. Build the Disk Image

### Configure Podman (one-time setup)

```bash
podman machine stop
podman machine set --rootful
podman machine start
podman system connection default podman-machine-default-root
```

### Create config.toml

```bash
tee config.toml <<EOF
[customizations.kernel]
append = "console=ttyTCU0"

[[customizations.user]]
name = "root"
password = "${JETSON_PASSWORD}"
key = "$(cat ~/.ssh/*.pub)"
groups = ["video", "wheel"]
EOF
```

> **Note:** IF known that BootC RHEL 9.7 (not 9.8+) deploy with non graphical target **please change** the append to `"console=ttyTCU0 pd_ignore_unused"` instead `"console=ttyTCU0"` | 
> The `key` field must contain the **public key content** (not a file path).
> `$(cat ~/.ssh/*.pub)` expands to your actual public key string.

> **Why `pd_ignore_unused`needed?** Display tests need `nvidia_drm` loaded, which on RHEL 9.7
> can cause a kernel hang without this flag. Baking it into the image avoids a reboot
> during testing — important because Jumpstarter's SSH tunnel can't survive a device reboot.

> **Why `pd_ignore_unused`not needed?** Display tests need `nvidia_drm` loaded, which not loaded on RHEL 9.7 without graphical target,
> If this is the case, only for RHEL9.7 and no graphical.target deployed , kernel falg pd_ignore_unused is needed.

### Pull and Build

```bash
podman login quay.io

podman pull quay.io/redhat-user-workloads/jetpack-for-rhel-tenant/rhel97-ga-5140-61151-stage@sha256:<hash>

mkdir -p ./output && sudo chmod 777 ./output

sudo podman run --rm --privileged \
  --security-opt label=type:unconfined_t \
  -v /var/lib/containers/storage:/var/lib/containers/storage \
  -v ./config.toml:/config.toml:ro \
  -v ./output:/output \
  registry.redhat.io/rhel9/bootc-image-builder:latest build \
  --output /output --type raw --target-arch arm64 \
  quay.io/redhat-user-workloads/jetpack-for-rhel-tenant/rhel97-ga-5140-61151-stage@sha256:<hash>
```

### Compress

```bash
xz ./output/image/disk.raw
# Result: ./output/image/disk.raw.xz
export DISK_IMAGE_PATH="<full path to disk.raw.xz>"
# Please provide ENV system for disk location
```

---

## 2. Flash via Jumpstarter

### Install Jumpstarter Client

```bash
curl -fsSL https://raw.githubusercontent.com/jumpstarter-dev/jumpstarter/main/python/install.sh | bash -s -- -s main
```

### Login and Get a Lease

```bash
jmp login $USER@jumpstarter-ecosystems.apps.rosa.auto-devcluster.bzdx.p3.openshiftapps.com

jmp get exporters                 # list available devices
jmp get leases --all              # see what's taken
jmp shell --selector device=<DEVICE> --duration 1:30:00
```

### Flash the Image

Inside the Jumpstarter shell:

```bash
j storage flash --compression xz ./output/image/disk.raw.xz
```

---

## 3. Run Tests

Two options after flashing:

### Option A: Run via wrapper.py (automated, recommended)

The wrapper automates: power cycle → enable SSH → growpart → run pytest.

```bash
cd qe-rhel-jetson
source .venv/bin/activate
pip install -r requirements.txt

jmp get leases --client $USER     # copy your lease name
jmp shell --lease <LEASE_NAME> -- python jumpstarter/wrapper.py pytest tests_suites/
```
*If no PXE menu appears during the running* , stop and run the following command

```bash
j storage dut # witch storage to the Jetson so it can boot from it
j power cycle # power on the device
j serial start-console # Verify boot : 
# -> IF "grub>" appears write exit to force rebooting 
# -> IF stuck try press ENTER , if "login:" showed up everyting work
# -> for exit: Ctrl+B x3
```
TRY AGAIN 
```jmp shell --lease <LEASE_NAME> -- python jumpstarter/wrapper.py pytest tests_suites/```

### Release the Lease

```bash
exit    # from the Jumpstarter shell
```

### Option B: Run Directly (outside Jumpstarter shell)

In the Jumpstarter shell:

```bash
j storage dut # witch storage to the Jetson so it can boot from it
j power cycle # power on the device
j serial start-console # verify boot (exit: Ctrl+B x3)
# On the console's machine itself
# login via root, for password- what you configured in config.toml
/usr/libexec/bootc-generic-growpart # expand the root partition
exit # exit from the console , back to the Jumpstarter Shell
```

Keep the Jumpstarter shell open in one terminal. In another terminal:

```bash
export JETSON_HOST=<device-hostname>
export JETSON_USERNAME=root
export JETSON_PASSWORD="your-password"

cd qe-rhel-jetson
source .venv/bin/activate # In case you already created py interpreter
pytest tests_suites/
```

> See [tests_suites/README.md](../tests_suites/README.md) for test configuration details.

---

## Optional: Build a GUI-Enabled Disk Image

By default, the bootc not including GNOME desktop packages. To build a disk image with GNOME desktop (GDM + gnome-shell) for display testing or KVM usage, use `Containerfile-gui`:

### 1. Build the GUI container image

```bash
# Default (RHEL 9.7 with default base image):
podman build --platform linux/arm64 -t localhost/jetson-gui:latest -f Containerfile-gui .

# Custom base image and/or RHEL version:
podman build --platform linux/arm64 \
  --build-arg BASE_IMAGE=<your-base-image> \
  --build-arg RHEL_VERSION=9.8 \
  -t localhost/jetson-gui:latest -f Containerfile-gui .
```

| Build arg | Default | Description |
|-----------|---------|-------------|
| `BASE_IMAGE` | `quay.io/.../rhel97-5140-611421-stage:...` | Base bootc image to layer GUI on top of |
| `RHEL_VERSION` | `9.7` | RHEL version for repo URLs and EPEL |

> **Note:** `Containerfile-gui` layers GDM, gnome-shell, x11vnc, and xorg-x11-drivers on top of the base image.

### 2. Build the raw disk image

```bash
mkdir -p ./output && sudo chmod 777 ./output

podman run --rm --privileged \
  --security-opt label=type:unconfined_t \
  -v /var/lib/containers/storage:/var/lib/containers/storage \
  -v ./config.toml:/config.toml:ro \
  -v ./output:/output \
  registry.redhat.io/rhel9/bootc-image-builder:latest build \
  --output /output --type raw --target-arch arm64 \
  localhost/jetson-gui:latest
```

### 3. Compress and flash

```bash
xz ./output/image/disk.raw
export DISK_IMAGE_PATH="$(pwd)/output/image/disk.raw.xz"
```
 
Then flash and run tests as usual (see Step 2 and Step 3 above).

### Display Output Notes

- The nvidia driver on AGX Orin exposes a single DRM connector: `card1-DP-1` (DisplayPort)
- GDM renders to this connector at 1920x1080
- GDM uses `-displayfd` so verify display number with:
  ```bash
  cat /proc/$(pgrep -o gnome-shell)/environ | tr '\0' '\n' | grep DISPLAY
  ```

### VNC Remote Desktop Access

If the KVM shows a black screen after boot, use VNC to access the GNOME desktop remotely.

**On the device** (transient install, lost on reboot):

```bash
# 1. Install EPEL and x11vnc (should be hundled by the jumpstarter/Containerfile-gui)
dnf install --transient -y https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm
dnf install --transient -y x11vnc

# 2. Find the actual display number and Xauthority path
export XDISPLAY=$(cat /proc/$(pgrep -o gnome-shell)/environ | tr '\0' '\n' | grep DISPLAY | cut -d= -f2)
# or if gnome-shell directory not exists
export XDISPLAY=$(ls /tmp/.X11-unix/ | sed 's/X/:/') # probebly without gnome-shell directory will fail
export XAUTH=$(ps aux | grep Xorg | grep -oP '(?<=-auth )\S+')

# 3. Start x11vnc
x11vnc -display $XDISPLAY -auth $XAUTH -forever -noshm -passwd $JETSON_PASSWORD -rfbport 5900 &
```
-> **Note:** example : x11vnc -display $XDISPLAY -auth $XAUTH -forever -noshm -passwd 123 -rfbport 5900 &

**From your local machine** (SSH tunnel + VNC client):

```bash
# Terminal 1: SSH tunnel
ssh -L 5900:localhost:5900 root@<device-hostname>

# Terminal 2: Connect VNC
# Mac:
open vnc://localhost:5900
# Linux:
vncviewer localhost:5900
```

> **Note:** The Xauthority path is `/run/user/0/gdm/Xauthority` (uid 0 = root).
> Direct VNC without SSH tunnel may fail if port 5900 is firewalled.

---

## What wrapper.py Does

1. Connects storage to DUT and power-cycles the device
2. Waits for boot (serial console login prompt)
3. Enables `PermitRootLogin yes` via serial console (if password auth is used)
4. Opens SSH tunnel via Jumpstarter port forwarding
5. Runs `bootc-generic-growpart` to expand the root partition
6. Launches `pytest` with `JETSON_HOST`/`JETSON_PORT` set to the forwarded address

### SSH Auth Priority

| JETSON_KEY_PATH | JETSON_PASSWORD | Behavior |
|---|---|---|
| set | set | Key first → password fallback |
| set | not set | Key only |
| not set | set | Password only |
