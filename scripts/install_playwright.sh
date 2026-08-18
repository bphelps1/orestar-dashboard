#!/usr/bin/env bash
#
# Install the Playwright browser, bounded and retried.
#
# `python -m playwright install chromium --with-deps` is the least reliable step
# in this repo, and it sits in front of every scraper. Two failures in three days:
#
#   2026-08-16  HTTP 403 from cdn.playwright.dev — "this service is not
#               available in your location". Killed a chain outright.
#   2026-08-18  Hung with no output for 3 hours. The job's timeout-minutes is
#               300, so it held the ORESTAR concurrency lane for that whole time
#               and would have been recorded as "cancelled" at the end of it.
#
# Neither is our code, and neither is worth hours of a blocked lane. A hang is
# capped here at ATTEMPT_TIMEOUT rather than the job timeout, and a transient
# failure gets another try instead of ending the chain.
#
# `timeout` sends TERM and, after --kill-after, KILL — a wedged download that
# ignores TERM still dies rather than hanging the runner.

set -uo pipefail

ATTEMPTS="${PLAYWRIGHT_INSTALL_ATTEMPTS:-3}"
ATTEMPT_TIMEOUT="${PLAYWRIGHT_INSTALL_TIMEOUT:-420}"
BACKOFF=(10 30)

# `timeout` is GNU coreutils: present on the ubuntu runners, absent on a stock
# Mac. Resolve it rather than assume it, so running this locally degrades to
# "retry without a cap" instead of failing instantly with command-not-found —
# which is exactly what happened the first time this was tested, and made the
# test look like the script was broken when it was the harness.
TIMEOUT_BIN=""
for candidate in timeout gtimeout; do
  if command -v "$candidate" >/dev/null 2>&1; then TIMEOUT_BIN="$candidate"; break; fi
done
if [ -z "$TIMEOUT_BIN" ]; then
  echo "note: no timeout(1) available — retrying without a per-attempt cap."
fi

run_install() {
  if [ -n "$TIMEOUT_BIN" ]; then
    "$TIMEOUT_BIN" --kill-after=30s "${ATTEMPT_TIMEOUT}s" python -m playwright install chromium --with-deps
  else
    python -m playwright install chromium --with-deps
  fi
}

# Clear what a killed attempt leaves behind.
#
# `--with-deps` shells out to apt-get, and timeout(1) signals only the command
# it launched — not that command's children. So a timed-out attempt left an
# orphaned apt-get still holding /var/lib/apt/lists/lock, and every retry then
# died in under a second with:
#
#     E: Could not get lock /var/lib/apt/lists/lock. It is held by process 3464
#
# The retry could not possibly have succeeded; it turned one slow failure into
# three instant ones and reported "3 attempts" as though they were independent.
# Nothing here runs in the success path, and every step tolerates being a no-op
# on a runner where apt was never touched.
clear_apt_state() {
  command -v apt-get >/dev/null 2>&1 || return 0
  echo "Clearing any apt state left by the killed attempt..."
  sudo pkill -9 -x apt-get   >/dev/null 2>&1 || true
  sudo pkill -9 -x dpkg      >/dev/null 2>&1 || true
  sudo rm -f /var/lib/apt/lists/lock /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || true
  sudo dpkg --configure -a   >/dev/null 2>&1 || true
}

for attempt in $(seq 1 "$ATTEMPTS"); do
  echo "Installing Playwright chromium (attempt ${attempt}/${ATTEMPTS}, ${ATTEMPT_TIMEOUT}s cap)..."
  # rc is captured in the else branch on purpose. After `if cmd; then ... fi`,
  # $? is the status of the IF STATEMENT — 0 when no branch ran — not of cmd,
  # so reading it after `fi` reported every failure as "exit 0".
  if run_install; then
    echo "Playwright installed on attempt ${attempt}."
    exit 0
  else
    rc=$?
  fi
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    echo "Attempt ${attempt} exceeded ${ATTEMPT_TIMEOUT}s and was killed."
  else
    echo "Attempt ${attempt} failed with exit ${rc}."
  fi
  if [ "$attempt" -lt "$ATTEMPTS" ]; then
    clear_apt_state
    delay="${BACKOFF[$((attempt - 1))]:-30}"
    echo "Retrying in ${delay}s..."
    sleep "$delay"
  fi
done

echo "::error::Playwright install failed after ${ATTEMPTS} attempts. This is upstream (CDN block, outage, or a wedged download), not the scraper — re-run the workflow."
exit 1
