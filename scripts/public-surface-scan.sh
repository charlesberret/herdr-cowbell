#!/usr/bin/env sh
# Fail on house-local paths, hostnames, tokens, and internal vocabulary in the
# public tree. Copied from cowbell-core QA-C1 (LAB-623); do not depend across repos.
set -eu

REPO=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
TARGET=${1:-$REPO}
ALLOW=$REPO/scripts/public-surface-allow.txt
cd "$TARGET"

if [ ! -f "$ALLOW" ]; then
  echo "public-surface-scan: missing $ALLOW" >&2
  exit 1
fi

PATTERN='(/Users/|/home/[a-z]+|~/cloud|cloud/sync|git-projects|avicenna|hesiod|ficino|polytonic|relaytower|anaximander|encosmic|dolan|10\.100\.0\.|100\.64\.0\.|192\.168\.|Majordomo|clubhouse|agent-deck|World Zero|braintrust|Isruna|Kettle|tetrafold|Xanadu|majordomo-inbox|CLAUDE-base|constitution|handoff-|\b(LAB|SYS|OFC|DSK)-[0-9]+\b|sk-[A-Za-z0-9]{10,}|xoxb-|ghp_[A-Za-z0-9]{20,}|BEGIN [A-Z ]*PRIVATE KEY)'

allowed() {
  line=$1
  while IFS= read -r pat || [ -n "${pat:-}" ]; do
    case ${pat:-} in
      ''|'#'*) continue ;;
    esac
    if printf '%s\n' "$line" | grep -Eq "$pat"; then
      return 0
    fi
  done <"$ALLOW"
  return 1
}

scan() {
  if command -v rg >/dev/null 2>&1; then
    rg -n --no-heading -S -e "$PATTERN" \
      -g '!.git/**' \
      -g '!vendor/**' \
      -g '!tests/fixtures/public-surface/**' \
      -g '!scripts/public-surface-scan.sh' \
      -g '!scripts/public-surface-allow.txt' \
      .
  else
    grep -rnE "$PATTERN" . \
      | grep -Ev '(^|/)\.git/|/vendor/|tests/fixtures/public-surface/|scripts/public-surface-scan\.sh|scripts/public-surface-allow\.txt' \
      || true
  fi
}

hits=0
while IFS= read -r line; do
  [ -z "$line" ] && continue
  if allowed "$line"; then
    continue
  fi
  printf '%s\n' "$line"
  hits=$((hits + 1))
done <<EOF
$(scan)
EOF

if [ "$hits" -gt 0 ]; then
  echo "public-surface-scan: $hits hit(s) (see above)" >&2
  exit 1
fi

echo "public-surface-scan: clean"
