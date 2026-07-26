# How the Recommend tab works

A reference for what the recommendation engine does, what every weight is, and
where each number comes from. Source: [`docs/recommend.js`](recommend.js).

The engine answers one question: **given a committee, which donors should it
ask, and for how much?** It never invents donors — every suggestion is someone
who already gave to a *comparable* committee.

---

## The pipeline

```
1. Load the target committee's profile          (filer_detail)
2. Find comparable committees                   → similarity score, top 50
3. Pull every donor who gave to those           (their filer_detail records)
4. Score each donor 0–100                       → ranked list
5. Compute a target ask per donor               → scaled by seat competitiveness
```

Two lists come out: **Donor Targets** (people who already gave to this
committee and could give more) and **New Donor Prospects** (people who gave to
comparables but not to this committee).

---

## Step 2 — Comparable committees

Every other committee is scored for similarity. Anything scoring **≤ 20 is
discarded**; the top **50** survive.

| Signal | Weight |
|---|---|
| Same office | **+40** |
| Related office (State Rep ↔ State Senate, legislative → statewide) | **+30** |
| Same party | **+15** |
| Similar fundraising size | **+0 … 15** (ratio of the smaller total to the larger, × 15) |
| Both in leadership | **+25** |
| …and same leadership tier | **+10** (adjacent tier: +5) |
| Target is *not* leadership but the comparable is | **−15** |
| Same seat competitiveness band | **+12** |
| Opposite competitiveness bands (or either unopposed) | **−8** |
| Tagged `prolific` in admin (non-leadership target) | **−10** |
| Tagged `exclude` in admin | **removed entirely** |

**Hard filter:** if the target has a known party, committees of a *different*
party are dropped before scoring. Committees with no party (PACs) stay
eligible.

**Leadership tiers** come from `data/leadership_roles.json`, refreshed weekly:
tier 1 Speaker / Senate President · tier 2 Majority Leaders, Ways & Means
co-chairs · tier 3 other leadership.

---

## Step 4 — Donor score (0–100)

Each donor starts at 0, accumulates the factors below, and is clamped to
0–100.

| # | Factor | Range | Rule |
|---|---|---|---|
| 1 | **Breadth** — distinct comparable committees supported | 3 … **35** | `7 × committees`, capped at 35. Only one committee scores just **3** — this is the heaviest single signal. |
| 2 | **Total given** to comparables | 0 … **15** | `total ÷ 500`, capped |
| 3 | **Similarity-weighted giving** | 0 … **15** | gifts weighted by how comparable the recipient was, `÷ 300`, capped |
| 4 | **Headroom** — gap between the target ask and what they've already given here | 0 … **15** | `gap ÷ 200`, capped; 0 if no gap |
| 5 | **Recency** | **−10 … +20** | gave within 1 year **+20**; within 3 **+10**; more than 5 years ago **−10** |
| 6 | **Leadership donor** — gave to ≥ 2 leadership members | 0 … **20** | `5 × members`, capped |
| 7 | **One-time donor** (1 committee, 1 year) | **−5** | |
| 8 | **Single-cycle donor** — all giving inside one election cycle | **−25** | the largest penalty: a donor who appeared once is weak evidence of habit |

Prospects whose computed ask lands **below $1,000** are dropped from the list.

---

## Step 5 — The target ask

Start from what this donor gave to comparable committees:

```
base = midpoint(median, 75th percentile)   ← of their gifts to comparables
base = min(base, largest single gift)      ← never suggest more than they've ever given
ask  = base × competitiveness multiplier
```

### Competitiveness multiplier

Closer races draw larger gifts, so a safe-seat donor history understates a
swing seat — and vice versa. The multiplier is **bounded**, so it tilts the
number rather than inventing one.

| Seat (last general, current district map) | Multiplier |
|---|---|
| **Competitive** — under 10-point margin | **× 1.25** |
| **Lean** — 10–20 points | **× 1.05** |
| **Safe** — over 20 points | **× 0.85** |
| **Unopposed** | **× 0.75** |
| No margin on record | × 1.00 |

Worked example — the two extremes of 2024, on a $1,000 base:

| Seat | 2024 margin | Ask |
|---|---|---|
| House District 22 | 0.77 pts | **$1,250** |
| House District 43 | 84.67 pts | **$850** |

Whenever the multiplier moves the number, the reason appears in the donor's
"why" list — e.g. *"Ask raised 25% — seat is competitive (<10 pt margin)
(2024)"* — so a scaled figure is never unexplained.

### Where margins come from

The `race_margins` view, built from official Secretary of State results
(`election_results`, 2008–2026).

- **General elections only.** Primary margins are far more variable —
  unopposed incumbents, multi-way fields — and say little about how contested
  a seat actually is.
- **Current district era only.** Oregon redraws maps two years after each
  census (2012, 2022), so a pre-2022 margin describes a different electorate
  under the same district number. The engine reads only the `2022–` era.
- **Unopposed races are kept**, flagged rather than dropped — that is the
  extreme of "safe", not missing data.

---

## What it deliberately does not do

- **No cross-party suggestions** when the target's party is known.
- **No donor invented from nothing** — every suggestion has a giving history
  with a comparable committee.
- **No ask above a donor's largest observed gift**, even after scaling.
- **No primary-margin influence**, by design.

## Tuning it

| Change | Where |
|---|---|
| Similarity weights, the ≤ 20 cutoff, top-50 | `findComparables()` |
| Score factors 1–8 | `buildRepeatDonorTargets()` / `scoreDonors()` |
| Competitiveness bands and multipliers | `MARGIN_BANDS` / `UNOPPOSED` |
| $1,000 prospect floor | end of `scoreDonors()` |
| Exclude or flag a committee | `/admin` tags (`exclude`, `prolific`) |

All weights are plain constants — there is no trained model and no hidden
state, so a change here is fully predictable in the output.
