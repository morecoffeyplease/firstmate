#!/usr/bin/env bash
set -Eeuo pipefail

out=${1:?}
interval=${2:-10}
recipe_root=${3:?}

while :; do
  printf '\n===== sample_utc=%s =====\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '%s\n' '--- memory ---'
  cat /proc/meminfo
  printf '%s\n' '--- vmstat ---'
  cat /proc/vmstat
  printf '%s\n' '--- memory pressure ---'
  if [ -r /proc/pressure/memory ]; then cat /proc/pressure/memory; else printf '%s\n' unavailable; fi
  printf '%s\n' '--- cpu pressure ---'
  if [ -r /proc/pressure/cpu ]; then cat /proc/pressure/cpu; else printf '%s\n' unavailable; fi
  printf '%s\n' '--- cgroup membership ---'
  cat /proc/self/cgroup
  printf '%s\n' '--- process table ---'
  ps -ww -eo pid,ppid,pgid,etimes,time,pcpu,rss,vsz,stat,args
  printf '%s\n' '--- resolved cgroup metrics ---'
  python3 "$recipe_root/cgroup-snapshot.py"
  sleep "$interval"
done >> "$out/resources-samples.log" 2>&1
