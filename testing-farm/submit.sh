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
COMPOSE="${COMPOSE:-RHEL-9-Nightly}"
ARCH="${ARCH:-aarch64}"
TIMEOUT="${TIMEOUT:-240}"
QE_REPO_URL="${QE_REPO_URL:-${GIT_URL}}"
QE_REPO_REF="${QE_REPO_REF:-${GIT_REF}}"
SSH_SMOKE_ONLY="${SSH_SMOKE_ONLY:-0}"

if ! command -v testing-farm >/dev/null 2>&1; then
    echo "testing-farm CLI is required; install it with: python3 -m pip install tft-cli" >&2
    exit 2
fi

args=(
    request
    --git-url "${GIT_URL}"
    --git-ref "${GIT_REF}"
    --compose "${COMPOSE}"
    --arch "${ARCH}"
    --timeout "${TIMEOUT}"
    --environment "JETSON_HOST=${JETSON_HOST}"
    --environment "JETSON_USERNAME=${JETSON_USERNAME}"
    --environment "JETSON_PORT=${JETSON_PORT:-22}"
    --environment "JETSON_TIMEOUT=${JETSON_TIMEOUT:-60}"
    --environment "QE_REPO_URL=${QE_REPO_URL}"
    --environment "QE_REPO_REF=${QE_REPO_REF}"
    --environment "SSH_SMOKE_ONLY=${SSH_SMOKE_ONLY}"
)

if [[ -n "${TARGET_KERNEL_VERSION:-}" ]]; then
    args+=(--environment "TARGET_KERNEL_VERSION=${TARGET_KERNEL_VERSION}")
fi

if [[ -n "${JETSON_PASSWORD:-}" ]]; then
    args+=(--secret "JETSON_PASSWORD=${JETSON_PASSWORD}")
fi
if [[ -n "${SSH_PRIVATE_KEY:-}" ]]; then
    args+=(--secret "SSH_PRIVATE_KEY=${SSH_PRIVATE_KEY}")
fi

echo "Submitting direct-SSH Testing Farm request"
echo "  host: ${JETSON_HOST}"
echo "  user: ${JETSON_USERNAME}"
echo "  repo: ${GIT_URL}@${GIT_REF}"
echo "  compose: ${COMPOSE} (${ARCH})"

testing-farm "${args[@]}"
