from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

SOCKET_TIMEOUT = 120
# A healthy zero-cost endpoint answers an anchored edit in well under a minute; one still
# silent after two is wedged, not thinking. Waiting longer buys nothing, so the deadline is
# short and the attempts are many: six quick draws across the model list beat three slow ones
# when the failure being retried is an endpoint that never answers.
DEADLINE = 120
ATTEMPTS = 6
BUDGET = 780

# Every entry is zero-cost, so DevPilot still cannot select a paid model. They are tried in
# order and a retry deliberately moves to the next one: the failures worth retrying are a model
# answering in prose, in tool-call syntax, or with a null message, and repeating the same model
# repeats the same convention. The openrouter/free router is last because it picks a different
# model on each call, which is the behaviour these named entries exist to avoid.
FREE_MODELS = (
    "nvidia/nemotron-3-super-120b-a12b:free",
    "inclusionai/ling-3.0-flash-sante:free",
    "openrouter/free",
)


def models() -> tuple[str, ...]:
    """Return the models to try, in order.

    Zero-cost endpoints are throttled and frequently slow, which is survivable for a run you
    are watching and not for one you are demonstrating. DEVPILOT_MODELS is an explicit,
    deliberate opt-out: nothing is spent unless the operator names paid models themselves,
    so the default still cannot reach a paid model by accident.
    """
    chosen = os.environ.get("DEVPILOT_MODELS", "").strip()
    return tuple(m.strip() for m in chosen.split(",") if m.strip()) or FREE_MODELS


# Called about once a second with the seconds elapsed while a request is in flight, then with
# None when it settles, so an interface can show a live wait instead of a frozen screen.
Tick = Callable[[float | None], None]


class OpenRouterError(RuntimeError):
    pass


class RateLimited(OpenRouterError):
    """The account is out of free-tier quota. Retrying only consumes more of it."""


def post(request: urllib.request.Request, deadline: int, tick: Tick | None = None) -> dict:
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
    began = time.monotonic()
    while worker.is_alive():
        elapsed = time.monotonic() - began
        if elapsed >= deadline:
            raise OpenRouterError(f"OpenRouter did not respond within {deadline}s; abandoning the request.")
        worker.join(min(1.0, deadline - elapsed))
        if tick is not None and worker.is_alive():
            tick(time.monotonic() - began)
    if "error" in outcome:
        failure = outcome["error"]
        if getattr(failure, "code", None) == 429:
            raise RateLimited(
                "OpenRouter rejected the request: the free-model daily quota is exhausted. "
                "It resets at 00:00 UTC, or adding credits raises the free-model limit."
            )
        raise OpenRouterError(f"OpenRouter free-model request failed: {failure}")
    return outcome["data"]


def complete(
    api_key: str, system: str, prompt: str, deadline: int = DEADLINE, model: str = "", tick: Tick | None = None
) -> str:
    """Call only an explicitly zero-cost model."""
    if not api_key:
        raise OpenRouterError("OPENROUTER_API_KEY is missing. Add it to ~/.dev-pilot/.env.")
    payload = json.dumps({
        "model": model or models()[0], "temperature": 0.1,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }).encode()
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=payload, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "DevPilot"},
    )
    data = post(request, deadline, tick=tick)
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise OpenRouterError(f"OpenRouter returned an unexpected response shape: {error}") from error
    # Reasoning models leave "content" null and put their output under "reasoning"; an
    # overloaded one returns both empty. Raising here keeps every such reply inside the retry
    # loop rather than letting a null propagate into the caller as an AttributeError.
    content = message.get("content") or message.get("reasoning") or ""
    if not isinstance(content, str) or not content.strip():
        raise OpenRouterError("OpenRouter returned an empty message.")
    return content


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


def brief(error: Exception) -> str:
    """Reduce a failure to the few words worth showing beside a progress line."""
    text = str(error)
    if "did not respond" in text:
        return "no response"
    if "empty message" in text:
        return "empty reply"
    if "no usable JSON" in text:
        return "reply was not JSON"
    return text.removeprefix("OpenRouter free-model request failed: ")[:70]


def json_call(
    api_key: str,
    system: str,
    prompt: str,
    attempts: int = ATTEMPTS,
    budget: int = BUDGET,
    shape: Callable[[dict[str, Any]], bool] | None = None,
    progress: Callable[[str], None] | None = None,
    tick: Tick | None = None,
) -> dict[str, Any]:
    """Ask the free router for JSON, retrying because it intermittently routes the request to a
    model that ignores the JSON contract entirely (a safety classifier, for instance).

    `shape` is checked inside the retry loop. A reply that parses but carries the wrong keys is
    just as useless as one that does not parse, and is equally likely to succeed on a second
    attempt against a different model, so validating it outside the loop would throw away a
    whole run over one bad draw.

    Retries share one wall-clock budget so a run cannot spend attempts x deadline stalled.

    `progress` receives one line per attempt and one per outcome, naming the model, so a slow
    run reads as work in progress rather than a hang. `tick` drives a transient live wait.
    """
    say = progress or (lambda message: None)
    started, failure, reason = time.monotonic(), "", ""
    for attempt in range(attempts):
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            break
        available = models()
        model = available[attempt % len(available)]
        label = model.split("/")[-1].removesuffix(":free")
        then = "; trying the next model" if attempt + 1 < attempts else ""
        # A rejected reply is retried with the reason attached. Re-sending the identical prompt
        # invites the next model to make the identical mistake — the same mis-copied anchor,
        # the same skipped file — whereas naming it is usually enough to get it corrected.
        asked = f"{prompt}\n\nA previous reply to this request was rejected because {reason}. Correct that." if reason else prompt
        say(f"Asking {label} (attempt {attempt + 1}/{attempts})")
        began = time.monotonic()
        try:
            result = json_response(
                complete(api_key, system, asked, deadline=int(min(DEADLINE, remaining)), model=model, tick=tick)
            )
        except RateLimited:
            raise  # every model shares one account quota, so the next one cannot succeed either
        except OpenRouterError as error:
            failure = f"{model}: {error}"
            if brief(error) == "reply was not JSON":
                reason = "it was not a single valid JSON object"
            say(f"↳ {brief(error)} after {time.monotonic() - began:.0f}s{then}")
            continue
        finally:
            if tick is not None:
                tick(None)
        # A validator rejects generically by returning False, or specifically by raising
        # ValueError with a reason, which is both shown and fed into the next attempt.
        try:
            accepted = shape is None or shape(result)
            rejection = "" if accepted else "the reply parsed but did not match the requested shape"
        except ValueError as error:
            accepted, rejection = False, str(error)
        if accepted:
            say(f"↳ answered in {time.monotonic() - began:.0f}s")
            return result
        reason, failure = rejection, f"{model}: {rejection}"
        say(f"↳ rejected after {time.monotonic() - began:.0f}s: {rejection}{then}")
    raise OpenRouterError(f"The free model returned no usable JSON within {budget}s. Last failure: {failure}")
