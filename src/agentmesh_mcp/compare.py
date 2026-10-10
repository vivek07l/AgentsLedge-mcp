"""Conservative normalization helpers for marketplace result comparison.

The adapter preserves the raw record and only fills normalized fields when a
matching field exists. Missing values remain None; no ratings or capabilities
are invented.
"""

from __future__ import annotations

from typing import Any


_LIST_KEYS = ("agents", "items", "results", "data")
_ID_KEYS = ("agent_id", "id")
_NAME_KEYS = ("name", "agent_name", "title")
_PRICE_KEYS = ("price", "token_price", "price_tokens")
_CAPABILITY_KEYS = ("capabilities", "skills", "tags")
_RATING_KEYS = ("rating", "average_rating", "average_review_rating")
_SUCCESS_KEYS = ("success_rate", "successRate")


def extract_records(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    """Accept a list or a typical paginated/list wrapper without fabricating data."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], None
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            candidate = payload.get(key)
            if isinstance(candidate, list):
                total = payload.get("total", payload.get("total_count", payload.get("count")))
                return (
                    [item for item in candidate if isinstance(item, dict)],
                    total if isinstance(total, int) and not isinstance(total, bool) else None,
                )
    return [], None


def first_present(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def normalise_provider(record: dict[str, Any], reputation: Any = None) -> dict[str, Any]:
    """Return comparable fields plus the source records for transparency."""
    reputation_record = reputation if isinstance(reputation, dict) else {}
    rating = first_present(record, _RATING_KEYS)
    success_rate = first_present(record, _SUCCESS_KEYS)
    if rating is None:
        rating = first_present(reputation_record, _RATING_KEYS)
    if success_rate is None:
        success_rate = first_present(reputation_record, _SUCCESS_KEYS)

    capabilities = first_present(record, _CAPABILITY_KEYS)
    if capabilities is None:
        capabilities = first_present(reputation_record, _CAPABILITY_KEYS)

    return {
        "agent_id": first_present(record, _ID_KEYS),
        "name": first_present(record, _NAME_KEYS),
        "price": first_present(record, _PRICE_KEYS),
        "rating": rating,
        "success_rate": success_rate,
        "capabilities": capabilities,
        "listing": record,
        "reputation": reputation if reputation is not None else None,
    }
