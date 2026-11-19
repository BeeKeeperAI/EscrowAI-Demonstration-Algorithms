FROM python:3.9.16-slim

WORKDIR /app

COPY requirements.txt .
COPY *.py .
COPY run.sh .
COPY schema.json .
COPY *.bkenc .

# Ensure the secrets.yaml file is copied to the container when encrypting
COPY *.yaml .

# Install Python requirements
RUN pip install -r requirements.txt

# set the start command
ENTRYPOINT ["bash", "run.sh"]
