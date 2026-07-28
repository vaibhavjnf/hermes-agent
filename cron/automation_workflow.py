"""Durable, secret-safe workflow state for all local automations.

Best-effort telemetry only. Recorder failures never change automation behavior.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
_EVENT_LOCK = threading.Lock()
_TERMINAL = {
    "SUCCEEDED",
    "FAILED",
    "DELIVERY_FAILED",
    "INTERRUPTED",
    "SKIPPED",
    "HEALTHY",
    "UNHEALTHY",
    "WAITING",
}


def _now() -> str:
    return dt.datetime.now(IST).isoformat(timespec="milliseconds")


def _safe(value: Any, limit: int = 800) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    patterns = (
        r"(?i)(authorization|bearer|api[_-]?key|token|password|secret|otp)(\s*[:=]?\s*)\S+",
        r"\b\d{6}\b",
    )
    for pattern in patterns:
        text = re.sub(pattern, lambda m: f"{m.group(1)}{m.group(2)}<redacted>" if m.lastindex == 2 else "<redacted>", text)
    return text[:limit]


def _canonical_hermes_root() -> Path:
    override = os.environ.get("AUTOMATION_WORKFLOW_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    try:
        from hermes_constants import get_hermes_home

        home = get_hermes_home().resolve()
    except Exception:
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).resolve()
    parts = home.parts
    if "profiles" in parts:
        index = parts.index("profiles")
        if index > 0:
            return Path(*parts[:index]) / "state" / "automation_workflows"
    return home / "state" / "automation_workflows"


def _profile_name() -> str:
    home = Path(os.environ.get("HERMES_HOME", ""))
    if "profiles" in home.parts:
        index = home.parts.index("profiles")
        if index + 1 < len(home.parts):
            return home.parts[index + 1]
    return "main"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _append_event(root: Path, payload: dict[str, Any]) -> None:
    path = root / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    with _EVENT_LOCK:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned[:160] or "unknown"


def _current_path(root: Path, profile: str, kind: str, automation_id: str) -> Path:
    key = _slug(f"{profile}__{kind}__{automation_id}")
    return root / "current" / f"{key}.json"


@dataclass
class WorkflowRun:
    automation_id: str
    name: str
    kind: str
    source: str
    profile: str = field(default_factory=_profile_name)
    schedule: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: f"{dt.datetime.now(IST):%Y%m%dT%H%M%S.%f%z}-{uuid.uuid4().hex[:8]}")
    root: Path = field(default_factory=_canonical_hermes_root)
    state: dict[str, Any] = field(init=False)
    _finished: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        now = _now()
        self.state = {
            "version": 1,
            "automation_id": _safe(self.automation_id, 200),
            "name": _safe(self.name, 200),
            "kind": _safe(self.kind, 80),
            "source": _safe(self.source, 120),
            "profile": _safe(self.profile, 80),
            "schedule": _safe(self.schedule, 200),
            "run_id": self.run_id,
            "status": "RUNNING",
            "current_stage": "trigger",
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
            "stages": {
                "trigger": {"status": "COMPLETED", "updated_at": now}
            },
            "metadata": {str(k): _safe(v, 300) for k, v in self.metadata.items()},
        }
        self._persist("workflow_started")

    @property
    def current_path(self) -> Path:
        return _current_path(self.root, self.profile, self.kind, self.automation_id)

    @property
    def run_path(self) -> Path:
        key = _slug(f"{self.profile}__{self.kind}__{self.automation_id}")
        return self.root / "runs" / key / f"{_slug(self.run_id)}.json"

    def _persist(self, event: str, detail: str = "") -> None:
        _atomic_write(self.current_path, self.state)
        _atomic_write(self.run_path, self.state)
        item = {
            "at": _now(),
            "event": event,
            "automation_id": self.state["automation_id"],
            "name": self.state["name"],
            "kind": self.state["kind"],
            "source": self.state["source"],
            "profile": self.state["profile"],
            "run_id": self.run_id,
            "status": self.state["status"],
            "stage": self.state["current_stage"],
        }
        if detail:
            item["detail"] = _safe(detail)
        _append_event(self.root, item)

    def stage(self, name: str, status: str, detail: str = "") -> None:
        if self._finished:
            return
        now = _now()
        stage = {"status": _safe(status.upper(), 80), "updated_at": now}
        if detail:
            stage["detail"] = _safe(detail)
        safe_name = _slug(name)
        self.state["current_stage"] = safe_name
        self.state["stages"][safe_name] = stage
        self.state["updated_at"] = now
        self._persist("stage_updated", detail)

    def finish(self, status: str, detail: str = "") -> None:
        if self._finished:
            return
        normalized = status.upper()
        if normalized not in _TERMINAL:
            normalized = "FAILED"
            detail = f"invalid terminal status requested; {detail}".strip("; ")
        now = _now()
        self.state["status"] = normalized
        self.state["updated_at"] = now
        self.state["finished_at"] = now
        if detail:
            self.state["detail"] = _safe(detail)
        self._finished = True
        self._persist("workflow_finished", detail)
        self._prune_runs()

    def _prune_runs(self) -> None:
        try:
            keep = max(1, int(os.environ.get("AUTOMATION_WORKFLOW_RUN_RETENTION", "50")))
            directory = self.run_path.parent
            paths = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
            for path in paths[keep:]:
                path.unlink()
        except Exception:
            pass


class NullWorkflow:
    """No-op fallback. Telemetry must never break production work."""

    def stage(self, name: str, status: str, detail: str = "") -> None:
        return None

    def finish(self, status: str, detail: str = "") -> None:
        return None


def start_workflow(**kwargs: Any) -> WorkflowRun | NullWorkflow:
    try:
        return WorkflowRun(**kwargs)
    except Exception:
        return NullWorkflow()


def start_cron_workflow(job: dict[str, Any]) -> WorkflowRun | NullWorkflow:
    schedule = job.get("schedule") or {}
    display = schedule.get("display") or schedule.get("expr") or schedule.get("kind") or ""
    mode = "script" if job.get("no_agent") else "agent"
    return start_workflow(
        automation_id=str(job.get("id") or "unknown"),
        name=str(job.get("name") or job.get("id") or "cron job"),
        kind="cron",
        source="hermes_scheduler",
        schedule=str(display),
        metadata={"mode": mode, "script": Path(str(job.get("script") or "")).name},
    )


def observe_workflow(
    *,
    automation_id: str,
    name: str,
    kind: str,
    source: str,
    healthy: bool,
    stages: list[tuple[str, str, str]],
    profile: str = "main",
    schedule: str = "",
    metadata: Optional[dict[str, Any]] = None,
) -> WorkflowRun | NullWorkflow:
    root = _canonical_hermes_root()
    desired = {
        "healthy": bool(healthy),
        "schedule": _safe(schedule, 200),
        "stages": [[_slug(name), _safe(status.upper(), 80), _safe(detail)] for name, status, detail in stages],
        "metadata": {str(k): _safe(v, 300) for k, v in (metadata or {}).items()},
    }
    fingerprint = hashlib.sha256(
        json.dumps(desired, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    current_path = _current_path(root, profile, kind, automation_id)
    try:
        previous = json.loads(current_path.read_text())
        if (previous.get("metadata") or {}).get("observation_fingerprint") == fingerprint:
            return NullWorkflow()
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    observed_metadata = dict(metadata or {})
    observed_metadata["observation_fingerprint"] = fingerprint
    run = start_workflow(
        automation_id=automation_id,
        name=name,
        kind=kind,
        source=source,
        profile=profile,
        schedule=schedule,
        metadata=observed_metadata,
    )
    for stage_name, status, detail in stages:
        run.stage(stage_name, status, detail)
    run.finish("HEALTHY" if healthy else "UNHEALTHY")
    return run
