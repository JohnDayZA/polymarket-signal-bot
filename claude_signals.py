"""
Claude-powered signal estimator.

For each Polymarket market question, asks Claude to estimate the
probability of the YES outcome and return structured JSON.
"""

import json
import logging
import os
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


SYSTEM_PROMPT = """\
You are a prediction market analyst. When given a binary prediction market question,
estimate the probability that the YES outcome will resolve true.

You must respond with ONLY a valid JSON object — no prose before or after — matching
this exact schema:
{
  "probability": <float between 0.0 and 1.0>,
  "confidence": "<low|medium|high>",
  "reasoning": "<one to three sentence explanation>"
}

Rules:
- probability must be a float in [0.0, 1.0].
- confidence reflects how certain you are given available information:
    low    = high uncertainty or very limited information
    medium = moderate information, some uncertainty
    high   = strong evidence or near-certain outcome
- reasoning must be concise (1-3 sentences).
- Do not include any text outside the JSON object.
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


def estimate_signal(
    question: str,
    category: str,
    market_price: float | None,
) -> SignalResult | None:
    """
    Call the Claude API and return a SignalResult dict, or None on failure.
    """
    price_str = f"{market_price:.3f}" if market_price is not None else "unknown"
    user_msg = USER_TEMPLATE.format(
        question=question,
        category=category,
        price_str=price_str,
    )

    try:
        client = _get_client()
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            system=SYSTEM_PROMPT,
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

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("JSON parse error for question %r: %s | raw=%r", question[:80], exc, raw[:200])
        return None

    # Validate and clamp
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
