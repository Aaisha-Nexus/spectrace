"""Deterministic lexical and metadata retrieval over a :class:`ScopeAnchor`.

Runtime retrieval reads only the anchor, request text, explicit evidence
cutoff, and retrieval limits. Frozen-answer evaluation lives outside this
production module.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

from spectrace.advanced_models import (
    EvidenceCategory,
    EvidenceItem,
    RetrievalBundle,
    RetrievalLimits,
    RetrievedEvidence,
    ScopeAnchor,
    TemporalStatus,
)
from spectrace.scope_anchor import (
    FACET_KEYWORDS,
    ScopeAnchorError,
    build_scope_anchor,
    normalize_text,
    resolve_anchor_at_cutoff,
)


# Deliberately small and inspectable. Domain-bearing words are not stemmed away.
STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
        "can", "could", "do", "does", "for", "from", "have", "if", "in",
        "is", "it", "its", "may", "of", "on", "or", "please", "so", "that",
        "the", "their", "them", "this", "to", "we", "when", "where", "with",
        "would", "you",
    }
)


def retrieval_tokens(text: str) -> tuple[str, ...]:
    def canonical(token: str) -> str:
        if token.endswith("ies") and len(token) > 4:
            return token[:-3] + "y"
        if (
            token.endswith("s")
            and len(token) > 4
            and not token.endswith(("ss", "us", "is"))
        ):
            return token[:-1]
        return token

    return tuple(
        canonical(token)
        for token in normalize_text(text).split()
        if token not in STOPWORDS
    )


def _bigrams(tokens: tuple[str, ...]) -> set[str]:
    return {f"{first} {second}" for first, second in zip(tokens, tokens[1:])}


def _query_metadata(
    query_text: str, anchor: ScopeAnchor
) -> tuple[set[str], set[str]]:
    normalized = normalize_text(query_text)
    actors = {
        actor
        for item in anchor.items
        for actor in item.actor_terms
        if normalize_text(actor) in normalized
    }
    tokens = set(retrieval_tokens(query_text))
    facets = {
        facet for facet, keywords in FACET_KEYWORDS.items() if tokens & keywords
    }
    return actors, facets


def _idf(items: tuple[EvidenceItem, ...]) -> dict[str, float]:
    document_frequency: Counter[str] = Counter()
    for item in items:
        document_frequency.update(set(retrieval_tokens(item.source_text)))
    count = len(items)
    return {
        token: math.log((count + 1) / (frequency + 1)) + 1.0
        for token, frequency in document_frequency.items()
    }


def _score_item(
    item: EvidenceItem,
    query_tokens: tuple[str, ...],
    query_bigrams: set[str],
    query_actors: set[str],
    query_facets: set[str],
    idf: dict[str, float],
) -> tuple[float, dict[str, float]]:
    item_tokens = retrieval_tokens(item.source_text)
    token_overlap = set(query_tokens) & set(item_tokens)
    unigram = sum(idf.get(token, 1.0) for token in token_overlap)
    bigram = 1.5 * len(query_bigrams & _bigrams(item_tokens))
    actor = 2.0 * len(query_actors & set(item.actor_terms))
    object_overlap = 0.25 * len(set(query_tokens) & set(item.object_terms))
    facet = 1.25 * len(query_facets & set(item.facet_terms))
    temporal = 0.0
    lexical_metadata_score = unigram + bigram + actor + object_overlap + facet
    if item.category == EvidenceCategory.DECISION and lexical_metadata_score > 0:
        temporal = 1.0 if item.temporal_status == TemporalStatus.CURRENT else 0.35
    components = {
        "unigram_idf": unigram,
        "bigram_overlap": bigram,
        "actor_metadata": actor,
        "object_metadata": object_overlap,
        "facet_metadata": facet,
        "effective_decision_priority": temporal,
    }
    return sum(components.values()), components


def _balanced_selection(
    ranked: list[tuple[EvidenceItem, float, dict[str, float]]],
    limits: RetrievalLimits,
) -> tuple[list[tuple[EvidenceItem, float, dict[str, float]]], bool]:
    by_category: dict[EvidenceCategory, list[tuple[EvidenceItem, float, dict[str, float]]]] = defaultdict(list)
    for row in ranked:
        by_category[row[0].category].append(row)

    selected: list[tuple[EvidenceItem, float, dict[str, float]]] = []
    selected_ids: set[str] = set()
    for category in EvidenceCategory:
        quota = limits.category_quotas.get(category, 0)
        for row in [candidate for candidate in by_category[category] if candidate[1] > 0][:quota]:
            if len(selected) >= limits.max_total:
                break
            selected.append(row)
            selected_ids.add(row[0].evidence_id)

    for row in ranked:
        if len(selected) >= limits.max_total:
            break
        if row[1] > 0 and row[0].evidence_id not in selected_ids:
            selected.append(row)
            selected_ids.add(row[0].evidence_id)

    coverage = {row[0].category for row in selected}
    expanded = len(coverage) < limits.minimum_category_coverage
    if expanded:
        for category in EvidenceCategory:
            if category in coverage or not by_category[category]:
                continue
            row = by_category[category][0]
            if row[0].evidence_id not in selected_ids:
                selected.append(row)
                selected_ids.add(row[0].evidence_id)
                coverage.add(category)
            if len(coverage) >= limits.minimum_category_coverage:
                break
        for row in ranked:
            if len(selected) >= limits.expanded_max_total:
                break
            if row[1] > 0 and row[0].evidence_id not in selected_ids:
                selected.append(row)
                selected_ids.add(row[0].evidence_id)

    selected.sort(key=lambda row: (-row[1], row[0].category.value, row[0].evidence_id))
    limit = limits.expanded_max_total if expanded else limits.max_total
    return selected[:limit], expanded


def retrieve_evidence(
    anchor: ScopeAnchor,
    project_pack_path: str | Path,
    query_text: str,
    evidence_cutoff: str,
    limits: RetrievalLimits | None = None,
) -> RetrievalBundle:
    """Retrieve evidence deterministically with strict cutoff filtering."""

    limits = limits or RetrievalLimits()
    resolved = resolve_anchor_at_cutoff(anchor, project_pack_path, evidence_cutoff)
    available = tuple(
        item for item in resolved if item.temporal_status != TemporalStatus.FUTURE
    )
    query_tokens = retrieval_tokens(query_text)
    query_bigrams = _bigrams(query_tokens)
    query_actors, query_facets = _query_metadata(query_text, anchor)
    idf = _idf(available)
    ranked = []
    for item in available:
        score, components = _score_item(
            item,
            query_tokens,
            query_bigrams,
            query_actors,
            query_facets,
            idf,
        )
        ranked.append((item, score, components))
    ranked.sort(key=lambda row: (-row[1], row[0].category.value, row[0].evidence_id))
    selected, expanded = _balanced_selection(ranked, limits)
    retrieved = tuple(
        RetrievedEvidence(
            evidence=item,
            score=score,
            score_components=components,
            rank=rank,
        )
        for rank, (item, score, components) in enumerate(selected, start=1)
    )
    return RetrievalBundle(
        query_text=query_text,
        evidence_cutoff=evidence_cutoff,
        anchor_hash=anchor.anchor_hash,
        items=retrieved,
        category_coverage=tuple(sorted({item.evidence.category for item in retrieved}, key=lambda category: category.value)),
        expanded=expanded,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SpecTrace deterministic retrieval")
    parser.add_argument("project_pack", type=Path)
    parser.add_argument("--request-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        requests = json.loads((args.project_pack / "requests.json").read_text(encoding="utf-8"))
        current = next((item for item in requests if item["request_id"] == args.request_id), None)
        if current is None:
            raise ValueError(f"unknown request ID: {args.request_id}")
        anchor = build_scope_anchor(args.project_pack)
        bundle = retrieve_evidence(
            anchor,
            args.project_pack,
            current["message"],
            current["evidence_available_through"],
        )
        print(json.dumps(bundle.model_dump(mode="json"), indent=2, sort_keys=True))
        return 0
    except (ScopeAnchorError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Retrieval error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
