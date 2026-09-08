# ─────────────────────────────────────────────────────────────────────────────
#  Schwab bridge — release image (source baked in)
#  Commit as:  schwab-bridge/Dockerfile   (replaces the deps-only version)
#
#  WHAT CHANGED AND WHY
#  --------------------
#  The previous image held dependencies only, with the repo bind-mounted at
#  /bridge. That worked because code and state lived in the same folder, which
#  is exactly what the Python expects — every path in it is relative to the
#  working directory.
#
#  A registry install has no repo to mount, so the code now ships inside the
#  image at /opt/bridge. But six paths must still survive a container being
#  replaced, and the dashboard has to be able to write four of them:
#
#      .env               written by the dashboard's Settings page
#      credentials.env    written by the dashboard, read by the bridge
#      token.json         the OAuth token
#      .auth_state.json   pending-login state
#      reauth_inbox/      dashboard drops login requests here
#      task_inbox/        dashboard drops backfill/report requests here
#
#  Those live on the host, mounted at /state — and /state is the WORKING
#  DIRECTORY. Code and state are split, but every relative path in the Python
#  still lands on a real, writable file, so NOT ONE LINE OF PYTHON CHANGES:
#  "token.json" is /state/token.json, INBOX_DIR is /state/reauth_inbox, and
#  os.remove(".auth_state.json") deletes the real file.
#
#  Why not keep /opt/bridge as the working directory and symlink the six
#  names onto /state? Because the bridge runs as an unprivileged user and
#  /opt/bridge is root-owned: unlinking a symlink there is a permission error,
#  which reauth._clear_state() swallows — the pending-login state would never
#  clear, and a successful login would be reported as "Login link expired".
#
#  The six names are STILL symlinked from /opt/bridge onto /state, for two
#  reasons:
#    * load_dotenv() with no argument (auto_push.py) looks for .env next to
#      the calling script — in /opt/bridge, not in the working directory. The
#      symlink makes it find /state/.env. python-dotenv treats a dangling
#      symlink as "no .env", which is exactly right before first-run setup.
#    * A published image layer must never contain a credential. With the
#      names pinned to symlinks, `ls -la /opt/bridge` can only ever show
#      pointers into /state at those names — never a real file.
#
#  Developers can still bind-mount a working copy over /opt/bridge, or use the
#  build-from-source layout in the dashboard's docker-compose.yml (repo
#  mounted at /bridge, working_dir /bridge, no /state at all). The entrypoint
#  copes with both.
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim-bookworm

# Links the published package to this repo on GitHub, which is what makes the
# package page show the README and lets you manage the package from the repo.
LABEL org.opencontainers.image.source="https://github.com/justintimefordinner-lang/Schwab_Bridge_Public"
LABEL org.opencontainers.image.description="Read-only Schwab market-data bridge for the Portfolio Manager dashboard"
LABEL org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"

# tzdata so the bridge's market-hours logic reads the right local clock.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Dependencies first, so edits to the Python don't invalidate the pip layer.
COPY requirements.txt /tmp/requirements.txt

# streamlit is a dependency of app.py only — the standalone Streamlit tool.
# Nothing in the running bridge (auto_push, reauth, export_to_app, am_report,
# backfill, report_refresh) imports it, and it drags in pyarrow, which is the
# slowest thing to build for arm64. Dropping it is worth several minutes per
# build. To run app.py in a container, delete the grep and install the file whole.
RUN grep -v -i '^[[:space:]]*streamlit' /tmp/requirements.txt > /tmp/requirements.runtime.txt \
 && echo "── installing:" && cat /tmp/requirements.runtime.txt \
 && pip install --no-cache-dir -r /tmp/requirements.runtime.txt \
 && rm -f /tmp/requirements.txt /tmp/requirements.runtime.txt

# The application itself. .dockerignore keeps secrets and local state out of
# the build context; the rm below is belt and braces on top of that.
COPY . /opt/bridge

# Pin the six working-directory state names to symlinks onto /state.
RUN rm -rf /opt/bridge/reauth_inbox /opt/bridge/task_inbox \
 && rm -f  /opt/bridge/.env /opt/bridge/credentials.env \
           /opt/bridge/token.json /opt/bridge/.auth_state.json \
 && ln -s /state/.env             /opt/bridge/.env \
 && ln -s /state/credentials.env  /opt/bridge/credentials.env \
 && ln -s /state/token.json       /opt/bridge/token.json \
 && ln -s /state/.auth_state.json /opt/bridge/.auth_state.json \
 && ln -s /state/reauth_inbox     /opt/bridge/reauth_inbox \
 && ln -s /state/task_inbox       /opt/bridge/task_inbox

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# State is the working directory. Created here so `docker run` alone has one;
# compose bind-mounts ./bridge-state over it, and the host's ownership wins.
RUN mkdir -p /state
WORKDIR /state

# The script is given by absolute path because the working directory is not
# the code directory. Python puts the script's own folder (/opt/bridge) on
# sys.path, so `import reauth`, `import export_to_app` etc. resolve as before.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "/opt/bridge/auto_push.py"]
