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
│
└── .github/workflows/          # CI/CD - IN PROGRESS (Blocked by Firewall issues)
    └── beaker-test.yml         # Reserve Beaker machine, deploy, run tests
```

## Running Tests

```bash
pytest tests_suites/              # all tests
pytest tests_suites/cuda/         # specific component
pytest tests_suites/ -v           # verbose output
```
