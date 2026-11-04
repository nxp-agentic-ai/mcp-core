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

"""Helpers shared by the torch-backed model providers (embedder, reranker).

* :func:`resolve_device` - turn the configured device string into one the
  installed PyTorch build can actually use. Supported devices are ``"cpu"``
  and ``"cuda"`` / ``"cuda:N"`` (NVIDIA, Windows and Linux). Anything else,
  or CUDA when it is unavailable, falls back to ``"cpu"`` with a warning.
* :func:`is_missing_model_error` - tell a "model not in local cache" failure
  apart from every other load failure, so error messages do not blame the
  cache for unrelated problems (bad device, corrupt files, DLL errors, ...).
"""
import logging

# NOTE: import torch BEFORE anything that pulls in transformers. See the note
# in embeddings/sentence_transformers.py (Windows static-TLS / WinError 1114).
import torch

from nxp.mcp.knowledge.metadata import MCP_SERVER_NAME

logger = logging.getLogger(MCP_SERVER_NAME)

CPU = "cpu"
CUDA = "cuda"
SUPPORTED_DEVICES = (CPU, CUDA)

# CUDA build tag suggested in the fallback warning. cu132 is the newest CUDA
# build published for the pinned torch 2.14.x on both Windows and Linux
# (cu126 / cu130 / cu132 exist; there is no cu134). Update together with the
# torch pin and docs/KNOWLEDGE_BASE.md#gpu-acceleration-cuda.
SUGGESTED_CUDA_TAG = "cu132"
DOCS_ANCHOR = "docs/KNOWLEDGE_BASE.md#gpu-acceleration-cuda"


def cuda_install_hint() -> str:
    """Return the fix-it text appended to the CUDA fallback warning.

    The command uses the currently installed torch version (without the
    local "+cpu" tag) so it can be copied as-is on Windows and Linux.
    """
    version = torch.__version__.split("+", 1)[0]
    return (
        "To use the GPU, replace torch with a CUDA build in this environment: "
        f"'python -m pip uninstall -y torch' then 'python -m pip install torch=={version} "
        f"--index-url https://download.pytorch.org/whl/{SUGGESTED_CUDA_TAG}' "
        "(use cu130 or cu126 if 'nvidia-smi' reports an older CUDA Version; "
        f"see {DOCS_ANCHOR}), or set 'device: \"cpu\"' in the config to silence this warning."
    )


def _cuda_unavailable_reason() -> str:
    """Return a short human-readable reason why CUDA cannot be used."""
    if torch.version.cuda is None:
        return f"the installed PyTorch build ({torch.__version__}) is CPU-only"
    return (
        f"PyTorch {torch.__version__} was built with CUDA {torch.version.cuda} "
        "but no usable NVIDIA GPU / driver was detected"
    )


def resolve_device(requested: str, component: str) -> str:
    """Return a device string that the current PyTorch build can use.

    Only ``"cpu"`` and ``"cuda"`` / ``"cuda:N"`` are supported. A CUDA request
    that cannot be satisfied, or any other device value (e.g. ``"mps"``,
    ``"xpu"``, a typo), is downgraded to ``"cpu"`` with a single WARNING log
    line that explains why and how to fix it.

    Args:
        requested: Device string from the configuration.
        component: Label used in log messages, e.g. ``"reranker"``.
    """
    try:
        dev = torch.device(requested)
    except (RuntimeError, TypeError, ValueError):
        dev = None

    if dev is None or dev.type not in SUPPORTED_DEVICES:
        logger.warning(
            "%s: device '%s' is not supported - falling back to 'cpu'. "
            "Supported values are 'cpu' and 'cuda' (NVIDIA GPU, see %s).",
            component,
            requested,
            DOCS_ANCHOR,
        )
        return CPU

    if dev.type == CUDA:
        if not torch.cuda.is_available():
            logger.warning(
                "%s: device '%s' requested but %s - falling back to 'cpu' (slower). %s",
                component,
                requested,
                _cuda_unavailable_reason(),
                cuda_install_hint(),
            )
            return CPU
        count = torch.cuda.device_count()
        if dev.index is not None and dev.index >= count:
            logger.warning(
                "%s: device '%s' requested but only %d CUDA device(s) are visible - "
                "falling back to 'cpu'.",
                component,
                requested,
                count,
            )
            return CPU

    return requested


def is_missing_model_error(exc: BaseException) -> bool:
    """Return True if *exc* means "the model files are not in the local cache".

    ``huggingface_hub`` raises ``LocalEntryNotFoundError`` when
    ``local_files_only=True`` and the files are absent; ``transformers`` /
    ``sentence-transformers`` usually re-raise it wrapped in an ``OSError``.
    The whole ``__cause__`` / ``__context__`` chain is inspected.
    """
    try:
        from huggingface_hub.errors import LocalEntryNotFoundError
    except ImportError:  # very old huggingface_hub
        LocalEntryNotFoundError = None  # noqa: N806

    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if LocalEntryNotFoundError is not None and isinstance(cur, LocalEntryNotFoundError):
            return True
        if isinstance(cur, OSError):
            msg = str(cur).lower()
            if "cached files" in msg or "disk cache" in msg or "local_files_only" in msg:
                return True
        cur = cur.__cause__ or cur.__context__
    return False
