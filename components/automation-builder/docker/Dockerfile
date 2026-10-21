# AUTOSTATION build image: NSIS + osslsigncode + git/ssh + the builder scripts.
#
# The base image comes from the INTERNAL containeryard - there is deliberately no public-registry reference anywhere
# in this repo (airgapped CI). The build context is staged by docker/make_context.py so the same Dockerfile works from
# the monorepo and from a split-out automation-builder project (wrapper-core is needed for miniyaml/versions/secrets):
#
#   python components/automation-builder/docker/make_context.py build-context
#   docker build -f build-context/builder/docker/Dockerfile \
#       --build-arg BASE_IMAGE=containeryard.example.internal/base/python:3.12-slim \
#       --build-arg PIP_INDEX_URL=https://pypi-mirror.example.internal/simple \
#       --build-arg APT_MIRROR=https://apt-mirror.example.internal/debian \
#       -t "$CI_REGISTRY_IMAGE/builder:latest" build-context
#
# (the GitLab jobs in .gitlab-ci.yml do exactly this: prepare-context + kaniko).
ARG BASE_IMAGE=containeryard.example.internal/base/python:3.12-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=""
ARG APT_MIRROR=""

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WRAPPER_CORE_PATH=/opt/wrapper-core \
    PYTHONPATH=/opt/wrapper-core:/opt/autostation-builder/scripts:/opt/autostation-builder/prompts

# apt: optionally repoint at the internal mirror, then install the toolchain.
#   nsis           makensis (Unicode build, MUI2/LogicLib/FileFunc/WordFunc/TextFunc, nsExec + System plugins)
#   osslsigncode   Authenticode signing from Linux CI (Windows runners use installer/scripts/sign.ps1 instead)
#   git, openssh   used by CI jobs that open the registry merge request
RUN if [ -n "${APT_MIRROR}" ]; then \
      for f in /etc/apt/sources.list /etc/apt/sources.list.d/*; do \
        [ -f "$f" ] && sed -i "s#https\?://deb.debian.org/debian#${APT_MIRROR}#g; s#https\?://security.debian.org/debian-security#${APT_MIRROR}-security#g" "$f" || true; \
      done; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends nsis osslsigncode git openssh-client ca-certificates curl zip unzip \
 && rm -rf /var/lib/apt/lists/*

COPY builder/docker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

# wrapper-core is a library for the builder (stdlib only) and ALSO the bootstrap payload packaged into installers
COPY wrapper-core/wrapper_core /opt/wrapper-core/wrapper_core
COPY wrapper-core/VERSION /opt/wrapper-core/VERSION

COPY builder/scripts   /opt/autostation-builder/scripts
COPY builder/installer /opt/autostation-builder/installer
COPY builder/prompts   /opt/autostation-builder/prompts
COPY builder/docker/entrypoint.sh /usr/local/bin/autostation-build

RUN chmod +x /usr/local/bin/autostation-build \
 && makensis -VERSION \
 && osslsigncode --version \
 && python -c "import jinja2, package, build_installer, validate_nsi, loader" \
 && useradd --create-home --uid 10001 builder

USER builder
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/autostation-build"]
CMD ["--help"]
