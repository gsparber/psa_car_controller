ARG DEBIAN_FRONTEND=noninteractive
# Python of the image and CI (python-version in .github/workflows), change them together. Everything else comes
# from PyPI in the versions of poetry.lock: there are wheels for amd64 and arm64, armv7 isn't built anymore.
FROM python:3.15-slim-trixie@sha256:e97b88e416c3c01eb427b21fd8cd3ebf006e6ee38afcfcc3cae1c54ee2ab0a75 AS builder
ARG DEBIAN_FRONTEND
# pyyaml and mutf8 have no wheels for this Python yet
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev
ARG PSACC_VERSION="0.0.0"
COPY ./dist/psa_car_controller-${PSACC_VERSION}-py3-none-any.whl ./poetry.lock ./docker_files/lock_constraints.py ./
# without the browser (see below), playwright goes here: removed after the copy, it would stay in the image
ARG PSACC_BROWSER=true
RUN python3 lock_constraints.py poetry.lock > constraints.txt && \
    pip install --no-cache-dir --root-user-action=ignore --prefix=/install --constraint constraints.txt \
        psa_car_controller-${PSACC_VERSION}-py3-none-any.whl && \
    if [ "${PSACC_BROWSER}" != "true" ]; then \
        site=$(echo /install/lib/python3.*/site-packages) && \
        rm -rf /install/bin/playwright "$site"/playwright "$site"/playwright-*.dist-info "$site"/pyee \
            "$site"/pyee-*.dist-info "$site"/greenlet "$site"/greenlet-*.dist-info; \
    fi

FROM python:3.15-slim-trixie@sha256:e97b88e416c3c01eb427b21fd8cd3ebf006e6ee38afcfcc3cae1c54ee2ab0a75
ARG DEBIAN_FRONTEND
# false: no Chromium and no playwright (the -slim images), for small machines such as a Raspberry Pi. The login
# then goes through a login worker or the manual link, see docs/login_worker.md
ARG PSACC_BROWSER=true
WORKDIR /config
ENV PSACC_BASE_PATH=/ PSACC_PORT=5000 PSACC_OPTIONS="-c -r --web-conf" PSACC_CONFIG_DIR="/config" PYTHONPATH="/app"
COPY --from=builder /install /usr/local
# curl for the health check, and the Chromium headless shell with its dependencies for the headless login
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    if [ "${PSACC_BROWSER}" = "true" ]; then playwright install --with-deps --only-shell chromium; fi && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
EXPOSE 5000

COPY /docker_files/init.sh /init.sh
# /health only fails (503) once the MQTT broker keeps refusing the remote credentials
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -fsS -o /dev/null "http://127.0.0.1:${PSACC_PORT}${PSACC_BASE_PATH%/}/health" || exit 1
# git state shown by a dev build; last, as a new value rebuilds every RUN after these lines
ARG PSACC_GIT_VERSION=""
ARG PSACC_GIT_REPO_URL=""
ENV PSACC_GIT_VERSION="${PSACC_GIT_VERSION}" PSACC_GIT_REPO_URL="${PSACC_GIT_REPO_URL}"
CMD /init.sh
