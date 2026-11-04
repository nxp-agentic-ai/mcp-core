#!/usr/bin/env bash
# Copyright 2026 NXP
#
# NXP Proprietary. This software is owned or controlled by NXP and may
# only be used strictly in accordance with the applicable license terms.
# By expressly accepting such terms or by downloading, installing,
# activating and/or otherwise using the software, you are agreeing that
# you have read, and that you agree to comply with and are bound by,
# such license terms.  If you do not agree to be bound by the applicable
# license terms, then you may not retain, install, activate or otherwise
# use the software.

# ============================================================
#  run-mcp.sh - Wrapper to run a python -m module with args
#  inside the project's virtual environment.
#
#  Optionally enables zero-code OpenTelemetry auto-instrumentation
#  (traces -> OTLP/gRPC) when invoked with --telemetry / -t as
#  the first argument.
#
#  Usage:
#      bash run-mcp.sh [--telemetry|-t] <module> [args...]
#
#  Examples:
#      bash run-mcp.sh nxp.mcp.gateway configs/gateway.stdio.yaml
#      bash run-mcp.sh nxp.mcp.gateway configs/gateway.http.yaml
#      bash run-mcp.sh --telemetry nxp.mcp.gateway configs/gateway.stdio.extended.yaml
#
#  Telemetry mode requires:
#      pip install opentelemetry-distro
#      pip install opentelemetry-exporter-otlp
#
#  In telemetry mode OTEL_SERVICE_NAME is derived from the module
#  name (dots -> dashes), unless already set in the environment.
# ============================================================

set -euo pipefail

# --- Resolve script directory (works no matter where it's called from) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONUTF8=1
TELEMETRY=0

# --- Parse optional leading --telemetry / -t flag ---
if [ $# -gt 0 ]; then
    case "$1" in
        --telemetry|-t)
            TELEMETRY=1
            shift
            ;;
    esac
fi

# --- Validate arguments ---
if [ $# -eq 0 ]; then
    echo "[run-mcp] ERROR: missing module name."
    echo "Usage: $(basename "$0") [--telemetry|-t] <module> [args...]"
    echo "Example: $(basename "$0") nxp.mcp.gateway configs/gateway.stdio.yaml"
    echo "Example: $(basename "$0") --telemetry nxp.mcp.gateway configs/gateway.stdio.extended.yaml"
    exit 1
fi

MODULE="$1"
shift

# --- Move into project root ---
cd "$SCRIPT_DIR" || {
    echo "[run-mcp] ERROR: cannot cd to \"$SCRIPT_DIR\""
    exit 1
}

# --- Activate virtual environment (only if it exists) ---
if [ -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/.venv/bin/activate"
else
    echo "[run-mcp] INFO: no venv found at \"$SCRIPT_DIR/.venv\", using system Python" >&2
fi

# --- Verify python is available on PATH ---
if ! command -v python >/dev/null 2>&1; then
    echo "[run-mcp] ERROR: 'python' not found on PATH. Install Python or create a .venv at \"$SCRIPT_DIR/.venv\"."
    exit 1
fi

if [ "$TELEMETRY" != "1" ]; then
    # --- Plain run: python -m <module> [args...] ---
    python -m "$MODULE" "$@"
    exit $?
fi

# --- Telemetry run ---

# --- Derive OTEL_SERVICE_NAME from module if not already set ---
export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-${MODULE//./-}}"

# --- OTel configuration via environment variables ---
# Traces -> Jaeger via OTLP gRPC
export OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://localhost:4317}"
export OTEL_EXPORTER_OTLP_PROTOCOL="${OTEL_EXPORTER_OTLP_PROTOCOL:-grpc}"

# Metrics disabled: the useful RED metrics come from Jaeger's
# spanmetrics connector (derived from spans), not from the app.
export OTEL_METRICS_EXPORTER=none

# Logs disabled (still using stdlib logging + file handler)
export OTEL_LOGS_EXPORTER=none

# Sanity check: both distro (for sitecustomize) and the OTLP
# exporter (used by OTEL_TRACES_EXPORTER=otlp) must be installed.
if ! python -c "from opentelemetry.instrumentation.auto_instrumentation import sitecustomize" 2>/dev/null; then
    echo "[run-mcp] ERROR: opentelemetry-distro is not installed."
    echo "Install it with:  pip install opentelemetry-distro"
    exit 1
fi
if ! python -c "from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter" 2>/dev/null; then
    echo "[run-mcp] ERROR: opentelemetry-exporter-otlp is not installed."
    echo "Install it with:  pip install opentelemetry-exporter-otlp"
    exit 1
fi

# --- Run python with auto-instrumentation enabled in-process ---
python -c "from opentelemetry.instrumentation.auto_instrumentation import sitecustomize; import runpy; runpy.run_module('$MODULE', run_name='__main__', alter_sys=True)" "$@"
