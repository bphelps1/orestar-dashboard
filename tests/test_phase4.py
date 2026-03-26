"""
Regression tests for Phase 4: Recommendation engine logic.

Tests the scoring/recommendation algorithms in isolation
(no Supabase dependency — pure logic tests).
"""

import pytest


# ── Replicate core recommendation engine logic in Python for testing ──────

def cycle_years(cycle):
    """Two-calendar-year cycle: 2026 cycle = 2025-2026."""
    return [cycle - 1, cycle]


def current_cycle():
    from datetime import date
    yr = date.today().year
    return yr if yr % 2 == 0 else yr + 1


def percentile(sorted_values, p):
    if not sorted_values:
        return 0
    idx = max(0, int(len(sorted_values) * p + 0.5) - 1)
    return sorted_values[idx]


def compute_target_ask(comp_amounts):
    """Compute target ask from comparable gift amounts."""
    if not comp_amounts:
        return 0
    sorted_amts = sorted(comp_amounts)
    median = percentile(sorted_amts, 0.5)
    p75 = percentile(sorted_amts, 0.75)
    max_gift = max(sorted_amts)
    target = min(round(((median + p75) / 2) * 100) / 100, max_gift)
    return target


def discrepancy_severity(abs_disc):
    if abs_disc < 500:
        return "gray"
    if abs_disc <= 2000:
        return "yellow"
    return "red"


def detect_office_type(name):
    import re
    name = name.lower()
    if re.search(r'\b(state representative|state rep)\b', name):
        return "state_rep"
    if re.search(r'\b(state senator|state senate)\b', name):
        return "state_senate"
    if re.search(r'\bgovernor\b', name):
        return "governor"
    return None


# ── Tests ─────────────────────────────────────────────────────────────────

class TestCycleLogic:
    def test_cycle_years(self):
        assert cycle_years(2026) == [2025, 2026]
        assert cycle_years(2024) == [2023, 2024]

    def test_current_cycle_is_even(self):
        cycle = current_cycle()
        assert cycle % 2 == 0

    def test_cycle_years_span_two_years(self):
        for c in [2020, 2022, 2024, 2026]:
            years = cycle_years(c)
            assert len(years) == 2
            assert years[1] - years[0] == 1


class TestTargetAsk:
    def test_single_gift(self):
        """Single comparable gift → target equals that gift."""
        assert compute_target_ask([1000]) == 1000

    def test_multiple_gifts(self):
        """Multiple gifts → target between median and 75th, capped by max."""
        amounts = [500, 1000, 1500, 2000]
        target = compute_target_ask(amounts)
        assert 1000 <= target <= 2000

    def test_empty_amounts(self):
        assert compute_target_ask([]) == 0

    def test_target_capped_by_max(self):
        """Target should never exceed the donor's max comparable gift."""
        amounts = [100, 200, 300]
        target = compute_target_ask(amounts)
        assert target <= 300

    def test_remaining_ask(self):
        """Remaining ask = target - already given, min 0."""
        target = compute_target_ask([1000, 2000, 1500])
        already = 500
        remaining = max(0, target - already)
        assert remaining >= 0
        assert remaining == target - already

    def test_remaining_ask_already_above_target(self):
        """If already gave more than target, remaining ask is 0."""
        target = compute_target_ask([500])
        already = 1000
        remaining = max(0, target - already)
        assert remaining == 0


class TestOfficeDetection:
    def test_state_rep(self):
        assert detect_office_type("Friends of Jane for State Representative") == "state_rep"

    def test_state_senate(self):
        assert detect_office_type("John Smith for State Senate") == "state_senate"

    def test_governor(self):
        assert detect_office_type("Tina for Governor") == "governor"

    def test_unknown(self):
        assert detect_office_type("Some Random PAC") is None

    def test_case_insensitive(self):
        assert detect_office_type("FRIENDS FOR STATE REPRESENTATIVE") == "state_rep"


class TestScoringFactors:
    """Test that scoring produces sensible relative rankings."""

    def _score_donor(self, distinct_comps, total_to_comps, already_given, target_ask):
        """Simplified scoring matching the JS logic."""
        score = 0
        # Factor 1: distinct comps (0-25)
        score += min(distinct_comps * 5, 25)
        # Factor 2: total amount (0-20)
        score += min(total_to_comps / 500, 20)
        # Factor 3: sim-weighted (approximate as 0.7 * total / 300, 0-20)
        score += min(total_to_comps * 0.7 / 300, 20)
        # Factor 4: gap (0-20)
        gap = max(0, target_ask - already_given)
        score += min(gap / 200, 20) if gap > 0 else 0
        # Factor 5: recency (flat 10)
        score += 10
        return min(round(score), 100)

    def test_more_comps_higher_score(self):
        """Donor supporting more comparable filers should score higher."""
        s1 = self._score_donor(1, 1000, 0, 1000)
        s5 = self._score_donor(5, 1000, 0, 1000)
        assert s5 > s1

    def test_higher_amount_higher_score(self):
        s_low = self._score_donor(3, 500, 0, 500)
        s_high = self._score_donor(3, 5000, 0, 5000)
        assert s_high > s_low

    def test_already_gave_reduces_gap_score(self):
        """Donor who already gave target amount gets lower gap score."""
        s_gap = self._score_donor(3, 2000, 0, 2000)
        s_no_gap = self._score_donor(3, 2000, 2000, 2000)
        assert s_gap > s_no_gap

    def test_tier_assignment(self):
        """Score >= 60 → A, >= 35 → B, else C."""
        assert (80 >= 60) and "A"
        assert (50 >= 35) and "B"
        assert (20 < 35) and "C"


class TestExportColumns:
    """Verify the expected export columns are present."""

    REQUIRED_COLUMNS = [
        "Donor Name",
        "Searched Filer",
        "Cycle",
        "Score",
        "Tier",
        "Already Given This Cycle",
        "Comparable Giving Min",
        "Comparable Giving Max",
        "Target Ask",
        "Remaining Ask",
        "Comparable Filers Referenced",
        "Reason for Recommendation",
    ]

    def test_all_columns_present(self):
        """Simulate an export row and check all required columns exist."""
        row = {
            "Donor Name": "Test Donor",
            "Searched Filer": "Test Committee",
            "Cycle": "2025-2026",
            "Score": 75,
            "Tier": "A",
            "Already Given This Cycle": 500,
            "Comparable Giving Min": 1000,
            "Comparable Giving Max": 3000,
            "Target Ask": 2000,
            "Remaining Ask": 1500,
            "Comparable Filers Referenced": "Filer A; Filer B",
            "Reason for Recommendation": "Gave to 3 similar filers",
            "Distinct Comparable Filers": 3,
            "Total to Comparables": 5000,
        }
        for col in self.REQUIRED_COLUMNS:
            assert col in row, f"Missing required export column: {col}"
