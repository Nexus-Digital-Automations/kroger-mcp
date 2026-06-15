#!/bin/sh
# E2E pre-deploy gate: boot an isolated local instance, run the full Playwright
# suite against a throwaway account, tear everything down, exit with the suite's code.
#
# Contract: exit 0 = all green (safe to deploy), non-zero = block.
# 100% localhost — never references the deploy target. Isolation strategy:
#   - app runs from a temp cwd, so cwd-relative runtime files
#     (.kroger_token_*.json, kroger_cart.json) land in the temp dir, not the repo.
#   - app listens on :8099 (not the dev :8000); run() self-kills any prior :8099.
#   - a fresh dummy account is provisioned per run and torn down after.
set -u

PORT=8099
BASE_URL="http://127.0.0.1:${PORT}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/../.." && pwd)"

APP_PID=""
WORK=""

cleanup() {
  # Always runs (success or failure). Best-effort — never masks the suite's code.
  echo "[gate] tearing down…"
  E2E_BASE_URL="${BASE_URL}" npx tsx "${SCRIPT_DIR}/scripts/teardown.ts" 2>/dev/null || true
  if [ -n "${APP_PID}" ]; then
    kill "${APP_PID}" 2>/dev/null || true
  fi
  # Belt-and-suspenders: the captured PID is the subshell; ensure nothing lingers on the port.
  lsof -ti ":${PORT}" 2>/dev/null | xargs kill 2>/dev/null || true
  if [ -n "${WORK}" ]; then
    rm -rf "${WORK}"
  fi
}
trap cleanup EXIT

cd "${REPO}"

echo "[gate] ensuring Playwright Chromium is installed…"
npx playwright install chromium >/dev/null 2>&1 || npx playwright install chromium

WORK="$(mktemp -d)"
echo "[gate] isolated cwd: ${WORK}"

echo "[gate] booting app on :${PORT} from isolated cwd…"
( cd "${WORK}" && WEB_PORT="${PORT}" uv run --frozen --project "${REPO}" kroger-web ) >"${WORK}/app.log" 2>&1 &
APP_PID=$!

echo "[gate] waiting for ${BASE_URL}/login …"
READY=""
i=0
while [ "${i}" -lt 60 ]; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/login" 2>/dev/null || true)"
  if [ "${code}" = "200" ]; then
    READY="1"
    break
  fi
  # Fail fast if the app process already died.
  if ! kill -0 "${APP_PID}" 2>/dev/null; then
    break
  fi
  i=$((i + 1))
  sleep 0.5
done

if [ -z "${READY}" ]; then
  echo "[gate] ERROR: app never became ready on ${BASE_URL}/login. App log:" >&2
  cat "${WORK}/app.log" >&2
  exit 1
fi
echo "[gate] app is up."

echo "[gate] provisioning a fresh throwaway account…"
rm -f "${SCRIPT_DIR}/_discovery/account.json"
if ! E2E_BASE_URL="${BASE_URL}" npx tsx "${SCRIPT_DIR}/scripts/provision-account.ts"; then
  echo "[gate] ERROR: account provisioning failed." >&2
  exit 1
fi

echo "[gate] running the suite…"
E2E_BASE_URL="${BASE_URL}" npx playwright test --config="${SCRIPT_DIR}/playwright.config.ts"
SUITE_CODE=$?

echo "[gate] suite exited ${SUITE_CODE}."
exit "${SUITE_CODE}"
