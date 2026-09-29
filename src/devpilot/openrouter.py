from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any

SOCKET_TIMEOUT = 90
DEADLINE = 120
BUDGET = 300


class OpenRouterError(RuntimeError):
    pass


def post(request: urllib.request.Request, deadline: int) -> dict:
    """Perform the request under a wall-clock deadline.

    A socket timeout only fires when a read stalls completely. The free router can instead
    trickle bytes indefinitely, which keeps the socket alive and hangs the run with no upper
    bound — observed in practice. The request therefore runs on a daemon thread that is
    abandoned once the deadline passes, so a wedged connection cannot outlive the call or
    block interpreter exit.
    """
    outcome: dict[str, Any] = {}

    def attempt() -> None:
        try:
            with urllib.request.urlopen(request, timeout=SOCKET_TIMEOUT) as response:
                outcome["data"] = json.loads(response.read())
        except Exception as error:  # re-raised on the calling thread below
            outcome["error"] = error

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        raise OpenRouterError(f"OpenRouter did not respond within {deadline}s; abandoning the request.")
    if "error" in outcome:
        raise OpenRouterError(f"OpenRouter free-model request failed: {outcome['error']}")
    return outcome["data"]


def complete(api_key: str, system: str, prompt: str, deadline: int = DEADLINE) -> str:
    """Call only OpenRouter's explicitly zero-cost free router."""
    if not api_key:
        raise OpenRouterError("OPENROUTER_API_KEY is missing. Add it to ~/.dev-pilot/.env.")
    payload = json.dumps({
        "model": "openrouter/free", "temperature": 0.1,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }).encode()
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=payload, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "DevPilot"},
    )
    data = post(request, deadline)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise OpenRouterError(f"OpenRouter returned an unexpected response shape: {error}") from error


def json_response(content: str) -> dict[str, Any]:
    """Parse a model JSON response, accepting a Markdown fence or surrounding prose."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.MULTILINE).strip()
    for candidate in (cleaned, cleaned[cleaned.find("{"):cleaned.rfind("}") + 1]):
        try:
            result = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(result, dict):
            return result
    raise OpenRouterError(f"The free model returned no usable JSON object (got {content[:80]!r}).")


def json_call(api_key: str, system: str, prompt: str, attempts: int = 3, budget: int = BUDGET) -> dict[str, Any]:
    """Ask the free router for JSON, retrying because it intermittently routes the request to a
    model that ignores the JSON contract entirely (a safety classifier, for instance).

    Retries share one wall-clock budget so a run cannot spend attempts x deadline stalled.
    """
    started, failure = time.monotonic(), ""
    for _ in range(attempts):
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            break
        try:
            return json_response(complete(api_key, system, prompt, deadline=int(min(DEADLINE, remaining))))
        except OpenRouterError as error:
            failure = str(error)
    raise OpenRouterError(f"The free model returned no usable JSON within {budget}s. Last failure: {failure}")
