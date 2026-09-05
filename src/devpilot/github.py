from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

ATTEMPTS = 3
TIMEOUT = 60


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class Issue:
    title: str
    body: str
    url: str


class GitHubClient:
    def __init__(self, token: str) -> None:
        if not token:
            raise GitHubError("GITHUB_TOKEN is missing. Add it to ~/.dev-pilot/.env.")
        self.token = token

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict | list:
        data = json.dumps(payload).encode() if payload else None
        request = urllib.request.Request(
            f"https://api.github.com{path}", data=data, method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "DevPilot",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        for attempt in range(ATTEMPTS):
            last = attempt == ATTEMPTS - 1
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as error:
                # Only server-side faults are worth repeating; a 4xx will fail again identically.
                detail = error.read().decode(errors="replace")
                if error.code < 500 or last:
                    raise GitHubError(f"GitHub API {error.code}: {detail}") from error
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                if last:
                    raise GitHubError(f"Cannot reach GitHub after {ATTEMPTS} attempts: {error}") from error
            time.sleep(2 ** attempt)
        raise GitHubError("Unreachable.")

    def get_issue(self, owner: str, repository: str, number: int) -> Issue:
        data = self._request("GET", f"/repos/{owner}/{repository}/issues/{number}")
        if "pull_request" in data:
            raise GitHubError(f"#{number} is a pull request, not an issue.")
        return Issue(title=data["title"], body=data.get("body") or "", url=data["html_url"])

    def create_draft_pr(self, owner: str, repository: str, title: str, body: str, head: str, base: str) -> str:
        try:
            data = self._request("POST", f"/repos/{owner}/{repository}/pulls", {
                "title": title, "body": body, "head": head, "base": base, "draft": True,
            })
        except GitHubError as error:
            if "already exists" not in str(error):
                raise
            # A retried POST can land after the original succeeded; adopt that pull request
            # rather than failing a run whose work is already pushed.
            existing = self.open_pr_url(owner, repository, head)
            if existing is None:
                raise
            return existing
        return data["html_url"]

    def open_pr_url(self, owner: str, repository: str, head: str) -> str | None:
        """Return the URL of the open pull request for a head branch, if one exists."""
        existing = self._request("GET", f"/repos/{owner}/{repository}/pulls?head={owner}:{head}&state=open")
        return existing[0]["html_url"] if existing else None
