#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# End-to-end smoke test: unit tests, then a real daemon on a free loopback port.
#
#   ./scripts/smoke.sh
#
# Steps:
#   1. python3 -m unittest discover -s tests -v
#   2. sarvam-meter init  -> a throwaway config in a temp dir
#   3. sarvam-meter serve --socket <path>  (background, bounded wait)
#   4. sarvam-meter query health --socket <path>  (bounded wait for readiness)
#   5. kill the daemon (trap on EXIT) and remove the temp dir
#
# No root, no network beyond 127.0.0.1, and it never hangs: every wait is
# bounded and the daemon is killed on any exit path.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WAIT_TRIES=60        # 60 * 0.25s = 15s to come up
WAIT_SLEEP=0.25
CURL_TIMEOUT=5

log()  { printf '[smoke] %s\n' "$*"; }
die()  { printf '[smoke] ERROR: %s\n' "$*" >&2; exit 1; }

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sarvam-meter-smoke.XXXXXX")"
CONFIG_PATH="${TMP_DIR}/config.json"
DAEMON_PID=""
DAEMON_LOG="${TMP_DIR}/daemon.log"

cleanup() {
    local status=$?
    if [ -n "${DAEMON_PID}" ] && kill -0 "${DAEMON_PID}" 2>/dev/null; then
        log "stopping the daemon (pid ${DAEMON_PID})"
        kill "${DAEMON_PID}" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
            kill -0 "${DAEMON_PID}" 2>/dev/null || break
            sleep 0.1
        done
        kill -9 "${DAEMON_PID}" 2>/dev/null || true
    fi
    DAEMON_PID=""
    if [ "${status}" -ne 0 ] && [ -s "${DAEMON_LOG}" ]; then
        printf '[smoke] ---- daemon log ----\n' >&2
        cat "${DAEMON_LOG}" >&2
    fi
    rm -rf "${TMP_DIR}"
    exit "${status}"
}
trap cleanup EXIT INT TERM

# --- 0. what to run --------------------------------------------------------
cd "${REPO_ROOT}"
if command -v sarvam-meter >/dev/null 2>&1; then
    CLI=(sarvam-meter)
else
    log "sarvam-meter is not on PATH; using 'python3 -m sarvam_meter'"
    CLI=(python3 -m sarvam_meter)
fi
command -v curl >/dev/null 2>&1 || die "curl is required but was not found in PATH"
[ -d "${REPO_ROOT}/tests" ] || die "tests/ was not found in ${REPO_ROOT}"

# --- 1. unit tests ---------------------------------------------------------
log "running the unit test suite"
python3 -m unittest discover -s tests -v

# --- 2. throwaway config ---------------------------------------------------
log "writing a throwaway config to ${CONFIG_PATH}"
"${CLI[@]}" init --config "${CONFIG_PATH}"
[ -f "${CONFIG_PATH}" ] || die "init did not create ${CONFIG_PATH}"

# --- 3. the daemon's socket path ------------------------------------------
SOCKET_PATH="${TMP_DIR}/daemon.sock"
[ -n "${SOCKET_PATH}" ] || die "could not work out a socket path"
log "starting the daemon on ${SOCKET_PATH}"

"${CLI[@]}" serve --config "${CONFIG_PATH}" --socket "${SOCKET_PATH}" >"${DAEMON_LOG}" 2>&1 &
DAEMON_PID=$!

# --- 4. bounded wait for `query health` -----------------------------------
ready=0
attempt=0
while [ "${attempt}" -lt "${WAIT_TRIES}" ]; do
    attempt=$((attempt + 1))
    if ! kill -0 "${DAEMON_PID}" 2>/dev/null; then
        die "the daemon exited before it became ready (see the log above)"
    fi
    if "${CLI[@]}" query health --socket "${SOCKET_PATH}" >"${TMP_DIR}/health.json" 2>/dev/null; then
        ready=1
        break
    fi
    sleep "${WAIT_SLEEP}"
done
[ "${ready}" -eq 1 ] || die "the daemon did not answer 'query health' within 15s"

# --- 5. print both results ------------------------------------------------
log "query health"
cat "${TMP_DIR}/health.json"
printf '\n'

"${CLI[@]}" query status --socket "${SOCKET_PATH}" >"${TMP_DIR}/status.json" \
    || die "'query status' failed"
log "query status"
cat "${TMP_DIR}/status.json"
printf '\n'

python3 - "${TMP_DIR}/health.json" "${TMP_DIR}/status.json" <<'PY' || die "the daemon returned unexpected JSON"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    health = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    status = json.load(handle)

problems = []
if health.get("ok") is not True:
    problems.append("health.ok is not true")
if not isinstance(health.get("version"), str):
    problems.append("health.version is not a string")
if status.get("schema") != 2:
    problems.append("status.schema is not 2")
if not isinstance(status.get("sarvam"), dict):
    problems.append("status.sarvam is not an object")
if problems:
    sys.exit("; ".join(problems))
print("[smoke] status payload looks sane (schema 2, sarvam block present)")
PY

log "smoke test passed"
