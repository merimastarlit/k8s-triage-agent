FROM python:3.12-slim

# The Claude Agent SDK spawns the `claude` CLI as a subprocess.
# It ships as an npm package, so Node is a hard runtime dependency —
# without it, shutil.which("claude") returns None and the agent dies at startup.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Fail the build here rather than at 3am in the cluster.
RUN which claude

# Create the runtime user before COPY so files can be owned at copy time.
RUN useradd --create-home --uid 10001 triage
ENV HOME=/home/triage

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what runs in production. No tests/, no broken-cluster/, no .env.
COPY --chown=triage:triage agent/ ./agent/
COPY --chown=triage:triage mcp_server/ ./mcp_server/

# COPY preserves host file modes. Normalize them so a restrictive directory
# mode on any developer's machine can't make the package unreadable at runtime.
RUN chmod -R a+rX /app

USER triage

# Assert the import chain works as the runtime user. Catches permission and
# packaging bugs that tests can't — tests always run as you, from the repo root.
RUN python -c "import mcp_server.server, agent.triage"

ENTRYPOINT ["python", "-m", "agent.triage"]
CMD ["--namespace", "default"]

