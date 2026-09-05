from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any


class OpenRouterError(RuntimeError):
    pass


def complete(api_key: str, system: str, prompt: str) -> str:
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
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.loads(response.read())
        return data["choices"][0]["message"]["content"]
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, IndexError, json.JSONDecodeError) as error:
        raise OpenRouterError(f"OpenRouter free-model request failed: {error}") from error


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


def json_call(api_key: str, system: str, prompt: str, attempts: int = 3) -> dict[str, Any]:
    """Ask the free router for JSON, retrying because it intermittently routes the request to a
    model that ignores the JSON contract entirely (a safety classifier, for instance)."""
    failure = ""
    for _ in range(attempts):
        try:
            return json_response(complete(api_key, system, prompt))
        except OpenRouterError as error:
            failure = str(error)
    raise OpenRouterError(f"The free model returned no usable JSON in {attempts} attempts. Last failure: {failure}")
