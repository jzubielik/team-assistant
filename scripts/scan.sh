#!/usr/bin/env bash
# Scanners besides gitleaks (scripts/gitleaks.sh). Each tool runs through scripts/tool.sh
# (runner image, or NATIVE=1 / PATH).
#   scripts/scan.sh lint      actionlint, zizmor (offline), hadolint, shellcheck on every workflow, Dockerfile and shell script (make check)
#   scripts/scan.sh staged    trufflehog on the staged files; the linters when workflows or Dockerfiles are staged (pre-commit)
#   scripts/scan.sh history   trufflehog over every commit (make leaks)
#   scripts/scan.sh audit     pip-audit on the locked dependencies of / and, once it exists, infra/ (make audit; network)
# trufflehog fails on verified and unknown results (unknown: verification could not finish,
# e.g. offline). Secrets are never committed to fix a finding; see the gitleaks.sh hints.
set -euo pipefail
root=$(git rev-parse --show-toplevel)
cd "$root"
tool="$root/scripts/tool.sh"
TRUFFLEHOG=('--results=verified,unknown' --fail --no-update --no-color)

actionlint() {
	echo "+ actionlint" >&2
	"$tool" actionlint -no-color
}
zizmor() {
	echo "+ zizmor --offline" >&2
	"$tool" zizmor --offline --no-progress --color=never .github
}
hadolint() {
	echo "+ hadolint docker/*.Dockerfile" >&2
	"$tool" hadolint --no-color docker/*.Dockerfile
}

shellcheck() {
	echo "+ shellcheck scripts/*.sh docker/*.sh .githooks/*" >&2
	"$tool" shellcheck -x scripts/*.sh docker/*.sh .githooks/*
}

staged() {
	local status=0 tmp
	tmp=$(mktemp -d)
	trap 'rm -rf "$tmp"' RETURN
	# The staged content (not the working tree) of every added or changed file.
	git diff --cached --name-only -z --diff-filter=ACMR | git checkout-index --stdin -z --prefix="$tmp/"
	if [[ -n "$(ls -A "$tmp")" ]]; then
		"$tool" --mount "$tmp" trufflehog filesystem "${TRUFFLEHOG[@]}" "$tmp" || status=1
	fi
	local files
	files=$(git diff --cached --name-only --diff-filter=ACMR)
	if grep -q '^\.github/' <<<"$files"; then
		actionlint || status=1
		zizmor || status=1
	fi
	if grep -qi 'dockerfile$' <<<"$files"; then
		hadolint || status=1
	fi
	return "$status"
}

# Known vulnerabilities accepted for now, each with its reason. Remove an entry when the
# dependency that blocks the fix allows it.
# GHSA-mf5v-w3w3-f6cg (CVE-2026-126360), opentelemetry-api 1.45.1, fixed in 1.45.2: quadratic
# regular-expression time when a traceparent value holds a newline. aws-opentelemetry-distro
# 0.22.0 (the latest release) pins opentelemetry-api and opentelemetry-sdk to exactly 1.45.1.
# Not reachable here: HTTP header values cannot hold a newline, and the agent extracts no trace
# context from a JSON body.
AUDIT_IGNORED=(--ignore-vuln GHSA-mf5v-w3w3-f6cg)

audit() {
	local status=0 dir req
	for dir in . infra; do
		[[ -f "$dir/uv.lock" ]] || continue
		echo "+ pip-audit ($dir/uv.lock)" >&2
		req=$(mktemp)
		(cd "$dir" && uv export --frozen --no-emit-project --quiet --format requirements-txt -o "$req")
		pip-audit --strict --disable-pip --require-hashes --progress-spinner off "${AUDIT_IGNORED[@]}" \
			-r "$req" || status=1
		rm -f "$req"
	done
	return "$status"
}

case "${1:-}" in
lint)
	s=0
	# No workflow directory, nothing to lint (actionlint fails without one).
	if [[ -d .github/workflows ]]; then
		actionlint || s=1
		zizmor || s=1
	fi
	hadolint || s=1
	shellcheck || s=1
	exit "$s"
	;;
staged) staged ;;
history)
	echo "+ trufflehog git (every commit)" >&2
	"$tool" trufflehog git "${TRUFFLEHOG[@]}" file://.
	;;
audit) audit ;;
*)
	echo "usage: scripts/scan.sh lint | staged | history | audit" >&2
	exit 2
	;;
esac
