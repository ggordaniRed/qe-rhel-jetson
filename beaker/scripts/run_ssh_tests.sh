#!/usr/bin/env bash
# Run the qe-rhel-jetson pytest suite directly against a Beaker machine.
#
# This runner deliberately does not import or invoke Jumpstarter wrapper.py.
# The pytest fixtures create direct SSH connections using the JETSON_* variables.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PYTHON="${PYTHON:-python3}"

if [[ -z "${JETSON_HOST:-}" ]]; then
    echo "ERROR: JETSON_HOST is required (for example, nvidia-jetson-agx-orin-03.khw.eng.bos2.dc.redhat.com)" >&2
    exit 2
fi

if [[ -z "${JETSON_USERNAME:-}" ]]; then
    echo "ERROR: JETSON_USERNAME is required" >&2
    exit 2
fi

if [[ -z "${JETSON_PASSWORD:-}" && -z "${JETSON_KEY_PATH:-}" ]]; then
    echo "ERROR: set JETSON_PASSWORD or JETSON_KEY_PATH" >&2
    exit 2
fi

if [[ ! -d "${PROJECT_ROOT}/tests_suites" ]]; then
    echo "ERROR: tests_suites was not found under ${PROJECT_ROOT}" >&2
    exit 2
fi

if ! "${PYTHON}" -c 'import pytest' >/dev/null 2>&1; then
    echo "ERROR: pytest is not installed for ${PYTHON}" >&2
    echo "Install dependencies with: ${PYTHON} -m pip install -r ${PROJECT_ROOT}/requirements.txt" >&2
    exit 2
fi

if [[ $# -eq 0 ]]; then
    set -- tests_suites/ -v
fi

cd "${PROJECT_ROOT}"
export PYTHONPATH="${PROJECT_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

echo "Running pytest directly over SSH"
echo "  host: ${JETSON_HOST}"
echo "  user: ${JETSON_USERNAME}"
echo "  port: ${JETSON_PORT:-22}"
echo "  args: $*"

exec "${PYTHON}" -m pytest "$@"
