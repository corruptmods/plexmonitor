#!/bin/bash
# PlexMonitor health check. Reads the bot's own snapshot first
# (data/health.json, written every refresh), then the log for anything
# the snapshot cannot know about. Run from anywhere:
#
#   /opt/stacks/plex-monitor/tools/health.sh
#
# Optional first argument: log window (default 12h), e.g. health.sh 24h
set -u
STACK="$(cd "$(dirname "$0")/.." && pwd)"
WINDOW="${1:-12h}"
cd "$STACK" || exit 1

bold() { printf '\033[1m%s\033[0m\n' "$*"; }

bold "== snapshot (data/health.json) =="
if [ -f data/health.json ]; then
python3 - <<'PY'
import json, datetime as dt
h = json.load(open("data/health.json"))
c, r, s, src = h["cadence"], h["rate_limit"], h["streams"], h["sources"]
age = (dt.datetime.now(dt.timezone.utc)
       - dt.datetime.fromisoformat(h["written_at"])).total_seconds()
up  = (dt.datetime.now(dt.timezone.utc)
       - dt.datetime.fromisoformat(h["started_at"])).total_seconds()
# Stale relative to how often the bot is ACTUALLY refreshing: a throttled
# 60s cadence runs at 180s+, and a fixed 180s limit cried wolf there.
stale_after = max(180, 3 * float(c.get("effective_seconds") or 60))
flag = "" if age < stale_after else "   <-- STALE: bot not refreshing?"
print(f"build            {h['build']}   (snapshot {age:.0f}s old{flag})")
print(f"bot uptime       {up/3600:.1f} h")
print(f"cadence          configured {c['refresh_seconds']}s, "
      f"running {c['effective_seconds']}s "
      f"(throttle x{c['throttle']}{', STALLED ' + str(c['stalled_for']) + 's' if c['stalled_for'] else ''})")
print(f"429s lifetime    {r['count_429']}   "
      f"last: {('%.0fs ago' % r['last_429_age_s']) if r['last_429_age_s'] is not None else 'never'}"
      f"{(' (' + str(r['last_429_scope']) + ')') if r['last_429_scope'] else ''}")
print(f"ladder lifetime  soft {r['soft_limits_total']}  "
      f"escalations {r['escalations_total']}  hard stalls {r['hard_stalls_total']}  "
      f"absorbed {r['absorbed_429']}")
print(f"streams          {'active' if s['active'] else 'idle'}, "
      f"{s['cards_on_page']} card(s) on page {s['page']+1}/{s['pages']}")
arr = ", ".join(f"{a['kind']}{'' if a['enabled'] else ' (off)'}" for a in src['arr']) or "none"
print(f"sources          plex {'online' if src['plex_online'] else 'OFFLINE' if src['plex_online'] is False else 'unknown'}, "
      f"artwork={src['artwork']}, arr: {arr}")
verdict = []
if c['throttle'] > 1: verdict.append("throttled -- running slower than configured")
if r['last_429_age_s'] is not None and r['last_429_age_s'] < 3600: verdict.append("429 in the last hour")
if age >= stale_after: verdict.append("snapshot stale")
print("verdict          " + ("; ".join(verdict) if verdict else "healthy"))
PY
else
  echo "no data/health.json yet -- bot has not completed a refresh on this build"
fi

echo
bold "== log, last $WINDOW =="
# Fail loudly if the logs cannot be read (no docker access, wrong folder):
# with errors hidden, every count below printed 0 and looked healthy.
if ! docker compose logs --since 1s plex-monitor >/dev/null 2>&1; then
  echo "!! cannot read container logs (docker access? run from the stack) -- counts below are NOT meaningful"
fi
L() { docker compose logs --since "$WINDOW" plex-monitor 2>/dev/null; }
echo "429s:             $(L | grep -c '429 on PATCH')"
echo "soft escalations: $(L | grep -c 'slowing text refresh')"
echo "hard stalls:      $(L | grep -c 'pausing edits')"
echo "restarts:         $(L | grep -c 'Realtime webhook listening')"
echo "errors:           $(L | grep -cE ' ERROR | CRITICAL ')"
echo "tracebacks:       $(L | grep -c 'Traceback')"

echo
bold "== non-routine warnings/errors, last $WINDOW (deduped) =="
L | grep -E " ERROR | CRITICAL | WARNING " \
  | grep -vE "429 on PATCH|more shared-scope|was throttled|429 headers|Repeated soft limits" \
  | sed -E 's/.*(ERROR|CRITICAL|WARNING) - //; s/[0-9]+\.?[0-9]*/N/g' \
  | sort | uniq -c | sort -rn | head -12
