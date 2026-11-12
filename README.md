# qe-rhel-jetson

Pytest-based hardware test suite for NVIDIA Jetson devices on RHEL, with deployment automation via Beaker and Jumpstarter.

## Jetson Structure

- HARDWARE ACCELERATORS: GPU (CUDA), DLA (AI), PVA (Vision), VIC (Video Image Compositor), Video Enc/Dec
- INTERFACES: CSI Camera, USBs, PCIe, Ethernet, CAN bus, Display
- SOFTWARE FRAMEWORKS: GStreamer (MultiMedia), TensorRT (AI), VPI (Vision)

## Installation

```bash
pip install -r requirements.txt
```

## Testing Farm Dashboard and Workflow

The GitHub Actions workflow **Testing Farm Jetson E2E** submits the existing
`/testing-farm/beaker-e2e` plan from the private GitLab repository. It runs only
when manually dispatched because it reserves and reprovisions a real Beaker
Jetson. The dashboard refreshes from completed Testing Farm request artifacts,
parses pytest logs/JUnit results, and publishes `docs/jetson_test_matrix.html`.

Configure these GitHub repository secrets before dispatching the test workflow:
`TESTING_FARM_API_TOKEN`, `BEAKER_USERNAME`, `BEAKER_PASSWORD`,
`JETSON_SSH_KEY`, `GITLAB_USERNAME`, and `GITLAB_TOKEN`. Add `REGISTRY_USER` and
`REGISTRY_PASS` if the selected image needs registry authentication. The
`TESTING_FARM_CA_BUNDLE` secret is optional and can contain the Red Hat CA PEM
bundle used to validate artifact storage TLS. Artifact downloads also need a
runner with access to `artifacts.osci.redhat.com` (usually on the Red Hat VPN).
Set the repository variable `TESTING_FARM_DASHBOARD_RUNNER` to that runner's
label; it defaults to `ubuntu-latest`.

## Configuration

Tests are configured via environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `JETSON_HOST` | yes | Hostname or IP address |
| `JETSON_USERNAME` | yes | SSH username |
| `JETSON_PASSWORD` | one of these | SSH password |
| `JETSON_KEY_PATH` | one of these | SSH private key path (e.g. `~/.ssh/id_ed25519`) |
| `JETSON_PORT` | no (default: 22) | SSH port |

Auth priority: `JETSON_KEY_PATH` is tried first, `JETSON_PASSWORD` is the fallback.

## Quick Start — What Do You Want to Do? (Which README to choose)

```
┌─────────────────────────────────────────────────────┐
│           How are you deploying & testing?           │
└──────────────┬───────────────────────┬───────────────┘
               │                       │
       ┌───────▼───────┐       ┌───────▼───────┐
       │    Beaker      │       │  Jumpstarter   │
       │ (lab machines) │       │ (edge devices) │
       └───────┬───────┘       └───────┬───────┘
               │                       │
    ┌──────────▼──────────┐  ┌─────────▼─────────┐
    │ 1. Reserve machine  │  │ 1. Build .raw.xz  │
    │ 2. Deploy bootc/RPM │  │ 2. Flash via jmp   │
    │ 3. Run tests (SSH)  │  │ 3. Run tests       │
    └──────────┬──────────┘  └─────────┬─────────┘
               │                       │
               └───────────┬───────────┘
                           │
                  ┌────────▼────────┐
                  │   pytest tests   │
                  │  tests_suites/   │
                  └─────────────────┘
```

| Path | Guide | What It Covers |
|------|-------|----------------|
| **Beaker** | [beaker/README.md](beaker/README.md) | Reserve a Jetson in the lab, deploy bootc image or JetPack RPMs via Ansible, run tests over SSH |
| **Jumpstarter** | [jumpstarter/README.md](jumpstarter/README.md) | Build a disk image, flash to a Jetson via Jumpstarter, run tests (manual or automated with wrapper.py) |
| **Tests** | [tests_suites/README.md](tests_suites/README.md) | Test suite details, hardware variables, markers, per-component test info |

## Repository Structure

```
qe-rhel-jetson/
├── tests_suites/               # Pytest test suites (per hardware component)
│
├── infra_tests/                # SSH infrastructure, Collect hardware/system info from device via SSH
│
├── tests_resources/            # Shared utilities/functions for all tests suites
│
├── beaker/                     # Beaker reservation & deployment automation
│   ├── scripts/                # CLI tools (reserve_jetson.py)
│   └── ansible/                # Ansible playbooks (bootc install, RPM install)
│
├── jumpstarter/                # Jumpstarter integration for hardware testing
│   └── wrapper.py              # Flash existing image & test via Jumpstarter framework
│   └── container_images.yaml   # container images to pre-pull before pytest execution           
│
└── .github/workflows/          # Manual Testing Farm submit + dashboard refresh
    ├── beaker-test.yml         # Submit the Beaker E2E plan to Testing Farm
    └── generate-matrix.yml     # Parse Testing Farm artifacts and deploy Pages
```

## Running Tests

```bash
pytest tests_suites/                                          # all tests
pytest tests_suites/cuda/                                     # specific component
pytest tests_suites/ -v                                       # verbose output
pytest tests_suites/ --target-kernel-version=5.14.0-687.12.1  # To override expected kernel version, This avoids editing `jetson_hardware_specs.yaml`
```

Expected skips on stock RHEL 9.8 bootc (no GUI, no `v4l-utils`, SPI pinmux off, bootc switch opt-in) are listed under **Limitations** in [tests_suites/README.md](tests_suites/README.md).
