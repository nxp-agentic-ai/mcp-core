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

"""PDF parser -- extracts text layer using PyMuPDF."""
import logging
# from io import BytesIO
from pathlib import Path

import pymupdf  # canonical import; avoids the stdout print() regression in the fitz shim (1.28+)

from .base import Parser
from nxp.mcp.knowledge.metadata.server import MCP_SERVER_NAME

_logger = logging.getLogger(MCP_SERVER_NAME)


class PdfParser(Parser):
    """Extracts text from PDF files using PyMuPDF.

    Only the text layer is extracted (no OCR).  Scanned PDFs without a text
    layer will return an empty string and be skipped by the ingestion pipeline.
    """

    extensions: frozenset[str] = frozenset({".pdf"})

    def parse(self, path: Path) -> str:
        """Extract text layer from PDF using PyMuPDF (fast, no OCR)."""
        try:
            with pymupdf.open(str(path)) as doc:
                parts = []
                for page in doc:
                    text = page.get_text()
                    if text:
                        parts.append(text)
                return "\n".join(parts).strip()
        except Exception as e:
            _logger.error(f"Error reading PDF {path.name}: {e}")
        