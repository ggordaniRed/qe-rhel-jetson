#!/usr/bin/env bash

# Run pytest directly over Jumpstarter's SSH port-forward.
# This requires an existing Jumpstarter lease and does not invoke wrapper.py.

set -o errexit
set -o nounset
set -o pipefail

: "${JMP_LEASE:?Set JMP_LEASE to an active Jumpstarter lease name}"
: "${JETSON_USERNAME:?Set JETSON_USERNAME}"
if [[ -z "${JETSON_PASSWORD:-}" && -z "${JETSON_KEY_PATH:-}" ]]; then
    echo "Set JETSON_PASSWORD or JETSON_KEY_PATH" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

if [[ $# -eq 0 ]]; then
    set -- tests_suites/ -v
fi

jmp shell --lease "${JMP_LEASE}" -- python3 testing-farm/direct_ssh.py "$@"
