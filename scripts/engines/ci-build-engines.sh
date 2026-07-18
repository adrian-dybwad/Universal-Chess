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
#   - Weiss is 64-bit-only: TTIndex (transposition.h) uses the Lemire reduction
#     `((unsigned __int128)key * count) >> 64`, and __int128 is absent on 32-bit
#     ARM, so it builds only for arm64.
#   - Koivisto builds on both arm64 and armhf. Its upstream ARM NEON path is
#     AArch64-only as shipped (broken load/store placeholders vldrq_p128/exit(-1),
#     plus AArch64-only vmull_high_s16, vpaddq_s32, vaddvq_s32), so the build
#     rewrites all of them to a portable NEON path that also compiles on ARMv7
#     (keyed on the unique NEON tokens; the x86 macros are untouched). A PGO link
#     fix (-fprofile-update=single) is needed because armv7 libgcov lacks the
#     atomic value-profiler symbols. Patched builds produce a bit-identical bench
#     (3661572) to x86 on both arches.
#   - Arasan is 64-bit-only and pinned to a release tag. It requires clang (g++
#     rejects its NEON vector-type conversions) and BUILD_TYPE=neon (its non-SIMD
#     NNUE path is disabled by a static_assert), drops the Makefile's gold linker
#     (removed from binutils 2.44), and ships its NNUE network beside the binary.
#     32-bit ARM has no SIMD path in Arasan, so it builds only for arm64.
#   - Rodent IV needs -latomic on 32-bit ARM (8-byte std::atomic lowers to
#     libatomic calls); forced in with --no-as-needed because the recipe places
#     LDFLAGS before the objects.
#   - Reckless is a Rust engine (edition 2024, Rust >= 1.88). The container's apt
#     rustc is 1.63, so its build bootstraps a pinned rustup toolchain and invokes
#     cargo directly with --no-default-features (disables the syzygy feature, the
#     only clang caller). RUSTFLAGS is pinned to a portable baseline rather than
#     target-cpu=native, which is unreliable under QEMU.
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
	# The makefile lives in src/ (there is no root makefile), so build there --
	# matches the device's `cd src && make`. Pin CC=gcc so the prebuilt uses the
	# same compiler as the on-device build (avoids the heavyweight clang dep; the
	# makefile defaults to clang).
	cd /tmp/demolito/src && make -j"$(nproc)" CC=gcc
	cp demolito "${OUT}/"
}

weiss_build() {
	git clone --depth 1 https://github.com/TerjeKir/weiss.git /tmp/weiss
	cd /tmp/weiss/src && make -j"$(nproc)" EXE=weiss
	cp weiss "${OUT}/"
}

koivisto_build() {
	# Builds for both arm64 and armhf. Upstream's ARM NEON path is AArch64-only as
	# shipped, so patch every AArch64-only construct to a portable NEON path that
	# also compiles on ARMv7 (all bit-identical: bench 3661572 on x86, arm64 and a
	# real armv7l board). Mirrors engine_manager's koivisto build_commands.
	#   - nn/defs.h load/store: vldrq_p128 (wrong type) / exit(-1) (a stub) ->
	#     vld1q_s16 / vst1q_s16.
	#   - nn/defs.h avx_madd_epi16: AArch64-only vmull_high_s16 + vpaddq_s32 ->
	#     vmull_s16(vget_high_s16(...)) + vpadd_s32 / vcombine_s32.
	#   - nn/eval.cpp horizontal sum: AArch64-only vaddvq_s32 -> vadd_s32 /
	#     vpadd_s32 / vget_lane_s32.
	# The seds key on the unique NEON RHS tokens, leaving the x86 macros untouched.
	# PGO fix: the default `openbench` goal builds with -fprofile-generate, which
	# under -pthread needs atomic value-profiler gcov symbols that armv7 libgcov
	# lacks (link fails with __gcov_*_profiler_atomic); -fprofile-update=single uses
	# the non-atomic counters and links on both arches (PGO is kept for speed). The
	# NNUE net is embedded via INCBIN (the makefile fetches the networks submodule),
	# so the binary is self-contained.
	git clone --depth 1 https://github.com/Luecx/Koivisto.git /tmp/koivisto
	cd /tmp/koivisto/src_files
	sed -i \
		-e 's|#define avx_load_reg  *vldrq_p128|#define avx_load_reg(a) vld1q_s16((const int16_t*)(a))|' \
		-e 's|#define avx_store_reg  *exit(-1)|#define avx_store_reg(a, b) vst1q_s16((int16_t*)(a), (b))|' \
		-e 's|(vpaddq_s32(vmull_s16(vget_low_s16(a), vget_low_s16(b)), vmull_high_s16(a, b)))|(vcombine_s32(vpadd_s32(vget_low_s32(vmull_s16(vget_low_s16(a), vget_low_s16(b))), vget_high_s32(vmull_s16(vget_low_s16(a), vget_low_s16(b)))), vpadd_s32(vget_low_s32(vmull_s16(vget_high_s16(a), vget_high_s16(b))), vget_high_s32(vmull_s16(vget_high_s16(a), vget_high_s16(b))))))|' \
		nn/defs.h
	sed -i \
		-e 's|return vaddvq_s32(reg);|{ const int32x2_t r2 = vadd_s32(vget_low_s32(reg), vget_high_s32(reg)); return vget_lane_s32(vpadd_s32(r2, r2), 0); }|' \
		nn/eval.cpp
	make EXE=koivisto PGO_PRE_FLAGS='-fprofile-generate -fprofile-update=single -lgcov'
	cp koivisto "${OUT}/"
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

claudia_build() {
	# Sources and Makefile are in the repo root (no src/ subdir); the default
	# `make` target builds a `claudia` binary there. Portable C99 with no x86
	# intrinsics and a gcc-pinned Makefile with no -march, so it builds on both
	# arm64 and armhf -- matches the device's `make`.
	git clone --depth 1 https://github.com/antoniogarro/Claudia.git /tmp/claudia
	cd /tmp/claudia && make -j"$(nproc)"
	cp claudia "${OUT}/"
}

arasan_build() {
	# arm64-only (see gating below). Pinned to a tagged release: master's NEON path
	# has regressed and won't compile. Submodules carry the Syzygy probing code and
	# the NNUE network, both required.
	git clone --depth 1 --branch v25.4 --recurse-submodules --shallow-submodules \
		https://github.com/jdart1/arasan-chess.git /tmp/arasan
	cd /tmp/arasan/src
	# CC=clang++   : g++ rejects Arasan's NEON vector-type conversions; clang is the
	#                compiler doc/BUILD.md requires for ARM.
	# BUILD_TYPE=neon: mandatory -- the non-SIMD NNUE path is disabled by a
	#                static_assert, and neon is the only ARM SIMD path.
	# LDFLAGS=...   : drops the Makefile's hardcoded -fuse-ld=gold (gold was removed
	#                from binutils 2.44 / Trixie); the default bfd linker is used.
	# EXE=arasan    : fixes the output to ../bin/arasan (else arasanx-64).
	make -j2 CC=clang++ EXE=arasan BUILD_TYPE=neon LDFLAGS="-O3 -fno-rtti -DNDEBUG"
	cp ../bin/arasan "${OUT}/"
	# Ship the NNUE network beside the binary. The device's prebuilt installer copies
	# <arch>/<extra_files> next to the engine; the catalog lists the network as the
	# glob *.nnue. Without it Arasan fails at runtime ("failed to open network
	# file"). Copy by glob (not a fixed name) so bumping the pinned tag ships
	# whatever network the new release embeds with no edit here. Each release's
	# network/ dir holds exactly the one network its binary expects.
	cp ../network/*.nnue "${OUT}/"
}

zahak_build() {
	# Zahak's main package is in the zahak/ subdir and engine/nn.go is generated
	# by the Makefile's netgen step from default.nn, so a bare `go build` fails.
	# `make` runs netgen then builds to bin/zahak. CGO_ENABLED=0 avoids fathom's
	# Syzygy C code (no C toolchain assumed) -- matches the on-device build.
	git clone --depth 1 https://github.com/amanjpro/zahak.git /tmp/zahak
	cd /tmp/zahak && make FLAGS=CGO_ENABLED=0
	cp bin/zahak "${OUT}/"
}

reckless_build() {
	# Reckless is a Rust engine (edition 2024, needs Rust >= 1.88). The bookworm
	# container's apt rustc is 1.63 -- too old -- so bootstrap a pinned rustup
	# toolchain, the same approach as the on-device build. rustup/cargo are placed
	# under /opt so they do not depend on $HOME.
	export RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
	curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
		sh -s -- -y --profile minimal --default-toolchain 1.88.0
	. "${CARGO_HOME}/env"
	# Pin the same release tag the catalog installs so the prebuilt matches the
	# source-build fallback.
	git clone --depth 1 --branch v0.9.0 https://github.com/codedeliveryservice/Reckless.git /tmp/reckless
	cd /tmp/reckless
	# Invoke cargo directly, not the repo Makefile: v0.9.0's Makefile has no
	# `no-syzygy` target and hardcodes target-cpu=native. `--no-default-features`
	# disables the syzygy feature, skipping build.rs's Fathom binding (its only
	# clang caller); Rust still links via cc. `--emit link=reckless` writes the
	# binary to the repo root. RUSTFLAGS is pinned to a portable baseline rather
	# than native: under QEMU, native CPU detection can emit instructions a real
	# Pi lacks, producing a prebuilt that SIGILLs on hardware.
	RUSTFLAGS="-C target-cpu=generic" cargo rustc --release --no-default-features -- --emit link=reckless
	cp reckless "${OUT}/"
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

# Berserk, Weiss and Arasan are 64-bit-only. Berserk and Weiss use `__int128`
# (absent on 32-bit ARM: Berserk in its NNUE/eval, Weiss in TTIndex's Lemire
# reduction). Arasan requires SIMD (its non-SIMD NNUE path is disabled by a
# static_assert) and defines NEON flags only for arm64/aarch64, so there is no
# 32-bit ARM build. Koivisto builds on both: its patched NEON path replaces the
# AArch64-only intrinsics with ARMv7-compatible equivalents (see koivisto_build).
# All other engines build on both.
if [ "${ARCH}" = "arm64" ]; then
	build_engine berserk
	build_engine weiss
	build_engine arasan
fi
build_engine koivisto
build_engine ethereal
build_engine demolito
build_engine rodentIV
build_engine ct800
build_engine smallbrain
build_engine zahak
build_engine claudia
build_engine reckless
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
