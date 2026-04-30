from dataclasses import dataclass
from typing import Optional


@dataclass
class Event:
    source: str
    source_id: str
    course: str
    title: str
    event_type: str
    due: Optional[str]
    release: Optional[str]
    status: Optional[str]
    score: Optional[float]
    max_points: Optional[float]
    url: Optional[str]
    extra: Optional[str]  # JSON blob for type-specific fields


@dataclass
class Document:
    source: str
    source_id: str
    course: str
    title: str
    filename: str
    local_path: Optional[str]
    url: Optional[str]
    updated_at: Optional[str]
