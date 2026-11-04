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

"""Shared OpenTelemetry span helper for action execution.

Single source of truth for the per-call action span and its attributes so
every MCP server emits identical telemetry, regardless of whether the call
goes through the shared action dispatcher (``execute_action``) or through a
bespoke tool handler that does not use the dispatcher.

The server name and the namespaced tool name are inherently per-server, so
they are injected by the caller rather than discovered here; shared code sits
below the server packages in the dependency graph and cannot import them.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from . import errors

# Span attribute keys. Defined once here so there is exactly one definition
# shared by the dispatcher, the bespoke tool handlers, and the report reader.
ACTION_NAME_ATTR = "nxp.mcp.action.name"
TOOL_NAME_ATTR = "gen_ai.tool.name"
METHOD_NAME_ATTR = "mcp.method.name"
SERVER_NAME_ATTR = "fastmcp.server.name"

# Error attributes set on the span when an action fails.
ERROR_ATTR = "error"
ACTION_ERROR_CODE_ATTR = "mcp.action.error_code"
ACTION_ERROR_LABEL_ATTR = "mcp.action.error_label"
ACTION_ERROR_MESSAGE_ATTR = "mcp.action.error_message"

# Defensive clamp so a pathological message cannot bloat a span.
ERROR_MESSAGE_MAX_LEN = 4096

# MCP JSON-RPC method under which tool invocations are dispatched.
DEFAULT_METHOD = "tools/call"


def namespaced_tool_name(server_name: str, tool_name: str) -> str:
    """Mirror the FastMCP Namespace transform: ``nxp_<server>_<tool>``.

    The child span's tool name must match the auto-instrumented parent span,
    which FastMCP names by prepending ``nxp_<server>_`` to the registered
    tool name.
    """

    return "nxp_{}_{}".format(server_name, tool_name)


@contextmanager
def action_span(
    *,
    server_name: str,
    tool_name: str,
    action_name: str,
    method: str = DEFAULT_METHOD,
) -> Iterator[trace.Span]:
    """Open the per-call action span with the standard attributes.

    Args:
        server_name: FastMCP server name (e.g. ``"s32ds"``); also scopes
                     the tracer as ``nxp.mcp.<server_name>``.
        tool_name:   Already-namespaced tool name, e.g.
                     ``nxp_s32ds_execute_action`` (see
                     :func:`namespaced_tool_name`).
        action_name: The invoked action name; blank/None becomes
                     ``"unknown"``.
        method:      MCP method label; defaults to ``tools/call``.

    Yields:
        The active span, so callers can set error state on it after the
        wrapped work completes.

    An escaping ``Exception`` is marked INTERNAL_ERROR and re-raised, so that a
    tool that fails by raising is not recorded as a success. Cancellation derives
    from ``BaseException``, is not an action failure, and is thus not marked.
    """

    tracer = trace.get_tracer("nxp.mcp.{}".format(server_name))
    resolved = (action_name or "unknown").strip() or "unknown"
    with tracer.start_as_current_span(
        "{} {}".format(tool_name, resolved)
    ) as span:
        span.set_attribute(ACTION_NAME_ATTR, resolved)
        span.set_attribute(TOOL_NAME_ATTR, tool_name)
        span.set_attribute(METHOD_NAME_ATTR, method)
        span.set_attribute(SERVER_NAME_ATTR, server_name)
        try:
            yield span
        except Exception as exc:
            mark_span_error(
                span,
                errors.INTERNAL_ERROR,
                error_message=str(errors.ErrorLabel.INTERNAL_ERROR),
                error_details={"message": str(exc) or type(exc).__name__},
            )
            raise


def mark_span_error(
    span: Optional[trace.Span],
    error_code: Optional[int],
    *,
    error_message: Optional[str] = None,
    error_details: Optional[dict] = None,
) -> None:
    """Set the standard error attributes on ``span`` when an action failed.

    Records the error code, the category label (``error_message``), and the
    detailed reason (``error_details["message"]``, falling back to the label).
    No-op when ``span`` or ``error_code`` is ``None``.
    """

    if span is None or error_code is None:
        return
    span.set_attribute(ERROR_ATTR, True)
    span.set_attribute(ACTION_ERROR_CODE_ATTR, error_code)

    # error_message carries the category label (e.g. "INTERNAL_ERROR"); the
    # detailed reason, when available, is in error_details["message"].
    error_label = error_message

    # Set span Status so the spanmetrics connector emits STATUS_CODE_ERROR;
    # it reads Status, not the error attribute (which only Jaeger surfaces).
    span.set_status(
        Status(StatusCode.ERROR, str(error_label) if error_label else None)
    )
    if error_label:
        span.set_attribute(ACTION_ERROR_LABEL_ATTR, str(error_label))

    detail = (
        error_details.get("message") if isinstance(error_details, dict) else None
    )

    # Prefer the detailed reason; fall back to the label.
    msg = detail or error_label
    if msg:
        msg = str(msg)[:ERROR_MESSAGE_MAX_LEN]
        span.set_attribute(ACTION_ERROR_MESSAGE_ATTR, msg)
        span.add_event(
            "action.error",
            {
                "error_code": error_code,
                "error_label": str(error_label or ""),
                "error_message": msg,
            },
        )
