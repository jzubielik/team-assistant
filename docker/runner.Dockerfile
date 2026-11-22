# syntax=docker/dockerfile:1
# Tool runner: every operator tool at a pinned version. The Makefile runs each
# target in this image (make runner builds it; NATIVE=1 runs on the host instead).
# The repository is mounted at /work; project venvs and node_modules live in named volumes
# mounted over /work/.venv, /work/infra/.venv, /work/infra/node_modules and
# /work/web/node_modules (their mount points are created here, before the volumes are mounted).
ARG NODE_VERSION=24.22.2
ARG AWSCLI_VERSION=2.39.5
ARG DOCKER_VERSION=29.9.3
ARG BUILDX_VERSION=0.38.2
ARG SHELLCHECK_VERSION=0.11.0
ARG GITLEAKS_VERSION=8.30.1
# sha256 of gitleaks_<version>_linux_{x64,arm64}.tar.gz (from the release's checksums.txt);
# update together with GITLEAKS_VERSION.
ARG GITLEAKS_SHA256_AMD64=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
ARG GITLEAKS_SHA256_ARM64=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080
# Scanners (make check, make leaks, make audit, git hooks). Downloads: sha256 from the release's
# checksums file (hadolint: of the downloaded binary); update together with the version.
ARG TRUFFLEHOG_VERSION=3.99.7
ARG TRUFFLEHOG_SHA256_AMD64=bdbea0508e439b144f49de14e775908de5f902f3d14ea7ae8354bae72b0b72ef
ARG TRUFFLEHOG_SHA256_ARM64=3060f7c9f18c70afdc432984cacabdd1e9103729b5d5fee7fb6755a90d1c59d4
ARG ACTIONLINT_VERSION=1.7.12
ARG ACTIONLINT_SHA256_AMD64=8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
ARG ACTIONLINT_SHA256_ARM64=325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6
ARG HADOLINT_VERSION=2.15.1
ARG HADOLINT_SHA256_AMD64=c7187db94eeeeca956519a6af171adc31453941a1e777961f6e680f697c8c507
ARG HADOLINT_SHA256_ARM64=f6198ef8090f404dbb771abfee086eb8c48ac177f30da7fd3510aca35b344b5d
# Python tools, installed with uv tool at a pinned version.
ARG ZIZMOR_VERSION=1.31.1
ARG PIP_AUDIT_VERSION=2.10.1

# Base images: literal tag@sha256 (not ARGs) so Dependabot can bump them.
FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
ARG NODE_VERSION AWSCLI_VERSION DOCKER_VERSION BUILDX_VERSION SHELLCHECK_VERSION
ARG GITLEAKS_VERSION GITLEAKS_SHA256_AMD64 GITLEAKS_SHA256_ARM64
ARG TRUFFLEHOG_VERSION TRUFFLEHOG_SHA256_AMD64 TRUFFLEHOG_SHA256_ARM64
ARG ACTIONLINT_VERSION ACTIONLINT_SHA256_AMD64 ACTIONLINT_SHA256_ARM64
ARG HADOLINT_VERSION HADOLINT_SHA256_AMD64 HADOLINT_SHA256_ARM64
ARG ZIZMOR_VERSION PIP_AUDIT_VERSION
ARG TARGETARCH
# A failing curl in a pipe fails the build.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Debian packages follow the pinned base image digest (a version pin breaks on its updates).
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git make jq unzip xz-utils less \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /tmp
# x86_64/aarch64 names for the tarballs and installers that do not use Docker's arch names.
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) a=x86_64 n=x64 gl_sha=$GITLEAKS_SHA256_AMD64 th_sha=$TRUFFLEHOG_SHA256_AMD64 \
        al_sha=$ACTIONLINT_SHA256_AMD64 hl_sha=$HADOLINT_SHA256_AMD64 ;; \
      arm64) a=aarch64 n=arm64 gl_sha=$GITLEAKS_SHA256_ARM64 th_sha=$TRUFFLEHOG_SHA256_ARM64 \
        al_sha=$ACTIONLINT_SHA256_ARM64 hl_sha=$HADOLINT_SHA256_ARM64 ;; \
      *) exit 1 ;; esac; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${n}.tar.xz" \
      | tar -xJ -C /usr/local --strip-components=1 --exclude CHANGELOG.md --exclude LICENSE --exclude README.md; \
    curl -fsSLo awscli.zip "https://awscli.amazonaws.com/awscli-exe-linux-${a}-${AWSCLI_VERSION}.zip"; \
    unzip -q awscli.zip && ./aws/install && rm -rf aws awscli.zip; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${a}/docker-${DOCKER_VERSION}.tgz" \
      | tar -xz -C /usr/local/bin --strip-components=1 docker/docker; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    curl -fsSLo /usr/local/lib/docker/cli-plugins/docker-buildx \
      "https://github.com/docker/buildx/releases/download/v${BUILDX_VERSION}/buildx-v${BUILDX_VERSION}.linux-${TARGETARCH}"; \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx; \
    curl -fsSL "https://github.com/koalaman/shellcheck/releases/download/v${SHELLCHECK_VERSION}/shellcheck-v${SHELLCHECK_VERSION}.linux.${a}.tar.xz" \
      | tar -xJ -C /usr/local/bin --strip-components=1 "shellcheck-v${SHELLCHECK_VERSION}/shellcheck"; \
    curl -fsSLo gitleaks.tgz \
      "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${n}.tar.gz"; \
    echo "${gl_sha}  gitleaks.tgz" | sha256sum -c -; \
    tar -xzf gitleaks.tgz -C /usr/local/bin gitleaks && rm gitleaks.tgz; \
    curl -fsSLo trufflehog.tgz \
      "https://github.com/trufflesecurity/trufflehog/releases/download/v${TRUFFLEHOG_VERSION}/trufflehog_${TRUFFLEHOG_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    echo "${th_sha}  trufflehog.tgz" | sha256sum -c -; \
    tar -xzf trufflehog.tgz -C /usr/local/bin trufflehog && rm trufflehog.tgz; \
    curl -fsSLo actionlint.tgz \
      "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    echo "${al_sha}  actionlint.tgz" | sha256sum -c -; \
    tar -xzf actionlint.tgz -C /usr/local/bin actionlint && rm actionlint.tgz; \
    hl_arch=$a; [ "$TARGETARCH" = arm64 ] && hl_arch=arm64; \
    curl -fsSLo /usr/local/bin/hadolint \
      "https://github.com/hadolint/hadolint/releases/download/v${HADOLINT_VERSION}/hadolint-linux-${hl_arch}"; \
    echo "${hl_sha}  /usr/local/bin/hadolint" | sha256sum -c -; \
    chmod 755 /usr/local/bin/hadolint; \
    node --version; aws --version; docker --version; docker buildx version; shellcheck --version; gitleaks version; \
    trufflehog --version; actionlint --version; hadolint --version

# zizmor and pip-audit as uv tools in /opt/uv-tools, on PATH through /usr/local/bin.
RUN UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_DOWNLOADS=never \
    uv tool install --no-cache "zizmor==${ZIZMOR_VERSION}" \
    && UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_DOWNLOADS=never \
    uv tool install --no-cache "pip-audit==${PIP_AUDIT_VERSION}" \
    && zizmor --version && pip-audit --version

# The container runs as the operator's uid (files in /work stay theirs), so HOME and the
# volume mount points are world-writable; Docker copies a mount point's mode into a new volume.
RUN mkdir -p /home/runner/.cache /work/.venv /work/infra/.venv /work/infra/node_modules \
        /work/web/node_modules \
    && chmod 1777 /home/runner /home/runner/.cache /work/.venv /work/infra/.venv /work/infra/node_modules \
        /work/web/node_modules

# Install the locked dependencies into the volumes on first use (one set of volumes per image tag).
COPY --chmod=755 runner-entry.sh /usr/local/bin/runner-entry

ENV HOME=/home/runner \
    UV_CACHE_DIR=/home/runner/.cache/uv \
    npm_config_cache=/home/runner/.cache/npm \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system \
    UV_LINK_MODE=copy \
    AWS_PAGER="" \
    DOCKER_CONFIG=/home/runner/.docker \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
ENTRYPOINT ["runner-entry"]
CMD ["bash"]
