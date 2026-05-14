# Beaker Integration for Jetson Testing

This folder contains tools for automating Jetson system reservations via Red Hat Beaker,
deploying bootc images with Ansible, and running tests.

## Overview

The workflow is:
1. **Authenticate** to Beaker (Kerberos or Username/Password)
2. **Reserve** a Jetson system via Beaker job submission
3. **Wait** for the system to be provisioned with RHEL
4. **Deploy** bootc image with NVIDIA drivers via Ansible
5. **Run Tests** via SSH using the qejetson test suite

## Structure

```
beaker/
├── pybeaker/              # Python library for Beaker API
│   ├── client.py          # HTTP API client (Kerberos + Password auth)
│   ├── config.py          # Configuration management
│   ├── job_builder.py     # Fluent job XML builder
│   └── cli.py             # bkr CLI wrapper
│
├── scripts/               # Command-line tools
│   ├── _common.py         # Shared authentication utilities
│   └── reserve_jetson.py  # Submit Beaker job for Jetson
│
├── ansible/               # Ansible playbooks
│   ├── install_bootc.yml  # Bootc image deployment playbook
│   ├── install_jetpack_rpms.yml  # JetPack RPM installation playbook
│   ├── inventory.yml      # Target hosts
│   ├── ansible.cfg        # Ansible configuration
│   └── vars/
│       └── secrets.yml.example
│
└── requirements.txt       # Python dependencies
```

## Authentication Methods

The pybeaker client supports two authentication methods:

### Option 1: Username/Password (Recommended for CI)

```bash
export BEAKER_HUB_URL="https://beaker.engineering.redhat.com"
export BEAKER_AUTH_METHOD="password"
export BEAKER_USERNAME="your_username"
export BEAKER_PASSWORD="your_password"
export BEAKER_SSL_VERIFY="false"
export REGISTRY="registry.gitlab.com"
export JETSON_HOST="nvidia-jetson-agx-orin-05.khw.eng.bos2.dc.redhat.com"
export BOOTC_IMAGE_BASEURL="registry.gitlab.com/redhat/rhel/sst/orin-sidecar/nvidia-jetson-sidecar/rhel-9.7"
export BOOTC_IMAGE_TAG="411ed591" #choose the tag from here https://gitlab.com/redhat/rhel/sst/orin-sidecar/nvidia-jetson-sidecar/container_registry/
export RESERVATION_HOURS="99" #put between 3-99
```

### Option 2: Kerberos (Recommended for local development)

```bash
export BEAKER_HUB_URL="https://beaker.engineering.redhat.com"
export BEAKER_AUTH_METHOD="krbv"
export BEAKER_KRB_REALM="IPA.REDHAT.COM"
export BEAKER_SSL_VERIFY="false"
export REGISTRY="registry.gitlab.com"
export JETSON_HOST="nvidia-jetson-agx-orin-05.khw.eng.bos2.dc.redhat.com"
export BOOTC_IMAGE_BASEURL="registry.gitlab.com/redhat/rhel/sst/orin-sidecar/nvidia-jetson-sidecar/rhel-9.7"
export BOOTC_IMAGE_TAG="411ed591" #choose the tag from here https://gitlab.com/redhat/rhel/sst/orin-sidecar/nvidia-jetson-sidecar/container_registry/
export RESERVATION_HOURS="99" #put between 3-99

# Get Kerberos ticket first
kinit your-username@IPA.REDHAT.COM
```

## Quick Start

### 1. Set Environment Variables

Choose your authentication method (see above).

### 2. Reserve a Jetson System

! please make sure the lab is available/free: 
https://beaker.engineering.redhat.com/?systemsearch-0.table=System%2FModel&systemsearch-0.keyvalue=&systemsearch-0.operation=contains&systemsearch-0.value=orin&systemsearch_column_System%2FArch=System%2FArch&systemsearch_column_System%2FLoanedTo=System%2FLoanedTo&systemsearch_column_System%2FModel=System%2FModel&systemsearch_column_System%2FName=System%2FName&systemsearch_column_System%2FStatus=System%2FStatus&systemsearch_column_System%2FType=System%2FType&systemsearch_column_System%2FUser=System%2FUser&systemsearch_column_System%2FVendor=System%2FVendor

```bash
cd beaker
python3 scripts/reserve_jetson.py --target $JETSON_HOST --hours $RESERVATION_HOURS
```

### 3. Set Up Ansible Vault (First Time Only)

```bash
cd ansible
ansible-vault create vars/secrets.yml
# Write:
# registry_user: "Your Name"
# registry_pass: "glpat-your-personal-gitlab-token"
# rhsm_username: "rh-ee-youruser"
# rhsm_password: "your_kerberos_password"
```

For changing the current vars:

```bash
cd ansible
ansible-vault edit vars/secrets.yml
# should know the vault password
```

### 4. Deploy Bootc Image (You have Alternative deployment next Step)

Run the following command for overriding the default target host, and image tag/hash 
(base URL unchanged, from the gitlab nvidia-jetson-sidecar repo)

```bash
cd ansible
ansible-playbook -i inventory.yml install_bootc.yml --ask-vault-pass \
-e "target_host=${JETSON_HOST}" \
-e "registry_url=${REGISTRY}" \
-e "bootc_image_tag=${BOOTC_IMAGE_TAG}" \
-e "bootc_image_base=${BOOTC_IMAGE_BASEURL}" \
-e "reservation_hours=${RESERVATION_HOURS}"
```

### 5. Deploy JetPack RPMs (Alternative to Bootc)

Instead of deploying a bootc image, you can install JetPack RPMs directly on a freshly provisioned RHEL system.

**Basic usage:**

```bash
cd ansible
ansible-playbook -i inventory.yml install_jetpack_rpms.yml \
  -e "target_host=${JETSON_HOST}"
```

**Using ansible-vault for subscription credentials (recommended):**

You can store `rhsm_username` and `rhsm_password` in `vars/secrets.yml` (see Step 3) and use:

```bash
ansible-playbook -i inventory.yml install_jetpack_rpms.yml --ask-vault-pass \
  -e "target_host=${JETSON_HOST}" \
  -e "reservation_hours=${RESERVATION_HOURS}" \
```

**With Red Hat subscription credentials:**

```bash
ansible-playbook -i inventory.yml install_jetpack_rpms.yml \
  -e "target_host=${JETSON_HOST}" \
  -e "rhsm_username=rh-ee-youruser" \
  -e "rhsm_password=your_kerberos_password"
```

**Skip subscription (if already registered):**

```bash
ansible-playbook -i inventory.yml install_jetpack_rpms.yml \
  -e "target_host=${JETSON_HOST}" \
  -e "skip_subscription=true"
```

**Available options:**

| Variable | Default | Description |
|----------|---------|-------------|
| `target_host` | (required) | Target Jetson FQDN |
| `rhsm_username` | - | Red Hat subscription username |
| `rhsm_password` | - | Red Hat subscription password |
| `skip_subscription` | `false` | Skip subscription registration |
| `auto_reboot` | `true` | Reboot after installation to load kernel modules |
| `set_max_performance` | `false` | Set nvpmodel to MAXN mode |
| `install_x11` | `false` | Install X11/Xorg packages for display testing |
| `regenerate_initramfs` | `true` | Regenerate initramfs with dracut |
| `reservation_hours` | `24` | Beaker reservation hours (boot order restores 2h before end) |

**What the playbook does:**

1. Registers system with Red Hat subscription (optional)
2. Adds RHEL AppStream and BaseOS repositories
3. Adds NVIDIA JetPack repository
4. Installs JetPack RPMs (`dnf repository-packages nvidia-jetpack-el9 install`)
5. Installs NVIDIA Container Toolkit
6. Configures EFI boot order (RHEL first, then restores PXE for Beaker)
7. Sets up CDI generation service (`nvidia-cdi.service`)
8. Installs additional packages (podman, can-utils, usbutils, EPEL)
9. Sets graphical target and regenerates initramfs
10. Reboots to load kernel modules
11. Verifies installation with `nvidia-smi`
12. Restore Boot order to original

**Dry run:**

```bash
ansible-playbook -i inventory.yml install_jetpack_rpms.yml \
  -e "target_host=${JETSON_HOST}" --check
```

### Optional: Enable Graphical Desktop (GDM)

The base bootc image does not include GDM and defaults to multi-user.target. To enable graphical desktop, you have two options:

1. **Build a GUI container image** using `jumpstarter/Containerfile-gui` (layers GDM + GNOME on top of the base bootc image, sets graphical.target) — then use that image in the podman run command below.
2. **Use the podman run command below** with a pre-built GUI variant image that already includes GDM.

The graphical.target is only for enabling the desktop environment — it is not the default bootc target.

```bash
podman run --rm --privileged \
  -v /dev:/dev \
  -v /var/lib/containers:/var/lib/containers \
  -v /:/target \
  --pid=host \
  --security-opt label=type:unconfined_t \
  <gui-variant-image> \
  /bin/bash -c '
    bootc install to-existing-root \
      --root-ssh-authorized-keys=/target/root/.ssh/authorized_keys \
      --skip-fetch-check
    systemctl --root=/target enable gdm.service || true
    systemctl --root=/target set-default graphical.target || true
  '
```

The Jetson should boot with a graphical desktop environment (GDM login screen).

### 6. Run Tests

```bash
cd ../..  # Back to qejetson root
export JETSON_HOST="nvidia-jetson-agx-orin-05.khw.eng.bos2.dc.redhat.com"
export JETSON_USERNAME="root"
export JETSON_KEY_PATH="~/.ssh/id_rsa"  # or use JETSON_PASSWORD

pytest tests_suites/
```

## GitHub Workflow

See `.github/workflows/beaker-test.yml` for the automated CI/CD pipeline.

### Required GitHub Secrets

For **Password Authentication** (recommended for CI):
| Secret | Description |
|--------|-------------|
| `BEAKER_USERNAME` | Beaker username |
| `BEAKER_PASSWORD` | Beaker password |
| `GITLAB_REGISTRY_USER` | GitLab registry username |
| `GITLAB_REGISTRY_TOKEN` | GitLab registry token (glpat-xxx) |
| `SSH_PRIVATE_KEY` | SSH private key for Jetson access |

For **Kerberos Authentication** (alternative):
| Secret | Description |
|--------|-------------|
| `BEAKER_KEYTAB_BASE64` | Base64-encoded Kerberos keytab |
| `BEAKER_PRINCIPAL` | Kerberos principal (e.g., user@IPA.REDHAT.COM) |
| `GITLAB_REGISTRY_USER` | GitLab registry username |
| `GITLAB_REGISTRY_TOKEN` | GitLab registry token |
| `SSH_PRIVATE_KEY` | SSH private key for Jetson access |

### Setting Up Secrets

1. Go to your GitHub repository → Settings → Secrets and variables → Actions
2. Click "New repository secret"
3. Add each required secret

**For Kerberos keytab:**
```bash
# Generate base64-encoded keytab
cat /path/to/your.keytab | base64 -w0
# Copy the output and paste as BEAKER_KEYTAB_BASE64 secret
```

### Running the Workflow

1. Go to Actions tab in GitHub
2. Select "Beaker Jetson Testing"
3. Click "Run workflow"
4. Configure options:
   - **Target**: Jetson FQDN
   - **Distro**: RHEL distro version
   - **Auth method**: `password` or `krbv`
   - **Reservation hours**: Duration
   - **Bootc image**: Image to deploy

### Security Features

The workflow implements these security best practices:

1. **Secrets Management**: All credentials stored in GitHub Secrets
2. **Masked Logs**: Credentials never appear in workflow logs
3. **Secure Cleanup**: Sensitive files deleted after use
4. **Minimal Permissions**: SSH keys with strict permissions (600)
5. **No Hardcoded Values**: All auth values from secrets

## Python API Usage

```python
from pybeaker import BeakerClient, BeakerConfig

# Password authentication
config = BeakerConfig(
    hub_url="https://beaker.engineering.redhat.com",
    auth_method="password",
    username="your_username",
    password="your_password",
    ssl_verify=False,
)

# Or Kerberos authentication
config = BeakerConfig(
    hub_url="https://beaker.engineering.redhat.com",
    auth_method="krbv",
    krb_realm="IPA.REDHAT.COM",
    ssl_verify=False,
)

client = BeakerClient(config)

# Check who you are
user = client.whoami()
print(f"Logged in as: {user}")

# Submit a job
job_id = client.submit_job(job_xml)
print(f"Submitted: {job_id}")

# Check job status
status = client.get_job_status(job_id)
print(f"Status: {status.status}")
```

## Troubleshooting

### Password Authentication Failed

If you get "Invalid username or password":
1. Verify your credentials are correct
2. Some Beaker instances only support Kerberos - try `BEAKER_AUTH_METHOD=krbv`

### Kerberos Authentication Failed

```bash
# Check if ticket exists
klist

# Get new ticket
kinit your-username@IPA.REDHAT.COM

# Verify krb5.conf is configured
cat /etc/krb5.conf
```

### SSH Connection Issues

```bash
# Remove old host key after system reprovisioning
ssh-keygen -R nvidia-jetson-agx-orin-05.khw.eng.bos2.dc.redhat.com

# Test connection with verbose output
ssh -vvv root@nvidia-jetson-agx-orin-05.khw.eng.bos2.dc.redhat.com
```

### GitHub Actions Debugging

1. Check the "Set authentication environment" step for auth validation
2. Look at "Reserve Jetson via Beaker" step for Beaker errors
3. Check artifacts for test reports and debug info

## License

MIT License
