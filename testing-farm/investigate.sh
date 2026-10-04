#!/usr/bin/env bash

# Wait for a Testing Farm request, print its result, and fetch the test log.

set -o errexit
set -o nounset
set -o pipefail

usage() {
    echo "Usage: $0 REQUEST_ID" >&2
    exit 2
}

[[ $# -eq 1 ]] || usage

REQUEST_ID="$1"
API_URL="${TESTING_FARM_API_URL:-https://api.dev.testing-farm.io/v0.1}"

if ! command -v testing-farm >/dev/null 2>&1; then
    echo "testing-farm CLI is required; install it with: python3 -m pip install tft-cli" >&2
    exit 2
fi
if ! command -v curl >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    echo "curl and jq are required" >&2
    exit 2
fi

echo "Watching Testing Farm request ${REQUEST_ID}"
watch_status=0
testing-farm watch --id "${REQUEST_ID}" --skip-summary || watch_status=$?
if [[ ${watch_status} -ne 0 ]]; then
    echo "Testing Farm watch exited with status ${watch_status}; collecting API details anyway" >&2
fi

request_json="$(curl -fsSk "${API_URL}/requests/${REQUEST_ID}")"
state="$(jq -r '.state // "unknown"' <<<"${request_json}")"
overall="$(jq -r '.result.overall // "unknown"' <<<"${request_json}")"
summary="$(jq -r '.result.summary // "No summary"' <<<"${request_json}")"
artifacts="$(jq -r '.run.artifacts // empty' <<<"${request_json}")"
workdir="$(jq -r '.run.stages.provisioning.workdir // empty' <<<"${request_json}")"

echo "State: ${state}"
echo "Result: ${overall}"
echo "Summary: ${summary}"

if [[ -z "${artifacts}" || -z "${workdir}" ]]; then
    echo "Artifacts are not available for this request yet."
    [[ "${overall}" == "passed" ]] && exit 0
    exit 1
fi

echo "Artifacts: ${artifacts}"

results_url="${artifacts}/results.xml"
output_url="${artifacts}/${workdir}/testing-farm/plan/execute/data/guest/default-0/testing-farm/tests-1/output.txt"

echo
echo "Test output:"
if ! curl -fsSk "${output_url}"; then
    echo "Test output.txt was not available; showing results.xml instead:" >&2
    curl -fsSk "${results_url}" || true
fi

[[ "${overall}" == "passed" ]] && exit 0
exit 1
