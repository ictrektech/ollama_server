#!/usr/bin/env bash
# Python installer via uv
set -euxo pipefail

: "${PYTHON_VERSION:?You must define PYTHON_VERSION, e.g., 3.12}"
PYTHON_FREE_THREADING="${PYTHON_FREE_THREADING:-0}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}"

# Add 't' suffix for free-threaded builds
if [ "${PYTHON_FREE_THREADING}" = "1" ]; then
  PYTHON_INSTALL_VERSION="${PYTHON_VERSION}t"
  echo "========================================"
  echo "🔓 FREE-THREADED (NO-GIL) BUILD ENABLED"
  echo "========================================"
  echo "Installing Python ${PYTHON_INSTALL_VERSION} (free-threaded)"
  echo "PYTHON_GIL will be disabled"
else
  PYTHON_INSTALL_VERSION="${PYTHON_VERSION}"
  echo "Installing standard Python ${PYTHON_INSTALL_VERSION}"
fi

apt-get update
apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    build-essential

export UV_INSTALLER_GITHUB_BASE_URL="https://ghfast.top/https://github.com"
export UV_DEFAULT_INDEX="https://mirrors.aliyun.com/pypi/simple"
export UV_PYTHON_INSTALL_MIRROR="https://ghfast.top/https://github.com/astral-sh/python-build-standalone/releases/download"

# Install uv from the mirror first, then retry with the installer's official sources.
if ! curl --retry 3 --retry-all-errors -fsSL https://astral.sh/uv/install.sh | sh; then
  echo "uv mirror failed; retrying with official sources"
  unset UV_INSTALLER_GITHUB_BASE_URL
  curl --retry 3 --retry-all-errors -fsSL https://astral.sh/uv/install.sh | sh
fi

# Ensure uv is in PATH (move binary to /usr/local/bin for non-login environments)
if [ -f "${HOME}/.local/bin/uv" ]; then
  install -m 0755 "${HOME}/.local/bin/uv" /usr/local/bin/uv
fi

# Install the requested Python version via uv
uv python install "${PYTHON_INSTALL_VERSION}"

# Find the path to that Python version
PY_BIN="$(uv python find "${PYTHON_INSTALL_VERSION}")"

# Create a virtual environment in /opt/venv
uv venv --python "${PY_BIN}" --system-site-packages /opt/venv

# Activate the venv
source /opt/venv/bin/activate

# Checks
which python
python --version

# Upgrade pip and base utilities
uv pip install --upgrade --index-url "${PIP_INDEX_URL}" pip pkginfo

which pip || true
pip --version || true

uv pip install --no-binary :all: psutil

# Cleanup
rm -rf /var/lib/apt/lists/*
apt-get clean

# Symlinks for convenience
ln -sf /opt/venv/bin/python /usr/local/bin/python3
# ln -sf /opt/venv/bin/pip /usr/local/bin/pip3  # optional pip3 alias

# Final versions
which python3
python3 --version

which pip3 || true
pip3 --version || true

# Check if GIL is disabled for free-threaded builds
if [ "${PYTHON_FREE_THREADING}" = "1" ]; then
  echo ""
  echo "========================================"
  echo "🔍 VERIFYING NO-GIL PYTHON BUILD"
  echo "========================================"
  python3 -c "import sys; gil_disabled = not sys._is_gil_enabled() if hasattr(sys, '_is_gil_enabled') else 'N/A'; print(f'GIL Status: {\"✓ DISABLED (Free-threaded)\" if gil_disabled is True else \"✗ ENABLED\" if gil_disabled is False else \"Unknown (sys._is_gil_enabled not available)\"}')" || true
  echo "========================================"
fi

source /opt/venv/bin/activate
