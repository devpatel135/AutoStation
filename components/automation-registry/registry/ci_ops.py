"""CI entry point for the ``ops-action`` pipeline (the admin panel's backend - there is no server).

Inputs are CI variables, never interpolated into a shell:

====================  ====================================================================================
``ACTION``            pause | resume | fleet-pause | fleet-resume | promote | rollback | decommission | trigger-add
``TARGET``            automation/component name (not needed for fleet-*)
``REASON``            single line, <= 300 chars (required for pause/resume/fleet-*/rollback/decommission)
``PARAMS``            JSON object: promote {"to","version"?}; rollback {"ring","to"}; trigger-add {"slot"?,"params"?}
``BRANCH``            optional ``ops/...`` branch name (the admin panel pre-computes it so its MR link works)
====================  ====================================================================================

Governance: manifest-changing actions are committed to a *branch* and opened as a merge request - **the MR approval
stays the gate**. ``trigger-add`` does not change the manifest: it is committed straight to the protected ``triggers``
branch (only the ops bot may push there) and the Pages job is re-run. Emergency pause (``EMERGENCY=1``) may additionally
merge its own MR, but only for users listed in ``EMERGENCY_PAUSE_USERS``.

Writes go through the GitLab REST API (commits / files / merge_requests / pipeline) with ``OPS_BOT_TOKEN``, so the job
needs no git credentials and no push access to ``main``.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import ops, store
from .checks import validate_all
from .ops import OpError

TRIGGERS_BRANCH = "triggers"
BRANCH_RE = re.compile(r"^ops/[a-z0-9][a-z0-9._/-]{2,80}$")


# ---- GitLab API client (http injectable for tests) ---------------------------------------------------
def default_http(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - GitLab API of this instance
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


class GitLab:
    def __init__(self, env: dict, http=default_http):
        self.base = env["CI_API_V4_URL"].rstrip("/")
        self.pid = env["CI_PROJECT_ID"]
        self.default_branch = env.get("CI_DEFAULT_BRANCH", "main")
        self.token = env["OPS_BOT_TOKEN"]
        self.http = http

    def _u(self, path: str) -> str:
        return f"{self.base}/projects/{urllib.parse.quote(str(self.pid), safe='')}{path}"

    def call(self, method: str, path: str, body: dict | None = None):
        return self.http(method, self._u(path), self.token, body)

    def get_file(self, path: str, ref: str) -> str | None:
        status, data = self.call("GET", f"/repository/files/{urllib.parse.quote(path, safe='')}/raw?ref={urllib.parse.quote(ref, safe='')}")
        if status == 404:
            return None
        if status >= 400:
            raise OpError(f"GitLab API error reading {path}@{ref}: HTTP {status}")
        return data if isinstance(data, str) else json.dumps(data)

    def commit(self, branch: str, start_branch: str | None, message: str, files: dict[str, str], existing: set[str]):
        actions = [{"action": "update" if p in existing else "create", "file_path": p, "content": c} for p, c in files.items()]
        body = {"branch": branch, "commit_message": message, "actions": actions}
        if start_branch:
            body["start_branch"] = start_branch
        status, data = self.call("POST", "/repository/commits", body)
        if status >= 400:
            raise OpError(f"GitLab refused the commit on {branch}: HTTP {status} {str(data)[:200]}")
        return data

    def create_mr(self, source: str, title: str, description: str, labels: list[str]):
        status, data = self.call("POST", "/merge_requests", {
            "source_branch": source, "target_branch": self.default_branch, "title": title, "description": description,
            "labels": ",".join(labels), "remove_source_branch": True})
        if status >= 400:
            raise OpError(f"GitLab refused the merge request: HTTP {status} {str(data)[:200]}")
        return data

    def merge_mr(self, iid: int):
        return self.call("PUT", f"/merge_requests/{iid}/merge", {"should_remove_source_branch": True})

    def run_pages_pipeline(self):
        return self.call("POST", "/pipeline", {"ref": self.default_branch, "variables": [{"key": "PAGES_ONLY", "value": "1"}]})


# ---- the action --------------------------------------------------------------------------------------
def _parse_env(env: dict) -> tuple[str, str, str, dict, str | None]:
    action = (env.get("ACTION") or "").strip()
    target = (env.get("TARGET") or "").strip()
    reason = ops.clean_reason(env.get("REASON"))
    raw = env.get("PARAMS") or "{}"
    if len(raw) > 4000:
        raise OpError("PARAMS is too large")
    try:
        params = json.loads(raw)
    except ValueError:
        raise OpError("PARAMS must be a JSON object, e.g. {\"to\": \"pilot\"}")
    if not isinstance(params, dict):
        raise OpError("PARAMS must be a JSON object")
    return action, target, reason, params, env.get("GITLAB_USER_LOGIN") or None


def run_action(env: dict, manifest_path: Path, *, api: GitLab | None = None, dry_run: bool = False,
               allow_placeholders: bool = False) -> dict:
    action, target, reason, params, actor = _parse_env(env)
    mp = Path(manifest_path)
    m = store.load_manifest(mp)
    manifest_rel = env.get("MANIFEST_PATH", "manifest.yaml")
    history_rel = str(Path(manifest_rel).parent / store.HISTORY_DIR / store.HISTORY_FILE).replace("\\", "/").lstrip("./")
    triggers_rel = str(Path(manifest_rel).parent / store.TRIGGERS_FILE).replace("\\", "/").lstrip("./")
    result: dict = {"ok": True, "action": action, "target": target or "fleet", "actor": actor, "changed": False}

    doc = None
    if action == "trigger-add":
        if api:
            text = api.get_file(triggers_rel, TRIGGERS_BRANCH)
            doc = store.load_triggers_text(text) if text else {"schema_version": 1, "triggers": []}
        else:
            doc = store.load_triggers(mp.parent / store.TRIGGERS_FILE)
    res = ops.apply_action(m, action, target, reason, params, triggers_doc=doc, actor=actor)
    result.update(summary=res.summary, mr_title=res.mr_title)
    if not (res.manifest_changed or res.triggers_changed):
        return result
    result["changed"] = True

    if action == "trigger-add":
        text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
        if not dry_run:
            if api:
                existing = {triggers_rel} if api.get_file(triggers_rel, TRIGGERS_BRANCH) is not None else set()
                api.commit(TRIGGERS_BRANCH, None if existing else api.default_branch,
                           f"trigger {target} ({res.extra['trigger']['id']}) requested by {actor or 'unknown'}",
                           {triggers_rel: text}, existing)
                api.run_pages_pipeline()
            else:
                store.save_triggers(mp.parent / store.TRIGGERS_FILE, doc)
        result["trigger"] = res.extra["trigger"]
        return result

    report = validate_all(m, allow_placeholders=allow_placeholders)
    if report.errors:
        raise OpError("the resulting manifest would be invalid:\n  - " + "\n  - ".join(report.errors))
    new_text = store.dump_manifest(m)
    detail = reason or res.detail or action
    files = {manifest_rel: new_text}
    if api:
        hist_existing = api.get_file(history_rel, api.default_branch)
        files[history_rel] = store.render_history(hist_existing, action, target or "fleet", detail, actor)
        if action == "decommission":
            files[f"{Path(history_rel).parent.as_posix()}/decommissioned/{target}.md"] = (
                f"# {target} decommissioned {store.today()}\n\nReason: {reason}\n\nRequested by: {actor or 'unknown'}\n")
        branch = (env.get("BRANCH") or "").strip()
        if branch and not BRANCH_RE.match(branch):
            raise OpError("BRANCH must look like ops/<something>")
        branch = branch or "ops/{}-{}-{}".format(action, target or "fleet", datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
        existing = {p for p in files if api.get_file(p, api.default_branch) is not None}
        result["branch"] = branch
        if not dry_run:
            api.commit(branch, api.default_branch, res.mr_title, files, existing)
            labels = ["ops-action", action]
            emergency = (env.get("EMERGENCY") == "1" and action in ("pause", "fleet-pause")
                         and actor and actor in [u.strip() for u in (env.get("EMERGENCY_PAUSE_USERS") or "").split(",") if u.strip()])
            if emergency:
                labels.append("emergency")
            mr = api.create_mr(branch, res.mr_title, mr_description(res, action, target, reason, actor, env, emergency), labels)
            result["mr_url"], result["mr_iid"] = mr.get("web_url"), mr.get("iid")
            if emergency:
                st, _ = api.merge_mr(mr["iid"])
                result["emergency_merged"] = st < 300  # may be refused by approval rules - the MR stays open
    elif not dry_run:
        store.atomic_write(mp, new_text)
        store.append_history(mp, action, target or "fleet", detail, actor)
    return result


def mr_description(res: ops.OpResult, action: str, target: str, reason: str, actor: str | None, env: dict, emergency: bool) -> str:
    lines = [f"**ops-action** `{action}` on `{target or 'fleet'}`", "", f"- Requested by: {actor or 'unknown'}",
             f"- Reason: {reason or '(none given)'}"]
    if env.get("CI_PIPELINE_URL"):
        lines.append(f"- Pipeline: {env['CI_PIPELINE_URL']}")
    lines += ["", "### What changes", ""] + [f"- {s}" for s in res.summary]
    lines += ["", "### Review checklist", "",
              "- [ ] The reason is accurate and the right rings are affected (see the diff)",
              "- [ ] `registry validate` is green in this MR's pipeline"]
    if emergency:
        lines += ["", "**EMERGENCY pause** - merge attempted by the bot on behalf of an authorised user; review after the fact."]
    return "\n".join(lines)


def main(args) -> int:
    env = dict(os.environ)
    api = None
    if args.git:
        missing = [k for k in ("CI_API_V4_URL", "CI_PROJECT_ID", "OPS_BOT_TOKEN") if not env.get(k)]
        if missing:
            print(f"refused: --git needs {', '.join(missing)}", file=sys.stderr)
            return 2
        api = GitLab(env)
    allow = bool(getattr(args, "allow_placeholders", False) or env.get("REGISTRY_ALLOW_PLACEHOLDERS") in ("1", "true"))
    try:
        result = run_action(env, Path(args.manifest), api=api, dry_run=args.dry_run, allow_placeholders=allow)
    except OpError as exc:
        Path(args.result).write_text(json.dumps({"ok": False, "error": str(exc)}, indent=2), encoding="utf-8")
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    Path(args.result).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"[ops-action] {result['action']} {result['target']}: " + ("changed" if result["changed"] else "no change"))
    for line in result.get("summary", []):
        print("  " + line)
    if result.get("mr_url"):
        print(f"  merge request: {result['mr_url']}")
    return 0
