# Jetson RPM Tests

This directory contains pytest-based tests for Jetson RPMs using SSH connections via fabric (high-level paramiko wrapper).

## Jetson Structure
- HARDWARE ACCELERATORS: GPU (CUDA), DLA (AI), PVA (Vision), VIC (Video Image Compositor), Video Enc/Dec
- INTERFACES: CSI Camera, USBs, PCIe, Ethernet, CAN bus, Display
- SOFTWARE FRAMEWORKS: GStreamer (MultiMedia), TensorRT (For optimal running of AI on pytorch or TensorFlow frameworks on DLA or GPU hardwares), VPI (Vision)

## Repository Tests Structure

```
infra_tests/                    # infrastructure for the tests (e.g SSH)
├── ssh_client.py               # SSHConnection class using fabric
└── __init__.py

tests_resources/                # Shared utilities/functions for all tests suites
├── container_ops.py            # Container build/run utilities (general, works with any Dockerfile)
├── device_ops.py               # Device management utilities
├── device_logs_collector.py    # Diagnostic log collection
└── hardware_info.py            # Collect hardware and system information from a Jetson device.

tests_suites/
├── conftest.py                 # Shared fixtures + L4T image pre-pull
├── jetson_hardware_specs.yaml  # Jetson hardware expected values per test category
├── bootc/                      # Bootc image switch lifecycle tests (switch, reboot, verify)
├── kmod/                       # Kernel module (nvidia-jetpack-kmod)
├── cuda/                       # CUDA + cuDNN tests (PyTorch container + TensorFlow container + L4T container with outsource cuda-samples)
├── dla/                        # DLA + TensorRT tests (TensorRT container + L4T container, GPU + DLA cores)
├── pva/                        # PVA/VPI tests (L4T container, 19 VPI samples)
├── vic/                        # VIC tests (format conversion, scaling, rotation, crop via nvvidconv)
├── multimedia/                 # Multimedia tests (native GStreamer + L4T MMAPI)
├── usbs/                       # USB tests
├── pcis/                       # PCI tests
├── can_bus/                    # CAN bus tests
├── csi_camera/                 # CSI camera tests
├── display/                    # Display tests (X11, DRM/GBM, Wayland)
├── tools/                      # nvidia-jetpack-tools tests (nvpmodel, nvfancontrol)
├── sanity/                     # General Sanity verification (e.g Version/Signature checks)
└── ethernet/                   # Ethernet tests
```

## Requirements

```bash
pip install -r requirements.txt
```

## Configuration

Tests can be configured via environment variables:

- `JETSON_HOST`: Hostname or IP address of the Jetson device name
- `JETSON_USERNAME`: SSH username
- `JETSON_PASSWORD`: SSH password
- `JETSON_KEY_PATH`: Path to private key, e.g. ~/.ssh/id_rsa (use when auth is key-based)
- `JETSON_PORT`: SSH port (default: 22)

L4T container tests can be configured via:

- `L4T_JETPACK_IMAGE`: L4T container image (default: `nvcr.io/nvidia/l4t-jetpack:r36.4.0`)
- `CUDA_SAMPLES_VERSION`: cuda-samples git tag (default: `v12.9`)

## Running Tests

Run all tests:
```bash
pytest tests_suites/
```

Run tests for a specific component:
```bash
pytest tests_suites/cuda/
pytest tests_suites/dla/
pytest tests_suites/pva/
pytest tests_suites/multimedia/
pytest tests_suites/vic/
```

Run extra tests, along with basic tests (basic tests runs in Konflux/CI):
```bash
pytest --run-extra tests_suites/
```

Run only extra tests (marked with @pytest.mark.extra):
```bash
pytest -m extra --run-extra tests_suites/ 
```

Run only critical tests (marked with @pytest.mark.critical):
```bash
pytest -m critical tests_suites/
```

### Bootc Switch Tests

Run bootc switch tests to verify the system can switch to a new image, survive a reboot, and come back healthy:
```bash
pytest tests_suites/bootc/ --bootc-switch-image=<new_image>
```

Run the full upgrade-then-test pipeline (bootc switch first, then all hardware tests):
```bash
pytest tests_suites/bootc/ tests_suites/ \
    --bootc-switch-image=quay.io/redhat-user-workloads/jetpack-for-rhel-tenant/rhel-98-bootc:<new_tag>
```

The bootc directory is listed first so the switch and reboot complete before the rest of the suite starts. All subsequent tests open fresh SSH connections and run against the new image.

## Limitations (expected skips and warnings)

Skips are **not automatically failures**. On a stock RHEL 9.8 bootc image (`multi-user.target`, JetPack 6.2.x / L4T 36.5.x, no GUI, camera kmods blacklisted) a typical full run will skip or warn on the items below. Hardware that *is* present is still asserted (sysfs, device tree, GPU, VIC, SC7, etc.).

### Opt-in tests (skipped unless you pass a flag)

| Suite | Why it skips | How to run it |
|-------|----------------|---------------|
| `bootc/` switch tests | Destructive image switch + reboot | `pytest tests_suites/bootc/ --bootc-switch-image=<image>` |
| `@pytest.mark.extra` | Reserved for future long/optional tests; **SC7 is not extra** | `pytest --run-extra tests_suites/` |

### Stock bootc image (no desktop)

| Suite | Tests | Why |
|-------|--------|-----|
| Display | DRM connector status, X11, Wayland compositor | Base image is `multi-user.target`. Xorg/GDM/Wayland only exist on the GUI variant (`graphical.target` + GDM). |
| Display | DRM `/dev/dri`, tegra_drm, Wayland libs | These **do** run on multi-user (kernel display, RPM libs). |

### Packages not in enabled repos

| Suite | Tests | Why |
|-------|--------|-----|
| ISP | `v4l2-ctl`, `media-ctl`, `test_v4l2_utils_installed` | `v4l-utils` is **not** in RHEL 9.8 bootc AppStream on aarch64 (`dnf: No match for argument: v4l-utils`). ISP hardware is still covered by `TestISPSysfs` and `TestISPDeviceTree`. |

### Pinmux / physical setup

| Suite | Tests | Why |
|-------|--------|-----|
| SPI | `spidev` module, `/dev/spidev*`, loopback | `/dev/spidev*` needs a device-tree overlay (`jetson-io.py`). Loopback also needs a MOSI–MISO jumper on the 40-pin header. |
| CAN | loopback | Skips if every CAN interface is already UP (needs a free interface). |

### Camera / ISP kernel modules (warnings, not skips)

On RHEL 9, camera kmods (`tegra_camera`, `nvhost_isp`, `nvcsi`, `tegra_vi`, …) are **blacklisted** in `nvidia-camera.conf` (RHEL-56474 / not GA for MIPI CSI). `TestISPDevice`, `TestISPDriver`, and CSI camera tests **warn** when `/dev/nvhost-isp*`, `/dev/video*`, or modules are missing instead of failing. Device-tree and sysfs tests still **assert**.

### NGC / container images

| Topic | Limitation |
|-------|------------|
| L4T JetPack container | NGC has **no `r36.5.x`** (or `r39.x` / JetPack 7). Host L4T 36.5.x uses `nvcr.io/nvidia/l4t-jetpack:r36.4.0` (newer host driver + older container userspace). Published tags: `r36.4.0`, `r36.3.0`, `r36.2.0`, `r35.4.1`, `r35.3.1`, `r35.2.1`, `r35.1.0`. Override with `L4T_JETPACK_IMAGE`. |
| DeepStream | Default is `nvcr.io/nvidia/deepstream:7.1-samples-multiarch` (Jetson samples). Version, plugins, `nvvideoconvert`, and `nvstreammux` run. Sample **inference** may fail: this image often has neither `nvv4l2decoder` nor `avdec_h264`. The dGPU Triton image (`7.1-triton-multiarch`) prints driver `560.28+ UNAVAILABLE` on L4T; that banner is ignored, not used as a skip. Set `DEEPSTREAM_IMAGE` to a Jetson `deepstream-l4t` tag if you need full inference. |
| L4T image pull | Only CUDA/DLA/PVA/MMAPI fixtures pull `l4t-jetpack`. SC7/RTC/ISP do not. |

### Hardware / product spec

| Suite | Why it skips |
|-------|----------------|
| DLA `trtexec --useDLACore` | `dla.supported: false` in `jetson_hardware_specs.yaml` (e.g. Orin Nano). |
| VIC encode tests | `video_enc.supported: false` on that platform. |
| PCIe speed tests | No `capable_speed` / PCIe spec for that model. |
| SC7 | No wakealarm RTC, or kernel has no `mem_sleep=deep`. |

### Session-level (entire pytest run skipped)

`hardware_info_session` skips the **whole session** if the model is not in
`jetson_hardware_specs.yaml`, RHEL/JetPack is missing, or the detected UEFI
firmware does not match the firmware target for the detected JetPack release.
RHEL, L4T, and kernel versions are reported in the setup summary but do not
block latest/sidecar image testing. The legacy `--target-kernel-version`
option remains accepted for command-line compatibility.

### Other environment skips

| Suite | Why |
|-------|-----|
| Sanity root-password | Skipped on Jumpstarter (`config.toml`) and stage builds. |
| Sanity stage-only RPMs | Skipped when the image is not a stage build. |
| Tools `nvfancontrol` | Skipped if the binary is not on `PATH`. |

Warnings vs skips: see [WARNING_BEHAVIOR.md](WARNING_BEHAVIOR.md).

## How to Warn

for more information look at tests_suites/WARNING_BEHAVIOR.md

## Test Markers

- `@pytest.mark.critical`: Critical tests that must pass
- `@pytest.mark.xfail`: Tests that are expected to fail on certain hardware
- `@pytest.mark.extra`: Extra tests, skipped by default (run with `--run-extra`)

## Hardware / System Variables (for developers)

When running pytest, the session collects hardware and system info from the Jetson via SSH and exposes the following variables to all tests. **All variables default to `None` if the value is not found.** You can import them from `conftest` and use them to skip or adapt tests by RHEL version, Jetpack, firmware, bootc, RPMs, etc.

| Variable | Type | Description |
|----------|------|-------------|
| `RHEL_VERSION` | str or None | RHEL version as string (e.g. `'9.7'`, `'9.10'` from `/etc/redhat-release`). |
| `L4T_VERSION` | float, str, or None | L4T version from `/etc/nv_tegra_release`: str if X.Y.Z (e.g. `'36.5.0'`), float if X.Y. |
| `JETPACK_VERSION` | str or None | JetPack userspace RPM version (e.g. `'6.2.2'` from `nvidia-jetpack-*-core` RPM). |
| `JETPACK_KMOD_VERSION` | str or None | JetPack kmod RPM version (e.g. `'6.2.2'` from `nvidia-jetpack-*-kmod` RPM). |
| `FIRMWARE_VERSION` | float, str, or None | Firmware version: str if X.Y.Z (2 dots), float if X.Y (1 dot). |
| `FIRMWARE_TYPE` | str or None | Firmware type (e.g. `UEFI`, `BIOS`). |
| `HARDWARE_MODEL_NAME` | str or None | Hardware model name. |
| `KERNEL_VERSION` | str or None | Kernel version (e.g. `uname -r`). |
| `CPU_ARCH` | str or None | CPU architecture (e.g. `aarch64`, `x86_64`). |
| `BOOTC_AVAILABLE` | bool | Whether bootc / rpm-ostree is available (default False). |
| `BOOTC_VERSION` | float, str, or None | Bootc version: str if X.Y.Z, float if X.Y (only if bootc is available). |
| `BOOTC_IMAGE_VERSION` | str or None | Bootc image version, including last modify date (only if bootc is available). |
| `BOOTC_IMAGE_URL` | str or None | Bootc image URL including tag (only if bootc is available). |

**Example usage in a test:**

```python
from tests_suites import conftest as _conftest

def test_something():
    if not _conftest.RPMS_AVAILABLE:
        pytest.skip("nvidia-jetpack: need all 23 RPMs installed with same version")
    # ...
```

At the start of each pytest run, a **SETUP** block is printed with: RHEL version, Jetpack version, firmware type and version, hardware model name, whether bootc is available, and whether the nvidia-jetpack RPM is available.
