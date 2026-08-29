from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from spectrace.dataset import DatasetValidationError, validate_project_pack


DEMO_PACK = Path(__file__).parents[1] / "data" / "synthetic" / "demo_project"


@pytest.fixture
def copied_pack(tmp_path: Path) -> Path:
    destination = tmp_path / "demo_project"
    shutil.copytree(DEMO_PACK, destination)
    return destination


def _read(path: Path, name: str) -> list[dict[str, object]]:
    return json.loads((path / name).read_text(encoding="utf-8"))


def _write(path: Path, name: str, value: list[dict[str, object]]) -> None:
    (path / name).write_text(json.dumps(value, indent=2), encoding="utf-8")


def test_valid_frozen_pack_passes() -> None:
    pack = validate_project_pack(DEMO_PACK)
    assert len(pack.requests) == 10
    assert "DEC-006" in pack.evidence_ids


def test_project_pack_accepts_posix_style_path_string() -> None:
    pack = validate_project_pack(DEMO_PACK.as_posix())
    assert pack.path == DEMO_PACK


def test_duplicate_request_ids_fail(copied_pack: Path) -> None:
    requests = _read(copied_pack, "requests.json")
    requests[1]["request_id"] = requests[0]["request_id"]
    _write(copied_pack, "requests.json", requests)
    with pytest.raises(DatasetValidationError, match="duplicate request IDs"):
        validate_project_pack(copied_pack)


def test_missing_ground_truth_record_fails(copied_pack: Path) -> None:
    truth = _read(copied_pack, "ground_truth.json")
    truth.pop()
    _write(copied_pack, "ground_truth.json", truth)
    with pytest.raises(DatasetValidationError, match="ID sets differ"):
        validate_project_pack(copied_pack)


def test_unknown_evidence_id_fails(copied_pack: Path) -> None:
    truth = _read(copied_pack, "ground_truth.json")
    truth[0]["valid_supporting_evidence_ids"] = ["SOW-SCP-999"]
    _write(copied_pack, "ground_truth.json", truth)
    with pytest.raises(DatasetValidationError, match="unknown evidence IDs"):
        validate_project_pack(copied_pack)


def test_future_decision_evidence_fails(copied_pack: Path) -> None:
    truth = _read(copied_pack, "ground_truth.json")
    truth[0]["valid_supporting_evidence_ids"] = ["DEC-006"]
    _write(copied_pack, "ground_truth.json", truth)
    with pytest.raises(DatasetValidationError, match="future decision evidence"):
        validate_project_pack(copied_pack)


def test_answer_key_leakage_in_requests_fails(copied_pack: Path) -> None:
    requests = _read(copied_pack, "requests.json")
    requests[0]["expected_classification"] = "IN_SCOPE"
    _write(copied_pack, "requests.json", requests)
    with pytest.raises(DatasetValidationError, match="answer-key fields"):
        validate_project_pack(copied_pack)


def test_invalid_cumulative_fields_fail(copied_pack: Path) -> None:
    truth = _read(copied_pack, "ground_truth.json")
    truth[0]["cumulative_pattern_id"] = "CUM-INVALID-001"
    _write(copied_pack, "ground_truth.json", truth)
    with pytest.raises(DatasetValidationError, match="cumulative fields"):
        validate_project_pack(copied_pack)


def test_cli_failure_returns_nonzero_status(copied_pack: Path) -> None:
    requests = _read(copied_pack, "requests.json")
    requests[0]["expected_classification"] = "IN_SCOPE"
    _write(copied_pack, "requests.json", requests)
    completed = subprocess.run(
        [sys.executable, "-m", "spectrace.dataset", str(copied_pack)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode != 0
    assert "Validation failed:" in completed.stderr
