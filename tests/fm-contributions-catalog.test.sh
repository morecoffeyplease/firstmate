#!/usr/bin/env bash
# Focused observational catalog pagination and known-identity coverage.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/fm-catalog-contract.XXXXXX")
if [ "${FM_CATALOG_TEST_KEEP:-0}" = 1 ]; then
  printf 'fixture root: %s\n' "$TMP_ROOT"
else
  trap 'rm -rf "$TMP_ROOT"' EXIT
fi
mkdir -p "$TMP_ROOT/home/data" "$TMP_ROOT/home/state" "$TMP_ROOT/fakebin"
cat > "$TMP_ROOT/fakebin/gh" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$FM_FAKE_GH_LOG"
case "$2" in
  'repos/acme/widget/issues?state=all&per_page=100')
    printf '%s\n' '{"number":1,"title":"One","html_url":"https://github.com/ACME/Widget/issues/1","state":"open","updated_at":"2026-10-08T00:00:00Z"}' '{"number":2,"title":"Two","html_url":"https://github.com/acme/widget/issues/2","state":"closed","updated_at":"2026-10-07T00:00:00Z"}' '{"number":3,"title":"PR ignored","html_url":"https://github.com/acme/widget/issues/3","state":"open","pull_request":{"url":"https://api.github.com/repos/acme/widget/pulls/3"}}'
    echo 'HTTP 503 pagination interrupted' >&2
    exit 7
    ;;
  repos/acme/widget/issues/3) echo 'HTTP 403 private issue' >&2; exit 1 ;;
  repos/acme/widget/issues/4) echo 'HTTP 404 missing issue' >&2; exit 1 ;;
  *) echo "unexpected fake forge endpoint: $2" >&2; exit 91 ;;
esac
SH
chmod +x "$TMP_ROOT/fakebin/gh"
FM_HOME="$TMP_ROOT/home" FM_DATA_OVERRIDE="$TMP_ROOT/home/data" FM_STATE_OVERRIDE="$TMP_ROOT/home/state" FM_FAKE_GH_LOG="$TMP_ROOT/gh.log" PATH="$TMP_ROOT/fakebin:$PATH" \
  "$ROOT/bin/fm-contributions.sh" catalog acme/widget 3 4 > "$TMP_ROOT/catalog.json"
python3 - "$TMP_ROOT/catalog.json" "$TMP_ROOT/gh.log" <<'PY'
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text())
calls = pathlib.Path(sys.argv[2]).read_text().splitlines()
assert value["schema"] == "fm-issue-catalog.v1" and value["repository"] == "acme/widget"
assert value["complete"] is False and value["partial"] is True and "HTTP 503" in value["error"]
assert [item["number"] for item in value["issues"]] == [1, 2], value["issues"]
assert value["issues"][0]["url"] == "https://github.com/ACME/Widget/issues/1", "forge title/URL are preserved for later canonical validation"
checks = {item["url"]: item["status"] for item in value["identity_checks"]}
assert checks == {"https://github.com/acme/widget/issues/3": "not-visible-to-login", "https://github.com/acme/widget/issues/4": "not-visible-to-login"}, checks
assert "--paginate" in calls[0] and "issues?state=all&per_page=100" in calls[0]
assert len(calls) == 3, calls
print("pass: partial paginated inventory, pull request exclusion, and inaccessible known issue identities")
PY
