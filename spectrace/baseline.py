"""Transparent direct-prompt baseline and offline-safe command-line interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spectrace.config import (
    ConfigurationError,
    LLMSettings,
    load_llm_settings,
    safe_settings_summary,
)
from spectrace.dataset import DECISION_HEADING_RE, ProjectPack, validate_project_pack
from spectrace.llm import (
    GoogleGenAIClient,
    LLMError,
    RetryExhaustedError,
    StructuredGenerationClient,
    generate_prediction_with_retry,
)
from spectrace.models import ModelPrediction
from spectrace.scoring import score_predictions


DEFAULT_PROJECT_PACK = Path("data/synthetic/demo_project")
DEFAULT_PROMPT_PATH = Path("prompts/baseline.md")
TEMPERATURE = 0.0
DEFAULT_MAX_OUTPUT_TOKENS = 2048
DEFAULT_MAX_ATTEMPTS = 3

ANSWER_KEY_MARKERS = (
    "ground_truth.json",
    "expected_classification",
    "valid_supporting_evidence_ids",
    "expected_reasoning_summary",
    "expected_human_action",
    "planted_conflict",
    "unsupported_claims_forbidden",
    "cumulative_pattern_id",
)


class BaselineError(RuntimeError):
    """Raised when prompt construction or a baseline run is invalid."""


@dataclass(frozen=True)
class RenderedPrompt:
    request_id: str
    text: str
    prompt_hash: str
    included_request_ids: tuple[str, ...]
    included_decision_ids: tuple[str, ...]


def prompt_hash(prompt_path: str | Path = DEFAULT_PROMPT_PATH) -> str:
    """Hash the exact fixed prompt instructions stored in the repository."""

    return hashlib.sha256(Path(prompt_path).read_bytes()).hexdigest()


def _decision_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    headings = list(DECISION_HEADING_RE.finditer(text))
    if not headings:
        raise BaselineError("decision history contains no decision sections")
    preamble = text[: headings[0].start()].rstrip()
    sections: list[tuple[str, str]] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        sections.append((heading.group(1), text[heading.start() : end].rstrip()))
    return preamble, sections


def _available_decision_text(
    decisions_text: str, available_evidence_ids: frozenset[str]
) -> tuple[str, tuple[str, ...]]:
    preamble, sections = _decision_sections(decisions_text)
    included = [
        (decision_id, section)
        for decision_id, section in sections
        if decision_id in available_evidence_ids
    ]
    return (
        preamble + "\n\n" + "\n\n".join(section for _, section in included),
        tuple(decision_id for decision_id, _ in included),
    )


def _request_payload(request: Any) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "date": request.date.isoformat(),
        "source": request.source,
        "message": request.message,
        "chronological_order": request.chronological_order,
    }


def render_baseline_prompt(
    project_pack_path: str | Path,
    request_id: str,
    *,
    prompt_path: str | Path = DEFAULT_PROMPT_PATH,
) -> RenderedPrompt:
    """Assemble one request context with no future evidence or answer key."""

    pack = validate_project_pack(project_pack_path)
    requests_by_id = {request.request_id: request for request in pack.requests}
    if request_id not in requests_by_id:
        raise BaselineError(f"unknown request ID: {request_id}")
    current = requests_by_id[request_id]
    history = [
        request
        for request in pack.requests
        if request.chronological_order <= current.chronological_order
    ]
    project_path = Path(project_pack_path)
    instructions = Path(prompt_path).read_text(encoding="utf-8").rstrip()
    sow_text = (project_path / "sow.md").read_text(encoding="utf-8").rstrip()
    decisions_text = (project_path / "decisions.md").read_text(encoding="utf-8")
    available_decisions, decision_ids = _available_decision_text(
        decisions_text, pack.available_evidence_ids_by_request[request_id]
    )
    request_json = json.dumps(
        [_request_payload(request) for request in history],
        indent=2,
        ensure_ascii=False,
    )
    current_json = json.dumps(_request_payload(current), indent=2, ensure_ascii=False)
    text = (
        f"{instructions}\n\n"
        "--- COMPLETE STATEMENT OF WORK ---\n"
        f"{sow_text}\n\n"
        "--- APPROVED DECISIONS AVAILABLE AT THIS REQUEST'S CUTOFF ---\n"
        f"{available_decisions}\n\n"
        "--- ORDERED CLIENT REQUESTS AVAILABLE THROUGH THE CURRENT REQUEST ---\n"
        f"{request_json}\n\n"
        "--- CURRENT REQUEST TO CLASSIFY ---\n"
        f"{current_json}\n"
    )
    lowered = text.lower()
    leaked = [marker for marker in ANSWER_KEY_MARKERS if marker.lower() in lowered]
    if leaked:
        raise BaselineError(f"answer-key marker present in assembled prompt: {leaked}")
    return RenderedPrompt(
        request_id=request_id,
        text=text,
        prompt_hash=prompt_hash(prompt_path),
        included_request_ids=tuple(request.request_id for request in history),
        included_decision_ids=decision_ids,
    )


def dataset_hash(project_pack_path: str | Path) -> str:
    """Hash all frozen benchmark inputs, including the pre-existing answer key."""

    digest = hashlib.sha256()
    path = Path(project_pack_path)
    for name in ("sow.md", "decisions.md", "requests.json", "ground_truth.json"):
        data = (path / name).read_bytes()
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _git_commit() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def run_baseline(
    project_pack_path: str | Path,
    request_ids: list[str],
    *,
    client: StructuredGenerationClient,
    settings: LLMSettings,
    seed: int | None,
    max_output_tokens: int,
    max_attempts: int,
    results_root: str | Path = "results",
) -> Path:
    """Run independent calls and preserve raw responses, errors, and metadata."""

    pack = validate_project_pack(project_pack_path)
    known_ids = {request.request_id for request in pack.requests}
    unknown = sorted(set(request_ids) - known_ids)
    if unknown:
        raise BaselineError(f"unknown request IDs: {unknown}")
    ordered_ids = [
        request.request_id for request in pack.requests if request.request_id in request_ids
    ]
    if len(ordered_ids) != len(request_ids):
        raise BaselineError("request IDs must be unique")

    started_at = datetime.now(UTC)
    started_clock = time.perf_counter()
    run_directory = Path(results_root) / _run_id()
    run_directory.mkdir(parents=True, exist_ok=False)
    predictions: list[ModelPrediction] = []
    raw_records: list[dict[str, Any]] = []
    error_records: list[dict[str, Any]] = []
    total_usage: dict[str, int] = {}
    assembled_prompt_hashes: dict[str, str] = {}

    for request_id in ordered_ids:
        rendered = render_baseline_prompt(project_pack_path, request_id)
        assembled_hash = hashlib.sha256(rendered.text.encode("utf-8")).hexdigest()
        assembled_prompt_hashes[request_id] = assembled_hash
        try:
            result = generate_prediction_with_retry(
                client,
                rendered.text,
                max_attempts=max_attempts,
                expected_request_id=request_id,
            )
            predictions.append(result.prediction)
            raw_records.append(
                {
                    "request_id": request_id,
                    "assembled_prompt_hash": assembled_hash,
                    "attempt_count": result.attempt_count,
                    "raw_response": result.raw_response,
                    "raw_responses": list(result.raw_responses),
                    "attempt_errors": list(result.attempt_errors),
                    "usage": result.usage,
                }
            )
            if result.usage:
                for key, value in result.usage.items():
                    if isinstance(value, int):
                        total_usage[key] = total_usage.get(key, 0) + value
        except RetryExhaustedError as exc:
            error_records.append(
                {
                    "request_id": request_id,
                    "assembled_prompt_hash": assembled_hash,
                    "error": str(exc),
                    "attempt_errors": exc.errors,
                    "raw_responses": exc.raw_responses,
                }
            )
        except LLMError as exc:
            error_records.append(
                {
                    "request_id": request_id,
                    "assembled_prompt_hash": assembled_hash,
                    "error": str(exc),
                    "attempt_errors": [str(exc)],
                    "raw_responses": [],
                }
            )

    raw_path = run_directory / "raw_predictions.jsonl"
    raw_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in raw_records),
        encoding="utf-8",
    )
    _write_json(
        run_directory / "predictions.json",
        [prediction.model_dump(mode="json") for prediction in predictions],
    )
    if error_records:
        (run_directory / "errors.jsonl").write_text(
            "".join(
                json.dumps(record, ensure_ascii=False) + "\n"
                for record in error_records
            ),
            encoding="utf-8",
        )

    if predictions:
        truth_by_id = {record.request_id: record for record in pack.ground_truth}
        scores = score_predictions(
            predictions,
            [truth_by_id[prediction.request_id] for prediction in predictions],
            pack.evidence_ids,
            pack.available_evidence_ids_by_request,
        )
        score_payload: Any = scores.model_dump(mode="json")
    else:
        score_payload = {"status": "unavailable", "reason": "no valid predictions"}
    _write_json(run_directory / "scores.json", score_payload)

    runtime = time.perf_counter() - started_clock
    manifest = {
        "timestamp": started_at.isoformat(),
        "git_commit": _git_commit(),
        "dataset_identity": Path(project_pack_path).as_posix(),
        "dataset_hash": dataset_hash(project_pack_path),
        "prompt_hash": prompt_hash(),
        "assembled_prompt_hashes": assembled_prompt_hashes,
        "provider": settings.provider,
        "model": settings.model,
        "temperature": TEMPERATURE,
        "seed": seed,
        "max_output_tokens": max_output_tokens,
        "request_count": len(ordered_ids),
        "successful_request_count": len(predictions),
        "failed_request_count": len(error_records),
        "runtime_seconds": runtime,
        "token_usage": total_usage or None,
        "approximate_api_cost": None,
    }
    _write_json(run_directory / "manifest.json", manifest)
    return run_directory


def _dry_summary(rendered: RenderedPrompt) -> dict[str, Any]:
    return {
        "request_id": rendered.request_id,
        "prompt_hash": rendered.prompt_hash,
        "assembled_prompt_hash": hashlib.sha256(
            rendered.text.encode("utf-8")
        ).hexdigest(),
        "included_request_ids": list(rendered.included_request_ids),
        "included_decision_ids": list(rendered.included_decision_ids),
        "answer_key_markers_absent": all(
            marker.lower() not in rendered.text.lower() for marker in ANSWER_KEY_MARKERS
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SpecTrace direct-prompt baseline")
    parser.add_argument("--project-pack", type=Path, default=DEFAULT_PROJECT_PACK)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-config", help="validate settings without exposing secrets")

    dry = subparsers.add_parser("dry-run", help="render a prompt without any API call")
    dry.add_argument("--request-id", required=True)
    dry.add_argument("--summary", action="store_true")

    run = subparsers.add_parser("run", help="run exactly one request")
    run.add_argument("--request-id", required=True)
    run.add_argument("--confirm-api-call", action="store_true", required=True)

    run_all = subparsers.add_parser("run-all", help="run every request independently")
    run_all.add_argument("--confirm-api-call", action="store_true", required=True)
    for command in (run, run_all):
        command.add_argument("--seed", type=int)
        command.add_argument(
            "--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS
        )
        command.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
        command.add_argument("--results-root", type=Path, default=Path("results"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-config":
            settings = load_llm_settings()
            print(json.dumps(safe_settings_summary(settings), sort_keys=True))
            return 0
        if args.command == "dry-run":
            rendered = render_baseline_prompt(args.project_pack, args.request_id)
            print(
                json.dumps(_dry_summary(rendered), indent=2)
                if args.summary
                else rendered.text
            )
            return 0

        settings = load_llm_settings()
        pack = validate_project_pack(args.project_pack)
        request_ids = (
            [args.request_id]
            if args.command == "run"
            else [request.request_id for request in pack.requests]
        )
        client = GoogleGenAIClient(
            settings,
            temperature=TEMPERATURE,
            seed=args.seed,
            max_output_tokens=args.max_output_tokens,
        )
        run_directory = run_baseline(
            args.project_pack,
            request_ids,
            client=client,
            settings=settings,
            seed=args.seed,
            max_output_tokens=args.max_output_tokens,
            max_attempts=args.max_attempts,
            results_root=args.results_root,
        )
        print(f"Baseline run preserved at {run_directory}")
        return 0
    except (BaselineError, ConfigurationError, ValueError) as exc:
        print(f"Baseline error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
