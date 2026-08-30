"""Deterministic, inspectable tools for advanced request analysis."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence

from spectrace.advanced_models import (
    AmbiguityFinding,
    AmbiguityKind,
    CapabilitySignature,
    ConflictAssessment,
    ConflictFinding,
    DecisionPolarity,
    DriftAssessment,
    DriftSeverity,
    DriftThresholds,
    EvidenceCategory,
    RetrievalBundle,
    ScopeAnchor,
    SufficiencyAssessment,
    TemporalStatus,
)
from spectrace.models import Classification, IncomingRequest
from spectrace.retrieval import retrieval_tokens
from spectrace.scope_anchor import ACTION_WORDS, FACET_KEYWORDS, normalize_text


VAGUE_QUALITATIVE_TARGETS = frozenset(
    {
        "better", "efficient", "fast", "faster", "immediate", "immediately",
        "instant", "instantly", "quick", "quickly", "real time", "realtime",
        "responsive", "seamless", "user friendly",
    }
)
MEASURABLE_TARGET_RE = re.compile(
    r"\b(?:within|under|less than|no more than|at least)\s+\d+(?:\.\d+)?\s*"
    r"(?:ms|milliseconds?|seconds?|minutes?|hours?|%|percent)?\b|\b\d+(?:\.\d+)?%"
)
ROLE_ACTIONS = ACTION_WORDS | {
    "administer", "assign", "authorize", "edit", "handle", "moderate", "operate"
}
NON_ACTOR_TERMS = frozenset(
    {
        "application", "availability", "booking", "capacity", "place", "portal",
        "anybody", "anyone", "everybody", "person", "request", "reservation",
        "session", "somebody", "someone", "system", "workflow",
    }
)
GENERIC_MATCH_TERMS = frozenset(
    {
        "approve", "approved", "automatic", "automatically", "capability",
        "confirm", "confirmation", "email", "full", "make", "place", "receive",
        "request", "session", "system",
    }
)
LIFECYCLE_FACETS = frozenset(
    {"automation", "capacity", "notification", "ordering", "persistence", "workflow"}
)


def _sorted(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(value for value in values if value)))


def _question_evidence(bundle: RetrievalBundle, request_terms: set[str]) -> tuple[str, ...]:
    matches = []
    for retrieved in bundle.items:
        item = retrieved.evidence
        if item.category != EvidenceCategory.UNRESOLVED_QUESTION:
            continue
        if request_terms & set(retrieval_tokens(item.source_text)):
            matches.append(item.evidence_id)
    return tuple(sorted(matches))


def _known_actors(anchor: ScopeAnchor) -> set[str]:
    return {
        actor
        for item in anchor.items
        for actor in item.actor_terms
    }


def _undefined_actor_candidates(request_text: str, known_actors: set[str]) -> tuple[str, ...]:
    tokens = tuple(normalize_text(request_text).split())
    known_words = {word for actor in known_actors for word in actor.split()}
    candidates: set[str] = set()
    for index, token in enumerate(tokens):
        if token not in ROLE_ACTIONS or index == 0:
            continue
        raw_candidate = tokens[index - 1]
        canonical = retrieval_tokens(raw_candidate)
        if not canonical:
            continue
        candidate = canonical[0]
        if (
            candidate not in known_words
            and candidate not in NON_ACTOR_TERMS
            and candidate not in ROLE_ACTIONS
            and len(candidate) > 2
        ):
            candidates.add(candidate)
    return tuple(sorted(candidates))


def assess_sufficiency(
    request: IncomingRequest,
    retrieval: RetrievalBundle,
    anchor: ScopeAnchor,
) -> SufficiencyAssessment:
    """Separate classification-blocking ambiguity from later acceptance detail."""

    request_terms = set(retrieval_tokens(request.message))
    question_ids = _question_evidence(retrieval, request_terms)
    findings: list[AmbiguityFinding] = []

    unknown_actors = _undefined_actor_candidates(request.message, _known_actors(anchor))
    for actor in unknown_actors:
        findings.append(
            AmbiguityFinding(
                kind=AmbiguityKind.UNDEFINED_ACTOR,
                description=f"The actor or role '{actor}' is not defined by approved role evidence.",
                evidence_ids=question_ids,
                blocking=True,
                heuristic=not bool(question_ids),
                clarification_question=(
                    f"Which approved role, or what new role and permission boundary, does '{actor}' mean?"
                ),
            )
        )

    normalized = normalize_text(request.message)
    vague_matches = sorted(
        phrase for phrase in VAGUE_QUALITATIVE_TARGETS if phrase in normalized
    )
    if vague_matches and not MEASURABLE_TARGET_RE.search(normalized):
        target = vague_matches[0]
        findings.append(
            AmbiguityFinding(
                kind=AmbiguityKind.VAGUE_QUALITATIVE_TARGET,
                description=(
                    f"The qualitative target '{target}' has no measurable or observable boundary."
                ),
                evidence_ids=question_ids,
                blocking=True,
                heuristic=not bool(question_ids),
                clarification_question=(
                    "What observable behavior and measurable target must the requested change meet?"
                ),
            )
        )

    blocking_evidence = {
        evidence_id for finding in findings if finding.blocking for evidence_id in finding.evidence_ids
    }
    for evidence_id in question_ids:
        if evidence_id in blocking_evidence:
            continue
        findings.append(
            AmbiguityFinding(
                kind=AmbiguityKind.ACCEPTANCE_DETAIL,
                description=(
                    "A related unresolved question affects later acceptance details but does not "
                    "by itself prevent scope classification."
                ),
                evidence_ids=(evidence_id,),
                blocking=False,
                heuristic=False,
            )
        )

    blocking = [finding for finding in findings if finding.blocking]
    questions = _sorted(
        finding.clarification_question
        for finding in blocking
        if finding.clarification_question
    )
    return SufficiencyAssessment(
        sufficient_for_classification=not blocking,
        findings=tuple(findings),
        clarification_questions=questions,
        rationale=(
            "Classification is blocked by undefined or unmeasured request behavior."
            if blocking
            else "The requested capability is identifiable; remaining questions are acceptance details."
        ),
    )


def _labeled_clause(text: str, label_pattern: str) -> str:
    match = re.search(
        rf"^- \*\*(?:{label_pattern}):\*\* ([\s\S]*?)(?=^- \*\*[A-Za-z ]+(?: and [A-Za-z ]+)?:\*\*|\Z)",
        text,
        re.MULTILINE,
    )
    return " ".join(match.group(1).split()) if match else ""


def _overlap(request_terms: set[str], text: str, actors: set[str]) -> set[str]:
    ignored = {word for actor in actors for word in actor.split()}
    return (request_terms & set(retrieval_tokens(text))) - ignored - GENERIC_MATCH_TERMS


def _substantive_match(
    request_terms: set[str],
    text: str,
    actors: set[str],
    *,
    allow_facet_single: bool = False,
) -> bool:
    overlap = _overlap(request_terms, text, actors)
    request_facets = {
        facet for facet, keywords in FACET_KEYWORDS.items() if request_terms & keywords
    }
    text_terms = set(retrieval_tokens(text))
    text_facets = {
        facet for facet, keywords in FACET_KEYWORDS.items() if text_terms & keywords
    }
    return len(overlap) >= 2 or bool(
        allow_facet_single and request_facets & text_facets and overlap
    )


def find_effective_conflicts(
    request: IncomingRequest,
    retrieval: RetrievalBundle,
    anchor: ScopeAnchor,
) -> ConflictAssessment:
    """Return active approval, rejection, neutral-boundary, and exclusion signals."""

    request_terms = set(retrieval_tokens(request.message))
    actors = _known_actors(anchor)
    findings: list[ConflictFinding] = []
    approved: set[str] = set()
    conflicting: set[str] = set()
    exclusions: set[str] = set()
    neutral: set[str] = set()
    specific_approved: set[str] = set()

    for retrieved in retrieval.items:
        item = retrieved.evidence
        if item.temporal_status in {TemporalStatus.FUTURE, TemporalStatus.SUPERSEDED}:
            continue
        if item.category == EvidenceCategory.APPROVED_SCOPE:
            if _substantive_match(request_terms, item.source_text, actors):
                approved.add(item.evidence_id)
            continue
        if item.category == EvidenceCategory.EXCLUSION:
            if _substantive_match(request_terms, item.source_text, actors):
                exclusions.add(item.evidence_id)
            continue
        if item.category != EvidenceCategory.DECISION:
            continue

        rejects = _labeled_clause(item.source_text, r"Rejects")
        approves = _labeled_clause(item.source_text, r"Approves")
        neutral_clause = _labeled_clause(
            item.source_text,
            r"Does not approve(?: and does not reject)?|Does not approve",
        )
        reject_match = bool(rejects) and _substantive_match(request_terms, rejects, actors)
        neutral_match = bool(neutral_clause) and _substantive_match(
            request_terms, neutral_clause, actors, allow_facet_single=True
        )
        approve_match = bool(approves) and _substantive_match(request_terms, approves, actors)
        if item.decision_polarity == DecisionPolarity.REJECTS and _substantive_match(
            request_terms, item.source_text, actors
        ):
            reject_match = True

        if reject_match:
            conflicting.add(item.evidence_id)
            findings.append(
                ConflictFinding(
                    evidence_id=item.evidence_id,
                    polarity=DecisionPolarity.REJECTS,
                    description="The identifiable request opposes a current specific rejection.",
                    facet_terms=item.facet_terms,
                    specific=True,
                    active=True,
                )
            )
        elif neutral_match:
            neutral.add(item.evidence_id)
            findings.append(
                ConflictFinding(
                    evidence_id=item.evidence_id,
                    polarity=DecisionPolarity.DOES_NOT_APPROVE_OR_REJECT,
                    description="The current decision leaves this capability unapproved and unrejected.",
                    facet_terms=item.facet_terms,
                    specific=True,
                    active=True,
                )
            )
        elif approve_match:
            approved.add(item.evidence_id)
            specific_approved.add(item.evidence_id)
            findings.append(
                ConflictFinding(
                    evidence_id=item.evidence_id,
                    polarity=item.decision_polarity or DecisionPolarity.APPROVES,
                    description="A current decision approves the matching capability facet.",
                    facet_terms=item.facet_terms,
                    specific=True,
                    active=True,
                )
            )

    if neutral:
        approved = specific_approved
    return ConflictAssessment(
        findings=tuple(findings),
        proven_specific_contradiction=bool(conflicting),
        conflicting_evidence_ids=tuple(sorted(conflicting)),
        approved_evidence_ids=tuple(sorted(approved)),
        exclusion_evidence_ids=tuple(sorted(exclusions)),
        neutral_boundary_evidence_ids=tuple(sorted(neutral)),
        rationale=(
            "A current specific approved rejection conflicts with the request."
            if conflicting
            else "No current specific approved-decision contradiction was proven."
        ),
    )


def reconcile_classification(
    model_recommendation: Classification,
    sufficiency: SufficiencyAssessment,
    conflicts: ConflictAssessment,
    *,
    deterministic_evidence_available: bool = True,
) -> Classification:
    """Apply the frozen precedence without manufacturing missing evidence."""

    if conflicts.proven_specific_contradiction:
        return Classification.CONTRADICTS_APPROVED_DECISION
    if not sufficiency.sufficient_for_classification:
        return Classification.AMBIGUOUS
    if conflicts.approved_evidence_ids:
        return Classification.IN_SCOPE
    if conflicts.exclusion_evidence_ids:
        return Classification.OUT_OF_SCOPE
    if deterministic_evidence_available:
        return Classification.POTENTIAL_SCOPE_CHANGE
    return model_recommendation


def build_capability_signature(
    text: str,
    *,
    evidence_ids: Sequence[str] = (),
    request_ids: Sequence[str] = (),
    decision_ids: Sequence[str] = (),
    known_actors: Iterable[str] = (),
    dependencies: Sequence[str] = (),
) -> CapabilitySignature:
    terms = set(retrieval_tokens(text))
    actors = {actor for actor in known_actors if normalize_text(actor) in normalize_text(text)}
    actor_words = {word for actor in actors for word in actor.split()}
    actions = terms & ROLE_ACTIONS
    facets = {
        facet for facet, keywords in FACET_KEYWORDS.items() if terms & keywords
    }
    objects = terms - actions - actor_words
    dependency_terms = {
        term for dependency in dependencies for term in retrieval_tokens(dependency)
    }
    return CapabilitySignature(
        domain_terms=_sorted(terms),
        actors=_sorted(actors),
        actions=_sorted(actions),
        objects=_sorted(objects),
        facets=_sorted(facets),
        dependency_terms=_sorted(dependency_terms),
        evidence_ids=tuple(sorted(set(evidence_ids))),
        source_request_ids=tuple(sorted(set(request_ids))),
        source_decision_ids=tuple(sorted(set(decision_ids))),
        heuristic=True,
    )


def _related(left: CapabilitySignature, right: CapabilitySignature) -> bool:
    return bool(
        set(left.objects) & set(right.objects)
        or set(left.facets) & set(right.facets)
        or len(set(left.domain_terms) & set(right.domain_terms)) >= 2
    )


def calculate_cumulative_drift(
    current: CapabilitySignature,
    prior_approved_changes: Sequence[CapabilitySignature],
    *,
    thresholds: DriftThresholds | None = None,
) -> DriftAssessment:
    """Calculate drift from approved change history, never raw request history."""

    thresholds = thresholds or DriftThresholds()
    related = [change for change in prior_approved_changes if _related(current, change)]
    prior_facets = {facet for change in related for facet in change.facets}
    combined_facets = prior_facets | set(current.facets)
    prior_terms = {
        term
        for change in related
        for term in (*change.domain_terms, *change.objects, *change.dependency_terms)
    }
    distinct_current = bool(
        set(current.facets) - prior_facets
        or set(current.actions) - {action for change in related for action in change.actions}
        or set(current.objects) - {obj for change in related for obj in change.objects}
    )
    connected = bool(
        set(current.dependency_terms) & prior_terms
        or set(current.facets) & prior_facets & LIFECYCLE_FACETS
        or set(current.objects) & {obj for change in related for obj in change.objects}
    )
    total_increments = len(related) + 1
    subsystem = (
        len(related) >= thresholds.subsystem_prior_approved_changes
        and total_increments >= thresholds.subsystem_total_increments
        and len(combined_facets) >= thresholds.subsystem_minimum_facets
        and (connected or not thresholds.require_dependency_or_lifecycle_connection)
        and distinct_current
    )
    if subsystem:
        severity = DriftSeverity.SUBSYSTEM
    elif len(related) >= 1 and distinct_current:
        severity = DriftSeverity.EMERGING
    elif related:
        severity = DriftSeverity.RELATED
    else:
        severity = DriftSeverity.NONE

    related_requests = _sorted(
        request_id for change in related for request_id in change.source_request_ids
    )
    if severity == DriftSeverity.SUBSYSTEM:
        related_requests = _sorted((*related_requests, *current.source_request_ids))
    related_decisions = _sorted(
        decision_id for change in related for decision_id in change.source_decision_ids
    )
    pattern_source = "|".join(sorted(combined_facets | (set(current.objects) & prior_terms)))
    pattern_key = (
        hashlib.sha256(pattern_source.encode("utf-8")).hexdigest()[:16]
        if severity != DriftSeverity.NONE and pattern_source
        else None
    )
    return DriftAssessment(
        severity=severity,
        cumulative_drift_detected=severity == DriftSeverity.SUBSYSTEM,
        related_request_ids=related_requests,
        related_decision_ids=related_decisions,
        combined_facets=tuple(sorted(combined_facets)),
        approved_change_count=len(related),
        pattern_key=pattern_key,
        rationale=(
            "At least two related approved changes and the current connected increment form a multi-facet subsystem."
            if severity == DriftSeverity.SUBSYSTEM
            else "The approved change history does not meet the configurable subsystem threshold."
        ),
        heuristic=True,
    )
