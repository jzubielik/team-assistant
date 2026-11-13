#!/usr/bin/env bash
# Run one runner tool (gitleaks, trufflehog, actionlint, zizmor, hadolint, ...) in the
# repository root; used by the git hooks and scripts/gitleaks.sh, scripts/scan.sh.
#   scripts/tool.sh [--mount <host dir>] <tool> [args...]
# Where it runs: NATIVE=1 forces the host's tool; otherwise the runner image if it is built
# (make runner), else the host's tool if on PATH. --mount makes a directory outside the
# repository visible in the container at the same path.
set -euo pipefail
root=$(git rev-parse --show-toplevel)
cd "$root"

die() {
	echo "tool: $*" >&2
	exit 2
}

mounts=()
while [[ "${1:-}" == --mount ]]; do
	mounts+=(-v "$2:$2")
	shift 2
done
tool=${1:?usage: scripts/tool.sh [--mount <dir>] <tool> [args...]}
shift

if [[ -n "${NATIVE:-}" ]]; then
	command -v "$tool" >/dev/null || die "NATIVE=1 but $tool is not on PATH"
	exec "$tool" "$@"
fi
image=""
if command -v docker >/dev/null; then
	image=$(make --no-print-directory -s runner-image LOCAL_MK=/dev/null 2>/dev/null || :)
	[[ -n "$image" ]] && ! docker image inspect "$image" >/dev/null 2>&1 && image=""
fi
if [[ -n "$image" ]]; then
	exec docker run --rm -i --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$root:/work" -w /work \
		"${mounts[@]}" --entrypoint "$tool" "$image" "$@"
elif command -v "$tool" >/dev/null; then
	exec "$tool" "$@"
fi
die "neither the runner image nor $tool found: run 'make runner' (or install $tool)"
