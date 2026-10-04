#!/usr/bin/env bash

# Run the direct-SSH path locally. This does not submit a Testing Farm request.

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

exec "${PROJECT_ROOT}/beaker/scripts/run_ssh_tests.sh" "$@"
