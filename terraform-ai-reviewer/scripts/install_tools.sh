#!/usr/bin/env bash
# Install Terraform, TFLint and Checkov. Shared by the Dockerfile, AWS CodeBuild and CircleCI so
# every platform runs the exact same tool versions.
#
#   TERRAFORM_VERSION  (default 1.9.8)
#   TFLINT_VERSION     (default 0.53.0)
#   CHECKOV_VERSION    (default 3.2.255)
#   INSTALL_DIR        (default /usr/local/bin)
#   CHECKOV_VENV       (default /opt/checkov) - isolated so its pins never clash with the reviewer's deps
set -euo pipefail

TERRAFORM_VERSION="${TERRAFORM_VERSION:-1.9.8}"
TFLINT_VERSION="${TFLINT_VERSION:-0.53.0}"
CHECKOV_VERSION="${CHECKOV_VERSION:-3.2.255}"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
CHECKOV_VENV="${CHECKOV_VENV:-/opt/checkov}"

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi

case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"

for bin in curl unzip python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Missing prerequisite: $bin" >&2; exit 1; }
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

install_zip() {  # name url
  local name="$1" url="$2"
  echo ">> Installing ${name} from ${url}"
  curl -fsSL --retry 3 -o "$TMP/${name}.zip" "$url"
  unzip -oq "$TMP/${name}.zip" -d "$TMP/${name}"
  $SUDO install -m 0755 "$TMP/${name}/${name}" "${INSTALL_DIR}/${name}"
}

if command -v terraform >/dev/null 2>&1 && terraform version | head -1 | grep -q "v${TERRAFORM_VERSION}$"; then
  echo ">> terraform ${TERRAFORM_VERSION} already installed"
else
  install_zip terraform "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_${OS}_${ARCH}.zip"
fi

if command -v tflint >/dev/null 2>&1 && tflint --version | head -1 | grep -q "${TFLINT_VERSION}"; then
  echo ">> tflint ${TFLINT_VERSION} already installed"
else
  install_zip tflint "https://github.com/terraform-linters/tflint/releases/download/v${TFLINT_VERSION}/tflint_${OS}_${ARCH}.zip"
fi

if command -v checkov >/dev/null 2>&1 && checkov --version 2>/dev/null | grep -q "^${CHECKOV_VERSION}$"; then
  echo ">> checkov ${CHECKOV_VERSION} already installed"
else
  echo ">> Installing checkov ${CHECKOV_VERSION} into ${CHECKOV_VENV}"
  $SUDO python3 -m venv "$CHECKOV_VENV"
  $SUDO "$CHECKOV_VENV/bin/pip" install --no-cache-dir --quiet --upgrade pip
  $SUDO "$CHECKOV_VENV/bin/pip" install --no-cache-dir --quiet "checkov==${CHECKOV_VERSION}"
  $SUDO ln -sf "$CHECKOV_VENV/bin/checkov" "${INSTALL_DIR}/checkov"
fi

terraform version | head -1
tflint --version | head -1
echo "checkov $(checkov --version)"
