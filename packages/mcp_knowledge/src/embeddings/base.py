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

"""Abstract base class for embedding providers."""
from abc import ABC, abstractmethod


class EmbedderProvider(ABC):
    """Contract for all embedding providers.

    Implement this interface to add a new embedding backend
    (e.g. OpenAI, Cohere, a local GGUF model, etc.).

    The token chunker (``TokenChunker``) additionally needs
    :attr:`max_input_tokens` and :meth:`token_offsets`, because chunk sizes
    are counted in the model's own tokens. The defaults raise
    ``NotImplementedError``, so a provider that only implements
    :meth:`embed` can embed text but cannot be used with the token chunker.
    """

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return a list of embedding vectors, one per input text.

        Args:
            texts: Non-empty list of strings to embed.

        Returns:
            List of float vectors, same length as *texts*.
        """

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Dimensionality of the embedding vectors produced by this provider."""

    # ------------------------------------------------------------------
    # Optional - needed for token-aware chunking
    # ------------------------------------------------------------------

    @property
    def tokenizer_id(self) -> str | None:
        """Stable, machine-independent id of the tokenizer (e.g. the model name).

        Used in the chunking signature stored with every chunk. ``None`` when
        unknown.
        """
        return None

    @property
    def max_input_tokens(self) -> int:
        """Largest number of text tokens (excluding special tokens) the model reads.

        Anything past this limit is truncated by the model and does not
        contribute to the embedding.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not expose its token limit, "
            "which the token chunker needs"
        )

    def token_offsets(self, texts: list[str]) -> list[list[tuple[int, int]]]:
        """Return the (start, end) character offsets of the tokens of each text.

        Special tokens are not included, so ``len(result[i])`` is the token
        count of ``texts[i]``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not expose a tokenizer, "
            "which the token chunker needs"
        )
