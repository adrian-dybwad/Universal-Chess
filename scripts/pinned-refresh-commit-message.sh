#!/usr/bin/env bash
# ============================================================================
# Universal-Chess pinned-refresh commit message
# ============================================================================
#
# Description:
#   Build the commit message for a regenerated wheel lock. Reads the lock's
#   unified diff on stdin and writes the message to stdout.
#
#   Two things have to be right here, and neither can be checked by reading the
#   workflow that used to build this message inline. That is why it lives in a
#   script: src/universalchess/tests/test_pinned_refresh_workflow.py runs it.
#
#   The pins that moved are named in the body. A hash-pinned lock diffs into
#   version-and-digest pairs, so nothing -- not the log, not a reviewer, not the
#   release notes -- says what actually changed unless the message says it. The
#   lock's trailing backslashes are dropped on the way in: they are --hash
#   continuation syntax, they mean nothing in a commit message, and interpolating
#   them into a shell heredoc joins the very lines that name the versions.
#
#   The `Changelog: none` declaration is the last paragraph, because git parses
#   trailers only there. A refresh is not itself owed a changelog entry -- a board
#   cannot observe which version of a vendored library it runs -- but the audit
#   has to be able to read that decision. Without it every weekly refresh arrives
#   Undescribed, and a report that is mostly false alarms gets skimmed, which is
#   how the first merged refresh reached main unexplained.
#
#   The declaration is a default, not a verdict. A refresh that resolves an
#   advisory *is* owed an entry, this script cannot tell one from routine version
#   drift, and so it records the condition for the reviewer who can.
#
#   Diff content only ever reaches printf as an argument, never the shell, so a
#   lock line cannot be evaluated however it is spelled.
#
# Usage:
#   git diff --unified=0 -- "$LOCK" | ./scripts/pinned-refresh-commit-message.sh
#
# Exit status:
#   0  a message was written to stdout
#   1  the diff moved no pins, so there is nothing to describe
#
#   Refusing is deliberate, and the caller is expected to fail with it. A message
#   announcing pins it cannot name is wrong in the log permanently, and nothing
#   downstream can notice. A lock that changed without any pin moving means the
#   generator's header was edited without the lock being regenerated, which is
#   worth stopping for rather than describing as a refresh.
# ============================================================================

set -uo pipefail

readonly SUBJECT="Refresh the pinned Python closure"

# Added and removed pin lines, with the --hash continuation backslash stripped.
# A distribution name starts at column one, so hunk headers (@@), file headers
# (---, +++) and unchanged context lines all fail the match; what survives is
# exactly the versions that moved.
moved="$(sed -n 's/[[:space:]]*\\$//; /^[+-][a-z]/p')"

if [[ -z $moved ]]; then
	echo "Refusing to write a commit message: no pins moved in the diff on" \
		"stdin. The lock changed some other way -- most likely the generated" \
		"header -- and calling that a refresh would misdescribe it." >&2
	exit 1
fi

printf '%s\n\n' "$SUBJECT"
printf 'Pins that moved:\n\n'
printf '%s\n\n' "$moved"
printf 'An entry is owed instead when a moved pin resolves an advisory or changes\n'
printf 'behaviour a board can show; this declaration assumes routine drift.\n\n'
# The overriding condition belongs in the trailer, not only in the paragraph
# above it: the audit report prints the trailer value and nothing else, so a
# reason left in the body is invisible exactly where the exemption is reviewed.
printf 'Changelog: none -- version drift a board cannot observe;'
printf ' an advisory fix would owe an entry\n'
