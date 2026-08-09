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
#     Declared exempt    The commit carries a `Changelog: none` trailer, stating
#                        that no entry is owed. Listed, never hidden.
#
#   A commit that genuinely owes no entry says so with a trailer, optionally with
#   a reason:
#
#     Changelog: none -- developer tooling, no user-visible change
#
#   That is the same judgement the changelog rule already requires in the commit
#   body, written where the audit can read it too, so --strict can gate on real
#   omissions without blocking legitimate work. It is parsed as a git trailer, not
#   grepped: a commit explaining why it owes no entry discusses the changelog in
#   its body, and a grep would let that prose exempt the commit from the very gate
#   it is arguing about.
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

# The value of a `Changelog:` trailer, or empty. Parsed with interpret-trailers
# rather than grepped: a commit explaining why it owes no entry naturally
# discusses the changelog in its body, and a grep would let that prose exempt the
# commit from the gate it is arguing about.
changelog_trailer_value() {
	git log -1 --format=%B "$1" \
		| git interpret-trailers --parse \
		| sed -n 's/^[Cc]hangelog:[[:space:]]*//p' \
		| head -1
}

# Whether the commit states no entry is owed. Any value beginning with "none" is
# accepted so a reason can follow, which is the part a reviewer judges.
declares_no_entry_needed() {
	local value lowered
	value="$(changelog_trailer_value "$1")"
	[[ -n $value ]] || return 1
	lowered="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
	[[ $lowered == none* ]]
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
declared_exempt=()
last_changelog_commit=""
if ((${#changelog_commits[@]} > 0)); then
	# Indexed from the length rather than with [-1], which needs bash 4.3; macOS
	# still ships 3.2 as /bin/bash.
	last_changelog_commit="${changelog_commits[$((${#changelog_commits[@]} - 1))]}"
fi
for sha in "${candidates[@]:-}"; do
	[[ -n $sha ]] || continue
	# An explicit declaration outranks both inferences: it is the author saying
	# what the other two groups can only guess at.
	if declares_no_entry_needed "$sha"; then
		declared_exempt+=("$sha")
	# --is-ancestor answers "does a changelog commit come after this one?" over the
	# actual history rather than by comparing positions in a list, so it stays
	# correct on a non-linear range.
	elif [[ -n $last_changelog_commit ]] \
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

# Reported rather than skipped. An exemption nobody sees is indistinguishable from
# the audit not looking, and a wrong call could then never be caught in review.
if ((${#declared_exempt[@]} > 0)); then
	echo "Declared exempt -- the commit states no entry is owed (${#declared_exempt[@]}):"
	echo
	for sha in "${declared_exempt[@]}"; do
		printf '  %s %s\n' "$(git rev-parse --short "$sha")" "$(git log -1 --format=%s "$sha")"
		printf '      Changelog: %s\n\n' "$(changelog_trailer_value "$sha")"
	done
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
