"""
Claude-powered signal estimator.

For each Polymarket market question, asks Claude to estimate the
probability of the YES outcome and return structured JSON.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import TypedDict

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", DEFAULT_MODEL)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set in environment")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _build_system_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    return f"""\
You are a prediction market analyst. Today's date is {today}.
When given a binary prediction market question, estimate the probability
that the YES outcome will resolve true.

You must respond with ONLY a valid JSON object — no prose before or after — matching
this exact schema:
{{
  "probability": <float between 0.0 and 1.0>,
  "confidence": "<low|medium|high>",
  "reasoning": "<one to three sentence explanation>"
}}

Rules:
- probability must be a float in [0.0, 1.0].
- confidence reflects how certain you are given available information:
    low    = high uncertainty or very limited information
    medium = moderate information, some uncertainty
    high   = strong evidence or near-certain outcome
- reasoning must be concise (1-3 sentences).
- Do not include any text outside the JSON object.

Calibration guidelines:
- Consider base rates carefully. For sports qualification markets, account for the
  specific format (group stage, playoffs, intercontinental). For political markets,
  weight recent polling, historical patterns, and incumbency effects.
- Penalise overconfidence — most uncertain events should sit between 0.20 and 0.80
  unless evidence is overwhelming.
- If recent context is provided below, weight it heavily — it may post-date your
  training data and is more reliable for current market conditions.
"""


USER_TEMPLATE = """\
Prediction market question: {question}

Category: {category}
Current market price (implied probability): {price_str}

Estimate the probability of the YES outcome resolving true.
"""


class SignalResult(TypedDict):
    probability: float
    confidence: str
    reasoning: str


# ---------------------------------------------------------------------------
# Signal estimator
# ---------------------------------------------------------------------------

def estimate_signal(
    question: str,
    category: str,
    market_price: float | None,
) -> SignalResult | None:
    """
    Call the Claude API to estimate the probability for a market question.
    Returns a SignalResult dict, or None on failure.
    """
    system_prompt = _build_system_prompt()
    price_str = f"{market_price:.3f}" if market_price is not None else "unknown"
    user_msg = USER_TEMPLATE.format(
        question=question,
        category=category,
        price_str=price_str,
    )

    # Call Claude
    try:
        client = _get_client()
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:
        logger.error("Claude API error for question %r: %s", question[:80], exc)
        return None

    raw = response.content[0].text.strip() if response.content else ""

    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    # Parse and validate
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("JSON parse error for question %r: %s | raw=%r", question[:80], exc, raw[:200])
        return None

    try:
        prob = float(parsed["probability"])
        prob = max(0.0, min(1.0, prob))
        confidence = str(parsed.get("confidence", "medium")).lower()
        if confidence not in ("low", "medium", "high"):
            confidence = "medium"
        reasoning = str(parsed.get("reasoning", ""))
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("Unexpected Claude response structure: %s | parsed=%r", exc, parsed)
        return None

    return SignalResult(probability=prob, confidence=confidence, reasoning=reasoning)
