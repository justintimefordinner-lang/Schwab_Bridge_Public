# ─────────────────────────────────────────────────────────────────────────────
#  Schwab bridge — dependencies only
#  Commit as:  schwab-bridge/Dockerfile
#
#  This image deliberately contains NO application code. The repo is bind-
#  mounted at /bridge by docker-compose, which means:
#
#    * every relative path in the Python (token.json, reauth_inbox/,
#      task_inbox/, .env, credentials.env, .auth_state.json) resolves exactly
#      as it does on a bare-metal install — zero code changes required;
#    * `load_dotenv()` with no argument resolves from the script's own
#      directory, which is the same /bridge — so it finds the right .env;
#    * updating the bridge is `git pull` + `docker compose restart bridge`,
#      with no image rebuild. On a Pi that is the difference between seconds
#      and several minutes.
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim-bookworm

# tzdata so the bridge's market-hours logic reads the right local clock.
# curl is only here for a container healthcheck if you add one later.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /bridge

# Only requirements.txt is copied at build time — not the source.
COPY requirements.txt /tmp/requirements.txt

# streamlit is a dependency of app.py only (the standalone Streamlit tool). The
# running bridge — auto_push, reauth, export_to_app, am_report, backfill —
# never imports it, and it drags in pyarrow, which is the single slowest thing
# to install on arm64. Dropping it is the difference between a ~2 minute and a
# ~20 minute first build on a Raspberry Pi 4.
#
# If you ever want app.py inside the container, delete the grep and install
# requirements.txt whole.
RUN grep -v -i '^[[:space:]]*streamlit' /tmp/requirements.txt > /tmp/requirements.runtime.txt \
 && echo "── installing:" && cat /tmp/requirements.runtime.txt \
 && pip install --no-cache-dir -r /tmp/requirements.runtime.txt \
 && rm -f /tmp/requirements.txt /tmp/requirements.runtime.txt

# Overridden by docker-compose; stated here so `docker run` alone behaves.
CMD ["python", "auto_push.py"]
