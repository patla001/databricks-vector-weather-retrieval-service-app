"""
Is this weather good news, bad news, or neither?

The console shows a sentiment chip beside each search result. "Sentiment" here
means the *weather*, not the prose: a Tornado Warning is negative because a
tornado is bad, not because the text is angry. General-purpose sentiment models
are the wrong tool for exactly that reason - they score "Sunny, with a high near
99" as cheerful, when in Houston in August it is the single most dangerous
forecast in the corpus. Heat is the dominant hazard here (179 of 522 alerts are
heat-related), so getting that case wrong would mislabel the thing that matters
most.

Two paths, because the two source types carry different evidence:

  * alerts    NWS publishes a controlled vocabulary. `severity` is one of
              Extreme/Severe/Moderate/Minor/Unknown and `event` ends in a known
              noun (Warning, Watch, Advisory, Statement, Outlook). That is a
              classification already made by a meteorologist - deferring to it
              beats anything inferred from the prose.
  * forecasts free text, so a weighted lexicon plus the actual temperature.
              Weighted rather than first-match because forecasts routinely mix
              signals ("Isolated showers and thunderstorms after 11am. Mostly
              sunny, with a high near 91"), and hedged hazards count for less
              than flat ones.

Everything here is derived at read time from columns that already exist. No
schema change, no backfill, no per-search cost, and it applies to documents
harvested long before this module was written.
"""

from __future__ import annotations

import re

POSITIVE = "positive"
NEGATIVE = "negative"
NEUTRAL = "neutral"

VALID_SENTIMENTS = (POSITIVE, NEGATIVE, NEUTRAL)

# Alert event nouns that describe an active or expected hazard. NWS is strict
# about these suffixes, so matching the last word is reliable.
_HAZARD_NOUNS = ("warning", "watch", "advisory", "emergency")

# ...and the ones that are administrative or purely informational. A Special
# Weather Statement covers "here is what to expect", a Hydrologic Outlook is a
# long-range heads-up, and Test Message is not weather at all.
_INFORMATIONAL_NOUNS = ("statement", "outlook", "synopsis", "discussion", "message")

# Hazards that NWS sometimes publishes under an informational noun. A Rip
# Current Statement is a "Statement" but people drown in rip currents, and an
# Air Quality Alert uses a noun that is in neither list. Naming the hazard beats
# trusting the suffix, so these are checked before the noun is.
_HAZARD_SUBJECTS = (
    "rip current", "air quality", "surf", "flood", "heat", "cold", "freeze",
    "frost", "wind", "thunderstorm", "tornado", "hurricane", "tropical",
    "fire", "smoke", "snow", "ice", "winter", "fog", "dust", "avalanche",
    "tsunami", "flash", "hail",
)

# Forecast lexicon. Weights are relative, not calibrated probabilities: what
# matters is that a thunderstorm outranks sunshine and a hedge outranks neither.
_NEGATIVE_TERMS: tuple[tuple[str, int], ...] = (
    (r"\btornado", -6), (r"\bhurricane", -6), (r"\bblizzard", -5),
    (r"\bice storm", -5), (r"\bfreezing rain", -5), (r"\bdamaging", -4),
    (r"\bflood", -4), (r"\bsevere", -4), (r"\bhail", -3),
    (r"\bthunderstorm", -3), (r"\bheavy rain", -3), (r"\bsnow", -3),
    (r"\bwintry", -3), (r"\bsleet", -3), (r"\bsmoke", -3),
    (r"\bhaze", -2), (r"\bfog", -2), (r"\bgusts?\b", -2),
    (r"\bwindy", -2), (r"\bshowers", -2), (r"\brain\b", -1),
    (r"\bdrizzle", -1), (r"\bhumid", -2), (r"\bfrost", -2),
)

_POSITIVE_TERMS: tuple[tuple[str, int], ...] = (
    (r"\bmostly sunny", 3), (r"\bmostly clear", 3), (r"\bsunny", 3),
    (r"\bclear\b", 3), (r"\bpleasant", 4), (r"\bmild\b", 3),
    (r"\bpartly sunny", 1), (r"\bpartly cloudy", 1), (r"\bcalm\b", 1),
)

# Hedges that soften whatever hazard follows them. "A slight chance of showers"
# is not the same forecast as "Showers".
_HEDGE_RE = re.compile(
    r"\b(slight chance|chance|isolated|scattered|patchy|a few|possible)\b"
)

_HIGH_RE = re.compile(r"high (?:near|around|of)\s+(-?\d{1,3})")
_LOW_RE = re.compile(r"low (?:near|around|of)\s+(-?\d{1,3})")


def _temperature_score(text: str) -> int:
    """Comfort contribution of the forecast temperatures, in Fahrenheit.

    The thresholds follow the NWS heat/cold advisory bands rather than personal
    taste: heat becomes an advisory-level hazard in the mid-90s and cold below
    freezing, while the 60s and 70s are what the agency calls no impact.
    """
    score = 0

    high = _HIGH_RE.search(text)
    if high:
        value = int(high.group(1))
        if value >= 100:
            score -= 9
        elif value >= 95:
            score -= 6
        elif value >= 90:
            score -= 1
        elif 62 <= value <= 85:
            score += 2
        elif value <= 32:
            score -= 4
        elif value <= 45:
            score -= 1

    low = _LOW_RE.search(text)
    if low:
        value = int(low.group(1))
        if value <= 10:
            score -= 4
        elif value <= 32:
            score -= 2
        elif 50 <= value <= 70:
            score += 1

    return score


def score_forecast(text: str) -> int:
    """Net comfort score for forecast prose. Positive is nicer weather."""
    body = (text or "").lower()
    if not body:
        return 0

    score = _temperature_score(body)

    for pattern, weight in _NEGATIVE_TERMS:
        for match in re.finditer(pattern, body):
            # Halve a hazard that is hedged just before it, so "a slight chance
            # of showers" does not weigh the same as "showers".
            window = body[max(0, match.start() - 28):match.start()]
            score += int(weight / 2) if _HEDGE_RE.search(window) else weight

    for pattern, weight in _POSITIVE_TERMS:
        if re.search(pattern, body):
            score += weight

    return score


def classify(
    source_type: str | None,
    event: str | None = None,
    severity: str | None = None,
    text: str | None = None,
) -> str:
    """Sentiment for one document. Never raises; unknown input is neutral."""
    event_text = (event or "").strip().lower()
    severity_text = (severity or "").strip().lower()

    if (source_type or "").lower() == "alert":
        # Not weather at all - NWS pushes these through the live feed.
        if "test" in event_text:
            return NEUTRAL
        # A meteorologist already graded these two levels; take their word.
        if severity_text in ("extreme", "severe"):
            return NEGATIVE
        # Named hazard beats the suffix, so "Rip Current Statement" and "Air
        # Quality Alert" do not get filed as bulletins.
        if any(subject in event_text for subject in _HAZARD_SUBJECTS):
            return NEGATIVE
        # Genuinely informational: Special Weather Statement, Hydrologic
        # Outlook, Marine Weather Statement - context, not a call to act.
        if any(noun in event_text for noun in _INFORMATIONAL_NOUNS):
            return NEUTRAL
        if any(noun in event_text for noun in _HAZARD_NOUNS):
            return NEGATIVE
        if severity_text in ("moderate", "minor"):
            return NEGATIVE
        return NEUTRAL

    score = score_forecast(text or event or "")
    if score >= 3:
        return POSITIVE
    if score <= -3:
        return NEGATIVE
    return NEUTRAL


def annotate(row: dict, text_key: str = "narrative_text") -> dict:
    """Attach `sentiment` to a result row, in place, and return it."""
    row["sentiment"] = classify(
        row.get("source_type"),
        event=row.get("event"),
        severity=row.get("severity"),
        text=row.get(text_key) or row.get("chunk_text") or row.get("headline"),
    )
    return row
