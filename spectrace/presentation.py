"""BA-facing semantic summaries derived from verified structured outputs."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


STOPWORDS = frozenset(
    {
        "a", "an", "and", "first", "joined", "who", "person", "someone",
        "somebody", "the", "this", "that", "their", "they", "to", "with",
        "add", "make", "please", "could", "would", "can", "new", "one",
    }
)


def _values(value: Any) -> set[str]:
    if not value:
        return set()
    if isinstance(value, str):
        return {value.lower()}
    return {str(item).lower() for item in value if str(item).strip()}


def semantic_impacts(
    assessment: dict[str, Any], package: dict[str, Any] | None = None
) -> dict[str, tuple[str, ...]]:
    """Return conservative, evidence-backed impact categories without raw tokens."""

    signature = assessment.get("capability_signature") or {}
    package = package or {}
    actors = _values(signature.get("actors")) | _values(package.get("affected_actors"))
    objects = _values(signature.get("objects")) | _values(package.get("affected_components"))
    domains = _values(signature.get("domain_terms"))
    facets = _values(signature.get("facets"))
    dependencies = {
        value.strip() for value in package.get("dependencies", assessment.get("dependencies", ()))
        if value and value.strip().lower() not in STOPWORDS
    }
    vocabulary = actors | objects | domains | facets | {item.lower() for item in dependencies}
    joined = " ".join(sorted(vocabulary))
    result: dict[str, list[str]] = {}

    people = []
    for token, label in (
        ("member", "Member"), ("coordinator", "Studio coordinator"),
        ("administrator", "Schedule administrator"), ("system", "System"),
        ("student", "Student"), ("facilities", "Facilities coordinator"),
    ):
        if token in joined:
            people.append(label)
    if people:
        result["People and roles"] = people

    process = []
    if any(term in joined for term in ("queue", "full session", "capacity")):
        process.append("Full-session enrolment and availability path")
    if "cancel" in joined:
        process.append("Cancellation-to-availability path")
    if any(term in joined for term in ("reservation", "booking", "studio")):
        process.append("Reservation validation and status path")
    if process:
        result["Business process"] = process

    data = []
    if "queue" in joined:
        data.extend(("Queue membership", "Queue position"))
    if any(term in joined for term in ("capacity", "availability")):
        data.append("Session capacity and availability")
    if "reservation" in joined:
        data.append("Reservation status")
    if data:
        result["Data and records"] = data

    rules = []
    if any(term in joined for term in ("permission", "role", "authorization", "helper")):
        rules.append("Role permissions and authorized actions")
    if "queue" in joined:
        rules.append("Queue ordering")
        if any(term in joined for term in ("email", "notify", "notification", "alert")):
            rules.append("Notification order")
    if rules:
        result["Rules and policies"] = rules

    notifications = []
    if any(term in joined for term in ("email", "notify", "notification", "alert")):
        notifications.append("Transactional or availability email")
    if any(term in joined for term in ("calendar", "api", "integration", "sms")):
        notifications.append("External integration boundary")
    if notifications:
        result["Systems or notifications"] = notifications

    if dependencies:
        result.setdefault("Systems or notifications", []).extend(sorted(dependencies))

    branches = []
    if "queue" in joined:
        branches.extend(("Session full", "Capacity opens after a valid cancellation"))
    if "automation" in facets or "automatic" in joined:
        branches.append("Human action versus automatic allocation boundary")
    if branches:
        result.setdefault("Business process", []).extend(branches)

    return {
        heading: tuple(dict.fromkeys(values))
        for heading, values in result.items()
        if values
    }
