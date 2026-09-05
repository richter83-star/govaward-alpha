"""Legacy Claude enricher module — re-exports from gemini_enricher for backward compatibility."""

from __future__ import annotations

from govaward_alpha.enrich.gemini_enricher import (
    ClaudeAnalysis,
    GeminiAnalysis,
    enrich,
)

__all__ = ["ClaudeAnalysis", "GeminiAnalysis", "enrich"]
