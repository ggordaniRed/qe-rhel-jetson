#!/usr/bin/env bash

# Submit a direct-SSH pytest run to Testing Farm.
# The Beaker machine must already be reserved and reachable from the TF VM.

set -o errexit
set -o nounset
set -o pipefail

: "${TESTING_FARM_API_TOKEN:?Set TESTING_FARM_API_TOKEN}"
: "${JETSON_HOST:?Set JETSON_HOST to the reserved Beaker FQDN}"
: "${JETSON_USERNAME:?Set JETSON_USERNAME}"
if [[ -z "${JETSON_PASSWORD:-}" && -z "${SSH_PRIVATE_KEY:-}" ]]; then
    echo "Set JETSON_PASSWORD or SSH_PRIVATE_KEY" >&2
    exit 2
fi

GIT_URL="${GIT_URL:-https://github.com/rh-ecosystem-edge/qe-rhel-jetson.git}"
GIT_REF="${GIT_REF:-main}"
GIT_USERNAME="${GIT_USERNAME:-}"
GIT_PASSWORD="${GIT_PASSWORD:-}"
COMPOSE="${COMPOSE:-RHEL-9-Nightly}"
ARCH="${ARCH:-aarch64}"
TIMEOUT="${TIMEOUT:-240}"
if [[ -n "${GIT_USERNAME}" && -z "${GIT_PASSWORD}" ]] || [[ -z "${GIT_USERNAME}" && -n "${GIT_PASSWORD}" ]]; then
    echo "Set both GIT_USERNAME and GIT_PASSWORD, or neither" >&2
    exit 2
fi

GIT_AUTH_URL="${GIT_URL}"
if [[ -n "${GIT_USERNAME}" ]]; then
    GIT_AUTH_URL="${GIT_URL/https:\/\//https://${GIT_USERNAME}:${GIT_PASSWORD}@}"
fi

QE_REPO_URL="${QE_REPO_URL:-${GIT_AUTH_URL}}"
QE_REPO_REF="${QE_REPO_REF:-${GIT_REF}}"
SSH_SMOKE_ONLY="${SSH_SMOKE_ONLY:-0}"
SSH_PRIVATE_KEY_B64="${SSH_PRIVATE_KEY_B64:-}"
ANSIBLE_BOOTC="${ANSIBLE_BOOTC:-0}"
BOOTC_IMAGE_BASE="${BOOTC_IMAGE_BASE:-}"
BOOTC_IMAGE_TAG="${BOOTC_IMAGE_TAG:-}"
REGISTRY_URL="${REGISTRY_URL:-quay.io}"
REGISTRY_USER="${REGISTRY_USER:-}"
REGISTRY_PASSWORD="${REGISTRY_PASSWORD:-}"
ANSIBLE_AUTO_REBOOT="${ANSIBLE_AUTO_REBOOT:-true}"
ANSIBLE_RESTORE_BOOT_ORDER="${ANSIBLE_RESTORE_BOOT_ORDER:-true}"
ANSIBLE_RESERVATION_HOURS="${ANSIBLE_RESERVATION_HOURS:-24}"

if [[ "${ANSIBLE_BOOTC}" == "1" || "${ANSIBLE_BOOTC}" == "true" ]]; then
    : "${BOOTC_IMAGE_BASE:?Set BOOTC_IMAGE_BASE when ANSIBLE_BOOTC=1}"
    : "${BOOTC_IMAGE_TAG:?Set BOOTC_IMAGE_TAG when ANSIBLE_BOOTC=1}"
    : "${REGISTRY_USER:?Set REGISTRY_USER when ANSIBLE_BOOTC=1}"
    : "${REGISTRY_PASSWORD:?Set REGISTRY_PASSWORD when ANSIBLE_BOOTC=1}"
fi

# A PEM/OpenSSH key contains newlines. Encode it before passing it as one
# Testing Farm CLI secret argument; otherwise the key's "BEGIN OPENSSH" line
# is parsed as another CLI option.
if [[ -z "${SSH_PRIVATE_KEY_B64}" && -n "${SSH_PRIVATE_KEY:-}" ]]; then
    SSH_PRIVATE_KEY_B64=$(printf '%s' "${SSH_PRIVATE_KEY}" | base64 | tr -d '\n')
fi

if ! command -v testing-farm >/dev/null 2>&1; then
    echo "testing-farm CLI is required; install it with: python3 -m pip install tft-cli" >&2
    exit 2
fi

args=(
    request
    --git-url "${GIT_AUTH_URL}"
    --git-ref "${GIT_REF}"
    --compose "${COMPOSE}"
    --arch "${ARCH}"
    --timeout "${TIMEOUT}"
    --environment "JETSON_HOST=${JETSON_HOST}"
    --environment "JETSON_USERNAME=${JETSON_USERNAME}"
    --environment "JETSON_PORT=${JETSON_PORT:-22}"
    --environment "JETSON_TIMEOUT=${JETSON_TIMEOUT:-60}"
    --secret "QE_REPO_URL=${QE_REPO_URL}"
    --environment "QE_REPO_REF=${QE_REPO_REF}"
    --environment "SSH_SMOKE_ONLY=${SSH_SMOKE_ONLY}"
    --environment "ANSIBLE_BOOTC=${ANSIBLE_BOOTC}"
)

if [[ "${ANSIBLE_BOOTC}" == "1" || "${ANSIBLE_BOOTC}" == "true" ]]; then
    args+=(
        --environment "BOOTC_IMAGE_BASE=${BOOTC_IMAGE_BASE}"
        --environment "BOOTC_IMAGE_TAG=${BOOTC_IMAGE_TAG}"
        --environment "REGISTRY_URL=${REGISTRY_URL}"
        --environment "ANSIBLE_AUTO_REBOOT=${ANSIBLE_AUTO_REBOOT}"
        --environment "ANSIBLE_RESTORE_BOOT_ORDER=${ANSIBLE_RESTORE_BOOT_ORDER}"
        --environment "ANSIBLE_RESERVATION_HOURS=${ANSIBLE_RESERVATION_HOURS}"
        --secret "REGISTRY_USER=${REGISTRY_USER}"
        --secret "REGISTRY_PASSWORD=${REGISTRY_PASSWORD}"
    )
fi

if [[ -n "${TARGET_KERNEL_VERSION:-}" ]]; then
    args+=(--environment "TARGET_KERNEL_VERSION=${TARGET_KERNEL_VERSION}")
fi

if [[ -n "${JETSON_PASSWORD:-}" ]]; then
    args+=(--secret "JETSON_PASSWORD=${JETSON_PASSWORD}")
fi
if [[ -n "${SSH_PRIVATE_KEY_B64}" ]]; then
    args+=(--secret "SSH_PRIVATE_KEY_B64=${SSH_PRIVATE_KEY_B64}")
fi

echo "Submitting direct-SSH Testing Farm request"
echo "  host: ${JETSON_HOST}"
echo "  user: ${JETSON_USERNAME}"
DISPLAY_GIT_URL="${GIT_AUTH_URL}"
if [[ "${DISPLAY_GIT_URL}" == https://*@* ]]; then
    DISPLAY_GIT_URL="${DISPLAY_GIT_URL#https://}"
    DISPLAY_GIT_URL="https://${DISPLAY_GIT_URL#*@}"
fi
echo "  repo: ${DISPLAY_GIT_URL}@${GIT_REF}"
echo "  compose: ${COMPOSE} (${ARCH})"

# The CLI echoes the repository URL. Since private-repository credentials are
# embedded in that URL, redact userinfo before it reaches the terminal/log.
testing-farm "${args[@]}" 2>&1 \
    | sed -E 's#(https://)[^/@[:space:]]+:[^/@[:space:]]+@#\1***:***@#g'
