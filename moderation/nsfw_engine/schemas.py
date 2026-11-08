"""
Pydantic schemas for structured moderation results returned by the engine.
"""
from __future__ import annotations
import enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class SeverityLevel(str, enum.Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class CategoryScore(BaseModel):
    category: str
    triggered: bool
    confidence: float = Field(ge=0.0, le=1.0)
    severity: SeverityLevel = SeverityLevel.NONE
    reason: str = ""


class ModerationResult(BaseModel):
    flagged: bool = False
    severity: SeverityLevel = SeverityLevel.NONE
    max_confidence: float = 0.0
    categories: List[CategoryScore] = []
    detected_language: str = "unknown"
    content_type: str = "text"
    # Flat dict of triggered categories for quick lookup
    triggered_categories: Dict[str, CategoryScore] = {}
    processing_notes: List[str] = []

    def compute_aggregate(self) -> "ModerationResult":
        """Populate triggered_categories, flagged, severity, max_confidence from categories list."""
        severity_order = [s.value for s in SeverityLevel]
        max_sev = SeverityLevel.NONE
        max_conf = 0.0
        triggered: Dict[str, CategoryScore] = {}

        for cat in self.categories:
            if cat.triggered:
                triggered[cat.category] = cat
                if cat.confidence > max_conf:
                    max_conf = cat.confidence
                if severity_order.index(cat.severity.value) > severity_order.index(max_sev.value):
                    max_sev = cat.severity

        self.triggered_categories = triggered
        self.flagged = len(triggered) > 0
        self.severity = max_sev
        self.max_confidence = round(max_conf, 4)
        return self


class TextAnalysisRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=10000)
    language_hint: Optional[str] = None
    chat_id: Optional[int] = None


class MediaAnalysisRequest(BaseModel):
    url: Optional[str] = None  # Remote URL OR file upload via multipart
    media_type: str = "auto"   # photo, video, audio, gif, sticker, document, auto
    chat_id: Optional[int] = None
    caption: Optional[str] = None


class BatchAnalysisRequest(BaseModel):
    items: List[TextAnalysisRequest] = Field(..., max_length=20)
