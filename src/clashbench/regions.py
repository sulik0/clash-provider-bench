from __future__ import annotations

import re


DEFAULT_PATTERNS: dict[str, list[str]] = {
    "JP": [r"🇯🇵", r"(?i:(^|[\s_\-|])(jp|japan|tokyo|osaka|日本|东京|東京|大阪)([\s_\-|]|$))"],
    "HK": [r"🇭🇰", r"(?i:(^|[\s_\-|])(hk|hong.?kong|香港)([\s_\-|]|$))"],
    "SG": [r"🇸🇬", r"(?i:(^|[\s_\-|])(sg|singapore|新加坡|狮城)([\s_\-|]|$))"],
    "US": [r"🇺🇸", r"(?i:(^|[\s_\-|])(us|usa|united.?states|america|美国|美國|洛杉矶|洛杉磯|西雅图|纽约|硅谷)([\s_\-|]|$))"],
    "TW": [r"🇹🇼", r"(?i:(^|[\s_\-|])(tw|taiwan|台湾|台灣|台北)([\s_\-|]|$))"],
    "KR": [r"🇰🇷", r"(?i:(^|[\s_\-|])(kr|korea|韩国|韓國|首尔|首爾)([\s_\-|]|$))"],
    "UK": [r"🇬🇧", r"(?i:(^|[\s_\-|])(uk|united.?kingdom|britain|英国|英國|london)([\s_\-|]|$))"],
    "DE": [r"🇩🇪", r"(?i:(^|[\s_\-|])(de|germany|德国|德國|frankfurt)([\s_\-|]|$))"],
}


def classify_region(name: str, overrides: dict[str, list[str]] | None = None) -> str:
    patterns = dict(DEFAULT_PATTERNS)
    for code, values in (overrides or {}).items():
        patterns[code.upper()] = list(values)
    for code, values in patterns.items():
        if any(re.search(pattern, name) for pattern in values):
            return code
    return "OTHER"


def region_filter_regex(regions: list[str], overrides: dict[str, list[str]] | None = None) -> str:
    if not regions:
        return ".+"
    patterns = dict(DEFAULT_PATTERNS)
    for code, values in (overrides or {}).items():
        patterns[code.upper()] = list(values)
    selected: list[str] = []
    for region in regions:
        selected.extend(patterns.get(region.upper(), []))
    if not selected:
        raise ValueError(f"No region patterns for: {', '.join(regions)}")
    return "(?:" + ")|(?:".join(selected) + ")"
