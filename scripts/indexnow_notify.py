#!/usr/bin/env python3
"""Notify IndexNow only for HTML pages in a successful GitHub Pages push."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOST = "heyordo.app"
BASE = f"https://{HOST}"
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
KEY_PATTERN = re.compile(r"[A-Za-z0-9-]{8,128}\Z")


def page_url(path: str) -> str | None:
    """Map only site HTML entry points to their canonical URLs."""
    if path == "index.html":
        return BASE + "/"
    if path.endswith("/index.html") and not path.startswith(("drafts/", ".")):
        directory = path[: -len("index.html")]
        if all(part not in {".", "..", "drafts"} for part in directory.split("/")):
            return BASE + "/" + urllib.parse.quote(directory, safe="/")
    return None


def changed_page_urls(before: str, after: str) -> list[str]:
    if before == "0" * 40:
        raise ValueError("No prior commit in push; review the initial publish manually")
    paths = subprocess.check_output(
        ["git", "diff", "--no-renames", "--name-only", "-z", before, after],
    ).decode("utf-8").split("\0")
    return sorted({url for path in paths if (url := page_url(path))})


def github_json(path: str, token: str):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Ordo-IndexNow/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def wait_for_pages_deployment(repository: str, sha: str, token: str) -> None:
    encoded_sha = urllib.parse.quote(sha, safe="")
    deadline = time.monotonic() + 12 * 60
    while time.monotonic() < deadline:
        deployments = github_json(
            f"/repos/{repository}/deployments?sha={encoded_sha}&environment=github-pages&per_page=20",
            token,
        )
        for deployment in deployments:
            if deployment.get("sha") != sha or deployment.get("environment") != "github-pages":
                continue
            statuses = github_json(
                f"/repos/{repository}/deployments/{deployment['id']}/statuses?per_page=10",
                token,
            )
            if statuses and statuses[0]["state"] == "success":
                return
            if statuses and statuses[0]["state"] in {"failure", "error"}:
                raise RuntimeError("Matching GitHub Pages deployment failed; IndexNow not notified")
        time.sleep(10)
    raise TimeoutError("No successful matching GitHub Pages deployment; IndexNow not notified")


def main() -> None:
    event = json.loads(Path(os.environ["GH_EVENT_PATH"]).read_text(encoding="utf-8"))
    before, after = event["before"], event["after"]
    urls = changed_page_urls(before, after)
    if not urls:
        print("No changed HTML pages; no IndexNow notification needed.")
        return

    key_file = Path(os.environ["INDEXNOW_KEY_FILE"])
    key = key_file.read_text(encoding="utf-8").strip()
    if not KEY_PATTERN.fullmatch(key) or key_file.name != f"indexnow-{key}.txt":
        raise ValueError("IndexNow key file name or contents are invalid")

    wait_for_pages_deployment(os.environ["GH_REPOSITORY"], after, os.environ["GH_TOKEN"])
    key_url = BASE + "/" + key_file.name
    with urllib.request.urlopen(key_url, timeout=20) as response:
        live_key = response.read().decode("utf-8").strip()
    if live_key != key:
        raise RuntimeError("Live IndexNow key file does not match; no notification sent")

    payload = json.dumps(
        {"host": HOST, "key": key, "keyLocation": key_url, "urlList": urls}
    ).encode("utf-8")
    req = urllib.request.Request(
        INDEXNOW_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "Ordo-IndexNow/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"IndexNow rejected notification: HTTP {error.code}") from error
    if status not in {200, 202}:
        raise RuntimeError(f"Unexpected IndexNow response: HTTP {status}")
    print(f"IndexNow accepted {len(urls)} changed URL(s): {', '.join(urls)} (HTTP {status}).")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"IndexNow notification failed safely: {exc}", file=sys.stderr)
        raise SystemExit(1)
