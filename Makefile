# team-assistant operator interface. `make` or `make help` lists the targets.
# Logic lives in scripts/ops.sh; targets stay thin.
# Targets that touch AWS need ENV (an environment name, e.g. dev) on the command line, and
# PROFILE, REGION and ACCOUNT_<env> on the command line or in local.mk: make deploy ENV=dev
# Every target except help, hooks, runner, shell, arm64 and those that use a saved login
# (login, chat, ask, memory, smoke, measure) runs in the tool runner image
# (docker/runner.Dockerfile); NATIVE=1 runs it directly on the host instead.

.DEFAULT_GOAL := help
SHELL := /bin/bash

# ---- Operator settings ---------------------------------------------------------------------
# Settings come from the make command line or from local.mk (gitignored; copy
# local.mk.example), never from the shell environment: a shell variable named ENV, PROFILE,
# REGION or ACCOUNT (common in other tooling) must never pick the target account.
# ENV is taken from the command line only. PROFILE and REGION have no default. ACCOUNT is per
# environment: ACCOUNT_<env> in local.mk (the command line wins).
# LOCAL_MK=<file> (command line only) reads another file instead of local.mk (tests).
ifneq ($(origin LOCAL_MK),command line)
LOCAL_MK := local.mk
endif
-include $(LOCAL_MK)

# $(origin X) is "command line", "file" (this Makefile or local.mk), "environment" or
# "undefined". Remember where ENV came from, then drop any value not typed on the command line.
ENV_ORIGIN := $(origin ENV)
SHELL_ENV := $(if $(filter environment%,$(ENV_ORIGIN)),$(ENV))
ifneq ($(ENV_ORIGIN),command line)
ENV :=
endif

CONFIG_VARS := PROFILE REGION ACCOUNT GITHUB_CLIENT_ID LOGIN_SUFFIX
PER_ENV_VARS := ACCOUNT GITHUB_CLIENT_ID LOGIN_SUFFIX
# Settings that only the shell environment provides: refused by every AWS target (refuse_shell).
SHELL_CONFIG := $(strip $(foreach v,$(CONFIG_VARS) $(if $(ENV),$(addsuffix _$(ENV),$(PER_ENV_VARS))),\
	$(if $(filter environment%,$(origin $(v))),$(v))))
# FROM_<setting>: "command line", the local.mk path, or empty.
config_from = $(if $(filter command line,$(origin $(1))),command line,$(if $(filter file,$(origin $(1))),$(LOCAL_MK)))
$(foreach v,$(CONFIG_VARS),$(eval FROM_$(v) := $(call config_from,$(v))))
# A shell-only value is cleared, so no recipe can use it even where refuse_shell is not called.
$(foreach v,$(filter $(CONFIG_VARS),$(SHELL_CONFIG)),$(eval $(v) :=))
# ACCOUNT := ACCOUNT_<env> from local.mk, unless ACCOUNT is on the command line; the same for
# GITHUB_CLIENT_ID (the client id of the environment's GitHub OAuth App) and LOGIN_SUFFIX (the
# random end of the environment's Cognito login domain).
$(foreach v,$(PER_ENV_VARS),$(if $(filter command line,$(FROM_$(v))),,$(if $(and $(ENV),$(filter file,$(origin $(v)_$(ENV)))),\
	$(eval $(v) := $$($(v)_$(ENV)))$(eval FROM_$(v) := $(LOCAL_MK) $(v)_$(ENV)))))
# What make prints for a setting: its value, but never the account id, the GitHub client id or
# the login suffix (terminal output gets pasted into documents).
shown = $(if $($(1)),$(if $(filter ACCOUNT GITHUB_CLIENT_ID LOGIN_SUFFIX,$(1)),<set>,$($(1))),<unset>)
# Shown in every banner of scripts/ops.sh. In the runner the inner make gets the values on its
# command line and keeps the origins computed here (RUNNER_INNER=1).
ifneq ($(origin RUNNER_INNER),command line)
CONFIG_ORIGINS := $(foreach v,$(CONFIG_VARS),$(v)=$(call shown,$(v)) ($(or $(FROM_$(v)),unset)))
endif

# NATIVE=1: run targets on the host (its own tools) instead of in the runner image.
NATIVE ?=
# make runner FORCE=1: rebuild the image without the layer cache.
FORCE ?=
# make shell CMD='...': run one command in the runner instead of an interactive bash.
CMD ?=

OPS := scripts/ops.sh

# make user USER=<name>. USER is also the login name every shell exports, so only a value from
# the make command line counts; it travels on as TA_USER.
ifeq ($(origin USER),command line)
TA_USER := $(USER)
endif
# make chat SESSION=<id>, make ask PROMPT='...': command line only as well (shells set PROMPT).
ifeq ($(origin SESSION),command line)
TA_SESSION := $(SESSION)
endif
ifeq ($(origin PROMPT),command line)
TA_PROMPT := $(PROMPT)
endif
# make web MOCK=1: the page with the mock agent instead of a deployed environment.
ifeq ($(origin MOCK),command line)
TA_MOCK := $(MOCK)
endif
# make login PRINT=1: print the login address instead of opening the browser.
ifeq ($(origin PRINT),command line)
TA_PRINT_URL := $(PRINT)
endif

export ENV PROFILE REGION ACCOUNT GITHUB_CLIENT_ID LOGIN_SUFFIX CONFIG_ORIGINS TA_USER TA_SESSION TA_PROMPT TA_PRINT_URL \
	TA_MOCK
# The AWS profile and region come from PROFILE and REGION only.
unexport AWS_PROFILE AWS_DEFAULT_PROFILE AWS_REGION AWS_DEFAULT_REGION

# Guards, called at the start of a recipe. $(error ...) stops make before any command runs.
refuse_shell = $(if $(SHELL_CONFIG),$(error $(SHELL_CONFIG) set only in your shell environment; '$@' takes \
	them only from the make command line or $(LOCAL_MK), e.g. make $@ PROFILE=<admin-profile> REGION=<region>))
need_region = $(if $(REGION),,$(error REGION is required for '$@': make $@ REGION=<region>, or set it in \
	$(LOCAL_MK) (cp local.mk.example local.mk)))
require_aws = $(refuse_shell)$(if $(PROFILE),,$(error PROFILE is required for '$@': make $@ \
	PROFILE=<admin-profile>, or set it in $(LOCAL_MK) (cp local.mk.example local.mk)))$(need_region)
refuse_shell_env = $(if $(SHELL_ENV),$(error ENV=$(SHELL_ENV) comes from your shell environment ($(ENV_ORIGIN)); \
	'$@' accepts ENV only on the make command line, e.g. make $@ ENV=dev))
require_env = $(refuse_shell_env)$(if $(ENV),,$(error \
	ENV is required for '$@', e.g. make $@ ENV=dev))$(require_aws)

require_user = $(if $(TA_USER),,$(error USER is required for '$@' on the make command line: make $@ ENV=dev USER=<name>))

# ---- Tool runner ---------------------------------------------------------------------------
# The tag hashes the Dockerfile, the entry script and the project and lock files, so a
# dependency change gives a new tag and fresh dependency volumes (installed on first use).
RUNNER_INPUTS := docker/runner.Dockerfile docker/runner-entry.sh pyproject.toml uv.lock \
	infra/pyproject.toml infra/uv.lock infra/package-lock.json web/package-lock.json
RUNNER_TAG := $(shell cat $(RUNNER_INPUTS) 2>/dev/null | sha256sum | cut -c1-12)
RUNNER_IMAGE := team-assistant-runner:$(RUNNER_TAG)
RUNNER_VOLUME := team-assistant-runner-$(RUNNER_TAG)
BINFMT_IMAGE := tonistiigi/binfmt:qemu-v10.2.3
DOCKER_SOCK := /var/run/docker.sock
# Group of the Docker socket on the host: the container user joins it to reach the daemon.
DOCKER_GID = $(shell stat -c %g $(DOCKER_SOCK) 2>/dev/null || stat -f %g $(DOCKER_SOCK) 2>/dev/null)
TTY_FLAGS = $(shell [ -t 0 ] && echo -it || echo -i)
# Variables passed into the container by name (-e NAME: the value never appears on a command
# line): the settings banner and AWS credentials from the shell. The shell's AWS profile and
# region are never passed; PROFILE and REGION go to the inner make's command line (RUN).
RUNNER_VARS = CONFIG_ORIGINS TA_USER TA_MOCK \
	$(filter-out AWS_PROFILE AWS_DEFAULT_PROFILE AWS_REGION AWS_DEFAULT_REGION,$(filter AWS_%,$(.VARIABLES)))
# The build context is docker/ only: the image holds tools, not the project.
RUNNER_BUILD = docker build $(if $(FORCE),--pull --no-cache) -f docker/runner.Dockerfile -t $(RUNNER_IMAGE) docker
# Volumes over infra/ are mounted only once infra/ exists. MOUNT_POINTS are created on the
# host before the container starts: a missing mount point is created by Docker as a root-owned
# directory in the working tree.
INFRA_VOLUMES = $(if $(wildcard infra),-v $(RUNNER_VOLUME)-infra-venv:/work/infra/.venv \
	-v $(RUNNER_VOLUME)-node-modules:/work/infra/node_modules)
# The page (web/) has its own npm dependencies, in a volume of their own.
WEB_VOLUMES = $(if $(wildcard web/package.json),-v $(RUNNER_VOLUME)-web-node-modules:/work/web/node_modules)
MOUNT_POINTS = .venv $(if $(wildcard infra),infra/.venv infra/node_modules) \
	$(if $(wildcard web/package.json),web/node_modules)
# Build the image if this tag is missing, then run as the operator's uid with the repository
# at /work, the dependency volumes over it, ~/.aws (SSO cache) and the Docker socket.
DOCKER_RUN = mkdir -p "$$HOME/.aws" $(MOUNT_POINTS) && \
	{ docker image inspect $(RUNNER_IMAGE) >/dev/null 2>&1 || $(RUNNER_BUILD); } && \
	docker run --rm $(TTY_FLAGS) --user "$$(id -u):$$(id -g)" $(if $(DOCKER_GID),--group-add $(DOCKER_GID)) \
	-v "$(CURDIR):/work" -w /work \
	-v $(RUNNER_VOLUME)-venv:/work/.venv $(INFRA_VOLUMES) $(WEB_VOLUMES) \
	-v team-assistant-runner-cache:/home/runner/.cache \
	-v "$$HOME/.aws:/home/runner/.aws" \
	-v $(DOCKER_SOCK):$(DOCKER_SOCK) $(addprefix -e ,$(sort $(RUNNER_VARS))) $(PUBLISH) $(RUNNER_IMAGE)
# Ports a target publishes from the runner (make web); only on the host's loopback address.
PUBLISH :=
# Recipe prefix. With NATIVE=1 it is empty and the command after it runs on the host.
# Otherwise it runs the same target in the runner (settings on the inner command line, so the
# origin checks hold there too) and ends with a shell comment sign, which turns the native
# command into a comment. A wrapped recipe must therefore be one line.
HASH := \#
INNER_VARS = $(foreach v,$(CONFIG_VARS),$(if $($(v)),$(v)='$($(v))'))
RUN = $(if $(NATIVE),,$(DOCKER_RUN) make --no-print-directory $@ NATIVE=1 RUNNER_INNER=1 LOCAL_MK=/dev/null $(if $(ENV),ENV=$(ENV)) $(INNER_VARS) $(HASH))
HOST_CHECK = command -v docker >/dev/null || { echo "error: docker not found: install Docker" >&2; exit 1; }; \
	docker info >/dev/null 2>&1 || { echo "error: docker daemon not reachable (docker info failed): start Docker" >&2; exit 1; }; \
	echo "==> host: docker daemon reachable, make $(MAKE_VERSION); runner $(RUNNER_IMAGE)";

# ---- Targets -------------------------------------------------------------------------------
.PHONY: help runner runner-image shell arm64 setup check synth bootstrap diff deploy destroy status \
	user login chat ask memory smoke measure github web web-sync unbootstrap hooks leaks lint audit

help: ## Show this help (default target)
	@echo "team-assistant: operator targets"
	@echo
	@echo "TARGETS"
	@grep -hE '^[a-z0-9-]+:.*## ' $(firstword $(MAKEFILE_LIST)) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'
	@echo
	@echo "SETTINGS$(if $(ENV), for ENV=$(ENV),)"
	@printf '  %-16s %-30s %s\n' SETTING VALUE FROM
	@$(foreach v,$(CONFIG_VARS),printf '  %-16s %-30s %s\n' '$(v)' '$(call shown,$(v))' '$(or $(FROM_$(v)),not set)';)
	$(if $(ENV),,@echo "  ACCOUNT is read per environment: add ENV=<env> to see it.")
	@echo
	@echo "RULES"
	@echo "  ENV: command line only, e.g. make deploy ENV=dev"
	@echo "  PROFILE, REGION: command line or $(LOCAL_MK) (see local.mk.example); never the shell environment"
	@echo "  NATIVE=1: run a target on the host instead of in the runner image"

# Prints the runner image name; scripts/tool.sh uses it to find the image (not in make help).
runner-image:
	@echo $(RUNNER_IMAGE)

runner: ## Build the tool runner image if missing (FORCE=1: rebuild without cache)
	@echo "==> runner: $(RUNNER_IMAGE) (docker/runner.Dockerfile)"
	@$(if $(FORCE),$(RUNNER_BUILD),docker image inspect $(RUNNER_IMAGE) >/dev/null 2>&1 || $(RUNNER_BUILD))

shell: ## Interactive bash in the runner, same mounts as the targets (CMD='...': one command)
	@$(DOCKER_RUN) $(if $(CMD),bash -c '$(CMD)',bash)

arm64: ## Register QEMU arm64 emulation on the host (kernel setting, lost on reboot)
	@echo "==> arm64: register linux/arm64 emulation in the host kernel ($(BINFMT_IMAGE))"
	docker run --privileged --rm $(BINFMT_IMAGE) --install arm64

setup: ## Check Docker on the host, build the runner, check its tools, install dependencies
	@$(if $(NATIVE),,$(HOST_CHECK))$(RUN) $(OPS) setup

check: ## Lint (ruff, workflows, Dockerfiles, shell), format check, unit tests and the page's check (offline: no AWS call)
	@$(RUN) $(OPS) check

web-sync: ## Build the page and upload it to ENV's bucket and CloudFront, without a stack update
	@$(require_env)$(RUN) $(OPS) web-sync

web: PUBLISH := -p 127.0.0.1:5173:5173
web: ## The page's dev server on http://localhost:5173 against ENV's agent; MOCK=1: mock agent, no AWS
	@$(if $(TA_MOCK),,$(require_env))$(RUN) $(OPS) web

synth: ## Synthesize the stack offline into infra/cdk.out (ENV defaults to dev; needs REGION)
	@$(refuse_shell_env)$(refuse_shell)$(need_region)$(RUN) $(OPS) synth

bootstrap: ## One-time CDK bootstrap of the account and region (own roles, no admin policy)
	@$(require_env)$(RUN) $(OPS) bootstrap

diff: ## Show what make deploy would change (read-only)
	@$(require_env)$(RUN) $(OPS) diff

deploy: ## Deploy the environment: TeamAssistant-<env> and its permissions boundary
	@$(require_env)$(RUN) $(OPS) deploy

unbootstrap: ## Delete this project's CDK bootstrap once no environment is left (ENV picks the account)
	@$(require_env)$(RUN) $(OPS) unbootstrap

destroy: ## Delete the environment after you type its stack name (the bootstrap stays)
	@$(require_env)$(RUN) $(OPS) destroy

status: ## Show the state of the environment's stacks and of the bootstrap (read-only)
	@$(require_env)$(RUN) $(OPS) status

user: ## Create a user (USER=<name>) or reset the password; the password is typed at a hidden prompt
	@$(require_env)$(require_user)$(RUN) $(OPS) user

github: ## Store the GitHub OAuth App's client secret (hidden prompt); prints the app's redirect URI
	@$(require_env)$(RUN) $(OPS) github

login: ## Log USER=<name> in through the browser and save the tokens; PRINT=1 prints the address instead
	@$(require_env)$(require_user)$(OPS) login

chat: ## Talk to the agent as USER=<name>; SESSION=<id> continues a conversation (runs on the host)
	@$(require_env)$(require_user)$(OPS) chat

ask: ## One question: USER=<name> PROMPT='...' [SESSION=<id>] (runs on the host)
	@$(require_env)$(require_user)$(if $(TA_PROMPT),,$(error PROMPT is required for 'ask' on the make command line: make ask ENV=dev USER=<name> PROMPT='...'))$(OPS) ask

memory: ## Show what Memory stores for USER=<name> SESSION=<id> (operator's view; runs on the host)
	@$(require_env)$(require_user)$(OPS) memory

smoke: ## End-to-end checks against the deployed environment as alice and bob (runs on the host)
	@$(require_env)$(OPS) smoke

measure: ## Measure the long-term strategies as alice; writes measurements/<date>-<env>.md, not committed (runs on the host)
	@$(require_env)$(OPS) measure

hooks: ## Use the committed git hooks in .githooks (once per clone)
	@if [ "$$(git config --get core.hooksPath)" = .githooks ]; then \
		echo "hooks: core.hooksPath is already .githooks (nothing changed)"; \
	else git config core.hooksPath .githooks && \
		echo "hooks: set git config core.hooksPath .githooks (pre-commit, commit-msg, pre-push)"; fi
	@echo "hooks: the scanners run in $(RUNNER_IMAGE) (make runner) or, without it, from PATH"

leaks: ## Scan every commit with gitleaks (.gitleaks.toml) and trufflehog (verifies candidates online)
	@$(RUN) echo "==> leaks: gitleaks and trufflehog over the full history (all refs)" && NATIVE=1 scripts/gitleaks.sh history && NATIVE=1 scripts/scan.sh history

lint: ## Lint workflows (actionlint, zizmor), Dockerfiles (hadolint) and shell scripts (shellcheck); part of make check
	@$(RUN) echo "==> lint: actionlint, zizmor, hadolint, shellcheck" && NATIVE=1 scripts/scan.sh lint

audit: ## pip-audit and npm audit of the locked Python and npm dependencies (network: advisory databases)
	@$(RUN) echo "==> audit: pip-audit and npm audit on the lock files" && NATIVE=1 scripts/scan.sh audit
