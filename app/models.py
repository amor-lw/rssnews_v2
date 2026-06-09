from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any, Dict, Optional, Tuple


@dataclasses.dataclass(frozen=True)
class Article:
    title: str
    url: str
    source: str
    published_at: datetime
    summary: str = ""
    lang: Optional[str] = None
    score: float = 0.0
    comments: int = 0
    tags: Tuple[str, ...] = ()
    kind: str = "news"
    priority: float = 0.0
    guid: str = ""
    source_type: str = ""
    domain: str = ""
    discovered_at: Optional[datetime] = None
    content_text: str = ""
    hn_id: Optional[int] = None
    hn_points: float = 0.0
    hn_comments: int = 0
    source_rank: Optional[int] = None
    source_list: str = ""
    source_family: str = ""
    base_score: float = 0.0
    bonus_score: float = 0.0
    hot_signal_type: str = ""
    raw: Dict[str, Any] = dataclasses.field(default_factory=dict)
