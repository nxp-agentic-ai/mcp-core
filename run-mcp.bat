@rem Copyright 2026 NXP
@rem
@rem NXP Proprietary. This software is owned or controlled by NXP and may
@rem only be used strictly in accordance with the applicable license terms.
@rem By expressly accepting such terms or by downloading, installing,
@rem activating and/or otherwise using the software, you are agreeing that
@rem you have read, and that you agree to comply with and are bound by,
@rem such license terms.  If you do not agree to be bound by the applicable
@rem license terms, then you may not retain, install, activate or otherwise
@rem use the software.

@echo off
REM ============================================================
REM  run-mcp.bat - Wrapper to run a python -m module with args
REM  inside the project's virtual environment.
REM
REM  Optionally enables zero-code OpenTelemetry auto-instrumentation
REM  (traces -> OTLP/gRPC) when invoked with --telemetry / -t as
REM  the first argument.
REM
REM  Usage:
REM      run-mcp.bat [--telemetry|-t] <module> [args...]
REM
REM  Examples:
REM      run-mcp.bat nxp.mcp.gateway configs\gateway.stdio.yaml
REM      run-mcp.bat nxp.mcp.gateway configs\gateway.http.yaml
REM      run-mcp.bat --telemetry nxp.mcp.gateway configs\gateway.stdio.extended.yaml
REM
REM  Telemetry mode requires:
REM      pip install opentelemetry-distro
REM      pip install opentelemetry-exporter-otlp
REM
REM  In telemetry mode OTEL_SERVICE_NAME is derived from the module
REM  name (dots -> dashes), unless already set in the environment.
REM ============================================================

setlocal EnableDelayedExpansion

REM --- Resolve script directory (works no matter where it's called from) ---
set "SCRIPT_DIR=%~dp0"
set "PYTHONUTF8=1"
set "TELEMETRY=0"

REM --- Parse optional leading --telemetry / -t flag ---
if /I "%~1"=="--telemetry" (
    set "TELEMETRY=1"
    shift
) else if /I "%~1"=="-t" (
    set "TELEMETRY=1"
    shift
)

REM --- Validate arguments ---
if "%~1"=="" (
    echo [run-mcp] ERROR: missing module name.
    echo Usage: %~nx0 [--telemetry^|-t] ^<module^> [args...]
    echo Example: %~nx0 nxp.mcp.gateway configs\gateway.stdio.yaml
    echo Example: %~nx0 --telemetry nxp.mcp.gateway configs\gateway.stdio.extended.yaml
    exit /b 1
)

set "MODULE=%~1"
shift

REM --- Collect remaining args (unlimited count) into MOD_ARGS ---
set "MOD_ARGS="
:collect_args
if "%~1"=="" goto args_done
set "MOD_ARGS=!MOD_ARGS! %1"
shift
goto collect_args
:args_done

REM --- Move into project root ---
cd /d "%SCRIPT_DIR%" || (
    echo [run-mcp] ERROR: cannot cd to "%SCRIPT_DIR%"
    exit /b 1
)

REM --- Activate virtual environment (only if it exists) ---
if exist "%SCRIPT_DIR%.venv\Scripts\activate.bat" (
    call "%SCRIPT_DIR%.venv\Scripts\activate.bat"
) else (
    echo [run-mcp] INFO: no venv found at "%SCRIPT_DIR%.venv", using system Python >&2
)

REM --- Verify python is available on PATH ---
where python >nul 2>&1
if errorlevel 1 (
    echo [run-mcp] ERROR: 'python' not found on PATH. Install Python or create a .venv at "%SCRIPT_DIR%.venv".
    exit /b 1
)

if "%TELEMETRY%"=="1" goto run_telemetry

REM --- Plain run: python -m <module> [args...] ---
python -m %MODULE%%MOD_ARGS%
set "EXITCODE=%ERRORLEVEL%"
goto end

:run_telemetry
REM --- Derive OTEL_SERVICE_NAME from module if not already set ---
if not defined OTEL_SERVICE_NAME (
    set "OTEL_SERVICE_NAME=%MODULE:.=-%"
)

REM --- OTel configuration via environment variables ---
REM Traces -> Jaeger via OTLP gRPC
set "OTEL_TRACES_EXPORTER=otlp"
if not defined OTEL_EXPORTER_OTLP_ENDPOINT set "OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317"
if not defined OTEL_EXPORTER_OTLP_PROTOCOL set "OTEL_EXPORTER_OTLP_PROTOCOL=grpc"

REM Metrics disabled: the useful RED metrics come from Jaeger's
REM spanmetrics connector (derived from spans), not from the app.
set "OTEL_METRICS_EXPORTER=none"

REM Logs disabled (still using stdlib logging + file handler)
set "OTEL_LOGS_EXPORTER=none"

REM Sanity check: both distro (for sitecustomize) and the OTLP
REM exporter (used by OTEL_TRACES_EXPORTER=otlp) must be installed.
python -c "from opentelemetry.instrumentation.auto_instrumentation import sitecustomize" 2>nul
if errorlevel 1 (
    echo [run-mcp] ERROR: opentelemetry-distro is not installed.
    echo Install it with:  pip install opentelemetry-distro
    exit /b 1
)
python -c "from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter" 2>nul
if errorlevel 1 (
    echo [run-mcp] ERROR: opentelemetry-exporter-otlp is not installed.
    echo Install it with:  pip install opentelemetry-exporter-otlp
    exit /b 1
)

REM --- Run python with auto-instrumentation enabled in-process ---
python -c "from opentelemetry.instrumentation.auto_instrumentation import sitecustomize; import runpy; runpy.run_module('%MODULE%', run_name='__main__', alter_sys=True)"%MOD_ARGS%
set "EXITCODE=%ERRORLEVEL%"

:end
endlocal & exit /b %EXITCODE%
