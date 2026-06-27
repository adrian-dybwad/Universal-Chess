#!/usr/bin/env bash
# ============================================================================
# CI engine builder (runs inside the QEMU arch container)
# ============================================================================
#
# Builds the prebuilt chess-engine binaries for one target architecture and
# stages them under engine-binaries/<arch>/ for packaging into
# engines-<arch>.tar.gz (a GitHub release asset the device downloads).
#
# Why per-engine isolation:
#   Each engine builds in its own subshell. A single engine failing (an upstream
#   makefile change, a 32-bit incompatibility, a flaky download) is logged and
#   skipped instead of aborting the run. Previously the whole script used
#   `set -e`, so ONE broken engine discarded the entire archive for that arch --
#   which is why no engines-armhf.tar.gz was ever produced. The on-device source
#   build remains the fallback for any engine missing from the archive, so
#   shipping a partial archive is strictly better than shipping none.
#
# Architecture notes:
#   - Berserk is 64-bit-only (uses __int128 and AArch64-only NEON intrinsics) and
#     its makefile default goal forces x86 avx2/clang, so it is built only for
#     arm64 and via `make build ARCH=native CC=gcc` (host arch, gcc, no clang).
#   - Koivisto is NOT built here: its NNUE layer is x86-SIMD only with a broken
#     upstream ARM NEON path (store op is `exit(-1)`, load type-mismatches), so it
#     fails to compile on both armv7l and aarch64. It is marked unsupported in the
#     engine catalog (supported_archs=frozenset()) and the install button is
#     disabled, so there is nothing for the archive to ship.
#   - Rodent IV needs -latomic on 32-bit ARM (8-byte std::atomic lowers to
#     libatomic calls); forced in with --no-as-needed because the recipe places
#     LDFLAGS before the objects.
#
# Usage: ci-build-engines.sh <arch>     where <arch> is arm64 | armhf
# Exit status: always 0 (a missing engine is non-fatal); a per-engine summary is
# printed so failures are visible in the CI log.
# ============================================================================

set -u

ARCH="${1:?usage: ci-build-engines.sh <arch>}"
OUT="/workspace/engine-binaries/${ARCH}"
mkdir -p "${OUT}/maia/maia_weights" "${OUT}/maia/leela_weights"

SUCCEEDED=()
FAILED=()

# Run one engine build (a shell function) in an isolated `set -e` subshell so a
# mid-recipe failure stops only that engine. Records the outcome for the summary.
build_engine() {
	local name="$1"
	echo "=== Building ${name} (${ARCH}) ==="
	if ( set -e; "${name}_build" ); then
		SUCCEEDED+=("${name}")
		echo "--- ${name}: OK ---"
	else
		FAILED+=("${name}")
		echo "!!! ${name}: FAILED (skipped) !!!"
	fi
}

berserk_build() {
	git clone --depth 1 https://github.com/jhonnold/berserk.git /tmp/berserk
	cd /tmp/berserk/src
	# `build` (not the default `openbench`) avoids the forced x86 ARCH=avx2; it
	# also pulls in download-network to embed the NNUE file. ARCH=native targets
	# the host (AArch64 NEON on arm64); CC=gcc avoids a clang dependency.
	make build ARCH=native CC=gcc EXE=berserk
	cp berserk "${OUT}/"
}

ethereal_build() {
	git clone --depth 1 https://github.com/AndyGrant/Ethereal.git /tmp/ethereal
	# Ethereal's Makefile defaults to CC=clang; pin CC=gcc so the prebuilt is
	# built with the same compiler the on-device source fallback uses (engine
	# deps provide gcc, not clang). Keeps the two build paths consistent and
	# removes ethereal's incidental reliance on clang being installed in CI.
	cd /tmp/ethereal/src && make -j2 CC=gcc EXE=ethereal
	cp ethereal "${OUT}/"
}

demolito_build() {
	git clone --depth 1 https://github.com/lucasart/Demolito.git /tmp/demolito
	cd /tmp/demolito && make -j"$(nproc)" CC=clang
	cp demolito "${OUT}/"
}

weiss_build() {
	git clone --depth 1 https://github.com/TerjeKir/weiss.git /tmp/weiss
	cd /tmp/weiss/src && make -j"$(nproc)" EXE=weiss
	cp weiss "${OUT}/"
}

rodentIV_build() {
	git clone --depth 1 https://github.com/nescitus/rodent-iv.git /tmp/rodent
	cd /tmp/rodent/sources
	local ldflags="-s -lm"
	if [ "${ARCH}" = "armhf" ]; then
		ldflags="${ldflags} -Wl,--no-as-needed -latomic"
	fi
	make -j"$(nproc)" EXENAME=../rodentIV LDFLAGS="${ldflags}"
	cp /tmp/rodent/rodentIV "${OUT}/"
}

ct800_build() {
	git clone --depth 1 https://github.com/bcm314/CT800.git /tmp/ct800
	cd /tmp/ct800/source/application-uci && mkdir -p output && bash make_ct800_raspi.sh
	cp output/CT800_* "${OUT}/ct800"
}

smallbrain_build() {
	git clone --depth 1 https://github.com/Disservin/Smallbrain.git /tmp/smallbrain
	cd /tmp/smallbrain/src && make -j2 EXE=smallbrain
	cp smallbrain "${OUT}/"
}

arasan_build() {
	git clone --depth 1 https://github.com/jdart1/arasan-chess.git /tmp/arasan
	cd /tmp/arasan/src
	# Arasan writes to ../bin and names the exe arasanx-<bits> unless EXE is set;
	# EXE=arasan fixes the name/location to ../bin/arasan (matches the device
	# binary_path). NEON only on 64-bit ARM (the Makefile defines NEON flags for
	# arm64/aarch64 only); armhf falls back to the scalar NNUE path.
	local arasan_args="EXE=arasan"
	if [ "${ARCH}" = "arm64" ]; then
		arasan_args="${arasan_args} BUILD_TYPE=neon"
	fi
	make -j2 ${arasan_args}
	cp ../bin/arasan "${OUT}/"
}

zahak_build() {
	git clone --depth 1 https://github.com/amanjpro/zahak.git /tmp/zahak
	cd /tmp/zahak && go build -o zahak
	cp zahak "${OUT}/"
}

maia_build() {
	git clone --depth 1 --branch v0.32.1 --recurse-submodules \
		https://github.com/LeelaChessZero/lc0.git /tmp/lc0
	cd /tmp/lc0
	rm -rf build/release
	CC=clang CXX=clang++ meson setup build/release --buildtype=release \
		-Dplain_cuda=false -Dcudnn=false -Dopencl=false -Ddx=false \
		-Donednn=false -Dmetal=disabled -Dispc=false -Dgtest=false \
		-Donnx=false -Dnvcc=false -Dpython_bindings=false \
		-Dpopcnt=false -Df16c=false -Dpext=false
	ninja -C build/release -j2
	cp build/release/lc0 "${OUT}/maia/lc0"

	# Maia weights (human-like play across ELO levels).
	cd "${OUT}/maia/maia_weights"
	for elo in 1100 1200 1300 1400 1500 1600 1700 1800 1900; do
		wget -q "https://github.com/CSSLab/maia-chess/releases/download/v1.0/maia-${elo}.pb.gz"
	done

	# Leela weights: T1-256x10 (small/fast, CPU-friendly).
	cd "${OUT}/maia/leela_weights"
	wget -q -O t1-256x10.pb.gz "https://training.lczero.org/get_network?sha=00af53b081e80147172e6f281c01571016924e9aac89cdf6666a1cc3a4ecf5bf"
}

# Berserk only on 64-bit ARM (see header). Koivisto is excluded entirely (no
# working ARM build). All other engines build on both arches.
if [ "${ARCH}" = "arm64" ]; then
	build_engine berserk
fi
build_engine ethereal
build_engine demolito
build_engine weiss
build_engine rodentIV
build_engine ct800
build_engine smallbrain
build_engine arasan
build_engine zahak
build_engine maia

echo "=== ${ARCH} build summary ==="
echo "Succeeded (${#SUCCEEDED[@]}): ${SUCCEEDED[*]:-none}"
echo "Failed (${#FAILED[@]}): ${FAILED[*]:-none}"
echo "=== staged binaries ==="
ls -la "${OUT}/" || true
ls -la "${OUT}/maia/" || true

# Non-fatal: package whatever built. The device falls back to source build for
# anything missing.
exit 0
