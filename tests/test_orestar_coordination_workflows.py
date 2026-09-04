import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text()


def _step_block(text: str, name: str) -> str:
    marker = f"      - name: {name}"
    start = text.index(marker)
    end = text.find("\n      - name:", start + len(marker))
    return text[start:] if end < 0 else text[start:end]


def _concurrency_block(text: str) -> str:
    start = text.index("\nconcurrency:")
    end = text.index("\njobs:", start)
    return text[start:end]


@pytest.mark.parametrize(
    ("filename", "scrape_step", "required_dispatch_args"),
    [
        (
            "coverage-survey.yml",
            "Survey coverage",
            ("limit", "filer_ids", "recheck", "chain_index"),
        ),
        (
            "filer-metadata.yml",
            "Scrape filer metadata (party, office, type)",
            ("max_filers", "force"),
        ),
        ("candidate-filings.yml", "Scrape candidate filings", ()),
        (
            "verify-filers.yml",
            "Verify filer transactions",
            ("filer_id", "discrepancy_threshold", "max_filers", "force"),
        ),
    ],
)
def test_orestar_workflow_requeues_same_request_instead_of_overlapping(
    filename: str,
    scrape_step: str,
    required_dispatch_args: tuple[str, ...],
) -> None:
    text = _workflow(filename)
    wait = _step_block(text, "Wait for other ORESTAR jobs")
    requeue = _step_block(text, "Requeue after coordination timeout")
    refuse = _step_block(
        text,
        next(
            line.removeprefix("      - name: ")
            for line in text.splitlines()
            if line.startswith("      - name: Refuse uncoordinated")
        ),
    )

    assert "id: orestar_wait" in wait
    assert "continue-on-error: true" in wait
    assert 'fail-on-timeout: "true"' in wait
    assert "steps.orestar_wait.outcome == 'failure'" in requeue
    assert f"dispatch_retry.sh {filename}" in requeue
    assert "--ref" in requeue
    for arg in required_dispatch_args:
        assert f'-f {arg}="' in requeue

    assert "steps.orestar_wait.outcome == 'failure'" in refuse
    assert "exit 1" in refuse
    assert text.index("Wait for other ORESTAR jobs") < text.index(
        "Requeue after coordination timeout"
    ) < text.index("Refuse uncoordinated") < text.index(
        "Refresh branch after coordination wait"
    ) < text.index(scrape_step)

    refresh = _step_block(text, "Refresh branch after coordination wait")
    assert 'git fetch --depth=1 origin "$GITHUB_REF_NAME"' in refresh
    assert "git reset --hard FETCH_HEAD" in refresh


@pytest.mark.parametrize(
    "filename",
    ["coverage-survey.yml", "filer-metadata.yml", "candidate-filings.yml"],
)
def test_requeued_workflow_has_non_evicting_pending_slot(filename: str) -> None:
    concurrency = _concurrency_block(_workflow(filename))
    assert "${{ github.run_id }}" in concurrency
    assert "cancel-in-progress: false" in concurrency


@pytest.mark.parametrize(
    ("filename", "step_name"),
    [
        ("coverage-survey.yml", "Commit survey results"),
        ("coverage-survey.yml", "Re-trigger if the survey is unfinished"),
        ("filer-metadata.yml", "Re-aggregate with updated metadata"),
        ("filer-metadata.yml", "Commit and push updated data"),
        ("filer-metadata.yml", "Check if more filers remain and retrigger"),
        ("verify-filers.yml", "Upload verification reports"),
    ],
)
def test_always_cleanup_cannot_escape_failed_coordination(
    filename: str, step_name: str,
) -> None:
    block = _step_block(_workflow(filename), step_name)
    assert "always()" in block
    assert "steps.orestar_wait.outcome == 'success'" in block


def test_short_jobs_budget_for_wait_install_work_and_handoff() -> None:
    cases = {
        # coordination + browser install + collector + post-work/dispatch room
        "coverage-survey.yml": (30, 25, 70, 30),
        # coordination + browser install + short scrape/post-work/dispatch room
        "candidate-filings.yml": (25, 25, 0, 20),
    }
    for filename, components in cases.items():
        match = re.search(
            r"^    timeout-minutes: ([0-9]+)$", _workflow(filename), re.MULTILINE,
        )
        assert match is not None
        assert int(match.group(1)) >= sum(components)


def test_survey_successor_preserves_recheck_mode() -> None:
    requeue = _step_block(
        _workflow("coverage-survey.yml"),
        "Re-trigger if the survey is unfinished",
    )
    assert '-f recheck="${{ inputs.recheck }}"' in requeue


def test_survey_uses_retry_safe_publisher() -> None:
    commit = _step_block(_workflow("coverage-survey.yml"), "Commit survey results")
    assert "scripts/push_data.sh" in commit
    assert "data/coverage_survey.json" in commit
    assert "git pull --rebase" not in commit
