#!/usr/bin/env bash
# ============================================================================
# Universal-Chess changelog audit
# ============================================================================
#
# Description:
#   List commits in a range that changed something observable without touching
#   CHANGELOG.md, so a change cannot ship undescribed. Three did -- move times in
#   exported PGN, the screen blanking between splash and menu, and the clock
#   sync-state caching -- each found only by reading the log by hand before a
#   push. This encodes that reading.
#
#   Operates on the repository in the current directory.
#
#   The project accepts two shapes: the entry in the same commit, or a following
#   "Note ... in the changelog" commit. Whether such a later commit *covers* a
#   given change is a question about prose that no script can answer, but whether
#   one *exists* is mechanical, so the report is split:
#
#     Undescribed        No changelog commit follows. An entry is owed, unless the
#                        change turns out not to be observable.
#     Possibly described A changelog commit follows. Read it and confirm.
#
#   Advisory by default, on purpose. A check that usually fails gets bypassed, and
#   a bypassed check is worse than none. --strict is provided for wiring it into
#   something that must block, and gates on the Undescribed group only.
#
# Usage:
#   ./scripts/changelog-audit.sh [--strict] [BASE]
#
#   BASE       Ref the range starts after (exclusive). Defaults to origin/main,
#              falling back to the most recent tag.
#   --strict   Exit non-zero when candidates exist.
#
# Exit status:
#   0  nothing undescribed, or findings reported in the default advisory mode
#   1  an undescribed commit was found and --strict was given
#   2  usage error, not a repository, or BASE does not resolve
#
#   An unresolvable BASE is an error rather than an empty report: a range that was
#   never examined must not read like one with nothing owed. That confusion is the
#   same false reassurance the deploy verification was fixed to stop giving.
#
# Merge commits are skipped; their contents are audited through the commits they
# merge, and diffing a merge would attribute those files twice.
# ============================================================================

set -uo pipefail

STRICT=0
BASE=""

usage() { awk 'NR>4 && /^# ={10,}/ {exit} NR>4 {sub(/^# ?/, ""); print}' "${BASH_SOURCE[0]}"; }

while [[ $# -gt 0 ]]; do
	case "$1" in
		--strict) STRICT=1; shift ;;
		-h|--help) usage; exit 0 ;;
		-*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
		*) BASE="$1"; shift ;;
	esac
done

if ! git rev-parse --git-dir >/dev/null 2>&1; then
	echo "Not a git repository: $(pwd)" >&2
	exit 2
fi

# Paths whose change cannot be observed by a user of the product. Deliberately
# narrow: a needless candidate costs one glance, while a wrongly exempted path
# ships a change undescribed, which is the failure this script exists to catch.
# Notably absent is scripts/ -- deploy-to-pi.sh has its own changelog entries, so
# developer tooling is documented here and must stay in scope.
readonly EXEMPT_PATH_PATTERN='(^src/universalchess/tests/|(^|/)tests/|\.test\.tsx?$|^CHANGELOG\.md$|^\.cursor/|(^|/)MODULE_SUMMARY\.md$)'

readonly CHANGELOG_PATH="CHANGELOG.md"

# Resolve the default base only when the caller gave none, and say which was
# chosen: an audit of a different range than the reader assumes is misleading
# even when its output is correct.
if [[ -z $BASE ]]; then
	if git rev-parse --verify --quiet 'origin/main^{commit}' >/dev/null; then
		BASE="origin/main"
	elif BASE="$(git describe --tags --abbrev=0 2>/dev/null)" && [[ -n $BASE ]]; then
		: # most recent tag
	else
		echo "No BASE given and neither origin/main nor any tag resolves." >&2
		echo "Pass a base explicitly, e.g. ./scripts/changelog-audit.sh HEAD~10" >&2
		exit 2
	fi
fi

if ! git rev-parse --verify --quiet "${BASE}^{commit}" >/dev/null; then
	echo "BASE does not resolve: ${BASE}" >&2
	echo "Nothing was audited. Pass a ref that exists." >&2
	exit 2
fi

readonly RANGE="${BASE}..HEAD"

# Every non-exempt path the commit changed, one per line.
observable_paths() {
	git diff-tree --no-commit-id --name-only -r "$1" | while read -r path; do
		[[ -n $path ]] || continue
		[[ $path =~ $EXEMPT_PATH_PATTERN ]] || printf '%s\n' "$path"
	done
}

commit_touches_changelog() {
	git diff-tree --no-commit-id --name-only -r "$1" | grep -qxF "$CHANGELOG_PATH"
}

candidates=()
changelog_commits=()

while read -r sha; do
	[[ -n $sha ]] || continue
	if commit_touches_changelog "$sha"; then
		changelog_commits+=("$sha")
		continue
	fi
	paths="$(observable_paths "$sha")"
	[[ -n $paths ]] && candidates+=("$sha")
done < <(git rev-list --reverse --no-merges "$RANGE")

# Split the candidates by whether any changelog commit follows them in the range.
# Whether such a commit *covers* a candidate is a question about prose, but
# whether one *exists* is mechanical -- and a candidate with none is owed an entry
# outright. Reported apart because a flat list where most lines are false alarms
# trains the reader to skim it, which defeats the whole exercise.
undescribed=()
possibly_described=()
last_changelog_commit=""
if ((${#changelog_commits[@]} > 0)); then
	last_changelog_commit="${changelog_commits[-1]}"
fi
for sha in "${candidates[@]:-}"; do
	[[ -n $sha ]] || continue
	# --is-ancestor answers "does a changelog commit come after this one?" over the
	# actual history rather than by comparing positions in a list, so it stays
	# correct on a non-linear range.
	if [[ -n $last_changelog_commit ]] \
		&& git merge-base --is-ancestor "$sha" "$last_changelog_commit"; then
		possibly_described+=("$sha")
	else
		undescribed+=("$sha")
	fi
done

print_commits() {
	local sha
	for sha in "$@"; do
		printf '  %s %s\n' "$(git rev-parse --short "$sha")" "$(git log -1 --format=%s "$sha")"
		observable_paths "$sha" | sed 's/^/      /'
		echo
	done
}

echo "Changelog audit of ${RANGE}"
echo

if ((${#candidates[@]} == 0)); then
	echo "Clean: no commits changed observable files without touching ${CHANGELOG_PATH}."
fi

if ((${#undescribed[@]} > 0)); then
	echo "Undescribed -- no changelog commit follows these (${#undescribed[@]}):"
	echo
	print_commits "${undescribed[@]}"
fi

if ((${#possibly_described[@]} > 0)); then
	echo "Possibly described -- a changelog commit follows these; confirm it covers" \
		"them (${#possibly_described[@]}):"
	echo
	print_commits "${possibly_described[@]}"
fi

if ((${#changelog_commits[@]} > 0)); then
	echo "Changelog commits in the range:"
	for sha in "${changelog_commits[@]}"; do
		printf '  %s %s\n' "$(git rev-parse --short "$sha")" "$(git log -1 --format=%s "$sha")"
	done
fi

# Gates on the mechanical finding only. Failing on every candidate would block
# properly documented work, and a hook that does that gets bypassed.
if ((STRICT == 1 && ${#undescribed[@]} > 0)); then
	exit 1
fi
exit 0
