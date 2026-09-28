# builder stage: installs the runtime deps with uv into an isolated venv. uv is
# much faster than pip and reuses pyproject.toml-pinned versions consistently
# across dev and image builds. confining uv to this stage keeps it -- and the
# bytes of its COPY layer -- out of the final image entirely.
FROM alpine:3.23.4 AS builder

RUN apk add --update python3
COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /usr/local/bin/uv
RUN uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache \
        "Flask~=3.1" "Flask-WTF~=1.2" "waitress~=3.0" \
        "argon2-cffi~=23.1" "APScheduler~=3.10"

# final stage: starts clean and copies only the populated venv from the builder.
FROM alpine:3.23.4

LABEL org.opencontainers.image.title="BlackVue Sync v2"
LABEL org.opencontainers.image.description="Hands-off synchronization of BlackVue dashcam recordings with a web dashboard, viewer and statistics"
LABEL org.opencontainers.image.url="https://github.com/tekgnosis-net/blackvuesync-v2"
LABEL org.opencontainers.image.source="https://github.com/tekgnosis-net/blackvuesync-v2"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.authors="Alessandro Colomba (original BlackVue Sync), tekgnosis-net (v2)"
LABEL org.opencontainers.image.documentation="https://tekgnosis-net.github.io/blackvuesync-v2/"

RUN apk add --update bash python3 shadow su-exec tzdata \
    && rm -rf /var/cache/apk/* \
    && useradd -UMr dashcam \
    && mkdir -p /config \
    && chown dashcam:dashcam /config

# declared after /config is created; later build steps must not write here.
VOLUME ["/recordings", "/config"]

# copies the dependency venv built in the previous stage. both stages share the
# same alpine base, so the venv's interpreter matches the system python3 here.
# placing the venv first on PATH makes the entrypoint's `python` resolve to the
# interpreter that can import the runtime deps.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY COPYING /
COPY setuid.sh /setuid.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV ADDRESS="" \
    PUID="" \
    PGID="" \
    KEEP="" \
    GROUPING="" \
    PRIORITY="" \
    MAX_USED_DISK="" \
    TIMEOUT="" \
    VERBOSE=0 \
    QUIET="" \
    LOG_FORMAT="" \
    METRICS_FILE="" \
    METRICS_PUSHGATEWAY_URL="" \
    METRICS_JOB="" \
    METRICS_INSTANCE="" \
    METRICS_STATE_FILE="" \
    DRY_RUN="" \
    AFFINITY_KEY=""

COPY --chown=dashcam blackvuesync_v2 /app/blackvuesync_v2
ENV PYTHONPATH=/app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz').read()" || exit 1

ENTRYPOINT [ "/entrypoint.sh"]
