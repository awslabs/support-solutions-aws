# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Run-time check that rows returned by the model appear in the source tables.

The model normalizes table text into JSON rows; it can also invent rows, from
its own knowledge or because the page asked it to. A row is kept only when one
of its identifying values (identifier, name or version) occurs in the table
text that was sent to the model.

Matching: lowercase, only letters, digits and dots, parenthesised suffixes
dropped, and a leading "word-" prefix tried both ways, so "Python 3.8" matches
"python3.8" and "neptune-1.2.1.0" matches "1.2.1.0". A value of three or more
characters may occur anywhere in the text; a shorter one (a major version such
as "3" or "14") must be a whole word of the text ("Aurora MySQL version 3"),
since as a substring it would match almost anything.

scripts/audit_service_configs.py uses this same module for its offline
--check-stored ratio.
"""
import re
from typing import Any, Dict, Iterable, List, Tuple

# Fields that identify a row, in the order they are tried.
IDENTIFYING_FIELDS = ("identifier", "name", "version")
_MIN_SUBSTRING_LENGTH = 3


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "", str(text).lower())


class PageText:
    """The text a row is checked against, prepared once per page."""

    def __init__(self, text: str):
        lowered = str(text).lower()
        self.normalized = normalize(lowered)
        # Whole words, split on any separator ("version 3" -> "3") and on whitespace
        # only, normalized ("8-0" -> "80"), so a short value copied from a cell matches.
        self.words = {w.strip(".") for w in re.split(r"[^a-z0-9.]+", lowered)} \
            | {normalize(w).strip(".") for w in lowered.split()}
        self.words.discard("")

    def contains(self, needle: str) -> bool:
        if len(needle) >= _MIN_SUBSTRING_LENGTH:
            return needle in self.normalized
        return bool(needle) and needle in self.words


def _variants(value: str) -> Iterable[str]:
    core = re.split(r"\s*[\(\[]", str(value))[0].strip()
    yield core
    yield re.sub(r"^[a-z]+[-_]", "", core, flags=re.IGNORECASE)


def candidates_of(item: Dict[str, Any]) -> List[str]:
    return [str(item[f]) for f in IDENTIFYING_FIELDS if item.get(f) not in (None, "")]


def appears_on_page(candidates: Iterable[str], page: PageText) -> bool:
    return any(page.contains(normalize(variant).strip(".")) for value in candidates for variant in _variants(value))


def keep_items_on_page(items: List[Any], page_text: str) -> Tuple[List[Dict[str, Any]], List[Any]]:
    """Split model rows into (kept, dropped). Non-dict rows are dropped."""
    page = PageText(page_text)
    kept, dropped = [], []
    for item in items:
        if isinstance(item, dict) and appears_on_page(candidates_of(item), page):
            kept.append(item)
        else:
            dropped.append(item)
    return kept, dropped
