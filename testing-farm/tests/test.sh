#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

WORK_DIR="$(mktemp -d)"
SSH_KEY_FILE=""

cleanup() {
    if [[ -n "${SSH_KEY_FILE}" ]]; then
        rm -f "${SSH_KEY_FILE}"
    fi
    rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

: "${JETSON_HOST:?JETSON_HOST is required}"
: "${JETSON_USERNAME:?JETSON_USERNAME is required}"
if [[ -z "${JETSON_PASSWORD:-}" && -z "${SSH_PRIVATE_KEY:-}" ]]; then
    echo "JETSON_PASSWORD or SSH_PRIVATE_KEY is required" >&2
    exit 2
fi

QE_REPO_URL="${QE_REPO_URL:-https://github.com/rh-ecosystem-edge/qe-rhel-jetson.git}"
QE_REPO_REF="${QE_REPO_REF:-main}"
PYTHON="${PYTHON:-python3}"

echo "[testing-farm] Cloning qe-rhel-jetson ref ${QE_REPO_REF}"
git clone --depth 1 --branch "${QE_REPO_REF}" "${QE_REPO_URL}" "${WORK_DIR}/qe-rhel-jetson"

if [[ -n "${SSH_PRIVATE_KEY:-}" ]]; then
    SSH_KEY_FILE="${WORK_DIR}/id_jetson"
    printf '%s\n' "${SSH_PRIVATE_KEY}" > "${SSH_KEY_FILE}"
    chmod 600 "${SSH_KEY_FILE}"
    export JETSON_KEY_PATH="${SSH_KEY_FILE}"
fi

cd "${WORK_DIR}/qe-rhel-jetson"

if [[ "${SSH_SMOKE_ONLY:-0}" == "1" ]]; then
    echo "[testing-farm] Running SSH smoke test only"
    "${PYTHON}" testing-farm/tests/ssh_smoke.py
    exit 0
fi

pytest_args=(tests_suites/ -v)
if [[ -n "${TARGET_KERNEL_VERSION:-}" ]]; then
    pytest_args+=("--target-kernel-version=${TARGET_KERNEL_VERSION}")
fi

echo "[testing-farm] Running pytest directly over SSH"
echo "[testing-farm] Host: ${JETSON_HOST}"
echo "[testing-farm] User: ${JETSON_USERNAME}"
export PYTHONPATH="${WORK_DIR}/qe-rhel-jetson${PYTHONPATH:+:${PYTHONPATH}}"
"${PYTHON}" -m pytest "${pytest_args[@]}"
