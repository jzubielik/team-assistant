#!/usr/bin/env bash
# Operator commands behind the Makefile (make help lists them). Usage: scripts/ops.sh <command>
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

cmd_setup() {
	banner_offline "setup: check tools, install locked dependencies"
	local tool missing=()
	for tool in uv aws docker node npm npx; do
		command -v "$tool" >/dev/null || missing+=("$tool")
	done
	((${#missing[@]} == 0)) || die "missing tools: ${missing[*]}"
	local awsv
	awsv=$(aws --version 2>&1)
	[[ "$awsv" == aws-cli/2.* ]] || die "AWS CLI v2 is required (found: $awsv)"
	# Offline probe: prints a request skeleton, no API call (works without groff, unlike help).
	aws bedrock-agentcore-control list-agent-runtimes --generate-cli-skeleton </dev/null >/dev/null 2>&1 ||
		die "this AWS CLI ($awsv) has no bedrock-agentcore-control commands; update the AWS CLI v2"
	docker info >/dev/null 2>&1 || die "docker daemon not reachable (docker info failed): start Docker"
	docker buildx version >/dev/null 2>&1 || die "docker buildx is missing"
	# The agent image is linux/arm64; an x86-64 host builds it under QEMU emulation.
	if [[ $(uname -m) != aarch64 && $(uname -m) != arm64 ]] &&
		! docker buildx ls 2>/dev/null | grep -q 'linux/arm64'; then
		echo "warning: no linux/arm64 builder; run make arm64 before the first deploy of the agent image (lost on reboot)"
	fi
	echo "    tools: $(uv --version), $awsv, node $(node --version)"
	(cd "$ROOT" && uv sync --locked)
	if [[ -f "$INFRA/uv.lock" ]]; then
		(cd "$INFRA" && uv sync --locked)
	fi
	if [[ -f "$INFRA/package-lock.json" ]]; then
		(cd "$INFRA" && npm ci --no-audit --no-fund)
	fi
	if [[ -f "$ROOT/web/package-lock.json" ]]; then
		(cd "$ROOT/web" && npm ci --no-audit --no-fund)
	fi
	echo "setup: done"
}

# Each command is echoed before it runs, so a failure names the step.
cmd_check() {
	banner_offline "check: ruff, actionlint, zizmor, hadolint, shellcheck, pytest (not integration), web"
	local c commands=("uv run ruff check" "uv run ruff format --check" "NATIVE=1 scripts/scan.sh lint"
		"uv run pytest -m 'not integration'")
	if [[ -f "$INFRA/pyproject.toml" ]]; then
		commands+=("cd infra && uv run pytest")
	fi
	# The page: types, unit tests and a production build.
	if [[ -f "$ROOT/web/package.json" ]]; then
		commands+=("cd web && npm run --silent check")
	fi
	for c in "${commands[@]}"; do
		echo "+ $c"
		(cd "$ROOT" && eval "$c")
	done
}

# Writes the CloudFormation template to infra/cdk.out. No AWS call: no context lookups, no
# notices fetch. Without ACCOUNT (no ENV on the command line) a placeholder account is used;
# the template is for reading, deploy always passes the real one.
cmd_synth() {
	ENV=${ENV:-dev}
	[[ -n "$REGION" ]] || die "REGION is required: make synth REGION=<region>, or set it in local.mk"
	banner_offline "synth: synthesize TeamAssistant-$ENV for region $REGION"
	[[ -n "$ACCOUNT" ]] || echo "    ACCOUNT not set: using the placeholder 123456789012"
	(cd "$INFRA" && npx --no-install cdk synth --no-lookups --no-notices -q \
		-c "env=$ENV" -c "account=${ACCOUNT:-123456789012}" -c "region=$REGION" \
		-c "github_client_id=${GITHUB_CLIENT_ID:-placeholder}" -c "login_suffix=${LOGIN_SUFFIX:-00000000}")
	echo "synth: templates in infra/cdk.out"
}

# Deploy and diff need the GitHub OAuth App's client id and the login suffix; bootstrap and
# destroy do not (the placeholders in cdk_ only let the app synthesize).
need_github_client_id() {
	[[ -n "${GITHUB_CLIENT_ID:-}" ]] ||
		die "GITHUB_CLIENT_ID_$ENV is not set: add the client id of the GitHub OAuth App to local.mk (docs/runbook.md)"
}

need_login_suffix() {
	[[ -n "${LOGIN_SUFFIX:-}" ]] ||
		die "LOGIN_SUFFIX_$ENV is not set: add LOGIN_SUFFIX_$ENV = <output of: openssl rand -hex 4> to local.mk (docs/runbook.md)"
}

cdk_() {
	(cd "$INFRA" && npx --no-install cdk "$@" --profile "$PROFILE" --no-notices \
		-c "env=$ENV" -c "account=$CALLER_ACCOUNT" -c "region=$REGION" \
		-c "github_client_id=${GITHUB_CLIENT_ID:-placeholder}" -c "login_suffix=${LOGIN_SUFFIX:-00000000}")
}

# Die unless this project's CDK bootstrap exists in the account and region.
need_bootstrap() {
	local out
	if ! out=$(aws_ ssm get-parameter --name "$BOOTSTRAP_PARAMETER" --query Parameter.Value --output text 2>&1); then
		[[ "$out" == *ParameterNotFound* ]] &&
			die "no team-assistant CDK bootstrap in this account and region ($BOOTSTRAP_PARAMETER): run make bootstrap ENV=$ENV"
		die "could not read the CDK bootstrap version: $(mask "$out")"
	fi
	echo "    CDK bootstrap version $out ($BOOTSTRAP_PARAMETER)"
}

# Once per account and region, shared by every environment (docs/adr/0001).
cmd_bootstrap() {
	need_aws
	banner "bootstrap: CDK bootstrap of this project (qualifier $QUALIFIER) in $REGION"
	echo "    creates or updates two stacks:"
	echo "      $POLICIES_STACK: the IAM policies $EXECUTION_POLICIES"
	echo "      $TOOLKIT_STACK: assets bucket, image repository, 5 roles cdk-$QUALIFIER-*, SSM $BOOTSTRAP_PARAMETER"
	echo "    no customer KMS key (the bucket uses the AWS managed key), no AdministratorAccess"
	local tmp template pattern partition policy policy_arns=""
	tmp=$(mktemp -d)
	# shellcheck disable=SC2064 # expand now: the directory name is fixed
	trap "rm -rf '$tmp'" EXIT
	template="$tmp/bootstrap-template.json"
	pattern=$(py scripts/bootstrap_template.py --caller-arn "$CALLER_ARN" "$template") ||
		die "could not write the bootstrap template (see above)"
	echo "    the deploy, publishing and lookup roles trust only: $(mask "$pattern")"
	partition=$(cut -d: -f2 <<<"$CALLER_ARN")
	for policy in ${EXECUTION_POLICIES//,/ }; do
		policy_arns+="${policy_arns:+,}arn:$partition:iam::$CALLER_ACCOUNT:policy/$policy"
	done
	echo "--- $POLICIES_STACK"
	aws_ cloudformation deploy --stack-name "$POLICIES_STACK" \
		--template-file "$INFRA/bootstrap/execution-policies.yaml" \
		--capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset --tags project=team-assistant
	echo "--- $TOOLKIT_STACK"
	cdk_ bootstrap "aws://$CALLER_ACCOUNT/$REGION" --qualifier "$QUALIFIER" \
		--toolkit-stack-name "$TOOLKIT_STACK" --template "$template" \
		--bootstrap-kms-key-id AWS_MANAGED_KEY \
		--cloudformation-execution-policies "$policy_arns" --tags project=team-assistant
	echo "bootstrap: done ($POLICIES_STACK, $TOOLKIT_STACK)"
}

cmd_diff() {
	need_aws
	banner "diff: what make deploy would change in $STACK"
	need_bootstrap
	need_github_client_id
	need_login_suffix
	cdk_ diff "$STACK"
}

cmd_deploy() {
	need_aws
	banner "deploy: $STACK"
	need_bootstrap
	need_github_client_id
	need_login_suffix
	echo "--- $BOUNDARY_STACK (permissions boundary of this environment's roles)"
	aws_ cloudformation deploy --stack-name "$BOUNDARY_STACK" \
		--template-file "$INFRA/bootstrap/boundary.yaml" \
		--parameter-overrides "Env=$ENV" "RuntimeName=$RUNTIME" "MemoryName=$MEMORY" \
		--capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset \
		--tags project=team-assistant "env=$ENV"
	echo "--- $STACK"
	cdk_ deploy "$STACK"
	# CloudFormation cannot set return URLs on the workload identity the Runtime creates, so
	# the operator's profile does it after every deploy (same values each time): make chat's
	# listener, and the web page's return page on its development server and on CloudFront.
	local workload web_url
	workload=$(stack_output RuntimeArn)
	workload=${workload##*runtime/}
	web_url=$(stack_output WebUrl)
	echo "--- return URLs for GitHub consent on workload identity $workload"
	aws_ bedrock-agentcore-control update-workload-identity --name "$workload" \
		--allowed-resource-oauth2-return-urls "$GITHUB_RETURN_URL" "$WEB_LOCAL_RETURN_URL" \
		"$web_url/callback.html" >/dev/null
	publish_web
	echo "deploy: done ($STACK); the page: $web_url"
}

# The web page: build it, write this environment's config.json and Content Security
# Policy into it (scripts/web_files.py), upload it and drop CloudFront's cached copies. Vite's
# hashed assets (assets/) are cached for a year; everything else (HTML, config.json, the
# favicon) is revalidated on every load. New assets go up before the HTML that names them, and
# old ones are deleted only after it, so no visitor gets a page whose scripts are gone.
publish_web() {
	# WEB_DIST moves the built page (tests).
	local gateway domain client audience bucket distribution dist=${WEB_DIST:-$ROOT/web/dist}
	gateway=$(stack_output GatewayUrl)
	domain=$(stack_output LoginDomain)
	client=$(stack_output UserPoolClientId)
	audience=$(stack_output Audience)
	bucket=$(stack_output WebBucket)
	distribution=$(stack_output WebDistributionId)
	echo "--- web page: build"
	(cd "$ROOT/web" && npm run --silent build)
	py scripts/web_files.py --dist "$dist" --gateway-url "$gateway" --login-domain "$domain" \
		--client-id "$client" --audience "$audience" --env "$ENV"
	echo "--- web page: upload to s3://$bucket"
	aws_ s3 sync "$dist/assets" "s3://$bucket/assets" --only-show-errors \
		--cache-control "public, max-age=31536000, immutable"
	aws_ s3 sync "$dist" "s3://$bucket" --only-show-errors --exclude "assets/*" --cache-control "no-cache"
	aws_ s3 sync "$dist" "s3://$bucket" --only-show-errors --delete
	echo "--- web page: invalidate CloudFront's cached copies"
	aws_ cloudfront create-invalidation --distribution-id "$distribution" --paths "/*" >/dev/null
}

cmd_web_sync() {
	need_aws
	banner "web-sync: build and upload the page of $STACK (no stack update)"
	publish_web
	local web_url
	web_url=$(stack_output WebUrl)
	echo "web-sync: done; the page: $web_url"
}

cmd_destroy() {
	need_aws
	banner "destroy: delete $STACK and $BOUNDARY_STACK (the CDK bootstrap stays)"
	local answer=""
	read -r -p "Type the stack name ($STACK) to delete it: " answer || :
	[[ "$answer" == "$STACK" ]] || die "the name did not match; nothing deleted"
	delete_secret "GitHub client secret" "$GITHUB_SECRET"
	delete_secret "placeholder secret of the token exchange provider" "$EXCHANGE_SECRET"
	empty_web_bucket
	cdk_ destroy "$STACK" --force
	delete_agent_images
	delete_runtime_logs
	echo "--- $BOUNDARY_STACK"
	aws_ cloudformation delete-stack --stack-name "$BOUNDARY_STACK"
	aws_ cloudformation wait stack-delete-complete --stack-name "$BOUNDARY_STACK"
	echo "destroy: done"
}

# Secrets Manager keeps a deleted secret for a recovery window (default 30 days) under the same
# name, which a later deploy of the same environment could not reuse, and make destroy is to
# leave no data. Each secret of the stack is therefore deleted at once, before the stack;
# CloudFormation then finds it gone.
delete_secret() { # label, name
	echo "--- $1: deleting $2 without a recovery window"
	aws_ secretsmanager delete-secret --secret-id "$2" --force-delete-without-recovery \
		>/dev/null 2>&1 || echo "    (not found: nothing to delete)"
}

# The agent images live in the bootstrap's repository, which outlasts the environment. Every
# deploy that changed the agent pushed one, and nothing else is stored there, so with no other
# environment left the repository is emptied. By digest: an image may have several tags or
# none. At most 100 ids per call (ECR API reference, BatchDeleteImage).
# This project's CDK bootstrap in the account and region: TeamAssistantToolkit and
# team-assistant-cdk-policies (docs/adr/0001). Shared by every environment, so it refuses while
# one is deployed. The bootstrap template keeps its assets bucket (DeletionPolicy: Retain,
# versioned), so the bucket is emptied of every version and deleted after the stack; the image
# repository must be empty for the stack delete. Other bootstraps (CDKToolkit, other projects'
# qualifiers) are not touched.
cmd_unbootstrap() {
	need_aws
	banner "unbootstrap: delete this project's CDK bootstrap ($TOOLKIT_STACK, $POLICIES_STACK) in $REGION"
	local envs
	envs=$(aws_ cloudformation list-stacks \
		--stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE ROLLBACK_COMPLETE \
		UPDATE_ROLLBACK_FAILED DELETE_FAILED CREATE_IN_PROGRESS UPDATE_IN_PROGRESS DELETE_IN_PROGRESS \
		--query "StackSummaries[?starts_with(StackName,'TeamAssistant-')].StackName" --output text) ||
		die "could not list the stacks of the account"
	[[ -z "$envs" || "$envs" == None ]] ||
		die "environments are still deployed ($envs): make destroy ENV=<env> for each first"
	local answer=""
	read -r -p "Type the toolkit stack name ($TOOLKIT_STACK) to delete the bootstrap: " answer || :
	[[ "$answer" == "$TOOLKIT_STACK" ]] || die "the name did not match; nothing deleted"
	local bucket
	bucket=$(aws_ cloudformation describe-stacks --stack-name "$TOOLKIT_STACK" \
		--query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text 2>/dev/null || :)
	delete_agent_images
	echo "--- $TOOLKIT_STACK"
	aws_ cloudformation delete-stack --stack-name "$TOOLKIT_STACK"
	aws_ cloudformation wait stack-delete-complete --stack-name "$TOOLKIT_STACK"
	if [[ -n "$bucket" && "$bucket" != None ]]; then
		echo "--- assets bucket kept by the stack: emptying and deleting it"
		py scripts/empty_bucket.py --profile "$PROFILE" --region "$REGION" --bucket "$bucket"
	fi
	echo "--- $POLICIES_STACK"
	aws_ cloudformation delete-stack --stack-name "$POLICIES_STACK"
	aws_ cloudformation wait stack-delete-complete --stack-name "$POLICIES_STACK"
	echo "unbootstrap: done; make bootstrap ENV=<env> sets it up again"
}

# CloudFormation cannot delete a bucket that holds objects (and the stack sets no
# autoDeleteObjects, which would add a Lambda function): the page's files go first.
empty_web_bucket() {
	local bucket
	bucket=$(aws_ cloudformation describe-stacks --stack-name "$STACK" \
		--query "Stacks[0].Outputs[?OutputKey=='WebBucket'].OutputValue" --output text 2>/dev/null || :)
	[[ -n "$bucket" && "$bucket" != None ]] || return 0
	echo "--- web page: emptying s3://$bucket"
	aws_ s3 rm "s3://$bucket" --recursive --only-show-errors ||
		echo "warning: could not empty s3://$bucket; the stack delete will fail on it"
}

delete_agent_images() {
	local others repository="cdk-$QUALIFIER-container-assets-$CALLER_ACCOUNT-$REGION"
	others=$(aws_ cloudformation list-stacks \
		--stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE \
		--query "StackSummaries[?starts_with(StackName,'TeamAssistant-') && StackName!='$STACK'].StackName" \
		--output text 2>/dev/null || :)
	if [[ -n "$others" && "$others" != None ]]; then
		echo "--- agent images kept: another environment is deployed and may use them ($others)"
		return 0
	fi
	local digest digests=() batch=()
	while read -r digest; do
		[[ "$digest" == sha256:* ]] && digests+=("imageDigest=$digest")
	done < <(aws_ ecr list-images --repository-name "$repository" \
		--query 'imageIds[].imageDigest' --output text 2>/dev/null | tr '\t' '\n' | sort -u)
	((${#digests[@]})) || return 0
	echo "--- agent images: deleting ${#digests[@]} from the bootstrap's image repository"
	while ((${#digests[@]})); do
		batch=("${digests[@]:0:100}")
		digests=("${digests[@]:100}")
		aws_ ecr batch-delete-image --repository-name "$repository" --image-ids "${batch[@]}" >/dev/null ||
			echo "warning: could not delete agent images; delete them by hand (docs/runbook.md, Teardown)"
	done
}

# The Runtime writes its logs to groups it creates itself, outside the stack.
delete_runtime_logs() {
	local group groups
	groups=$(aws_ logs describe-log-groups \
		--log-group-name-prefix "/aws/bedrock-agentcore/runtimes/$RUNTIME-" \
		--query 'logGroups[].logGroupName' --output text 2>/dev/null || :)
	for group in $groups; do
		[[ "$group" == None ]] && continue
		echo "--- log group: deleting $group"
		aws_ logs delete-log-group --log-group-name "$group" || echo "warning: could not delete $group"
	done
}

stack_status() { # stack
	local out
	if out=$(aws_ cloudformation describe-stacks --stack-name "$1" --query 'Stacks[0].StackStatus' --output text 2>&1); then
		echo "$1 $out"
	elif [[ "$out" == *"does not exist"* ]]; then
		echo "$1 does not exist"
	else
		die "could not read the status of $1: $(mask "$out")"
	fi
}

cmd_status() {
	need_aws
	banner "status: stacks of env $ENV and the CDK bootstrap"
	stack_status "$STACK"
	stack_status "$BOUNDARY_STACK"
	stack_status "$TOOLKIT_STACK"
	stack_status "$POLICIES_STACK"
	outlasting
}

# What make destroy leaves behind, so that "both stacks do not exist" is not read as "nothing
# is left". Printed, not queried: none of it belongs to one environment.
outlasting() {
	cat <<-EOF

		Outlasts make destroy ENV=$ENV, on purpose:
		  - the CDK bootstrap ($TOOLKIT_STACK, $POLICIES_STACK): shared by every environment
		    of the account; make unbootstrap ENV=$ENV removes it once no environment is left
		  - the token vault of AgentCore Identity: one per account and region, created by the
		    service with the first credential provider. No API deletes the users' GitHub
		    tokens stored in it; whether they go with the environment's provider is not
		    documented
		  - the GitHub OAuth App: outside AWS; delete it in GitHub (Settings, Developer
		    settings), and a user can revoke its access in their own GitHub settings

		Outlasts it because it cannot be deleted, and expires by itself:
		  - CloudWatch metrics that AWS services recorded for the environment: 15 months
		  - CloudTrail event history (who called which API, no message content): 90 days
	EOF
}

# One output value of the environment's stack.
stack_output() { # key
	local out
	out=$(aws_ cloudformation describe-stacks --stack-name "$STACK" \
		--query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>&1) ||
		die "could not read the outputs of $STACK (is it deployed? make deploy ENV=$ENV): $(mask "$out")"
	[[ -n "$out" && "$out" != None ]] || die "$STACK has no output $1: run make deploy ENV=$ENV"
	printf '%s\n' "$out"
}

# Users are created by the operator; nobody can register. The password is typed at a hidden
# prompt of scripts/user.py.
cmd_user() {
	need_aws
	[[ -n "${TA_USER:-}" ]] || die "USER is required: make user ENV=$ENV USER=<name>"
	banner "user: create '$TA_USER' in the user pool of $STACK (or reset the password)"
	local pool
	pool=$(stack_output UserPoolId)
	py scripts/user.py --profile "$PROFILE" --region "$REGION" --user-pool-id "$pool" "$TA_USER"
}

# The GitHub OAuth App's client secret, typed at a hidden prompt, into the stack's secret; then
# the redirect URI the OAuth App must list.
cmd_github() {
	need_aws
	banner "github: store the client secret of the GitHub OAuth App of $STACK"
	py scripts/github_secret.py --profile "$PROFILE" --region "$REGION" --secret-id "$GITHUB_SECRET"
	local redirect
	redirect=$(stack_output GithubCallbackUrl)
	cat <<-EOF

		NOW, BY HAND: GitHub consent fails until the OAuth App lists this deployment's address.
		  1. Open https://github.com/settings/developers -> OAuth Apps -> team-assistant-$ENV
		  2. Under "Redirect URIs" replace the old address with:
		     $redirect
		  3. Click "Update application".
		Every deployment from scratch has a new address, so repeat this after each one.
	EOF
}

# Runs on the host (the Makefile does not wrap it): the browser and the listener that receives
# the login must be on the same machine. scripts/auth.py needs only Python's standard library.
cmd_login() {
	need_aws
	[[ -n "${TA_USER:-}" ]] || die "USER is required: make login ENV=$ENV USER=<name>"
	banner "login: sign '$TA_USER' in to $STACK in the browser"
	local domain client audience
	domain=$(stack_output LoginDomain)
	client=$(stack_output UserPoolClientId)
	audience=$(stack_output Audience)
	py scripts/auth.py --login-domain "$domain" --client-id "$client" --audience "$audience" \
		--env "$ENV" --user "$TA_USER" ${TA_PRINT_URL:+--print-url}
}

# chat and ask run on the host, like login: they read the tokens that make login saved there.
chat_() { # extra arguments for scripts/chat.py
	local domain client gateway
	domain=$(stack_output LoginDomain)
	client=$(stack_output UserPoolClientId)
	gateway=$(stack_output GatewayUrl)
	py scripts/chat.py --gateway-url "$gateway" --gateway-target "$GATEWAY_TARGET" \
		--login-domain "$domain" --client-id "$client" --env "$ENV" --user "$TA_USER" \
		--return-url "$GITHUB_RETURN_URL" "$@"
}

cmd_chat() {
	need_aws
	[[ -n "${TA_USER:-}" ]] || die "USER is required: make chat ENV=$ENV USER=<name>"
	banner "chat: talk to the agent of $STACK as '$TA_USER'"
	chat_ ${TA_SESSION:+--session "$TA_SESSION"}
}

cmd_ask() {
	need_aws
	[[ -n "${TA_USER:-}" ]] || die "USER is required: make ask ENV=$ENV USER=<name> PROMPT='...'"
	[[ -n "${TA_PROMPT:-}" ]] || die "PROMPT is required: make ask ENV=$ENV USER=$TA_USER PROMPT='...'"
	chat_ ${TA_SESSION:+--session "$TA_SESSION"} --prompt "$TA_PROMPT"
}

# The user's id in the pool (the actor id in Memory), from the login saved by make login.
user_sub() { # user
	py - "$ENV" "$1" <<'PY' || die "'$1' is not logged in to env $ENV: run make login ENV=$ENV USER=$1"
import base64, json, pathlib, sys
env, user = sys.argv[1:]
cache = json.loads((pathlib.Path.home() / ".config" / "team-assistant" / env / f"{user}.json").read_text())
body = cache["access_token"].split(".")[1]
print(json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["sub"])
PY
}

# Operator's view of one conversation, read with the admin profile.
cmd_memory() {
	need_aws
	[[ -n "${TA_USER:-}" && -n "${TA_SESSION:-}" ]] ||
		die "USER and SESSION are required: make memory ENV=$ENV USER=<name> SESSION=<id>"
	banner "memory: stored messages and long-term records of '$TA_USER' in session $TA_SESSION"
	local actor memory
	actor=$(user_sub "$TA_USER")
	memory=$(stack_output MemoryId)
	py scripts/memory.py --profile "$PROFILE" --region "$REGION" \
		--memory-id "$memory" --actor "$actor" --session "$TA_SESSION"
}

cmd_smoke() {
	need_aws
	banner "smoke: end-to-end checks on $STACK as alice and bob (a few model calls; waits up to 5 min for long-term records; alice must have consented to GitHub)"
	# Read before the call: a failed $(...) inside an argument list would not stop the script.
	local gateway runtime memory domain client web_url web_bucket
	gateway=$(stack_output GatewayUrl)
	web_url=$(stack_output WebUrl)
	web_bucket=$(stack_output WebBucket)
	runtime=$(stack_output RuntimeArn)
	memory=$(stack_output MemoryId)
	domain=$(stack_output LoginDomain)
	client=$(stack_output UserPoolClientId)
	py scripts/smoke.py --profile "$PROFILE" --region "$REGION" \
		--gateway-url "$gateway" --gateway-target "$GATEWAY_TARGET" \
		--runtime-arn "$runtime" --memory-id "$memory" \
		--login-domain "$domain" --client-id "$client" --env "$ENV" \
		--web-url "$web_url" --web-bucket "$web_bucket"
}

# The page's development server. With MOCK=1 the mock agent of web/mock answers and
# nothing touches AWS. Without it the page talks to the environment's gateway and Cognito:
# web/public/config.json (gitignored: it names the login domain) is written from the stack's
# outputs first. WEB_CONFIG moves that file (tests).
cmd_web() {
	if [[ "${TA_MOCK:-}" == 1 ]]; then
		banner_offline "web: the page with the mock agent on http://localhost:5173 (Ctrl+C stops it)"
		cd "$ROOT/web"
		exec npm run --silent mock
	fi
	need_aws
	banner "web: the page on http://localhost:5173 against $STACK (Ctrl+C stops it)"
	local gateway domain client audience config
	gateway=$(stack_output GatewayUrl)
	domain=$(stack_output LoginDomain)
	client=$(stack_output UserPoolClientId)
	audience=$(stack_output Audience)
	config=${WEB_CONFIG:-$ROOT/web/public/config.json}
	mkdir -p "$(dirname "$config")"
	jq -n --arg g "$gateway" --arg d "$domain" --arg c "$client" --arg a "$audience" --arg e "$ENV" \
		'{gatewayUrl: $g, loginDomain: $d, clientId: $c, audience: $a, env: $e}' >"$config"
	echo "--- config.json written from the outputs of $STACK"
	cd "$ROOT/web"
	exec npm run --silent dev
}

main() {
	local cmd=${1:-}
	case "$cmd" in
	setup | check | synth | bootstrap | diff | deploy | destroy | status | user | login | chat | ask | memory | smoke | github | web)
		"cmd_$cmd"
		;;
	web-sync) cmd_web_sync ;;
	unbootstrap) cmd_unbootstrap ;;
	*) die "unknown command '$cmd' (see make help)" ;;
	esac
}

main "$@"
