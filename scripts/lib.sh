# shellcheck shell=bash
# Shared helpers for scripts/ops.sh (sourced, not executed).
# Inputs come from the Makefile as environment variables. The Makefile takes them from its
# command line or local.mk only, and CONFIG_ORIGINS says which:
#   ENV PROFILE REGION ACCOUNT CONFIG_ORIGINS
# PROFILE and REGION have no default.

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC2034 # used by scripts/ops.sh
INFRA="$ROOT/infra"
ENV=${ENV:-} PROFILE=${PROFILE:-} REGION=${REGION:-} ACCOUNT=${ACCOUNT:-}
# Set by need_aws (names from scripts/names.py, identity from sts); used by scripts/ops.sh.
# shellcheck disable=SC2034
STACK="" BOUNDARY_STACK="" RUNTIME="" MEMORY="" GITHUB_PROVIDER="" GITHUB_SECRET="" EXCHANGE_SECRET="" GITHUB_RETURN_URL="" GATEWAY_TARGET="" MEMORY_TARGET="" QUALIFIER="" TOOLKIT_STACK="" POLICIES_STACK="" EXECUTION_POLICIES=""
# shellcheck disable=SC2034
BOOTSTRAP_PARAMETER="" CALLER_ACCOUNT="" CALLER_ARN=""
# The profile and region always come from PROFILE and REGION (passed explicitly to each AWS
# command), never from these.
unset AWS_PROFILE AWS_DEFAULT_PROFILE AWS_REGION AWS_DEFAULT_REGION
# The CDK CLI asks the npm registry for a newer release unless this is set. The version is
# pinned in infra/package.json, so the answer changes nothing.
export CDK_DISABLE_VERSION_CHECK=1

die() {
	echo "error: $*" >&2
	exit 1
}

# First line of every command that makes no AWS call.
banner_offline() { # title
	echo "==> $1 (offline: no AWS call)"
}

# Python from the root project (scripts/ helpers).
py() {
	(cd "$ROOT" && uv run --quiet python "$@")
}

aws_() {
	aws --profile "$PROFILE" --region "$REGION" "$@"
}

# Replace the caller's account id in text that goes to the terminal: output gets pasted into
# documents and tickets, and the account id must not travel with it.
mask() {
	local text=$1
	[[ -z "${CALLER_ACCOUNT:-}" ]] || text=${text//$CALLER_ACCOUNT/<account>}
	printf '%s\n' "$text"
}

# First lines of every command that calls AWS.
banner() { # title
	echo "==> $1"
	echo "    env=$ENV region=$REGION profile=$PROFILE"
	[[ -z "${CONFIG_ORIGINS:-}" ]] || echo "    settings from: $CONFIG_ORIGINS"
}

# Every AWS command starts here: the settings, the names, then the account check. The profile
# must point at the account recorded for this environment (ACCOUNT_<env> in local.mk); nothing
# is created, changed or deleted in any other account.
need_aws() {
	[[ -n "$ENV" ]] || die "ENV is required, e.g. make <target> ENV=dev"
	[[ -n "$PROFILE" ]] || die "PROFILE is required: make <target> PROFILE=<admin-profile>, or set it in local.mk (local.mk.example)"
	[[ -n "$REGION" ]] || die "REGION is required: make <target> REGION=<region>, or set it in local.mk (local.mk.example)"
	local assignments
	assignments=$(py scripts/names.py --env "$ENV") || exit 1
	eval "$assignments"
	[[ -n "$ACCOUNT" ]] || die "ACCOUNT_$ENV is not set: add ACCOUNT_$ENV = <account id> to local.mk (the account of this environment)"
	local identity
	identity=$(aws_ sts get-caller-identity --query '[Account,Arn]' --output text 2>/dev/null) ||
		die "could not read the identity of profile '$PROFILE': log in first (aws sso login --profile $PROFILE)"
	# shellcheck disable=SC2034 # CALLER_ARN is used by scripts/ops.sh
	read -r CALLER_ACCOUNT CALLER_ARN <<<"$identity"
	[[ "$CALLER_ACCOUNT" == "$ACCOUNT" ]] ||
		die "profile '$PROFILE' is not in the account set as ACCOUNT_$ENV; nothing done"
}
