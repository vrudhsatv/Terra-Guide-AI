# Terraform AI Reviewer: Terraform + TFLint + Checkov + Python reviewer in one portable image.
# Runs identically on GitHub Actions, AWS CodeBuild and CircleCI.
FROM python:3.11-slim

ARG TERRAFORM_VERSION=1.9.8
ARG TFLINT_VERSION=0.53.0
ARG CHECKOV_VERSION=3.2.255

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TF_IN_AUTOMATION=1 \
    TF_PLUGIN_CACHE_DIR=/tmp/terraform-plugin-cache

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/terraform-ai-reviewer

COPY scripts/install_tools.sh scripts/install_tools.sh
RUN TERRAFORM_VERSION="$TERRAFORM_VERSION" TFLINT_VERSION="$TFLINT_VERSION" CHECKOV_VERSION="$CHECKOV_VERSION" \
    ./scripts/install_tools.sh

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY rules/ rules/
COPY config/ config/

# The reviewed repository is bind-mounted with a different owner; let git operate on it.
RUN git config --system --add safe.directory '*' \
 && mkdir -p "$TF_PLUGIN_CACHE_DIR" && chmod 1777 "$TF_PLUGIN_CACHE_DIR"

WORKDIR /workspace
ENTRYPOINT ["python", "/opt/terraform-ai-reviewer/src/main.py"]
