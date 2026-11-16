FROM python:3.9.16-slim

WORKDIR /app

# requirements.txt and run.sh always exist, so this COPY succeeds even when
# the optional encrypted (*.bkenc) and secrets (*.yaml) files are absent,
# e.g. for a local docker build. In EscrowAI the encryption tool adds them.
COPY requirements.txt run.sh *.py *.json *.bkenc *.yaml ./

# Install Python requirements
RUN pip install --no-cache-dir -r requirements.txt

# set the start command
ENTRYPOINT ["bash", "run.sh"]
