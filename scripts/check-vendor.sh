#!/usr/bin/env sh
# Verify vendor/cowbell.py still matches the pinned cowbell-core revision.
#
# We record the pin in vendor/PIN (cowbell-core@<short-sha>) and a SHA-256 of
# vendor/cowbell.py in vendor/SHA256. This avoids fetching charlesberret/cowbell-core
# in CI: the default GITHUB_TOKEN only reads the workflow repository, so a live
# git archive would need a PAT or deploy key. Regenerate vendor/SHA256 when you
# run scripts/vendor-core and commit the new checksum with the vendored files.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
PIN_FILE=$ROOT/vendor/PIN
SUM_FILE=$ROOT/vendor/SHA256
VENDORED=$ROOT/vendor/cowbell.py
JSON=$ROOT/vendor/VENDORED.json

if [ ! -f "$VENDORED" ]; then
  echo "check-vendor: missing $VENDORED" >&2
  exit 1
fi

if [ ! -f "$PIN_FILE" ]; then
  echo "check-vendor: missing $PIN_FILE" >&2
  exit 1
fi

if [ ! -f "$SUM_FILE" ]; then
  echo "check-vendor: missing $SUM_FILE" >&2
  exit 1
fi

PIN=$(tr -d ' \t\r\n' <"$PIN_FILE")
case $PIN in
  cowbell-core@*) SHA=${PIN#cowbell-core@} ;;
  *)
    echo "check-vendor: vendor/PIN must look like cowbell-core@<sha>, got: $PIN" >&2
    exit 1
    ;;
esac

if [ -f "$JSON" ]; then
  JSON_SHA=$(sed -n 's/.*"commit"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$JSON" | head -n1)
  if [ -n "${JSON_SHA:-}" ] && [ "$JSON_SHA" != "$SHA" ]; then
    echo "check-vendor: vendor/PIN ($SHA) disagrees with vendor/VENDORED.json ($JSON_SHA)" >&2
    exit 1
  fi
fi

want=$(tr -d ' \t\r\n' <"$SUM_FILE" | tr 'A-F' 'a-f')
got=$(sha256sum "$VENDORED" | awk '{print $1}')

if [ "$want" != "$got" ]; then
  echo "check-vendor: vendor/cowbell.py sha256 mismatch" >&2
  echo "  want: $want (from vendor/SHA256)" >&2
  echo "  got:  $got" >&2
  echo "  pin:  cowbell-core@$SHA" >&2
  exit 1
fi

echo "check-vendor: vendor/cowbell.py matches cowbell-core@$SHA"
