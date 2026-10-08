#!/usr/bin/env bash
set -Eeuo pipefail

out=${1:?}
interval=${2:-10}
recipe_root=${3:?}
exec python3 "$recipe_root/sample.py" "$out" "$interval" "$recipe_root"
