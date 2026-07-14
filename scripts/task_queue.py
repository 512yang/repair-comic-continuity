"""Deterministic metadata-only task queue for comic continuity repairs."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline_contracts import TASK_STATES, canonical_hash, normalize_page_id


SCHEMA_VERSION = "1.2"

_EXPENSIVE_VISUAL_TYPES = frozenset({"visual", "full_page_redraw"})

_TASK_REQUIRED_FIELDS = frozenset(
    {
        "task_id",
        "page_id",
        "task_type",
        "payload_hash",
        "cluster_id",
        "prompt_reference_hash",
        "state",
        "attempt_count",
        "created_at",
        "updated_at",
        "queued_at",
        "leased_at",
        "completed_at",
        "failed_at",
        "completed_by",
        "lease_owner",
        "lease_expires_at",
        "candidate_path",
        "candidate_hash",
        "failure_code",
        "failure_record_id",
        "timestamps",
        "candidate",
        "failure",
        "is_canary",
        "canary_task_id",
    }
)
_TASK_TIMESTAMP_FIELDS = (
    "created_at",
    "updated_at",
    "queued_at",
    "leased_at",
    "completed_at",
    "failed_at",
)


def queue_registry_hash(queue: Mapping[str, Any]) -> str:
    """Hash the final public queue payload, excluding registry/internal fields."""
    if not isinstance(queue, Mapping):
        raise ValueError("queue must be a JSON object")
    payload = {
        key: value
        for key, value in queue.items()
        if key != "registry_hash" and not key.startswith("_")
    }
    return canonical_hash(payload)


def _refresh_registry_hash(queue: dict[str, Any]) -> None:
    if "registry_hash" in queue:
        queue["registry_hash"] = queue_registry_hash(queue)


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value.strip()


def _aware_time(value: datetime | str | None, name: str = "now") -> datetime:
    if value is None:
        result = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO timestamp") from exc
    else:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return result.astimezone(timezone.utc)


def _timestamp(value: datetime | str | None, name: str = "now") -> str:
    return _aware_time(value, name).isoformat()


def _lane_key(cluster_id: object, task_type: object) -> str:
    cluster = "unknown" if cluster_id is None else _require_text(cluster_id, "cluster_id")
    return f"{cluster}::{_require_text(task_type, 'task_type')}"


def _validate_all_timestamps(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and (key.endswith("_at") or key == "timestamp"):
                if item is not None:
                    _aware_time(item, key)
            else:
                _validate_all_timestamps(item)
    elif isinstance(value, list):
        for item in value:
            _validate_all_timestamps(item)


def _validate_task(task: object) -> None:
    if not isinstance(task, dict):
        raise ValueError("every task must be an object")
    missing = _TASK_REQUIRED_FIELDS - set(task)
    if missing:
        raise ValueError(f"task is missing required fields: {sorted(missing)!r}")
    if task["state"] not in TASK_STATES:
        raise ValueError("queue contains an invalid task state")
    task_id = _require_text(task["task_id"], "task_id")
    canonical_page = normalize_page_id(task["page_id"])
    if canonical_page != task["page_id"]:
        raise ValueError("task page_id must be canonical")
    task_type = _require_text(task["task_type"], "task_type")
    if not isinstance(task["is_canary"], bool):
        raise ValueError("is_canary must be a boolean")
    if task["canary_task_id"] is not None:
        _require_text(task["canary_task_id"], "canary_task_id")
    if task["is_canary"] and task["canary_task_id"] is not None:
        raise ValueError("canary task must not reference another canary")
    payload_hash = _require_text(task["payload_hash"], "payload_hash")
    expected_task_id = canonical_hash(
        {
            "page_id": canonical_page,
            "task_type": task_type,
            "payload_hash": payload_hash,
        }
    )
    if task_id != expected_task_id:
        raise ValueError("task_id does not match canonical task identity")
    if "idempotency_key" in task and (
        _require_text(task["idempotency_key"], "idempotency_key") != task_id
    ):
        raise ValueError("task idempotency key must equal task_id")
    for optional_name in ("cluster_id", "prompt_reference_hash"):
        if task[optional_name] is not None:
            _require_text(task[optional_name], optional_name)
    attempts = task["attempt_count"]
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise ValueError("attempt_count must be a nonnegative integer")

    timestamps = task["timestamps"]
    if not isinstance(timestamps, dict) or not set(_TASK_TIMESTAMP_FIELDS).issubset(
        timestamps
    ):
        raise ValueError("task timestamps must contain all timestamp fields")
    for field in _TASK_TIMESTAMP_FIELDS:
        flat_value = task[field]
        nested_value = timestamps[field]
        if flat_value != nested_value:
            raise ValueError(f"task timestamp mismatch for {field}")
        if flat_value is not None:
            _aware_time(flat_value, field)
    for required_time in ("created_at", "updated_at", "queued_at"):
        if task[required_time] is None:
            raise ValueError(f"task {required_time} must not be empty")

    candidate = task["candidate"]
    if not isinstance(candidate, dict) or not {"path", "hash"}.issubset(candidate):
        raise ValueError("task candidate must contain path and hash")
    if task["candidate_path"] != candidate["path"] or task["candidate_hash"] != candidate["hash"]:
        raise ValueError("task candidate fields are inconsistent")

    failure = task["failure"]
    failure_fields = {
        "code",
        "record_id",
        "cluster_id",
        "prompt_reference_hash",
        "timestamp",
    }
    if not isinstance(failure, dict) or not failure_fields.issubset(failure):
        raise ValueError("task failure must contain all failure fields")
    if task["failure_code"] != failure["code"] or task["failure_record_id"] != failure["record_id"]:
        raise ValueError("task failure fields are inconsistent")
    if failure["timestamp"] is not None:
        _aware_time(failure["timestamp"], "failure timestamp")

    state = task["state"]
    completed_by = task["completed_by"]
    owner = task["lease_owner"]
    expiry = task["lease_expires_at"]
    candidate_is_empty = candidate["path"] is None and candidate["hash"] is None
    failure_is_empty = all(failure[field] is None for field in failure_fields)
    if state == "queued":
        if completed_by is not None:
            raise ValueError("queued task must not retain completed_by")
        if owner is not None or expiry is not None or task["leased_at"] is not None:
            raise ValueError("queued task must not retain lease metadata")
        if task["completed_at"] is not None or task["failed_at"] is not None:
            raise ValueError("queued task must not retain terminal timestamps")
        if not candidate_is_empty or not failure_is_empty:
            raise ValueError("queued task must not retain terminal metadata")
    elif state == "leased":
        if completed_by is not None:
            raise ValueError("leased task must not retain completed_by")
        _require_text(owner, "lease_owner")
        _aware_time(expiry, "lease_expires_at")
        if task["leased_at"] is None:
            raise ValueError("leased task must have leased_at")
        if task["completed_at"] is not None or task["failed_at"] is not None:
            raise ValueError("leased task must not retain terminal timestamps")
        if not candidate_is_empty or not failure_is_empty:
            raise ValueError("leased task must not retain terminal metadata")
    elif state == "completed":
        _require_text(completed_by, "completed_by")
        if owner is not None or expiry is not None:
            raise ValueError("completed task must not retain lease metadata")
        _require_text(candidate["path"], "candidate path")
        _require_text(candidate["hash"], "candidate hash")
        if task["completed_at"] is None:
            raise ValueError("completed task must have completed_at")
        if task["failed_at"] is not None or not failure_is_empty:
            raise ValueError("completed task must not retain failure metadata")
    elif state == "failed":
        if completed_by is not None:
            raise ValueError("failed task must not retain completed_by")
        if owner is not None or expiry is not None:
            raise ValueError("failed task must not retain lease metadata")
        _require_text(failure["code"], "failure code")
        _require_text(failure["record_id"], "failure record_id")
        if failure["timestamp"] is None or task["failed_at"] is None:
            raise ValueError("failed task must have failure timestamps")
        if failure["timestamp"] != task["failed_at"]:
            raise ValueError("failure timestamp must equal failed_at")
        if failure["cluster_id"] != task["cluster_id"] or (
            failure["prompt_reference_hash"] != task["prompt_reference_hash"]
        ):
            raise ValueError("failure context must match task context")
        if task["completed_at"] is not None or not candidate_is_empty:
            raise ValueError("failed task must not retain candidate metadata")


def _nonnegative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _validate_metrics_and_cache(metrics: dict[str, Any], cache: dict[str, Any]) -> None:
    required_metrics = {
        "cache_hits",
        "cache_misses",
        "lane_failure_streaks",
        "prompt_failure_streaks",
        "retry_counts_by_failure_code",
        "lane_reopen_history",
    }
    if not required_metrics.issubset(metrics):
        raise ValueError("queue metrics are missing required fields")
    cache_hits = _nonnegative_int(metrics["cache_hits"], "cache_hits")
    cache_misses = _nonnegative_int(metrics["cache_misses"], "cache_misses")

    lane_streaks = metrics["lane_failure_streaks"]
    if not isinstance(lane_streaks, dict):
        raise ValueError("lane_failure_streaks must be an object")
    for lane, record in lane_streaks.items():
        _require_text(lane, "lane failure key")
        if not isinstance(record, Mapping):
            raise ValueError("lane failure streak must be an object")
        _require_text(record.get("failure_code"), "lane failure_code")
        _nonnegative_int(record.get("count"), "lane failure count")

    for field in ("prompt_failure_streaks", "retry_counts_by_failure_code"):
        counts = metrics[field]
        if not isinstance(counts, dict):
            raise ValueError(f"{field} must be an object")
        for key, count in counts.items():
            _require_text(key, f"{field} key")
            _nonnegative_int(count, f"{field} count")

    total_hits = 0
    total_misses = 0
    for key, record in cache.items():
        _require_text(key, "cache key")
        if not isinstance(record, Mapping) or not {"hits", "misses"}.issubset(record):
            raise ValueError("cache entry must contain hits and misses")
        total_hits += _nonnegative_int(record["hits"], "cache entry hits")
        total_misses += _nonnegative_int(record["misses"], "cache entry misses")
    if total_hits != cache_hits or total_misses != cache_misses:
        raise ValueError("cache entry totals must match metric totals")


def _validate_queue(queue: object) -> dict[str, Any]:
    if not isinstance(queue, dict):
        raise ValueError("queue must be a JSON object")
    required = {
        "schema_version",
        "revision",
        "tasks",
        "paused_lanes",
        "metrics",
        "cache",
        "max_active_workers",
        "canary_gates",
    }
    if not required.issubset(queue):
        raise ValueError("queue is missing required fields")
    if queue["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported queue schema_version")
    if "registry_hash" in queue:
        registry_hash = queue["registry_hash"]
        if (
            not isinstance(registry_hash, str)
            or registry_hash != queue_registry_hash(queue)
        ):
            raise ValueError("queue registry_hash mismatch")
    if not isinstance(queue["revision"], int) or isinstance(queue["revision"], bool) or queue["revision"] < 0:
        raise ValueError("queue revision must be a nonnegative integer")
    if not isinstance(queue["tasks"], list):
        raise ValueError("queue tasks must be a list")
    if not isinstance(queue["paused_lanes"], dict):
        raise ValueError("paused_lanes must be an object")
    if not isinstance(queue["metrics"], dict) or not isinstance(queue["cache"], dict):
        raise ValueError("queue metrics and cache must be objects")
    max_active_workers = queue["max_active_workers"]
    if (
        not isinstance(max_active_workers, int)
        or isinstance(max_active_workers, bool)
        or not 1 <= max_active_workers <= 3
    ):
        raise ValueError("max_active_workers must be an integer from 1 to 3")
    if not isinstance(queue["canary_gates"], dict):
        raise ValueError("canary_gates must be an object")
    _validate_metrics_and_cache(queue["metrics"], queue["cache"])
    task_ids: set[str] = set()
    for task in queue["tasks"]:
        _validate_task(task)
        task_id = task["task_id"]
        if task_id in task_ids:
            raise ValueError("task_id values must be globally unique")
        task_ids.add(task_id)
    tasks_by_id = {task["task_id"]: task for task in queue["tasks"]}
    for task in queue["tasks"]:
        canary_task_id = task["canary_task_id"]
        if task["is_canary"]:
            if task["cluster_id"] is None:
                raise ValueError("canary task must have a cluster_id")
            if task["task_id"] not in queue["canary_gates"]:
                raise ValueError("canary task must have a canary gate")
        elif task["task_type"] in _EXPENSIVE_VISUAL_TYPES:
            if canary_task_id is None:
                raise ValueError("expensive visual task must reference a canary")
        if canary_task_id is not None:
            canary = tasks_by_id.get(canary_task_id)
            if canary is None or not canary["is_canary"]:
                raise ValueError("canary_task_id must reference a canary task")
            if canary["cluster_id"] != task["cluster_id"]:
                raise ValueError("task and canary must belong to the same cluster")
    gate_fields = {
        "task_id",
        "cluster_id",
        "approved",
        "review_id",
        "reviewed_by",
        "reviewed_at",
    }
    for task_id, gate in queue["canary_gates"].items():
        _require_text(task_id, "canary gate task_id")
        if not isinstance(gate, Mapping) or not gate_fields.issubset(gate):
            raise ValueError("canary gate is missing required fields")
        if gate["task_id"] != task_id:
            raise ValueError("canary gate key must match task_id")
        canary = tasks_by_id.get(task_id)
        if canary is None or not canary["is_canary"]:
            raise ValueError("canary gate must reference a canary task")
        if gate["cluster_id"] != canary["cluster_id"]:
            raise ValueError("canary gate cluster must match canary task")
        if not isinstance(gate["approved"], bool):
            raise ValueError("canary gate approved must be a boolean")
        review_values = (gate["review_id"], gate["reviewed_by"], gate["reviewed_at"])
        if gate["approved"]:
            if canary["state"] != "completed":
                raise ValueError("approved canary must be completed")
            _require_text(gate["review_id"], "canary review_id")
            _require_text(gate["reviewed_by"], "canary reviewed_by")
            _aware_time(gate["reviewed_at"], "canary reviewed_at")
        elif any(value is not None for value in review_values):
            raise ValueError("unapproved canary must not retain review metadata")
    for lane, record in queue["paused_lanes"].items():
        _require_text(lane, "paused lane")
        if not isinstance(record, Mapping):
            raise ValueError("paused lane record must be an object")
    history = queue["metrics"].get("lane_reopen_history", [])
    if not isinstance(history, list):
        raise ValueError("lane_reopen_history must be a list")
    for record in history:
        if not isinstance(record, Mapping):
            raise ValueError("lane reopen history records must be objects")
        _require_text(record.get("lane"), "reopened lane")
        _require_text(record.get("coordinator_review_id"), "coordinator_review_id")
        _aware_time(record.get("reopened_at"), "reopened_at")
    _validate_all_timestamps(queue)
    return queue


def new_queue(max_active_workers: int = 3) -> dict[str, Any]:
    """Return an empty queue using the current durable schema."""
    if (
        not isinstance(max_active_workers, int)
        or isinstance(max_active_workers, bool)
        or not 1 <= max_active_workers <= 3
    ):
        raise ValueError("max_active_workers must be an integer from 1 to 3")
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "tasks": [],
        "paused_lanes": {},
        "metrics": {
            "cache_hits": 0,
            "cache_misses": 0,
            "lane_failure_streaks": {},
            "prompt_failure_streaks": {},
            "retry_counts_by_failure_code": {},
            "lane_reopen_history": [],
        },
        "cache": {},
        "max_active_workers": max_active_workers,
        "canary_gates": {},
    }


def add_task(
    queue: dict[str, Any],
    page_id: object,
    task_type: str,
    payload_hash: str,
    cluster_id: str | None = None,
    prompt_reference_hash: str | None = None,
    now: datetime | str | None = None,
    is_canary: bool = False,
    canary_task_id: str | None = None,
) -> dict[str, Any]:
    """Append a queued task unless its canonical idempotency key already exists."""
    queue = _validate_queue(queue)
    canonical_page_id = normalize_page_id(page_id)
    canonical_task_type = _require_text(task_type, "task_type")
    canonical_payload_hash = _require_text(payload_hash, "payload_hash")
    if cluster_id is not None:
        cluster_id = _require_text(cluster_id, "cluster_id")
    if prompt_reference_hash is not None:
        prompt_reference_hash = _require_text(prompt_reference_hash, "prompt_reference_hash")
    if not isinstance(is_canary, bool):
        raise ValueError("is_canary must be a boolean")
    if canary_task_id is not None:
        canary_task_id = _require_text(canary_task_id, "canary_task_id")
    if is_canary:
        if cluster_id is None:
            raise ValueError("canary task must have a cluster_id")
        if canary_task_id is not None:
            raise ValueError("canary task must not reference another canary")
    elif canonical_task_type in _EXPENSIVE_VISUAL_TYPES:
        if canary_task_id is None:
            raise ValueError("expensive visual task must reference a canary")
        canary = _task(queue, canary_task_id)
        if not canary["is_canary"]:
            raise ValueError("canary_task_id must reference a canary task")
        if canary["cluster_id"] != cluster_id:
            raise ValueError("task and canary must belong to the same cluster")
    task_id = canonical_hash(
        {
            "page_id": canonical_page_id,
            "task_type": canonical_task_type,
            "payload_hash": canonical_payload_hash,
        }
    )
    for task in queue["tasks"]:
        if task.get("task_id") == task_id:
            return task
    created_at = _timestamp(now)
    task = {
        "task_id": task_id,
        "idempotency_key": task_id,
        "page_id": canonical_page_id,
        "task_type": canonical_task_type,
        "payload_hash": canonical_payload_hash,
        "cluster_id": cluster_id,
        "prompt_reference_hash": prompt_reference_hash,
        "state": "queued",
        "attempt_count": 0,
        "created_at": created_at,
        "updated_at": created_at,
        "queued_at": created_at,
        "leased_at": None,
        "completed_at": None,
        "failed_at": None,
        "completed_by": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "candidate_path": None,
        "candidate_hash": None,
        "failure_code": None,
        "failure_record_id": None,
        "timestamps": {
            "created_at": created_at,
            "updated_at": created_at,
            "queued_at": created_at,
            "leased_at": None,
            "completed_at": None,
            "failed_at": None,
        },
        "candidate": {"path": None, "hash": None},
        "failure": {
            "code": None,
            "record_id": None,
            "cluster_id": None,
            "prompt_reference_hash": None,
            "timestamp": None,
        },
        "is_canary": is_canary,
        "canary_task_id": canary_task_id,
    }
    queue["tasks"].append(task)
    if is_canary:
        queue["canary_gates"][task_id] = {
            "task_id": task_id,
            "cluster_id": cluster_id,
            "approved": False,
            "review_id": None,
            "reviewed_by": None,
            "reviewed_at": None,
        }
    _refresh_registry_hash(queue)
    return task


def _task(queue: dict[str, Any], task_id: str) -> dict[str, Any]:
    wanted = _require_text(task_id, "task_id")
    for task in queue["tasks"]:
        if task.get("task_id") == wanted:
            return task
    raise ValueError(f"unknown task_id: {wanted}")


def is_lane_paused(queue: dict[str, Any], cluster_id: str | None, task_type: str) -> bool:
    queue = _validate_queue(queue)
    return _is_lane_paused_unchecked(queue, cluster_id, task_type)


def _is_lane_paused_unchecked(
    queue: dict[str, Any], cluster_id: str | None, task_type: str
) -> bool:
    return _lane_key(cluster_id, task_type) in queue["paused_lanes"]


def _canary_gate_allows(queue: dict[str, Any], task: Mapping[str, Any]) -> bool:
    if task["task_type"] not in _EXPENSIVE_VISUAL_TYPES or task["is_canary"]:
        return True
    gate = queue["canary_gates"].get(task["canary_task_id"])
    return isinstance(gate, Mapping) and gate.get("approved") is True


def claim_task(
    queue: dict[str, Any],
    worker: str,
    now: datetime | str,
    lease_seconds: int = 900,
    task_types: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    """Lease the first eligible queued task while preserving queue order."""
    queue = _validate_queue(queue)
    owner = _require_text(worker, "worker")
    current = _aware_time(now)
    if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be a positive integer")
    allowed_types = None
    if task_types is not None:
        allowed_types = {_require_text(value, "task type filter") for value in task_types}
    recover_expired(queue, current)
    active_lease_owners = {
        task["lease_owner"]
        for task in queue["tasks"]
        if task["state"] == "leased"
        and task.get("lease_owner") is not None
        and task.get("lease_expires_at") is not None
        and _aware_time(task["lease_expires_at"], "lease_expires_at") > current
    }
    if owner in active_lease_owners or len(active_lease_owners) >= queue["max_active_workers"]:
        return None
    actively_leased_pages = {
        task["page_id"]
        for task in queue["tasks"]
        if task["state"] == "leased"
        and task.get("lease_expires_at") is not None
        and _aware_time(task["lease_expires_at"], "lease_expires_at") > current
    }
    for task in queue["tasks"]:
        if task["state"] != "queued":
            continue
        if allowed_types is not None and task["task_type"] not in allowed_types:
            continue
        if _is_lane_paused_unchecked(queue, task.get("cluster_id"), task["task_type"]):
            continue
        if not _canary_gate_allows(queue, task):
            continue
        if task["page_id"] in actively_leased_pages:
            continue
        task["state"] = "leased"
        task["attempt_count"] += 1
        task["lease_owner"] = owner
        task["leased_at"] = current.isoformat()
        task["lease_expires_at"] = (current + timedelta(seconds=lease_seconds)).isoformat()
        task["updated_at"] = current.isoformat()
        task["timestamps"]["leased_at"] = current.isoformat()
        task["timestamps"]["updated_at"] = current.isoformat()
        _refresh_registry_hash(queue)
        return task
    return None


def approve_canary(
    queue: dict[str, Any],
    task_id: str,
    review_id: str,
    reviewed_by: str,
    reviewed_at: datetime | str,
) -> dict[str, Any]:
    """Open a completed canary's gate after an independent review is recorded."""
    queue = _validate_queue(queue)
    canary = _task(queue, task_id)
    if not canary["is_canary"]:
        raise ValueError("task must be a canary")
    if canary["state"] != "completed":
        raise ValueError("canary task must be completed before approval")
    canonical_review_id = _require_text(review_id, "review_id")
    canonical_reviewer = _require_text(reviewed_by, "reviewed_by")
    if canonical_reviewer.casefold() == str(canary.get("completed_by", "")).casefold():
        raise ValueError("canary approval must be independent from its generator")
    reviewed_timestamp = _timestamp(reviewed_at, "reviewed_at")
    gate = queue["canary_gates"][canary["task_id"]]
    gate.update(
        {
            "approved": True,
            "review_id": canonical_review_id,
            "reviewed_by": canonical_reviewer,
            "reviewed_at": reviewed_timestamp,
        }
    )
    queue["registry_hash"] = queue_registry_hash(queue)
    return gate


def recover_expired(queue: dict[str, Any], now: datetime | str) -> int:
    """Return every expired lease to queued state."""
    queue = _validate_queue(queue)
    current = _aware_time(now)
    recovered = 0
    for task in queue["tasks"]:
        if task["state"] != "leased" or task.get("lease_expires_at") is None:
            continue
        if _aware_time(task["lease_expires_at"], "lease_expires_at") <= current:
            task["state"] = "queued"
            task["lease_owner"] = None
            task["lease_expires_at"] = None
            task["leased_at"] = None
            task["queued_at"] = current.isoformat()
            task["updated_at"] = current.isoformat()
            task["timestamps"]["leased_at"] = None
            task["timestamps"]["queued_at"] = current.isoformat()
            task["timestamps"]["updated_at"] = current.isoformat()
            recovered += 1
    if recovered:
        _refresh_registry_hash(queue)
    return recovered


def _require_lease(task: dict[str, Any], worker: str, now: datetime) -> str:
    owner = _require_text(worker, "worker")
    if task["state"] != "leased":
        raise ValueError("task must be leased")
    if task.get("lease_owner") != owner:
        raise ValueError("task lease owner mismatch")
    expires_at = task.get("lease_expires_at")
    if expires_at is None or _aware_time(expires_at, "lease_expires_at") <= now:
        raise ValueError("task lease has expired")
    return owner


def _apply_context(
    task: dict[str, Any], cluster_id: str | None, prompt_reference_hash: str | None
) -> None:
    if cluster_id is not None:
        task["cluster_id"] = _require_text(cluster_id, "cluster_id")
    if prompt_reference_hash is not None:
        task["prompt_reference_hash"] = _require_text(
            prompt_reference_hash, "prompt_reference_hash"
        )


def complete_task(
    queue: dict[str, Any],
    task_id: str,
    worker: str,
    candidate_path: str,
    candidate_hash: str,
    now: datetime | str | None = None,
    *,
    cluster_id: str | None = None,
    prompt_reference_hash: str | None = None,
) -> dict[str, Any]:
    """Complete an owned lease and record candidate metadata."""
    queue = _validate_queue(queue)
    task = _task(queue, task_id)
    current = _aware_time(now)
    owner = _require_lease(task, worker, current)
    path = _require_text(candidate_path, "candidate_path")
    digest = _require_text(candidate_hash, "candidate_hash")
    completed_at = current.isoformat()
    _apply_context(task, cluster_id, prompt_reference_hash)
    task.update(
        {
            "state": "completed",
            "candidate_path": path,
            "candidate_hash": digest,
            "completed_at": completed_at,
            "completed_by": owner,
            "updated_at": completed_at,
            "lease_owner": None,
            "lease_expires_at": None,
        }
    )
    task["timestamps"]["completed_at"] = completed_at
    task["timestamps"]["updated_at"] = completed_at
    task["candidate"] = {"path": path, "hash": digest}
    lane = _lane_key(task.get("cluster_id"), task["task_type"])
    queue["metrics"].setdefault("lane_failure_streaks", {}).pop(lane, None)
    prompt_hash = task.get("prompt_reference_hash")
    if prompt_hash is not None:
        queue["metrics"].setdefault("prompt_failure_streaks", {}).pop(prompt_hash, None)
    _refresh_registry_hash(queue)
    return task


def fail_task(
    queue: dict[str, Any],
    task_id: str,
    worker: str,
    failure_code: str,
    failure_record_id: str,
    now: datetime | str | None = None,
    *,
    cluster_id: str | None = None,
    prompt_reference_hash: str | None = None,
) -> dict[str, Any]:
    """Fail an owned lease, record evidence, and apply lane circuit breakers."""
    queue = _validate_queue(queue)
    task = _task(queue, task_id)
    current = _aware_time(now)
    _require_lease(task, worker, current)
    code = _require_text(failure_code, "failure_code")
    record_id = _require_text(failure_record_id, "failure_record_id")
    failed_at = current.isoformat()
    _apply_context(task, cluster_id, prompt_reference_hash)
    task.update(
        {
            "state": "failed",
            "failure_code": code,
            "failure_record_id": record_id,
            "failed_at": failed_at,
            "updated_at": failed_at,
            "lease_owner": None,
            "lease_expires_at": None,
        }
    )
    task["timestamps"]["failed_at"] = failed_at
    task["timestamps"]["updated_at"] = failed_at
    task["failure"] = {
        "code": code,
        "record_id": record_id,
        "cluster_id": task.get("cluster_id"),
        "prompt_reference_hash": task.get("prompt_reference_hash"),
        "timestamp": failed_at,
    }
    retry_attempts = max(int(task.get("attempt_count", 0)) - 1, 0)
    if retry_attempts:
        retry_by_code = queue["metrics"].setdefault(
            "retry_counts_by_failure_code", {}
        )
        retry_by_code[code] = int(retry_by_code.get(code, 0)) + retry_attempts
    lane = _lane_key(task.get("cluster_id"), task["task_type"])
    lane_streaks = queue["metrics"].setdefault("lane_failure_streaks", {})
    previous = lane_streaks.get(lane, {})
    lane_count = previous.get("count", 0) + 1 if previous.get("failure_code") == code else 1
    lane_streaks[lane] = {"failure_code": code, "count": lane_count}

    prompt_count = 0
    prompt_hash = task.get("prompt_reference_hash")
    if prompt_hash is not None:
        prompt_streaks = queue["metrics"].setdefault("prompt_failure_streaks", {})
        prompt_count = int(prompt_streaks.get(prompt_hash, 0)) + 1
        prompt_streaks[prompt_hash] = prompt_count
    if lane_count >= 3 or prompt_count >= 3:
        queue["paused_lanes"].setdefault(
            lane,
            {
                "cluster_id": task.get("cluster_id"),
                "task_type": task["task_type"],
                "reason": "lane_failure_streak" if lane_count >= 3 else "prompt_failure_streak",
                "failure_code": code,
                "prompt_reference_hash": prompt_hash,
                "paused_at": failed_at,
            },
        )
    _refresh_registry_hash(queue)
    return task


def reopen_lane(
    queue: dict[str, Any],
    cluster_id: str | None,
    task_type: str,
    coordinator_review_id: str,
    now: datetime | str | None = None,
) -> bool:
    """Reopen one paused lane after an explicit coordinator review."""
    queue = _validate_queue(queue)
    review_id = _require_text(coordinator_review_id, "coordinator_review_id")
    reopened_at = _timestamp(now)
    lane = _lane_key(cluster_id, task_type)
    paused_record = queue["paused_lanes"].pop(lane, None)
    existed = paused_record is not None
    queue["metrics"].setdefault("lane_failure_streaks", {}).pop(lane, None)
    if isinstance(paused_record, Mapping):
        prompt_hash = paused_record.get("prompt_reference_hash")
        if isinstance(prompt_hash, str):
            queue["metrics"].setdefault("prompt_failure_streaks", {}).pop(
                prompt_hash, None
            )
    if existed:
        history = queue["metrics"].setdefault("lane_reopen_history", [])
        if not isinstance(history, list):
            raise ValueError("lane_reopen_history must be a list")
        history.append(
            {
                "lane": lane,
                "coordinator_review_id": review_id,
                "reopened_at": reopened_at,
            }
        )
    _refresh_registry_hash(queue)
    return existed


def make_cache_key(
    prompt_hash: str, reference_hash: str, generation_config_hash: str
) -> str:
    """Return a stable cache key for generation metadata."""
    return canonical_hash(
        {
            "prompt_hash": _require_text(prompt_hash, "prompt_hash"),
            "reference_hash": _require_text(reference_hash, "reference_hash"),
            "generation_config_hash": _require_text(
                generation_config_hash, "generation_config_hash"
            ),
        }
    )


def record_cache_access(queue: dict[str, Any], key: str, hit: bool) -> None:
    """Record cache metadata without reading or writing any image."""
    queue = _validate_queue(queue)
    cache_key = _require_text(key, "cache key")
    if not isinstance(hit, bool):
        raise ValueError("hit must be a boolean")
    entry = queue["cache"].setdefault(cache_key, {"hits": 0, "misses": 0})
    field = "hits" if hit else "misses"
    entry[field] = int(entry.get(field, 0)) + 1
    metric = "cache_hits" if hit else "cache_misses"
    queue["metrics"][metric] = int(queue["metrics"].get(metric, 0)) + 1
    _refresh_registry_hash(queue)


def queue_metrics(
    queue: dict[str, Any], now: datetime | str | None = None
) -> dict[str, Any]:
    """Compute current queue, retry, failure, lease, and cache metrics."""
    queue = _validate_queue(queue)
    current = _aware_time(now)
    depth = {state: 0 for state in ("queued", "leased", "completed", "failed")}
    for task in queue["tasks"]:
        depth[task["state"]] += 1
    lease_expiry_count = sum(
        1
        for task in queue["tasks"]
        if task["state"] == "leased"
        and task.get("lease_expires_at") is not None
        and _aware_time(task["lease_expires_at"], "lease_expires_at") <= current
    )
    completed = [task for task in queue["tasks"] if task["state"] == "completed"]
    first_pass = sum(task.get("attempt_count") == 1 for task in completed)
    terminal_count = len(completed) + depth["failed"]
    first_pass_rate = first_pass / terminal_count if terminal_count else 0.0
    retry_count = sum(max(int(task.get("attempt_count", 0)) - 1, 0) for task in queue["tasks"])
    failures = [task for task in queue["tasks"] if task["state"] == "failed"]
    failure_codes = Counter(task.get("failure_code") for task in failures)
    failure_codes.pop(None, None)
    seen_failures: set[tuple[object, object, object]] = set()
    repeated = 0
    for task in failures:
        marker = (task.get("cluster_id"), task.get("task_type"), task.get("failure_code"))
        if marker in seen_failures:
            repeated += 1
        seen_failures.add(marker)
    hits = int(queue["metrics"].get("cache_hits", 0))
    misses = int(queue["metrics"].get("cache_misses", 0))
    retry_by_code = queue["metrics"].get("retry_counts_by_failure_code", {})
    if not isinstance(retry_by_code, Mapping):
        raise ValueError("retry_counts_by_failure_code must be an object")
    accesses = hits + misses
    return {
        "depth": depth,
        "lease_expiry_count": lease_expiry_count,
        "first_pass_rate": first_pass_rate,
        "retry_count": retry_count,
        "cache_hits": hits,
        "cache_misses": misses,
        "cache_hit_rate": hits / accesses if accesses else 0.0,
        "failure_codes": dict(sorted(failure_codes.items())),
        "retry_counts_by_failure_code": dict(sorted(retry_by_code.items())),
        "repeated_failure_rate": repeated / len(failures) if failures else 0.0,
    }


def load_queue(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load and validate a queue JSON document."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid queue JSON: {path}") from exc
    return _validate_queue(data)


@contextmanager
def _exclusive_file_lock(lock_path: Path):
    """Hold an OS-level exclusive lock for one queue path."""
    with open(lock_path, "a+b") as lock_handle:
        if os.name == "nt":
            import msvcrt

            lock_handle.seek(0, os.SEEK_END)
            if lock_handle.tell() == 0:
                lock_handle.write(b"\0")
                lock_handle.flush()
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def save_queue(
    path: str | os.PathLike[str],
    queue: dict[str, Any],
    expected_revision: int | None = None,
) -> int:
    """Atomically persist a queue and return its incremented revision."""
    queue = _validate_queue(queue)
    if expected_revision is not None and (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision must be a nonnegative integer")
    target = Path(path)
    effective_expected_revision = (
        queue["revision"] if expected_revision is None else expected_revision
    )
    lock_path = target.with_name(target.name + ".lock")
    temporary: Path | None = None
    try:
        with _exclusive_file_lock(lock_path):
            disk_revision = load_queue(target)["revision"] if target.exists() else 0
            if disk_revision != effective_expected_revision:
                raise ValueError(
                    "queue revision conflict: "
                    f"expected {effective_expected_revision}, got {disk_revision}"
                )
            new_revision = disk_revision + 1
            document = {
                key: value
                for key, value in queue.items()
                if not key.startswith("_")
            }
            document["revision"] = new_revision
            document["registry_hash"] = queue_registry_hash(document)
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    prefix=f".{target.name}.",
                    suffix=".tmp",
                    dir=target.parent,
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(
                        document,
                        handle,
                        allow_nan=False,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                temporary = None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    except ValueError:
        raise
    except (OSError, TypeError) as exc:
        raise ValueError(f"failed to save queue: {target}") from exc
    queue["revision"] = new_revision
    queue["registry_hash"] = document["registry_hash"]
    return new_revision
