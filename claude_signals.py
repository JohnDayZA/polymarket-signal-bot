"""
Claude-powered signal estimator.

For each Polymarket market question, asks Claude to estimate the
probability of the YES outcome and return structured JSON.

Web search context (optional):
    Set BRAVE_API_KEY in .env to enable pre-call web search.
    Free tier: 2,000 queries/month at https://brave.com/search/api/
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import TypedDict

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", DEFAULT_MODEL)
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

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
{context_block}
Estimate the probability of the YES outcome resolving true.
"""


class SignalResult(TypedDict):
    probability: float
    confidence: str
    reasoning: str


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------

def _search_recent_context(question: str) -> str | None:
    """
    Search Brave for recent news about the market question.
    Returns a 2-3 sentence context string, or None if unavailable.
    Requires BRAVE_API_KEY in .env (free tier: 2,000 queries/month).
    """
    if not BRAVE_API_KEY:
        return None

    # Append current year to bias toward recent results
    year = datetime.now(timezone.utc).year
    query = f"{question} {year}"

    try:
        resp = requests.get(
            BRAVE_SEARCH_URL,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": BRAVE_API_KEY,
            },
            params={
                "q": query,
                "count": 3,
                "search_lang": "en",
                "freshness": "pm",      # past month
                "text_decorations": 0,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("Brave search failed for %r: %s", question[:60], exc)
        return None

    snippets = []
    for result in data.get("web", {}).get("results", [])[:3]:
        desc = (result.get("description") or result.get("extra_snippets", [""])[0] or "").strip()
        if desc and len(desc) > 20:
            snippets.append(desc)

    if not snippets:
        return None

    # Return up to 2 snippets joined — keeps context tight
    return " ".join(snippets[:2])


# ---------------------------------------------------------------------------
# Signal estimator
# ---------------------------------------------------------------------------

def estimate_signal(
    question: str,
    category: str,
    market_price: float | None,
) -> SignalResult | None:
    """
    Optionally search for recent context, then call the Claude API.
    Returns a SignalResult dict, or None on failure.
    """
    # 1. Web search for recent context
    context = _search_recent_context(question)
    if context:
        logger.debug("  [search] %s", context[:120])
        context_block = f"\nRecent context: {context}\n"
    else:
        context_block = ""

    # 2. Build prompts
    system_prompt = _build_system_prompt()
    price_str = f"{market_price:.3f}" if market_price is not None else "unknown"
    user_msg = USER_TEMPLATE.format(
        question=question,
        category=category,
        price_str=price_str,
        context_block=context_block,
    )

    # 3. Call Claude
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

    # 4. Parse and validate
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
