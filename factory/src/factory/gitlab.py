"""Publishing the generated project to the hackathon GitLab: create the project, push the code, CI starts by itself.

Enabled by GITLAB_URL + GITLAB_TOKEN (personal/group access token with api + write_repository); GITLAB_GROUP is the
optional namespace path. Without them the step is skipped. The token is never written to logs or to the git config.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .builder import run


def configured() -> bool:
    return bool(os.environ.get("GITLAB_URL") and os.environ.get("GITLAB_TOKEN"))


def _api(method: str, path: str, data: dict | None = None) -> dict | list:
    base = os.environ["GITLAB_URL"].rstrip("/")
    req = urllib.request.Request(f"{base}/api/v4{path}", method=method,
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"PRIVATE-TOKEN": os.environ["GITLAB_TOKEN"], "Content-Type": "application/json"})
    ctx = ssl.create_default_context()
    if os.environ.get("GITLAB_INSECURE", "1") == "1":  # локальный GitLab площадки часто с самоподписанным сертификатом
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return json.loads(resp.read() or b"{}")


def _project(slug: str) -> dict:
    group = os.environ.get("GITLAB_GROUP", "").strip("/")
    full_path = f"{group}/{slug}" if group else slug
    try:
        return _api("GET", f"/projects/{urllib.parse.quote(full_path, safe='')}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    # private: в приватной группе площадки проект с видимостью internal создать нельзя.
    payload: dict = {"name": slug, "path": slug, "visibility": os.environ.get("GITLAB_VISIBILITY", "private")}
    if group:
        payload["namespace_id"] = _api("GET", f"/groups/{urllib.parse.quote(group, safe='')}")["id"]
    return _api("POST", "/projects", payload)


async def publish(project_dir: Path, slug: str, message: str) -> tuple[bool, str]:
    """Push the project to GitLab; returns (ok, web_url or error)."""
    if not configured():
        return False, "GitLab не настроен (GITLAB_URL, GITLAB_TOKEN)"
    try:
        info = _project(slug)
    except Exception as e:  # noqa: BLE001 — сообщение без токена
        return False, f"GitLab API: {str(e)[:200]}"
    # Адрес — из GITLAB_URL: external_url сервера может указывать на имя, недоступное с этой машины.
    base = urllib.parse.urlsplit(os.environ["GITLAB_URL"].rstrip("/"))
    path = f"{base.path}/{info['path_with_namespace']}"
    http_url = urllib.parse.urlunsplit((base.scheme, base.netloc, path + ".git", "", ""))
    web_url = urllib.parse.urlunsplit((base.scheme, base.netloc, path, "", ""))
    token = urllib.parse.quote(os.environ["GITLAB_TOKEN"], safe="")
    push_url = urllib.parse.urlunsplit((base.scheme, f"oauth2:{token}@{base.netloc}", path + ".git", "", ""))
    git = ["git", "-C", str(project_dir), "-c", "user.name=Агентский конвейер", "-c", "user.email=factory@localhost",
           "-c", f"http.sslVerify={'false' if os.environ.get('GITLAB_INSECURE', '1') == '1' else 'true'}"]
    if not (project_dir / ".git").exists():
        await run([*git, "init", "-q", "-b", "main"])
    await run([*git, "add", "-A"])
    await run([*git, "commit", "-q", "-m", message, "--allow-empty"])
    code, out = await run([*git, "push", "-q", "--force", push_url, "HEAD:main"], timeout=600)
    if code != 0:
        return False, "git push: " + out.replace(token, "***")[-300:]
    return True, web_url
