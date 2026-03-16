#!/usr/bin/env bash
set -euo pipefail

# Pixiv-XP-Pusher lightweight monitor
# - checks service health
# - samples recent logs
# - detects repeating errors (window-based)

STATE_DIR="/opt/Pixiv-XP-Pusher/ops/state"
mkdir -p "$STATE_DIR"

WINDOW_MINUTES=${WINDOW_MINUTES:-360}   # 6h
MAX_LOG_LINES=${MAX_LOG_LINES:-400}

# For log sampling, prefer incremental since last run to avoid re-alerting on old errors.
SINCE_FILE="$STATE_DIR/last_since_utc.txt"
if [[ -f "$SINCE_FILE" ]]; then
  SINCE_SPEC=$(cat "$SINCE_FILE" 2>/dev/null || true)
else
  SINCE_SPEC="${WINDOW_MINUTES} min ago"
fi

PUSH_SVC="pixiv-pusher.service"
WEB_SVC="pixiv-web.service"

TS_UTC=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
TS_LOCAL=$(date +"%Y-%m-%d %H:%M:%S %Z")

active_state() {
  systemctl is-active "$1" 2>/dev/null || echo "unknown"
}

restart_count_in_window() {
  local svc="$1"
  # count Starts in the last WINDOW_MINUTES
  journalctl -u "$svc" --since "${WINDOW_MINUTES} min ago" --no-pager 2>/dev/null \
    | grep -cE "Started .*" || true
}

sample_errors() {
  local svc="$1"
  journalctl -u "$svc" --since "$SINCE_SPEC" -n "$MAX_LOG_LINES" --no-pager 2>/dev/null \
    | grep -E "OOM|Killed|Traceback|429|timeout|Invalid model name|unauthorized|rate limit|Too Many Requests" \
    | tail -n 80 || true
}

mem_swap_snapshot() {
  # MemAvailable (kB), SwapFree (kB)
  awk 'BEGIN{ma=0;sf=0} $1=="MemAvailable:"{ma=$2} $1=="SwapFree:"{sf=$2} END{print ma" "sf}' /proc/meminfo
}

disk_free_pct() {
  df -P /opt | awk 'NR==2{gsub(/%/,"",$5); print 100-$5}'
}

push_state=$(active_state "$PUSH_SVC")
web_state=$(active_state "$WEB_SVC")

push_restarts=$(restart_count_in_window "$PUSH_SVC")
web_restarts=$(restart_count_in_window "$WEB_SVC")

read -r mem_avail_kb swap_free_kb < <(mem_swap_snapshot)
opt_free_pct=$(disk_free_pct)

push_err=$(sample_errors "$PUSH_SVC")
web_err=$(sample_errors "$WEB_SVC")

# severity
severity="OK"
reasons=()

if [[ "$push_state" != "active" ]]; then severity="CRIT"; reasons+=("pusher_not_active:$push_state"); fi
if [[ "$web_state" != "active" ]]; then severity="CRIT"; reasons+=("web_not_active:$web_state"); fi

# restart storm heuristic
if [[ "$push_restarts" -ge 3 ]]; then severity="WARN"; reasons+=("pusher_restarts:${push_restarts}/${WINDOW_MINUTES}m"); fi
if [[ "$web_restarts" -ge 3 ]]; then severity="WARN"; reasons+=("web_restarts:${web_restarts}/${WINDOW_MINUTES}m"); fi

# resource sentinels (very conservative)
# MemAvailable < 300MB or SwapFree < 200MB
if [[ "$mem_avail_kb" -lt 307200 ]]; then severity="WARN"; reasons+=("low_mem_avail_mb:$((mem_avail_kb/1024))"); fi
if [[ "$swap_free_kb" -lt 204800 ]]; then severity="WARN"; reasons+=("low_swap_free_mb:$((swap_free_kb/1024))"); fi

# disk free < 10%
if [[ "$opt_free_pct" -lt 10 ]]; then severity="WARN"; reasons+=("low_disk_free_pct:$opt_free_pct"); fi

# log errors
if [[ -n "$push_err" ]]; then severity="WARN"; reasons+=("pusher_log_errors"); fi
if [[ -n "$web_err" ]]; then severity="WARN"; reasons+=("web_log_errors"); fi

# escalation to CRIT if OOM/Killed appears
if echo "$push_err\n$web_err" | grep -qE "OOM|Killed"; then severity="CRIT"; reasons+=("oom_or_killed"); fi

# build report
reason_str="${reasons[*]:-none}"

report_file="$STATE_DIR/last_report.txt"
{
  echo "ts_utc=$TS_UTC"
  echo "ts_local=$TS_LOCAL"
  echo "window_minutes=$WINDOW_MINUTES"
  echo "severity=$severity"
  echo "reasons=$reason_str"
  echo "push_state=$push_state"
  echo "web_state=$web_state"
  echo "push_restarts=$push_restarts"
  echo "web_restarts=$web_restarts"
  echo "mem_avail_mb=$((mem_avail_kb/1024))"
  echo "swap_free_mb=$((swap_free_kb/1024))"
  echo "opt_free_pct=$opt_free_pct"
  echo "---pusher_errors---"
  echo "$push_err"
  echo "---web_errors---"
  echo "$web_err"
} > "$report_file"

# write to journald (single line summary)
logger -t pixiv-monitor "severity=$severity window=${WINDOW_MINUTES}m push=$push_state web=$web_state restarts(push/web)=${push_restarts}/${web_restarts} mem_avail_mb=$((mem_avail_kb/1024)) swap_free_mb=$((swap_free_kb/1024)) opt_free_pct=$opt_free_pct reasons=[$reason_str]"

# print to stdout for systemd logs
cat "$report_file"

# advance cursor for next run (UTC ISO)
echo "$TS_UTC" > "$SINCE_FILE"

# exit code for systemd
if [[ "$severity" == "CRIT" ]]; then exit 2; fi
if [[ "$severity" == "WARN" ]]; then exit 1; fi
exit 0
