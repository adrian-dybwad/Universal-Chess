#!/usr/bin/env bash
# Build id for the web app: a hash of the built files, stamped into the copy
# that ships.
#
# The id replaces __BUILD_ID__ in sw.js (the service-worker cache name) and in
# index.html (the uc-build meta tag). The page compares the id it loaded with
# the id in index.html on disk, which the board reports on /events, and that
# is how an open tab learns a new frontend was published. The id changes only
# when a built file changes. Stamping the build time made every publish look
# like a new frontend, so the banner appeared for an unchanged bundle.

# Print a short hash of every file under <dist-dir> except sw.js.
#
# index.html is hashed with its placeholder, before stamping, so the result
# does not depend on the stamp.
#
# Usage: web_build_hash <dist-dir>
web_build_hash() {
	local dist="$1" hasher
	if command -v sha256sum >/dev/null 2>&1; then
		hasher=(sha256sum)
	else
		hasher=(shasum -a 256)
	fi
	(
		cd "$dist" &&
			find . -type f ! -name sw.js -print0 | LC_ALL=C sort -z | xargs -0 "${hasher[@]}"
	) | "${hasher[@]}" | cut -c1-16
}

# Stamp the build id of <dist-dir> into sw.js and index.html under <target-dir>.
#
# <target-dir> is the copy of <dist-dir> that ships. Prints the id.
#
# Usage: stamp_web_build <dist-dir> <target-dir>
stamp_web_build() {
	local dist="$1" target="$2" build_id file
	build_id="$(web_build_hash "$dist")"
	for file in "${target}/sw.js" "${target}/index.html"; do
		[[ -f "$file" ]] || continue
		# GNU sed takes no backup argument; BSD/macOS sed needs ''.
		if sed --version >/dev/null 2>&1; then
			sed -i "s/__BUILD_ID__/${build_id}/g" "$file"
		else
			sed -i '' "s/__BUILD_ID__/${build_id}/g" "$file"
		fi
	done
	echo "$build_id"
}
