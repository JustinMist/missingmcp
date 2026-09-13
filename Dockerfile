FROM python:3.12-slim
RUN pip install --no-cache-dir uv
WORKDIR /app

# Pin the unmodified garmin_mcp worker to a reviewed commit (override at build
# time). Bumping this is a deliberate, reviewed action: the worker runs with each
# user's decrypted Garmin tokens, so a floating ref would run unreviewed code.
# e8554bc (2026-09-01): reviewed 2026-09-03 — no dependency additions, no new
# network destinations; NOTE the worker now logs in on a background thread and
# answers /healthz before the sign-in resolves, which is why WorkerManager gates
# spawns on the sign-in log lines (forward.login_outcome).
ARG GARMIN_MCP_REF=e8554bcd761a4494dc12a98461224bb3dcf1fbc5
ENV GARMIN_MCP_REF=${GARMIN_MCP_REF}

# git: uv installs the pinned garmin_mcp worker from a git ref.
# tini: reaps the many worker subprocesses the gateway spawns.
RUN apt-get update && apt-get install -y --no-install-recommends git tini && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock garmin-worker-override.txt README.md LICENSE ./
COPY src ./src
COPY scripts ./scripts
# Install the gateway and worker from their reviewed frozen locks. The pinned
# worker lock predates the CN DI endpoint fix, so after its frozen sync apply
# the exact garminconnect version independently locked by this repository.
RUN uv lock --check && uv sync --frozen
RUN git clone https://github.com/Taxuspt/garmin_mcp /opt/garmin-mcp && \
    git -C /opt/garmin-mcp checkout --detach "${GARMIN_MCP_REF}" && \
    uv lock --check --project /opt/garmin-mcp && \
    uv sync --project /opt/garmin-mcp --frozen --no-dev && \
    uv pip install --python /opt/garmin-mcp/.venv/bin/python --no-deps \
      --reinstall --require-hashes -r /app/garmin-worker-override.txt && \
    /opt/garmin-mcp/.venv/bin/python -c \
      "from importlib.metadata import version; assert version('garminconnect') == '0.3.6'"
ENV PATH="/opt/garmin-mcp/.venv/bin:/app/.venv/bin:${PATH}"
ENTRYPOINT ["tini", "--"]
CMD ["missingmcp"]
EXPOSE 8080
# No VOLUME directive: Railway's builder rejects it ("use Railway Volumes") and
# provides /data via a platform-managed volume; self-hosters mount /data with
# `docker run -v`. Persistence is supplied by the runtime, not the image.
