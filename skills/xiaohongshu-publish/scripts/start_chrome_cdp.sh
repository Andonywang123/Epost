#!/bin/zsh
set -euo pipefail

profile_dir="${1:-}"
debug_port="${2:-9222}"

if [[ -z "$profile_dir" || "$profile_dir" != /* ]]; then
  print -u2 "Usage: scripts/start_chrome_cdp.sh /absolute/chrome-profile [port]"
  exit 2
fi
if [[ ! "$debug_port" =~ '^[0-9]+$' ]]; then
  print -u2 "port must be numeric"
  exit 2
fi

open -na "Google Chrome" --args \
  "--remote-debugging-port=${debug_port}" \
  "--user-data-dir=${profile_dir}" \
  "--no-first-run" \
  "--no-default-browser-check"

print "Chrome CDP requested at http://127.0.0.1:${debug_port}"
