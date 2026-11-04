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

"""Filesystem detection of installed NXP tools.

Answers "is this product on this machine, and where" so a workflow can offer to
install what is missing instead of failing. The scan is read-only, shallow, and
limited to directory and executable existence - it deliberately does not look
inside an installation to decide what it contains. Which Eclipse p2 features a
Design Studio carries belongs to ``nxp.mcp.shared.install``.

The shared layer ships no detectors. Each product package declares its own
:class:`DetectorSpec` and registers it with :func:`register_detector`. Doing so
is optional: an unregistered component is reported as not detected, never as an
error.
"""

from .action import DETECT_ACTION_NAME, build_detect_action
from .engine import (
    DETECTORS,
    DetectorRegistry,
    detect,
    detect_all,
    detect_one,
    register_detector,
)
from .models import S32DS_COMPONENT, DetectedTool, DetectorSpec
from .scan import default_scan_roots, iter_install_dirs, parse_s32ds_version

__all__ = [
    "DETECTORS",
    "DETECT_ACTION_NAME",
    "DetectedTool",
    "DetectorRegistry",
    "DetectorSpec",
    "S32DS_COMPONENT",
    "build_detect_action",
    "default_scan_roots",
    "detect",
    "detect_all",
    "detect_one",
    "iter_install_dirs",
    "parse_s32ds_version",
    "register_detector",
]
