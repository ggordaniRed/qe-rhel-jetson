#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

WORK_DIR="$(mktemp -d)"
SSH_KEY_FILE=""
ANSIBLE_SECRETS_FILE=""
ANSIBLE_CONNECTION_VARS_FILE=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

cleanup() {
    if [[ -n "${SSH_KEY_FILE}" ]]; then
        rm -f "${SSH_KEY_FILE}"
    fi
    if [[ -n "${ANSIBLE_SECRETS_FILE}" ]]; then
        rm -f "${ANSIBLE_SECRETS_FILE}"
    fi
    if [[ -n "${ANSIBLE_CONNECTION_VARS_FILE}" ]]; then
        rm -f "${ANSIBLE_CONNECTION_VARS_FILE}"
    fi
    rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

: "${JETSON_HOST:?JETSON_HOST is required}"
: "${JETSON_USERNAME:?JETSON_USERNAME is required}"
if [[ -z "${JETSON_PASSWORD:-}" && -z "${SSH_PRIVATE_KEY:-}" ]]; then
    if [[ -z "${SSH_PRIVATE_KEY_B64:-}" ]]; then
        echo "JETSON_PASSWORD or SSH_PRIVATE_KEY is required" >&2
        exit 2
    fi
fi

if [[ -n "${SSH_PRIVATE_KEY_B64:-}" ]]; then
    SSH_PRIVATE_KEY="$(printf '%s' "${SSH_PRIVATE_KEY_B64}" | base64 --decode)"
    export SSH_PRIVATE_KEY
fi

if [[ -z "${JETSON_PASSWORD:-}" && -z "${SSH_PRIVATE_KEY:-}" ]]; then
    echo "JETSON_PASSWORD or SSH_PRIVATE_KEY is required" >&2
    exit 2
fi

QE_REPO_URL="${QE_REPO_URL:-https://github.com/rh-ecosystem-edge/qe-rhel-jetson.git}"
QE_REPO_REF="${QE_REPO_REF:-main}"
PYTHON="${PYTHON:-python3}"

if [[ -n "${SSH_PRIVATE_KEY:-}" ]]; then
    SSH_KEY_FILE="${WORK_DIR}/id_jetson"
    printf '%s\n' "${SSH_PRIVATE_KEY}" > "${SSH_KEY_FILE}"
    chmod 600 "${SSH_KEY_FILE}"
    export JETSON_KEY_PATH="${SSH_KEY_FILE}"
fi

if [[ "${ANSIBLE_BOOTC:-0}" == "1" || "${ANSIBLE_BOOTC:-0}" == "true" ]]; then
    : "${BOOTC_IMAGE_BASE:?BOOTC_IMAGE_BASE is required when ANSIBLE_BOOTC=1}"
    : "${BOOTC_IMAGE_TAG:?BOOTC_IMAGE_TAG is required when ANSIBLE_BOOTC=1}"
    : "${REGISTRY_USER:?REGISTRY_USER is required when ANSIBLE_BOOTC=1}"
    : "${REGISTRY_PASSWORD:?REGISTRY_PASSWORD is required when ANSIBLE_BOOTC=1}"

    ANSIBLE_SECRETS_FILE="${WORK_DIR}/ansible-secrets.yml"
    ANSIBLE_SECRETS_FILE="${ANSIBLE_SECRETS_FILE}" "${PYTHON}" - <<'PY'
import os
from pathlib import Path
import yaml

path = Path(os.environ["ANSIBLE_SECRETS_FILE"])
path.write_text(yaml.safe_dump({
    "registry_user": os.environ["REGISTRY_USER"],
    "registry_pass": os.environ["REGISTRY_PASSWORD"],
}, default_flow_style=False))
os.chmod(path, 0o600)
PY

    echo "[testing-farm] Deploying bootc with Ansible"
    ansible_args=(
        -i "${QE_ROOT}/beaker/ansible/inventory.yml"
        "${QE_ROOT}/beaker/ansible/install_bootc.yml"
        -e "target_host=${JETSON_HOST}"
        -e "ansible_user=${JETSON_USERNAME}"
        -e "bootc_image_base=${BOOTC_IMAGE_BASE}"
        -e "bootc_image_tag=${BOOTC_IMAGE_TAG}"
        -e "registry_url=${REGISTRY_URL:-registry.gitlab.com}"
        -e "ansible_secrets_file=${ANSIBLE_SECRETS_FILE}"
        -e "auto_reboot=${ANSIBLE_AUTO_REBOOT:-true}"
        -e "restore_boot_order=${ANSIBLE_RESTORE_BOOT_ORDER:-true}"
        -e "reservation_hours=${ANSIBLE_RESERVATION_HOURS:-24}"
    )
    if [[ -n "${SSH_KEY_FILE}" ]]; then
        ansible_args+=("-e" "ansible_ssh_private_key_file=${SSH_KEY_FILE}")
    fi
    if [[ -n "${JETSON_PASSWORD:-}" ]]; then
        ANSIBLE_CONNECTION_VARS_FILE="${WORK_DIR}/ansible-connection-vars.yml"
        ANSIBLE_CONNECTION_VARS_FILE="${ANSIBLE_CONNECTION_VARS_FILE}" "${PYTHON}" - <<'PY'
import os
from pathlib import Path
import yaml

path = Path(os.environ["ANSIBLE_CONNECTION_VARS_FILE"])
path.write_text(yaml.safe_dump({
    "ansible_password": os.environ["JETSON_PASSWORD"],
    "ansible_become_password": os.environ["JETSON_PASSWORD"],
}, default_flow_style=False))
os.chmod(path, 0o600)
PY
        ansible_args+=("-e" "@${ANSIBLE_CONNECTION_VARS_FILE}")
    fi
    ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook "${ansible_args[@]}"
fi

if [[ "${SSH_SMOKE_ONLY:-0}" == "1" ]]; then
    echo "[testing-farm] Running SSH smoke test only"
    "${PYTHON}" "${SCRIPT_DIR}/ssh_smoke.py"
    exit 0
fi

echo "[testing-farm] Cloning qe-rhel-jetson ref ${QE_REPO_REF}"
git clone --depth 1 --branch "${QE_REPO_REF}" "${QE_REPO_URL}" "${WORK_DIR}/qe-rhel-jetson"

cd "${WORK_DIR}/qe-rhel-jetson"

pytest_args=(tests_suites/ -v)
if [[ -n "${TARGET_KERNEL_VERSION:-}" ]]; then
    pytest_args+=("--target-kernel-version=${TARGET_KERNEL_VERSION}")
fi

echo "[testing-farm] Running pytest directly over SSH"
echo "[testing-farm] Host: ${JETSON_HOST}"
echo "[testing-farm] User: ${JETSON_USERNAME}"
export PYTHONPATH="${WORK_DIR}/qe-rhel-jetson${PYTHONPATH:+:${PYTHONPATH}}"
"${PYTHON}" -m pytest "${pytest_args[@]}"
