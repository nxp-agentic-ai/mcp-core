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

"""Sentence Transformers embedding provider."""
import logging
from pathlib import Path

# NOTE: import torch BEFORE sentence_transformers. On Windows, loading the
# transformers native extensions first can exhaust static-TLS slots and make
# torch's c10.dll init fail (WinError 1114). Importing torch first reserves
# its TLS slot. Do not remove or reorder, and do not drop as "unused".
import torch  # noqa: F401
from sentence_transformers import SentenceTransformer

from nxp.mcp.knowledge.metadata import MCP_SERVER_NAME
from nxp.mcp.knowledge.model_utils import is_missing_model_error, resolve_device

from .base import EmbedderProvider

PROVIDER = "sentence_transformers"
# Default model — lightweight, fast, good all-round quality for semantic search
DEFAULT_MODEL = "bge-base-en-v1.5"
DEFAULT_DIMENSION = 768
# Default directory where downloaded models are cached / loaded from
DEFAULT_CACHE = "models"
# Default network policy - when True, never contact the HuggingFace Hub
DEFAULT_OFFLINE = False


logger = logging.getLogger(MCP_SERVER_NAME)


def _is_local_path(model: str) -> bool:
    """Return True if *model* refers to an existing filesystem path."""
    return Path(model).exists()


def tokenizer_id_from_model(model: str) -> str:
    """Return a machine-independent tokenizer id for a model reference.

    ``BAAI/bge-base-en-v1.5`` and ``C:/models/bge-base-en-v1.5`` both map to
    ``bge-base-en-v1.5``, so the id does not change between machines that
    keep the model in different places.
    """
    name = Path(str(model).replace("\\", "/").rstrip("/")).name
    return name or str(model)


class SentenceTransformersEmbedder(EmbedderProvider):
    """Embedding provider backed by the ``sentence-transformers`` library.

    The underlying model is loaded **lazily** on the first call to
    :meth:`embed`, so that server startup is fast when all indexed
    files are unchanged and no embedding is required.

    Two loading modes are selected automatically based on the ``model`` value:

    * **Model name** (e.g. ``"bge-base-en-v1.5"``) — the library downloads the
      model to the HuggingFace cache on first use and loads from disk on
      subsequent runs.
    * **Local path** (e.g. ``"C:/models/bge-base-en-v1.5"``) — the model is
      loaded directly from the given directory; no network access is performed.

    Args:
        model:     HuggingFace model name **or** absolute/relative path to a
                   pre-downloaded model directory.
        device:    ``"cpu"`` or ``"cuda"`` / ``"cuda:N"`` (NVIDIA GPU). If CUDA
                   is unavailable (for example a CPU-only PyTorch build) or
                   the value is not supported, the model is loaded on
                   ``"cpu"`` instead and a warning is logged.
        dimension: Expected embedding dimension.  Must match the chosen model.
                   Used to configure the LanceDB schema without loading the
                   model at startup.  Defaults to 768 (``bge-base-en-v1.5``).
        cache:     Directory used to cache / load model files. Passed through to
                   the underlying ``SentenceTransformer`` as ``cache_folder``.
                   Defaults to ``"models"``.
        offline:   When True, load only from the local cache and never contact
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
        dimension: int = DEFAULT_DIMENSION,
        cache: str = DEFAULT_CACHE,
        offline: bool = DEFAULT_OFFLINE,
    ) -> None:
        self._model_name = model
        self._device = device
        self._dimension = dimension
        self._cache = cache
        self._offline = offline
        self._model = None  # loaded lazily on first embed() call


    # ------------------------------------------------------------------
    # Lazy loader
    # ------------------------------------------------------------------

    def _ensure_model(self) -> None:
        """Load the SentenceTransformer model if not already loaded."""
        if self._model is not None:
            return

        # Downgrade an unusable accelerator (e.g. "cuda" on a CPU-only torch
        # build) to "cpu" instead of failing every embed / search request.
        self._device = resolve_device(self._device, "embedder")

        if _is_local_path(self._model_name):
            logger.info(
                "Loading embedding model from local path '%s' on device '%s'",
                self._model_name,
                self._device,
            )
        elif self._offline:
            logger.info(
                "Loading embedding model '%s' on device '%s' (offline: local cache only)",
                self._model_name,
                self._device,
            )
        else:
            logger.info(
                "Loading embedding model '%s' on device '%s' (will download if not cached)",
                self._model_name,
                self._device,
            )

        try:
            self._model = SentenceTransformer(
                self._model_name,
                device=self._device,
                cache_folder=self._cache,
                local_files_only=self._offline,
            )
        except Exception as exc:
            if _is_local_path(self._model_name):
                logger.error(
                    "Failed to load embedding model from local path '%s' on device '%s': %s",
                    self._model_name,
                    self._device,
                    exc,
                )
            elif is_missing_model_error(exc):
                if self._offline:
                    logger.error(
                        "Failed to load embedding model '%s' in offline mode: %s\n"
                        "The model was not found in the local cache '%s'. Pre-download it "
                        "(e.g. with the model id, so a '%s' cache snapshot is created) or set "
                        "'embedder.offline: false' to allow a download.",
                        self._model_name,
                        exc,
                        self._cache,
                        self._model_name,
                    )
                else:
                    logger.error(
                        "Failed to load embedding model '%s': %s\n"
                        "Either the name is invalid or the download is blocked",
                        self._model_name,
                        exc,
                    )
            else:
                # Not a cache problem (device, corrupt files, DLL load, OOM, ...):
                # report the real error instead of blaming the cache.
                logger.error(
                    "Failed to load embedding model '%s' on device '%s': %s: %s",
                    self._model_name,
                    self._device,
                    type(exc).__name__,
                    exc,
                )
            raise


        actual_dim = self._model.get_embedding_dimension()
        if actual_dim != self._dimension:
            logger.warning(
                "Configured dimension=%d does not match model dimension=%d — "
                "update 'embedder.dimension' in your config to avoid schema mismatch",
                self._dimension,
                actual_dim,
            )
            self._dimension = actual_dim

        logger.info("Embedding model ready — dimension=%d", self._dimension)

    # ------------------------------------------------------------------
    # EmbedderProvider interface
    # ------------------------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure_model()
        self._warn_if_truncated(texts)
        vectors = self._model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return [v.tolist() for v in vectors]

    @property
    def dimension(self) -> int:
        return self._dimension

    # ------------------------------------------------------------------
    # Tokenizer access (token-aware chunking)
    # ------------------------------------------------------------------

    @property
    def tokenizer_id(self) -> str:
        """Model name without its location; does not load the model."""
        return tokenizer_id_from_model(self._model_name)

    @property
    def max_input_tokens(self) -> int:
        """Text tokens the model reads per input, excluding special tokens.

        Derived from the loaded model (``max_seq_length`` minus the special
        tokens the tokenizer adds, e.g. [CLS] and [SEP] for BERT models), so
        it follows whatever model is configured. Loads the model on first use.
        """
        self._ensure_model()
        max_len = self._model.max_seq_length
        if not max_len:
            max_len = getattr(self._model.tokenizer, "model_max_length", None)
        if not max_len or max_len > 1_000_000:
            raise ValueError(
                f"Embedding model '{self._model_name}' does not declare a maximum "
                "sequence length; cannot use token-based chunking"
            )
        specials = self._model.tokenizer.num_special_tokens_to_add(pair=False)
        return int(max_len) - int(specials)

    def token_offsets(self, texts: list[str]) -> list[list[tuple[int, int]]]:
        """Character offsets of every token, without special tokens and untruncated."""
        self._ensure_model()
        tok = self._model.tokenizer
        if not getattr(tok, "is_fast", False):
            raise ValueError(
                f"Embedding model '{self._model_name}' has no fast tokenizer; "
                "token-based chunking needs offset mappings"
            )
        enc = tok(
            list(texts),
            add_special_tokens=False,
            return_offsets_mapping=True,
            return_attention_mask=False,
            return_token_type_ids=False,
            verbose=False,
        )
        return [[(int(a), int(b)) for a, b in offs] for offs in enc["offset_mapping"]]

    def _warn_if_truncated(self, texts: list[str]) -> None:
        """Log a warning when an input is longer than the model reads.

        The model silently drops everything past its limit, so such text can
        never be found by kb_search.
        """
        try:
            limit = self.max_input_tokens
            ids = self._model.tokenizer(
                list(texts),
                add_special_tokens=False,
                return_attention_mask=False,
                return_token_type_ids=False,
                verbose=False,
            )["input_ids"]
        except Exception as exc:  # never block embedding on a diagnostic
            logger.debug("Token-length check skipped: %s", exc)
            return
        lengths = [len(x) for x in ids]
        over = [n for n in lengths if n > limit]
        if over:
            logger.warning(
                "embed: %d of %d input(s) exceed the model limit of %d tokens "
                "(longest %d); text past the limit is ignored by the model. "
                "Keep settings.chunk_size <= %d.",
                len(over), len(texts), limit, max(over), limit,
            )
