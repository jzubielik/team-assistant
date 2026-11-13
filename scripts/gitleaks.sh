#!/usr/bin/env bash
# Run gitleaks with the project config (.gitleaks.toml); used by the git hooks and make leaks.
#   scripts/gitleaks.sh staged             staged changes (pre-commit)
#   scripts/gitleaks.sh log <git log args> commits selected by git log arguments (pre-push)
#   scripts/gitleaks.sh history            every commit of every ref (make leaks, CI)
# Where gitleaks runs: NATIVE=1 forces the host's gitleaks; otherwise the runner image if it is
# built (make runner), else the host's gitleaks if on PATH. Secrets are never printed (--redact).
set -euo pipefail
root=$(git rev-parse --show-toplevel)
cd "$root"

die() { echo "gitleaks: $*" >&2; exit 2; }

mode=${1:-}
[[ $# -gt 0 ]] && shift
case "$mode" in
staged) args=(git --staged) ;;
log) args=(git "--log-opts=$*") ;;
history) args=(git --log-opts=--all) ;;
*) die "usage: scripts/gitleaks.sh staged | log <git log args> | history" ;;
esac
args+=(--config .gitleaks.toml --redact --verbose --no-banner --no-color --log-level warn .)

run() { "$root/scripts/tool.sh" gitleaks "$@"; }

status=0
run "${args[@]}" || status=$?
if [[ $status -eq 0 ]]; then
	echo "gitleaks: no leaks found ($mode)" >&2
elif [[ $status -eq 1 ]]; then
	cat >&2 <<'EOF'

gitleaks: leak(s) found above (RuleID, File, Line; the values are redacted).
How to fix:
  - Remove the value; use a placeholder (123456789012, user@example.com) or refer to
    local.mk (ACCOUNT_<env>).
  - Staged: edit the file, then git add it again. A file that must not be committed
    (local.mk, *.pem, .env*): git rm --cached <file>.
  - Already committed: amend or rebase the commit before you push.
  - A false positive: the allowlists are in .gitleaks.toml.
EOF
fi
exit "$status"
