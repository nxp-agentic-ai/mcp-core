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

"""Text chunking - splits extracted text into overlapping chunks.

Chunk size and overlap are counted in the embedding model's own tokens.
:class:`TokenChunker` verifies that every chunk fits the model's hard
input limit. The model silently truncates anything past that limit, so
text beyond it would never contribute to the vector and could not be
found by ``kb_search``.

The token chunker works on ``text.split()`` words and keeps word
boundaries. It only cuts inside a word when that single word is longer
than the chunk budget; the cut is made at a token boundary.

Token chunks are slices of the normalized text ``" ".join(text.split())``
and carry their character offsets into it, so callers can verify that the
chunks cover the whole text with no gaps (see :meth:`TokenChunker.verify`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# Bump the version when the algorithm changes chunk boundaries for identical
# settings. The version is part of the chunking signature stored with every
# chunk, so a bump makes change detection re-ingest the affected sources.
_TOKENS_ALGO = "tokens-v1"

# Separator between the file content hash and the chunking signature inside
# the stored ``source_hash`` column. SHA-256 hex digests never contain it.
_HASH_SEPARATOR = "|"

# Words passed to the tokenizer per call when measuring single words.
_WORD_BATCH = 20_000
# Chunk-sized texts passed to the tokenizer per call when counting tokens.
# Kept small because offset mappings are materialized as Python tuples.
_TEXT_BATCH = 256

# Tokenizer hook used by the token chunker. For every input string it returns
# the (start, end) character offsets of its tokens, without special tokens
# such as [CLS] / [SEP]. The number of offsets is the token count.
TokenOffsetsFn = Callable[[list[str]], list[list[tuple[int, int]]]]


# ---------------------------------------------------------------------------
# Token-based chunking
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Unit:
    """Smallest piece of text the token chunker moves as a whole.

    Normally one whitespace-separated word. A word longer than the chunk
    budget is cut into several units at token boundaries. ``start`` / ``end``
    are character offsets into the normalized text.
    """

    start: int
    end: int
    ntok: int


@dataclass(frozen=True, slots=True)
class TokenChunk:
    """One chunk produced by :meth:`TokenChunker.split_detailed`.

    ``text == normalized[char_start:char_end]`` where ``normalized`` is
    ``" ".join(source_text.split())``. ``ntok`` is the token count of
    ``text`` measured with the tokenizer (without special tokens).
    """

    text: str
    char_start: int
    char_end: int
    ntok: int


class TokenChunker:
    """Split text into overlapping chunks measured in model tokens.

    Args:
        token_offsets: Tokenizer hook, see :data:`TokenOffsetsFn`.
        max_tokens:    Hard limit of the embedding model in tokens, excluding
                       special tokens (for BERT models ``max_seq_length - 2``).
                       Every returned chunk fits this limit.
        chunk_size:    Target chunk size in tokens. Clamped to ``max_tokens``.
        chunk_overlap: Target number of tokens repeated at the start of the
                       next chunk. Clamped to ``[0, chunk_size - 1]``. The
                       overlap is made of whole units, so the effective
                       overlap is at most this value.
    """

    def __init__(
        self,
        token_offsets: TokenOffsetsFn,
        max_tokens: int,
        chunk_size: int = 400,
        chunk_overlap: int = 64,
    ) -> None:
        if max_tokens is None or int(max_tokens) < 1:
            raise ValueError(f"TokenChunker requires max_tokens >= 1, got {max_tokens!r}")
        self._offsets = token_offsets
        self._max_tokens = int(max_tokens)
        self._chunk_size = max(1, min(int(chunk_size), self._max_tokens))
        self._chunk_overlap = max(0, min(int(chunk_overlap), self._chunk_size - 1))

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    @property
    def chunk_size(self) -> int:
        """Effective (clamped) chunk size in tokens."""
        return self._chunk_size

    @property
    def chunk_overlap(self) -> int:
        """Effective (clamped) overlap in tokens."""
        return self._chunk_overlap

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def count_tokens(self, texts: list[str]) -> list[int]:
        """Return the token count (without special tokens) of every text."""
        counts: list[int] = []
        for i in range(0, len(texts), _TEXT_BATCH):
            counts.extend(len(o) for o in self._offsets(texts[i:i + _TEXT_BATCH]))
        return counts

    def split(self, text: str) -> list[str]:
        """Split *text* into chunks; returns an empty list for blank text."""
        return [c.text for c in self.split_detailed(text)[1]]

    def split_detailed(self, text: str) -> tuple[str, list[TokenChunk]]:
        """Split *text* and return ``(normalized_text, chunks)``."""
        words = text.split()
        if not words:
            return "", []
        norm = " ".join(words)
        units = self._make_units(words)
        spans = self._windows(units)
        texts = [norm[units[s].start:units[e - 1].end] for s, e in spans]
        counts = self.count_tokens(texts)
        chunks: list[TokenChunk] = []
        for (s, e), t, n in zip(spans, texts, counts):
            if n <= self._max_tokens:
                chunks.append(TokenChunk(t, units[s].start, units[e - 1].end, n))
            else:
                # Token counts are not strictly additive for every tokenizer
                # (re-tokenizing a joined string can merge or split pieces
                # differently), so split again anything that ended up too long.
                chunks.extend(self._refit(norm, units, s, e))
        return norm, chunks

    def verify(self, normalized: str, chunks: list[TokenChunk]) -> list[str]:
        """Return a list of problems; an empty list means the chunks are valid.

        Checks that every chunk is an exact slice of *normalized*, fits the
        token limit, that chunks move forward, and that together they cover
        *normalized* from the first to the last character with no gap other
        than whitespace.
        """
        problems: list[str] = []
        if not normalized:
            return [] if not chunks else ["chunks produced for empty text"]
        if not chunks:
            return ["no chunks produced for non-empty text"]
        if chunks[0].char_start != 0:
            problems.append(f"first chunk starts at {chunks[0].char_start}, expected 0")
        if chunks[-1].char_end != len(normalized):
            problems.append(
                f"last chunk ends at {chunks[-1].char_end}, expected {len(normalized)}"
            )
        prev: TokenChunk | None = None
        for i, c in enumerate(chunks):
            if normalized[c.char_start:c.char_end] != c.text:
                problems.append(f"chunk {i} text is not the slice it claims to be")
            if c.ntok > self._max_tokens:
                problems.append(f"chunk {i} has {c.ntok} tokens > limit {self._max_tokens}")
            if prev is not None:
                if c.char_start <= prev.char_start:
                    problems.append(f"chunk {i} does not move forward")
                gap = normalized[prev.char_end:c.char_start]
                if gap.strip():
                    problems.append(f"gap before chunk {i}: {gap[:40]!r}")
            prev = c
        return problems

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _make_units(self, words: list[str]) -> list[_Unit]:
        units: list[_Unit] = []
        budget = self._chunk_size
        pos = 0  # start of the current word inside the normalized text
        for i in range(0, len(words), _WORD_BATCH):
            batch = words[i:i + _WORD_BATCH]
            for word, offs in zip(batch, self._offsets(batch)):
                n = len(offs)
                if n <= budget:
                    units.append(_Unit(pos, pos + len(word), n))
                else:
                    # Oversized single word: cut at token boundaries. Each
                    # piece runs up to the first character of the next piece,
                    # so no character of the word is dropped.
                    for a in range(0, n, budget):
                        b = min(a + budget, n)
                        cs = 0 if a == 0 else offs[a][0]
                        ce = len(word) if b == n else offs[b][0]
                        units.append(_Unit(pos + cs, pos + ce, b - a))
                pos += len(word) + 1
        return units

    def _windows(self, units: list[_Unit]) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        n = len(units)
        start = 0
        while start < n:
            total = 0
            end = start
            # Always take at least one unit so the loop makes progress.
            while end < n and (end == start or total + units[end].ntok <= self._chunk_size):
                total += units[end].ntok
                end += 1
            spans.append((start, end))
            if end >= n:
                break
            # Walk back from the end to collect up to chunk_overlap tokens of
            # whole units, but always move the next start forward.
            nxt = end
            overlap = 0
            while nxt - 1 > start and overlap + units[nxt - 1].ntok <= self._chunk_overlap:
                nxt -= 1
                overlap += units[nxt].ntok
            start = nxt
        return spans

    def _refit(self, norm: str, units: list[_Unit], start: int, end: int) -> list[TokenChunk]:
        """Recursively halve ``units[start:end]`` until every piece fits."""
        cs, ce = units[start].start, units[end - 1].end
        text = norm[cs:ce]
        n = self.count_tokens([text])[0]
        if n <= self._max_tokens:
            return [TokenChunk(text, cs, ce, n)]
        if end - start > 1:
            mid = (start + end) // 2
            return self._refit(norm, units, start, mid) + self._refit(norm, units, mid, end)
        # A single unit that re-tokenizes above the limit: cut by characters.
        return self._split_chars(norm, cs, ce)

    def _split_chars(self, norm: str, cs: int, ce: int) -> list[TokenChunk]:
        text = norm[cs:ce]
        n = self.count_tokens([text])[0]
        if n <= self._max_tokens or ce - cs <= 1:
            return [TokenChunk(text, cs, ce, n)]
        mid = (cs + ce) // 2
        return self._split_chars(norm, cs, mid) + self._split_chars(norm, mid, ce)


# ---------------------------------------------------------------------------
# Change-detection signature
# ---------------------------------------------------------------------------

def chunking_signature(
    chunk_size: int,
    chunk_overlap: int,
    tokenizer_id: str | None = None,
) -> str:
    """Describe the chunking settings that produced a set of chunks.

    Sizes are embedding-model tokens. Stored together with the file hash so
    that changing the chunking settings, the tokenizer or the chunking
    algorithm version re-ingests otherwise unchanged files.
    """
    return (
        f"chunker={_TOKENS_ALGO};size={chunk_size};overlap={chunk_overlap};"
        f"tokenizer={tokenizer_id or 'unknown'}"
    )


def compose_source_hash(file_hash: str, signature: str) -> str:
    """Combine a file content hash and a chunking signature for storage."""
    return f"{file_hash}{_HASH_SEPARATOR}{signature}"


def split_source_hash(stored: str) -> tuple[str, str]:
    """Inverse of :func:`compose_source_hash`.

    A legacy value without a signature returns ``(stored, "")``.
    """
    file_hash, _, signature = (stored or "").partition(_HASH_SEPARATOR)
    return file_hash, signature
