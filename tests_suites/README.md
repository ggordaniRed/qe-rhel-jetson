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
