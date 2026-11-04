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

"""Ingestion pipeline - discovers files, parses, chunks, and upserts into a corpus."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from .chunker import (
    TokenChunker,
    chunking_signature,
    compose_source_hash,
    split_source_hash,
)
from .parsers import get_parser, supported_extensions
from nxp.mcp.knowledge.metadata import MCP_SERVER_NAME
from nxp.mcp.knowledge.db.store import KnowledgeStore
from nxp.mcp.knowledge.db.registry import KnowledgeStoreRegistry

logger = logging.getLogger(MCP_SERVER_NAME)


class IngestionPipeline:
    """Orchestrates file discovery, parsing, chunking, and vector upsert.

    Each public ingest/remove method takes a ``corpus`` argument selecting
    which table within the registry to write to. ``corpus=None`` resolves
    to the registry's default corpus (only valid when exactly one corpus
    is configured).

    Args:
        registry:      :class:`KnowledgeStoreRegistry` providing per-corpus stores.
        chunk_size:    Target chunk size in embedding-model tokens. Capped at
                       the model's input limit, so all chunk text is embedded.
        chunk_overlap: Tokens repeated between consecutive chunks.

    Change detection stores ``<file sha256>|<chunking signature>`` per source,
    so a file is re-ingested when its content OR the chunking settings change.
    """

    def __init__(
        self,
        registry: KnowledgeStoreRegistry,
        chunk_size: int = 400,
        chunk_overlap: int = 64,
    ) -> None:
        self._registry = registry
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        # Built on first use: it needs the embedding model's tokenizer, and
        # loading the model is skipped entirely when every file is unchanged.
        self._token_chunker: TokenChunker | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def registry(self) -> KnowledgeStoreRegistry:
        return self._registry

    @property
    def chunk_signature(self) -> str:
        """Signature of the configured chunking; does not load the model."""
        return chunking_signature(
            self._chunk_size, self._chunk_overlap, self._registry.embedder.tokenizer_id
        )

    def chunk(self, text: str) -> list[str]:
        """Split *text* into chunks that fit the embedding model."""
        return self.token_chunker().split(text)

    def token_chunker(self) -> TokenChunker:
        """Return the token chunker, creating it (and loading the model) once."""
        if self._token_chunker is None:
            embedder = self._registry.embedder
            limit = embedder.max_input_tokens
            if self._chunk_size > limit:
                logger.warning(
                    "chunk_size=%d tokens exceeds the embedding model limit of %d "
                    "tokens; chunks are capped at %d tokens",
                    self._chunk_size, limit, limit,
                )
            self._token_chunker = TokenChunker(
                embedder.token_offsets, limit, self._chunk_size, self._chunk_overlap
            )
            logger.info(
                "Token chunker ready: chunk_size=%d overlap=%d model_limit=%d tokens",
                self._token_chunker.chunk_size, self._token_chunker.chunk_overlap, limit,
            )
        return self._token_chunker

    def ingest_path(self, path: str | Path, corpus: str | None = None) -> dict:
        """Ingest a single file or recursively ingest a directory into *corpus*.

        Args:
            path:   File or directory path to ingest.
            corpus: Target corpus name; ``None`` falls back to the registry default.

        Returns:
            Dict with keys ``ingested``, ``skipped``, ``errors``.
        """
        store = self._registry.resolve(corpus)
        target = Path(path)
        if not target.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")

        files = [target] if target.is_file() else [f for f in target.rglob("*") if f.is_file()]

        ingested = skipped = errors = 0
        for file_path in sorted(files):
            result = self._ingest_file(file_path, store)
            if result == "ingested":
                ingested += 1
            elif result == "skipped":
                skipped += 1
            else:
                errors += 1

        logger.info(
            "Ingestion complete for '%s' (corpus=%s): ingested=%d skipped=%d errors=%d",
            path, store.collection, ingested, skipped, errors,
        )
        return {"ingested": ingested, "skipped": skipped, "errors": errors}

    def ingest_dirs(self, dirs: Iterable[str], corpus: str | None = None) -> dict:
        """Ingest all files across multiple directories into *corpus*."""
        total = {"ingested": 0, "skipped": 0, "errors": 0}
        for d in dirs:
            try:
                result = self.ingest_path(d, corpus=corpus)
                for key in total:
                    total[key] += result[key]
            except FileNotFoundError as exc:
                logger.warning("Skipping non-existent dir '%s': %s", d, exc)
        return total

    def ingest_all_configured(self, corpora) -> dict:
        """Run startup ingestion: for every configured corpus, scan its dirs.

        Args:
            corpora: Iterable of objects with ``name`` and ``dirs`` attributes
                     (typically :class:`CorpusSettings` instances).

        Returns:
            Aggregated stats across every corpus.
        """
        total = {"ingested": 0, "skipped": 0, "errors": 0}
        for c in corpora:
            if not c.dirs:
                logger.info("Corpus '%s' has no dirs - skipping", c.name)
                continue
            logger.info("Ingesting corpus '%s' from dirs: %s", c.name, list(c.dirs))
            result = self.ingest_dirs(c.dirs, corpus=c.name)
            for key in total:
                total[key] += result[key]
        return total

    def remove(self, source: str, corpus: str | None = None) -> None:
        """Remove all chunks for a given source path from *corpus*."""
        store = self._registry.resolve(corpus)
        store.delete(source)
        logger.info("Removed source '%s' from corpus '%s'", source, store.collection)

    @property
    def supported_extensions(self) -> frozenset[str]:
        """File extensions that can be parsed by the registry."""
        return supported_extensions()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ingest_file(self, path: Path, store: KnowledgeStore) -> str:
        """Parse, chunk, and upsert a single file into *store*."""
        parser = get_parser(path)
        if parser is None:
            logger.debug("No parser for '%s' (%s) - skipping", path.name, path.suffix)
            return "skipped"

        source = str(path)

        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.warning("Cannot read '%s': %s", source, exc)
            return "error"

        file_hash = store._file_hash(raw)
        signature = self.chunk_signature
        expected = compose_source_hash(file_hash, signature)
        stored = store.stored_source_hash(source)
        if stored == expected:
            logger.debug("Unchanged '%s' - skipping", source)
            return "skipped"
        if stored is not None:
            old_file_hash, old_signature = split_source_hash(stored)
            if old_file_hash == file_hash:
                logger.info(
                    "Re-chunking unchanged '%s': chunking settings changed (%s -> %s)",
                    source, old_signature or "legacy", signature,
                )

        try:
            text = parser.parse(path)
        except Exception as exc:
            logger.warning("Parse error for '%s': %s", source, exc)
            return "error"

        if not text.strip():
            logger.debug("Empty content for '%s' - skipping", source)
            return "skipped"

        try:
            chunks = self.chunk(text)
        except Exception as exc:
            logger.warning("Chunking error for '%s': %s", source, exc)
            return "error"
        if not chunks:
            return "skipped"

        try:
            count = store.upsert(source, chunks, source_hash=expected)
            logger.debug("Ingested '%s' -> %d chunks", source, count)
            return "ingested"
        except Exception as exc:
            logger.warning("Store error for '%s': %s", source, exc)
            return "error"
