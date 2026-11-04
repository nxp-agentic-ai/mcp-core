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

"""Sentence-Transformers CrossEncoder-based reranker (offline, local).

Pairs naturally with the ``sentence_transformers`` embedder. Designed
to be the second stage of a retrieve-then-rerank pipeline: the
bi-encoder (``bge-base-en-v1.5``) does cheap recall over thousands of
chunks, then this reranker rescores the top-N candidates jointly with
the query.

Recommended model pairings:

* ``bge-base-en-v1.5``       -> ``bge-reranker-base``  (English, default)
* ``bge-large-en-v1.5``      -> ``bge-reranker-large`` (English, higher quality)
* ``bge-m3``                 -> ``bge-reranker-v2-m3`` (multilingual, long context)
"""
import logging
from pathlib import Path

# NOTE: import torch BEFORE sentence_transformers. On Windows, loading the
# transformers native extensions first can exhaust static-TLS slots and make
# torch's c10.dll init fail (WinError 1114). Importing torch first reserves
# its TLS slot. Do not remove or reorder, and do not drop as "unused".
import torch  # noqa: F401
from sentence_transformers import CrossEncoder

from nxp.mcp.knowledge.metadata import MCP_SERVER_NAME
from nxp.mcp.knowledge.model_utils import is_missing_model_error, resolve_device

from .base import RerankerProvider

PROVIDER = "cross_encoder"
# Default reranker - English, ~278M params, pairs with bge-base-en-v1.5.
DEFAULT_MODEL = "models/bge-reranker-base"
# Cross-encoders truncate the (query, document) pair to this many tokens.
# 512 matches the underlying BERT context window for the BGE rerankers.
DEFAULT_MAX_LENGTH = 512
# Default directory where downloaded models are cached / loaded from
DEFAULT_CACHE = "models"
# Default network policy - when True, never contact the HuggingFace Hub
DEFAULT_OFFLINE = False

logger = logging.getLogger(MCP_SERVER_NAME)



def _is_local_path(model: str) -> bool:
    """Return True if *model* refers to an existing filesystem path."""
    return Path(model).exists()


class CrossEncoderReranker(RerankerProvider):
    """Reranker backed by the ``sentence-transformers`` ``CrossEncoder`` class.

    The underlying model is loaded **lazily** on the first call to
    :meth:`rerank`, so server startup stays fast when the reranker is
    configured but never exercised (e.g. when ``enabled=True`` but no
    search request has arrived yet).

    Two loading modes are selected automatically based on the ``model`` value,
    mirroring :class:`~mcp_knowledge.embeddings.sentence_transformers.SentenceTransformersEmbedder`:

    * **Model name** (e.g. ``"bge-reranker-base"``) - the library downloads the
      model to the HuggingFace cache on first use.
    * **Local path** (e.g. ``"models/bge-reranker-base"``) - the model is
      loaded directly from disk; no network access is performed. Use
      ``scripts/download_model.py --type reranker`` to prepare the directory.

    Args:
        model:      HuggingFace model name **or** absolute/relative path to a
                    pre-downloaded model directory.
        device:     ``"cpu"`` or ``"cuda"`` / ``"cuda:N"`` (NVIDIA GPU). If CUDA
                    is unavailable (for example a CPU-only PyTorch build) or
                    the value is not supported, the model is loaded on
                    ``"cpu"`` instead and a warning is logged.
        max_length: Maximum tokens per (query, document) pair before truncation.
        cache:      Directory used to cache / load model files. Passed through to
                    the underlying ``CrossEncoder`` as ``cache_folder``.
                    Defaults to ``"models"``.
        offline:    When True, load only from the local cache and never contact
                    the HuggingFace Hub (passes ``local_files_only=True``). A
                    cached model then loads with zero network access; a model
                    that is not cached fails fast instead of attempting a
                    download. Ignored when ``model`` is a local filesystem path
                    (already fully local). Defaults to ``False``.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        device: str = "cpu",
        max_length: int = DEFAULT_MAX_LENGTH,
        cache: str = DEFAULT_CACHE,
        offline: bool = DEFAULT_OFFLINE,
    ) -> None:
        self._model_name = model
        self._device = device
        self._max_length = max_length
        self._cache = cache
        self._offline = offline
        self._model: CrossEncoder | None = None  # loaded lazily on first rerank()


    # ------------------------------------------------------------------
    # Lazy loader
    # ------------------------------------------------------------------

    def _ensure_model(self) -> None:
        if self._model is not None:
            return

        # Downgrade an unusable accelerator (e.g. "cuda" on a CPU-only torch
        # build) to "cpu" instead of failing every search request.
        self._device = resolve_device(self._device, "reranker")

        if _is_local_path(self._model_name):
            logger.info(
                "Loading reranker model from local path '%s' on device '%s'",
                self._model_name,
                self._device,
            )
        elif self._offline:
            logger.info(
                "Loading reranker model '%s' on device '%s' (offline: local cache only)",
                self._model_name,
                self._device,
            )
        else:
            logger.info(
                "Loading reranker model '%s' on device '%s' (will download if not cached)",
                self._model_name,
                self._device,
            )

        try:
            self._model = CrossEncoder(
                self._model_name,
                device=self._device,
                max_length=self._max_length,
                cache_folder=self._cache,
                local_files_only=self._offline,
            )
        except Exception as exc:
            if _is_local_path(self._model_name):
                logger.error(
                    "Failed to load reranker model from local path '%s' on device '%s': %s",
                    self._model_name,
                    self._device,
                    exc,
                )
            elif is_missing_model_error(exc):
                if self._offline:
                    logger.error(
                        "Failed to load reranker model '%s' in offline mode: %s\n"
                        "The model was not found in the local cache '%s'. Pre-download it or "
                        "set 'reranker.offline: false' to allow a download. "
                        "See docs/KNOWLEDGE_BASE.md#cross-encoder-reranker.",
                        self._model_name,
                        exc,
                        self._cache,
                    )
                else:
                    logger.error(
                        "Failed to load reranker model '%s': %s\n"
                        "The model is not cached in '%s' and could not be downloaded. If the "
                        "download is blocked, copy the model into the cache from a machine with "
                        "internet access, or set 'reranker.model' to a local directory path. "
                        "See docs/KNOWLEDGE_BASE.md#cross-encoder-reranker.",
                        self._model_name,
                        exc,
                        self._cache,
                    )
            else:
                # Not a cache problem (device, corrupt files, DLL load, OOM, ...):
                # report the real error instead of blaming the cache.
                logger.error(
                    "Failed to load reranker model '%s' on device '%s': %s: %s",
                    self._model_name,
                    self._device,
                    type(exc).__name__,
                    exc,
                )
            raise


        logger.info(
            "Reranker model ready - device=%s max_length=%d", self._device, self._max_length
        )

    # ------------------------------------------------------------------
    # RerankerProvider interface
    # ------------------------------------------------------------------

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        logger.info("Reranker received '%s' with %d documents", query, len(documents))
        if not documents:
            return []
        self._ensure_model()
        pairs = [(query, doc) for doc in documents]
        # ``predict`` returns a numpy array of logits/relevance scores.
        scores = self._model.predict(pairs, convert_to_numpy=True)
        return [float(s) for s in scores]
