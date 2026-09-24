#!/usr/bin/env bash
# Pipeline state, shared by both workflows, kept as ONE encrypted file on
# the `pipeline-state` GitHub release.
#
#   scripts/ci_state.sh restore            download, decrypt, unpack into the repo root
#   scripts/ci_state.sh persist [message]  pack, encrypt, upload
#
# Needs: STATE_KEY (the passphrase; a repository secret in CI) and an
# authenticated `gh` (GH_TOKEN in CI). Also runs locally from a clone,
# e.g. to seed the first state from a desktop run:
#   STATE_KEY=... bash scripts/ci_state.sh persist "Seeded from desktop"
# NOTE: `restore` OVERWRITES the local financials.db, portfolio.db, etc.
#
# A release asset can be up to 2 GB and is replaced rather than
# accumulated in history. financials.db holds Yahoo prices, which this
# project does not redistribute, so the state is only ever uploaded as
# ciphertext.
#
# Inside the archive:
#   financials.db portfolio.db parameters.json refdata_tables.json shares.csv
#   screen_config.json   the fiscal window, once the screen has rolled it
#   runs/run_DATE.json   the screen's fair values, read by the price update
#   public/              the last published site, so a price update can
#                        redeploy it with only portfolio.html rebuilt
set -euo pipefail
shopt -s nullglob

RELEASE=pipeline-state
STATE_FILES="financials.db portfolio.db parameters.json refdata_tables.json shares.csv screen_config.json"
KEEP=2                       # newest assets kept; the older one is a fallback

: "${STATE_KEY:?STATE_KEY is not set (add it as a repository secret)}"

crypt() {  # crypt -e|-d  <in >out
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt "$1" -pass env:STATE_KEY
}

assets() {  # asset names on the release, oldest first (names embed a UTC timestamp)
  gh release view "$RELEASE" --json assets --jq '.assets[].name' 2>/dev/null \
    | grep '^state-.*\.tar\.gz\.enc$' | sort || true
}

restore() {
  local latest tmp
  latest=$(assets | tail -n 1)
  if [ -z "$latest" ]; then
    echo "no saved state on release '$RELEASE' -- first run"
    return 0
  fi
  tmp=$(mktemp -d)
  gh release download "$RELEASE" --pattern "$latest" --dir "$tmp"
  mkdir "$tmp/x"
  if ! crypt -d <"$tmp/$latest" | tar -xzf - -C "$tmp/x"; then
    echo "::error::could not decrypt $latest -- STATE_KEY is not the passphrase it was saved with" >&2
    exit 1
  fi
  echo "restored $latest"

  for f in $STATE_FILES; do
    if [ -f "$tmp/x/$f" ]; then cp "$tmp/x/$f" "$f"; echo "  $f"; fi
  done
  for r in "$tmp"/x/runs/run_*.json; do
    d=$(basename "$r" .json); d=${d#run_}
    mkdir -p "valuations/$d"
    cp "$r" "valuations/$d/"
    echo "  valuations/$d/$(basename "$r")"
  done
  if [ -d "$tmp/x/public" ]; then
    mkdir -p public
    cp -r "$tmp/x/public/." public/
    echo "  public/"
  fi
  rm -rf "$tmp"
}

persist() {
  local tmp name
  tmp=$(mktemp -d)
  mkdir -p "$tmp/x/runs"
  for f in $STATE_FILES; do
    if [ -f "$f" ]; then cp "$f" "$tmp/x/"; fi
  done
  for r in valuations/*/run_*.json; do cp "$r" "$tmp/x/runs/"; done
  if [ -d public ]; then cp -r public "$tmp/x/public"; fi

  name="state-$(date -u +%Y%m%dT%H%M%SZ).tar.gz.enc"
  tar -czf - -C "$tmp/x" . | crypt -e >"$tmp/$name"
  echo "${1:-Pipeline state}: $name ($(du -h "$tmp/$name" | cut -f1))"

  if ! gh release view "$RELEASE" >/dev/null 2>&1; then
    gh release create "$RELEASE" --prerelease --latest=false \
      --title "Pipeline state (encrypted)" \
      --notes "Written by the workflows: the pipeline's working state, encrypted with the STATE_KEY secret. Not a software release; do not delete."
  fi
  # Upload under a new name FIRST, and only then prune: a failed upload
  # must never leave the release without a usable state.
  gh release upload "$RELEASE" "$tmp/$name"
  assets | head -n -"$KEEP" | while read -r old; do
    gh release delete-asset "$RELEASE" "$old" --yes
  done
  rm -rf "$tmp"
}

case "${1:-}" in
  restore) restore ;;
  persist) persist "${2:-}" ;;
  *) echo "usage: $0 restore | persist [message]" >&2; exit 2 ;;
esac
