#!/bin/zsh
set -euo pipefail
profile_dir="${1:-}"
debug_port="${2:-9223}"
if [[ -z "$profile_dir" || "$profile_dir" != /* || "$profile_dir" == *'/../'* ]]; then
  print -u2 "Usage: start_chrome_cdp.sh /absolute/dedicated-profile [port]"
  exit 2
fi
if [[ "$profile_dir" == "$HOME" || "$profile_dir" == '/' || "$profile_dir" == *'/Google/Chrome'* ]]; then
  print -u2 "Use a dedicated profile, never your ordinary Chrome profile."
  exit 2
fi
if [[ ! "$debug_port" =~ '^[0-9]+$' ]] || (( debug_port < 1024 || debug_port > 65535 )); then
  print -u2 "port must be between 1024 and 65535"
  exit 2
fi
open -na "Google Chrome" --args \
  "--remote-debugging-address=127.0.0.1" \
  "--remote-debugging-port=${debug_port}" \
  "--user-data-dir=${profile_dir}" \
  --no-first-run --no-default-browser-check https://studio.youtube.com/
print "Chrome connection requested at http://127.0.0.1:${debug_port}; sign in manually."
