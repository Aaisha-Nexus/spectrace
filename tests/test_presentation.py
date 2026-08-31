from __future__ import annotations

from spectrace.presentation import semantic_impacts


def test_semantic_impacts_group_queue_meaning_and_filter_raw_tokens() -> None:
    result = semantic_impacts(
        {
            "capability_signature": {
                "actors": ["member"],
                "objects": ["first", "joined", "person", "queue", "capacity"],
                "domain_terms": ["queue", "email", "cancellation", "reservation"],
                "facets": ["ordering", "persistence"],
            },
            "dependencies": ["Valid cancellation", "Capacity-change event"],
        }
    )
    assert result["People and roles"] == ("Member",)
    assert result["Data and records"] == (
        "Queue membership", "Queue position", "Session capacity and availability", "Reservation status"
    )
    assert "Transactional or availability email" in result["Systems or notifications"]
    assert result["Rules and policies"] == ("Queue ordering", "Notification order")
    rendered = repr(result)
    assert "First" not in rendered and "Joined" not in rendered and "Person" not in rendered
