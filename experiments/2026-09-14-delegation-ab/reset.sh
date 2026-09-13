#!/usr/bin/env bash
# Move an arm's output out of rin-website, leaving only assets/. Nothing is deleted.
# Usage: ./reset.sh <archive-name>    e.g. ./reset.sh arm-b
set -euo pipefail
site=/Users/georgiy/Projects/rin-website
dest="/Users/georgiy/Projects/rin-website-archive/${1:?archive name required}"
[ -e "$dest" ] && { echo "refusing: $dest exists"; exit 1; }
mkdir -p "$dest"
shopt -s dotglob
for f in "$site"/*; do
  case "$(basename "$f")" in assets|.DS_Store) ;; *) mv "$f" "$dest"/ ;; esac
done
ls -A "$site"
echo "archived to $dest"
