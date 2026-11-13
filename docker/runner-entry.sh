#!/bin/bash
# Entry point of the tool runner. The dependency volumes belong to one image tag, and the tag
# hashes the lock files, so an empty volume means "this set of locked dependencies was never
# installed": install it once, leave a stamp, then run the requested command.
set -euo pipefail
stamp=/work/.venv/.runner-synced
if [[ -f /work/uv.lock && ! -e $stamp ]]; then
	echo "==> runner: installing locked dependencies into the volumes (first run of this image tag)" >&2
	(cd /work && uv sync --locked --quiet) >&2
	# Each part (infra/, web/) is installed when it has a lock file.
	if [[ -f /work/infra/uv.lock ]]; then
		(cd /work/infra && uv sync --locked --quiet) >&2
	fi
	if [[ -f /work/infra/package-lock.json ]]; then
		(cd /work/infra && npm ci --no-audit --no-fund --silent) >&2
	fi
	if [[ -f /work/web/package-lock.json ]]; then
		(cd /work/web && npm ci --no-audit --no-fund --silent) >&2
	fi
	touch "$stamp"
fi
exec "$@"
