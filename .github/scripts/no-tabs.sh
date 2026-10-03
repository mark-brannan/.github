#!/usr/bin/env bash
# Fail when a line the change ADDS starts with a tab. Lines already in the
# tree never fail; only the diff is read. Files matching the allowlist are
# exempt. Called by .github/workflows/nonempty-diff.yml.
#
#   no-tabs.sh <base-commit> <allowlist-file>
#
# Diffs <base-commit>..HEAD in the current repository.
set -euo pipefail

base=${1:?usage: no-tabs.sh <base-commit> <allowlist-file>}
allowlist=${2:?usage: no-tabs.sh <base-commit> <allowlist-file>}
[ -f "$allowlist" ] || { echo "::error::tab allowlist not found: $allowlist"; exit 1; }

patterns=()
while IFS= read -r line || [ -n "$line" ]; do
  line=${line%%#*}
  line=${line#"${line%%[![:space:]]*}"}
  line=${line%"${line##*[![:space:]]}"}
  [ -n "$line" ] && patterns+=("$line")
done < "$allowlist"

allowed() {
  local p
  for p in ${patterns[@]+"${patterns[@]}"}; do
    # shellcheck disable=SC2254  # $p is meant to be a glob
    case $1 in $p | */$p) return 0 ;; esac
  done
  return 1
}

# awk walks the -U0 diff, counting each hunk's lines so a content line that
# happens to look like a "+++" header is never mistaken for one. It prints
# "path<TAB>lineno<TAB>text" per added line that starts with a tab.
hits=$(git -c core.quotepath=off diff -U0 --no-color --no-ext-diff --no-renames "$base" HEAD |
  awk '
    old > 0 || new > 0 {
      c = substr($0, 1, 1)
      if (c == "-") old--
      else if (c == "+") {
        new--
        if (substr($0, 2, 1) == "\t") printf "%s\t%d\t%s\n", file, ln, substr($0, 3)
        ln++
      }
      next
    }
    /^\+\+\+ / {
      file = substr($0, 5)
      if (file ~ /^"/) file = substr(file, 2, length(file) - 2)
      sub(/^b\//, "", file)
      next
    }
    /^@@ / {
      split($0, h, " ")
      split(substr(h[2], 2), o, ","); old = (o[2] == "" ? 1 : o[2])
      split(substr(h[3], 2), n, ","); new = (n[2] == "" ? 1 : n[2])
      ln = n[1]
    }
  ')

fail=0
while IFS=$'\t' read -r path lineno text; do
  [ -n "$path" ] || continue
  allowed "$path" && continue
  echo "::error file=${path},line=${lineno}::added line starts with a tab; use spaces (tab-exempt files are listed in .github/config/no-tabs-allowlist.txt of mark-brannan/.github)"
  echo "${path}:${lineno}: added line starts with a tab: ${text}"
  fail=1
done <<< "$hits"

if [ "$fail" -ne 0 ]; then exit 1; fi
echo "ok: no added line starts with a tab (outside the allowlist)"
