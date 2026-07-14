"""Evidence-driven failure learning contracts for comic continuity repair."""

from __future__ import annotations

import copy
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from pipeline_contracts import canonical_hash, normalize_page_id


SCHEMA_VERSION = "1.0"
FAILURE_CODES = frozenset(
    {
        "style_drift",
        "identity_drift",
        "costume_prop_drift",
        "composition_drift",
        "anatomy_error",
        "scene_drift",
        "text_leak",
        "over_rendering",
    }
)
RULE_SCOPES = ("page", "cluster", "project", "skill_candidate")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a nonempty string")
    result = unicodedata.normalize("NFKC", value).strip()
    if not result:
        raise ValueError(f"{name} must be a nonempty string")
    return result


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    return text.lower()


def _time(value: datetime | str | None, name: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO timestamp") from exc
    else:
        raise ValueError(f"{name} must be a timezone-aware timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _codes(values: object) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ValueError("codes must be a nonempty iterable")
    result = []
    for value in values:
        code = _text(value, "failure code")
        if code not in FAILURE_CODES:
            raise ValueError(f"unknown failure code: {code!r}")
        result.append(code)
    if not result:
        raise ValueError("codes must not be empty")
    return sorted(set(result))


def _signature(codes: list[str], corrective_action: str) -> str:
    return canonical_hash(
        {"codes": sorted(codes), "corrective_action": corrective_action}
    )


def _failure_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "page_id": record["page_id"],
        "cluster_id": record["cluster_id"],
        "character": record["character"],
        "codes": list(record["codes"]),
        "diagnosis": record["diagnosis"],
        "corrective_action": record["corrective_action"],
        "before_candidate": dict(record["before_candidate"]),
        "prompt_reference_hash": record["prompt_reference_hash"],
        "created_at": record["created_at"],
    }


def _failure_id(record: Mapping[str, Any]) -> str:
    return "failure-" + canonical_hash(_failure_identity(record))


def _semantic_key(
    scope: str,
    signature: str,
    page_id: str | None,
    cluster_id: str | None,
    character: str,
) -> str:
    return canonical_hash(
        {
            "scope": scope,
            "signature": signature,
            "page_id": page_id if scope == "page" else None,
            "cluster_id": cluster_id if scope == "cluster" else None,
            "character": character,
        }
    )


def _rule_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "semantic_key": record["semantic_key"],
        "scope": record["scope"],
        "signature": record["signature"],
        "page_id": record["page_id"],
        "cluster_id": record["cluster_id"],
        "character": record["character"],
        "codes": list(record["codes"]),
        "corrective_action": record["corrective_action"],
        "source_failure_ids": list(record["source_failure_ids"]),
        "source_outcome_ids": list(record["source_outcome_ids"]),
        "source_rule_ids": list(record["source_rule_ids"]),
        "promoted_by": record["promoted_by"],
        "promoted_at": record["promoted_at"],
    }


def _rule_id(record: Mapping[str, Any]) -> str:
    return "rule-" + canonical_hash(_rule_identity(record))


def _event_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: record[key]
        for key in (
            "event_type", "rule_id", "from", "to", "from_scope", "to_scope",
            "evidence_ids", "reviewer", "timestamp", "reason",
        )
    }


def _event_id(record: Mapping[str, Any]) -> str:
    return "event-" + canonical_hash(_event_identity(record))


def _outcome_identity(failure: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "failure_id": failure["failure_id"],
        "after_candidate": dict(failure["after_candidate"]),
        "effective": failure["effective"],
        "reviewed_by": failure["reviewed_by"],
        "reviewed_at": failure["reviewed_at"],
    }


def _outcome_id(failure: Mapping[str, Any]) -> str:
    return "outcome-" + canonical_hash(_outcome_identity(failure))


def _commit(store: dict[str, Any], working: dict[str, Any]) -> None:
    validate_failure_store(working)
    store.clear()
    store.update(working)


def _reject_nonfinite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("failure store contains a non-finite number")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_nonfinite(key)
            _reject_nonfinite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_nonfinite(item)


def _identifier_list(value: object, name: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ValueError(f"{name} must be a {'possibly empty' if allow_empty else 'nonempty'} list")
    result = []
    for item in value:
        identifier = _text(item, name)
        if identifier != item:
            raise ValueError(f"{name} values must be canonical strings")
        result.append(identifier)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} contains duplicate identifiers")
    return result


def new_failure_store() -> dict[str, Any]:
    """Return an empty durable failure-learning store."""
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "failures": [],
        "rules": [],
        "promotion_events": [],
    }


def _failure(store: Mapping[str, Any], failure_id: str) -> dict[str, Any]:
    wanted = _text(failure_id, "failure_id")
    for failure in store["failures"]:
        if failure["failure_id"] == wanted:
            return failure
    raise ValueError(f"unknown failure_id: {wanted}")


def _rule(store: Mapping[str, Any], rule_id: str) -> dict[str, Any]:
    wanted = _text(rule_id, "rule_id")
    for rule in store["rules"]:
        if rule["rule_id"] == wanted:
            return rule
    raise ValueError(f"unknown rule_id: {wanted}")


def record_failure(
    store: dict[str, Any],
    page_id: object,
    cluster_id: str,
    character: str,
    codes: Iterable[str],
    diagnosis: str,
    corrective_action: str,
    before_candidate_path: str,
    before_candidate_hash: str,
    *,
    prompt_reference_hash: str | None = None,
    created_at: datetime | str,
) -> dict[str, Any]:
    """Record immutable before-evidence for one observed failure."""
    validate_failure_store(store)
    canonical_page = normalize_page_id(page_id)
    canonical_codes = _codes(codes)
    cluster = _text(cluster_id, "cluster_id")
    subject = _text(character, "character")
    diagnosis_text = _text(diagnosis, "diagnosis")
    action = _text(corrective_action, "corrective_action")
    before_path = _text(before_candidate_path, "before_candidate_path")
    before_hash = _sha256(before_candidate_hash, "before_candidate_hash")
    prompt_hash = _optional_text(prompt_reference_hash, "prompt_reference_hash")
    created = _time(created_at, "created_at")
    immutable = {
        "page_id": canonical_page,
        "cluster_id": cluster,
        "character": subject,
        "codes": canonical_codes,
        "diagnosis": diagnosis_text,
        "corrective_action": action,
        "signature": _signature(canonical_codes, action),
        "before_candidate": {"path": before_path, "hash": before_hash},
        "prompt_reference_hash": prompt_hash,
        "created_at": created,
    }
    identifier = _failure_id(immutable)
    immutable = {"failure_id": identifier, **immutable}
    for existing in store["failures"]:
        if existing["failure_id"] != identifier:
            continue
        if any(existing.get(key) != value for key, value in immutable.items()):
            raise ValueError("failure evidence conflict")
        return existing
    failure = {
        **immutable,
        "status": "observed",
        "effective": False,
        "outcome_id": None,
        "after_candidate": {"path": None, "hash": None},
        "reviewed_by": None,
        "reviewed_at": None,
    }
    working = copy.deepcopy(store)
    working["failures"].append(failure)
    working["revision"] += 1
    _commit(store, working)
    return _failure(store, identifier)


def record_outcome(
    store: dict[str, Any],
    failure_id: str,
    after_candidate_path: str,
    after_candidate_hash: str,
    effective: bool,
    reviewed_by: str,
    reviewed_at: datetime | str,
) -> dict[str, Any]:
    """Attach independently reviewed after-evidence exactly once."""
    validate_failure_store(store)
    failure = _failure(store, failure_id)
    path = _text(after_candidate_path, "after_candidate_path")
    digest = _sha256(after_candidate_hash, "after_candidate_hash")
    if digest == failure["before_candidate"]["hash"]:
        raise ValueError("after candidate hash must be different from before evidence")
    if not isinstance(effective, bool):
        raise ValueError("effective must be a boolean")
    reviewer = _text(reviewed_by, "reviewed_by")
    reviewed = _time(reviewed_at, "reviewed_at")
    outcome = {
        "after_candidate": {"path": path, "hash": digest},
        "effective": effective,
        "reviewed_by": reviewer,
        "reviewed_at": reviewed,
    }
    if failure["status"] == "reviewed":
        if all(failure[key] == value for key, value in outcome.items()):
            return failure
        raise ValueError("failure outcome was already recorded")
    working = copy.deepcopy(store)
    updated = _failure(working, failure_id)
    updated.update(outcome)
    updated["status"] = "reviewed"
    updated["outcome_id"] = _outcome_id(updated)
    working["revision"] += 1
    _commit(store, working)
    return _failure(store, failure_id)


def _active_rules(store: Mapping[str, Any], scope: str, signature: str) -> list[dict[str, Any]]:
    return [
        rule
        for rule in store["rules"]
        if rule["scope"] == scope
        and rule["signature"] == signature
        and rule["effective"]
        and not rule["revoked"]
    ]


def _common_character(failures: list[dict[str, Any]]) -> str | None:
    characters = {failure["character"] for failure in failures}
    if len(characters) != 1:
        raise ValueError("promotion evidence must refer to one character")
    return next(iter(characters))


def _promotion_evidence(
    store: dict[str, Any], failure: dict[str, Any], scope: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, str | None]:
    signature = failure["signature"]
    if scope == "page":
        same_page = sorted(
            (
                item for item in store["failures"]
                if item["effective"]
                and item["signature"] == signature
                and item["page_id"] == failure["page_id"]
                and item["character"] == failure["character"]
            ),
            key=lambda item: item["failure_id"],
        )
        return [same_page[0]], [], failure["page_id"], same_page[0]["cluster_id"]
    if scope == "cluster":
        page_rules = sorted(
            [
            rule
            for rule in _active_rules(store, "page", signature)
            if rule["cluster_id"] == failure["cluster_id"]
            and rule["character"] == failure["character"]
            ],
            key=lambda rule: (rule["page_id"], rule["semantic_key"], rule["rule_id"]),
        )
        selected_by_page: dict[str, dict[str, Any]] = {}
        for rule in page_rules:
            selected_by_page.setdefault(rule["page_id"], rule)
        selected = list(selected_by_page.values())
        if len(selected) < 2:
            raise ValueError("cluster promotion requires two effective page rules")
        evidence_ids = {
            identifier
            for rule in selected
            for identifier in rule["source_failure_ids"]
        }
        eligible = sorted(
            (item for item in store["failures"] if item["failure_id"] in evidence_ids),
            key=lambda item: item["failure_id"],
        )
        return eligible, selected, None, failure["cluster_id"]
    if scope == "project":
        cluster_rules = sorted(
            [
                rule for rule in _active_rules(store, "cluster", signature)
                if rule["character"] == failure["character"]
            ],
            key=lambda rule: (rule["cluster_id"], rule["semantic_key"], rule["rule_id"]),
        )
        selected_by_cluster: dict[str, dict[str, Any]] = {}
        for rule in cluster_rules:
            selected_by_cluster.setdefault(rule["cluster_id"], rule)
        selected = list(selected_by_cluster.values())
        if len(selected) < 2:
            raise ValueError("project promotion requires two effective cluster rules")
        evidence_ids = {
            item for rule in selected for item in rule["source_failure_ids"]
        }
        eligible = [
            item for item in store["failures"] if item["failure_id"] in evidence_ids
        ]
        return eligible, selected, None, None
    project_rules = sorted(
        [
            rule for rule in _active_rules(store, "project", signature)
            if rule["character"] == failure["character"]
        ],
        key=lambda rule: rule["rule_id"],
    )
    if not project_rules:
        raise ValueError("skill_candidate promotion requires an effective project rule")
    evidence_ids = {
        item for rule in project_rules for item in rule["source_failure_ids"]
    }
    eligible = [item for item in store["failures"] if item["failure_id"] in evidence_ids]
    return eligible, project_rules, None, None


def promote_rule(
    store: dict[str, Any], failure_id: str, scope: str
) -> dict[str, Any]:
    """Promote effective evidence through page, cluster, project, and skill scopes."""
    validate_failure_store(store)
    target_scope = _text(scope, "scope")
    if target_scope not in RULE_SCOPES:
        raise ValueError(f"invalid promotion scope: {target_scope!r}")
    failure = _failure(store, failure_id)
    if failure["status"] != "reviewed" or not failure["effective"]:
        raise ValueError("promotion requires effective complete before/after evidence")
    working = copy.deepcopy(store)
    working_failure = _failure(working, failure_id)
    failures, source_rules, page_id, cluster_id = _promotion_evidence(
        working, working_failure, target_scope
    )
    semantic_key = _semantic_key(
        target_scope,
        working_failure["signature"],
        page_id,
        cluster_id,
        working_failure["character"],
    )
    for existing in store["rules"]:
        if existing["semantic_key"] == semantic_key:
            return existing
    source_failure_ids = sorted(item["failure_id"] for item in failures)
    source_outcome_ids = sorted(item["outcome_id"] for item in failures)
    source_rule_ids = sorted(item["rule_id"] for item in source_rules)
    rule_identity = {
        "semantic_key": semantic_key,
        "signature": working_failure["signature"],
        "scope": target_scope,
        "page_id": page_id,
        "cluster_id": cluster_id,
        "character": _common_character(failures),
        "codes": list(working_failure["codes"]),
        "corrective_action": working_failure["corrective_action"],
        "source_failure_ids": source_failure_ids,
        "source_outcome_ids": source_outcome_ids,
        "source_rule_ids": source_rule_ids,
        "promoted_by": working_failure["reviewed_by"],
        "promoted_at": working_failure["reviewed_at"],
    }
    identifier = _rule_id(rule_identity)
    rule = {
        "rule_id": identifier,
        **rule_identity,
        "effective": True,
        "revoked": False,
        "revocation_reason": None,
        "revoked_by": None,
        "revoked_at": None,
        "evidence_failure_ids": list(source_failure_ids),
        "evidence_rule_ids": list(source_rule_ids),
    }
    event_evidence = source_rule_ids or source_outcome_ids
    from_scope = "observed" if target_scope == "page" else RULE_SCOPES[RULE_SCOPES.index(target_scope) - 1]
    event = {
        "event_type": "promoted",
        "rule_id": identifier,
        "from": from_scope,
        "to": target_scope,
        "from_scope": from_scope,
        "to_scope": target_scope,
        "evidence_ids": event_evidence,
        "reviewer": working_failure["reviewed_by"],
        "timestamp": working_failure["reviewed_at"],
        "reason": None,
    }
    event = {"event_id": _event_id(event), **event}
    working["rules"].append(rule)
    working["promotion_events"].append(event)
    working["revision"] += 1
    _commit(store, working)
    return _rule(store, identifier)


def select_effective_rules(
    store: dict[str, Any],
    page_id: object | None = None,
    cluster_id: str | None = None,
    character: str | None = None,
    codes: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Return prompt-ready effective rules in most-specific-first order."""
    validate_failure_store(store)
    page = normalize_page_id(page_id) if page_id is not None else None
    cluster = _optional_text(cluster_id, "cluster_id")
    subject = _optional_text(character, "character")
    requested_codes = set(_codes(codes)) if codes is not None else None
    selected = []
    for rule in store["rules"]:
        if not rule["effective"] or rule["revoked"]:
            continue
        if rule["scope"] == "page" and rule["page_id"] != page:
            continue
        if rule["scope"] == "cluster" and rule["cluster_id"] != cluster:
            continue
        if rule["character"] is not None and rule["character"] != subject:
            continue
        if requested_codes is not None and not requested_codes.intersection(rule["codes"]):
            continue
        selected.append(
            {
                "rule_id": rule["rule_id"],
                "scope": rule["scope"],
                "codes": list(rule["codes"]),
                "corrective_action": rule["corrective_action"],
                "page_id": rule["page_id"],
                "cluster_id": rule["cluster_id"],
                "character": rule["character"],
            }
        )
    rank = {scope: index for index, scope in enumerate(RULE_SCOPES)}
    selected.sort(key=lambda rule: (rank[rule["scope"]], rule["rule_id"]))
    return selected


def revoke_rule(
    store: dict[str, Any],
    rule_id: str,
    reason: str,
    reviewed_by: str,
    reviewed_at: datetime | str,
) -> dict[str, Any]:
    """Audit and deactivate a rule without deleting its evidence."""
    validate_failure_store(store)
    rule = _rule(store, rule_id)
    reason_text = _text(reason, "reason")
    reviewer = _text(reviewed_by, "reviewed_by")
    timestamp = _time(reviewed_at, "reviewed_at")
    if rule["revoked"]:
        if (
            rule["revocation_reason"] == reason_text
            and rule["revoked_by"] == reviewer
            and rule["revoked_at"] == timestamp
        ):
            return rule
        raise ValueError("rule was already revoked")
    working = copy.deepcopy(store)
    pending = [rule["rule_id"]]
    revoked_ids: set[str] = set()
    while pending:
        current_id = pending.pop(0)
        current = _rule(working, current_id)
        if current["revoked"]:
            continue
        current.update(
            {
                "effective": False,
                "revoked": True,
                "revocation_reason": reason_text,
                "revoked_by": reviewer,
                "revoked_at": timestamp,
            }
        )
        event = {
            "event_type": "revoked",
            "rule_id": current["rule_id"],
            "from": current["scope"],
            "to": "revoked",
            "from_scope": current["scope"],
            "to_scope": "revoked",
            "evidence_ids": [current["rule_id"]],
            "reviewer": reviewer,
            "timestamp": timestamp,
            "reason": reason_text,
        }
        working["promotion_events"].append(
            {"event_id": _event_id(event), **event}
        )
        revoked_ids.add(current_id)
        dependents = sorted(
            candidate["rule_id"]
            for candidate in working["rules"]
            if candidate["effective"]
            and current_id in candidate["source_rule_ids"]
        )
        pending.extend(item for item in dependents if item not in revoked_ids)
    working["revision"] += 1
    _commit(store, working)
    return _rule(store, rule_id)


def _validate_failure_record(failure: object) -> None:
    required = {
        "failure_id", "page_id", "cluster_id", "character", "codes",
        "diagnosis", "corrective_action", "signature", "before_candidate",
        "prompt_reference_hash", "created_at", "status", "effective",
        "outcome_id", "after_candidate", "reviewed_by", "reviewed_at",
    }
    if not isinstance(failure, dict) or not required.issubset(failure):
        raise ValueError("failure record is incomplete")
    page = normalize_page_id(failure["page_id"])
    if page != failure["page_id"]:
        raise ValueError("failure page_id must be canonical")
    cluster = _text(failure["cluster_id"], "cluster_id")
    _text(failure["character"], "character")
    codes = _codes(failure["codes"])
    if codes != failure["codes"]:
        raise ValueError("failure codes must be sorted and unique")
    _text(failure["diagnosis"], "diagnosis")
    action = _text(failure["corrective_action"], "corrective_action")
    if failure["signature"] != _signature(codes, action):
        raise ValueError("failure signature mismatch")
    before = failure["before_candidate"]
    if not isinstance(before, dict) or set(before) != {"path", "hash"}:
        raise ValueError("before candidate evidence is invalid")
    _text(before["path"], "before candidate path")
    before_hash = _sha256(before["hash"], "before candidate hash")
    if failure["failure_id"] != _failure_id(failure):
        raise ValueError("failure_id does not match evidence")
    _optional_text(failure["prompt_reference_hash"], "prompt_reference_hash")
    _time(failure["created_at"], "created_at")
    if not isinstance(failure["effective"], bool):
        raise ValueError("failure effective must be a boolean")
    after = failure["after_candidate"]
    if not isinstance(after, dict) or set(after) != {"path", "hash"}:
        raise ValueError("after candidate evidence is invalid")
    if failure["status"] == "observed":
        if failure["outcome_id"] is not None or failure["effective"] or any(after.values()) or failure["reviewed_by"] is not None or failure["reviewed_at"] is not None:
            raise ValueError("observed failure must not contain outcome evidence")
    elif failure["status"] == "reviewed":
        _text(after["path"], "after candidate path")
        after_hash = _sha256(after["hash"], "after candidate hash")
        if after_hash == before_hash:
            raise ValueError("after evidence must differ from before evidence")
        _text(failure["reviewed_by"], "reviewed_by")
        _time(failure["reviewed_at"], "reviewed_at")
        if failure["outcome_id"] != _outcome_id(failure):
            raise ValueError("outcome_id does not match reviewed outcome evidence")
    else:
        raise ValueError("failure status is invalid")


def _validate_rule_record(rule: object, failures: dict[str, dict[str, Any]], rule_ids: set[str]) -> None:
    required = {
        "rule_id", "semantic_key", "signature", "scope", "page_id", "cluster_id", "character",
        "codes", "corrective_action", "effective", "revoked",
        "revocation_reason", "revoked_by", "revoked_at", "evidence_failure_ids",
        "evidence_rule_ids", "source_failure_ids", "source_outcome_ids", "source_rule_ids",
        "promoted_by", "promoted_at",
    }
    if not isinstance(rule, dict) or not required.issubset(rule):
        raise ValueError("rule record is incomplete")
    scope = rule["scope"]
    if scope not in RULE_SCOPES:
        raise ValueError("rule scope is invalid")
    codes = _codes(rule["codes"])
    if codes != rule["codes"]:
        raise ValueError("rule codes must be sorted and unique")
    action = _text(rule["corrective_action"], "corrective_action")
    if rule["signature"] != _signature(codes, action):
        raise ValueError("rule signature mismatch")
    page = normalize_page_id(rule["page_id"]) if rule["page_id"] is not None else None
    cluster = _optional_text(rule["cluster_id"], "cluster_id")
    if scope == "page" and page is None:
        raise ValueError("page rule must bind a page")
    if scope == "cluster" and cluster is None:
        raise ValueError("cluster rule must bind a cluster")
    if scope in {"project", "skill_candidate"} and (page is not None or cluster is not None):
        raise ValueError("broad rule must not bind page or cluster")
    character = _text(rule["character"], "character")
    expected_semantic_key = _semantic_key(
        scope, rule["signature"], page, cluster, character
    )
    if rule["semantic_key"] != expected_semantic_key:
        raise ValueError("rule semantic_key does not match applicability")
    evidence_failure_ids = _identifier_list(
        rule["evidence_failure_ids"], "evidence_failure_ids", allow_empty=False
    )
    evidence_rule_ids = _identifier_list(
        rule["evidence_rule_ids"], "evidence_rule_ids", allow_empty=True
    )
    source_failure_ids = _identifier_list(
        rule["source_failure_ids"], "source_failure_ids", allow_empty=False
    )
    source_outcome_ids = _identifier_list(
        rule["source_outcome_ids"], "source_outcome_ids", allow_empty=False
    )
    source_rule_ids = _identifier_list(
        rule["source_rule_ids"], "source_rule_ids", allow_empty=True
    )
    if evidence_failure_ids != source_failure_ids or evidence_rule_ids != source_rule_ids:
        raise ValueError("rule evidence aliases must match source identifiers")
    if any(item not in failures for item in evidence_failure_ids):
        raise ValueError("rule contains broken failure evidence")
    if any(item not in rule_ids for item in evidence_rule_ids):
        raise ValueError("rule contains broken rule evidence")
    if any(not failures[item]["effective"] for item in evidence_failure_ids):
        raise ValueError("rule evidence must be effective")
    expected_outcome_ids = sorted(
        failures[item]["outcome_id"] for item in source_failure_ids
    )
    if source_outcome_ids != expected_outcome_ids:
        raise ValueError("rule outcome evidence does not match source failures")
    if not isinstance(rule["effective"], bool) or not isinstance(rule["revoked"], bool):
        raise ValueError("rule state flags must be booleans")
    _text(rule["promoted_by"], "promoted_by")
    _time(rule["promoted_at"], "promoted_at")
    if rule["rule_id"] != _rule_id(rule):
        raise ValueError("rule_id does not match immutable rule content")
    if rule["revoked"]:
        if rule["effective"]:
            raise ValueError("revoked rule must not be effective")
        _text(rule["revocation_reason"], "revocation_reason")
        _text(rule["revoked_by"], "revoked_by")
        _time(rule["revoked_at"], "revoked_at")
    elif not rule["effective"] or any(rule[name] is not None for name in ("revocation_reason", "revoked_by", "revoked_at")):
        raise ValueError("active rule revocation state is invalid")


def _validate_rule_chain(
    rule: dict[str, Any],
    failures: dict[str, dict[str, Any]],
    rules: dict[str, dict[str, Any]],
) -> None:
    evidence_failures = [failures[item] for item in rule["source_failure_ids"]]
    if any(item["signature"] != rule["signature"] for item in evidence_failures):
        raise ValueError("rule failure evidence has a different signature")
    if any(item["character"] != rule["character"] for item in evidence_failures):
        raise ValueError("rule failure evidence has a different character")
    source_rules = [rules[item] for item in rule["source_rule_ids"]]
    scope = rule["scope"]
    if scope == "page":
        if source_rules or len(evidence_failures) != 1:
            raise ValueError("page rule must derive directly from one failure")
        failure = evidence_failures[0]
        if failure["page_id"] != rule["page_id"] or failure["cluster_id"] != rule["cluster_id"] or failure["character"] != rule["character"]:
            raise ValueError("page rule evidence context mismatch")
        return
    previous_scope = RULE_SCOPES[RULE_SCOPES.index(scope) - 1]
    if not source_rules or any(
        item["scope"] != previous_scope
        or item["signature"] != rule["signature"]
        or item["character"] != rule["character"]
        for item in source_rules
    ):
        raise ValueError("rule promotion chain has the wrong source scope")
    source_failure_ids = {
        identifier
        for source in source_rules
        for identifier in source["source_failure_ids"]
    }
    if source_failure_ids != set(rule["evidence_failure_ids"]):
        raise ValueError("rule promotion chain does not bind its failure evidence")
    if scope == "cluster":
        pages = {item["page_id"] for item in source_rules}
        if len(pages) < 2 or any(item["cluster_id"] != rule["cluster_id"] for item in source_rules):
            raise ValueError("cluster rule requires two page rules in one cluster")
    elif scope == "project":
        clusters = {item["cluster_id"] for item in source_rules}
        if len(clusters) < 2:
            raise ValueError("project rule requires two cluster rules")
    if any(source["revoked"] for source in source_rules) and not rule["revoked"]:
        raise ValueError("active rule depends on a revoked source rule")


def validate_failure_store(store: object) -> dict[str, Any]:
    """Deeply validate failure evidence, rules, and append-only audit events."""
    _reject_nonfinite(store)
    required = {"schema_version", "revision", "failures", "rules", "promotion_events"}
    if not isinstance(store, dict) or not required.issubset(store):
        raise ValueError("failure store is incomplete")
    if store["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported failure store schema_version")
    revision = store["revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("failure store revision must be a nonnegative integer")
    if not isinstance(store["failures"], list) or not isinstance(store["rules"], list) or not isinstance(store["promotion_events"], list):
        raise ValueError("failure store collections must be lists")
    failures: dict[str, dict[str, Any]] = {}
    for failure in store["failures"]:
        _validate_failure_record(failure)
        identifier = failure["failure_id"]
        if identifier in failures:
            raise ValueError("duplicate failure_id")
        failures[identifier] = failure
    rule_ids: set[str] = set()
    for rule in store["rules"]:
        if not isinstance(rule, dict):
            raise ValueError("rule record must be an object")
        identifier = _text(rule.get("rule_id"), "rule_id")
        if identifier in rule_ids:
            raise ValueError("duplicate rule_id")
        rule_ids.add(identifier)
    rules = {rule["rule_id"]: rule for rule in store["rules"]}
    semantic_keys: set[str] = set()
    for rule in store["rules"]:
        _validate_rule_record(rule, failures, rule_ids)
        if rule["semantic_key"] in semantic_keys:
            raise ValueError("duplicate rule semantic_key")
        semantic_keys.add(rule["semantic_key"])
    for rule in store["rules"]:
        _validate_rule_chain(rule, failures, rules)
    outcome_ids = {failure["outcome_id"] for failure in failures.values() if failure["outcome_id"] is not None}
    if len(outcome_ids) != sum(
        failure["outcome_id"] is not None for failure in failures.values()
    ):
        raise ValueError("duplicate outcome_id")
    all_evidence_ids = set(failures) | outcome_ids | rule_ids
    event_ids: set[str] = set()
    promoted_event_counts = {identifier: 0 for identifier in rule_ids}
    revoked_event_counts = {identifier: 0 for identifier in rule_ids}
    for event in store["promotion_events"]:
        required_event = {"event_id", "event_type", "rule_id", "from", "to", "from_scope", "to_scope", "evidence_ids", "reviewer", "timestamp", "reason"}
        if not isinstance(event, dict) or not required_event.issubset(event):
            raise ValueError("promotion event is incomplete")
        identifier = _text(event["event_id"], "event_id")
        if identifier in event_ids:
            raise ValueError("duplicate promotion event_id")
        event_ids.add(identifier)
        if event["event_type"] not in {"promoted", "revoked"} or event["rule_id"] not in rule_ids:
            raise ValueError("promotion event target is invalid")
        expected_event_id = _event_id(event)
        if identifier != expected_event_id:
            raise ValueError("promotion event_id does not match its identity")
        if event["from"] != event["from_scope"] or event["to"] != event["to_scope"]:
            raise ValueError("promotion event scope fields are inconsistent")
        event_evidence_ids = _identifier_list(
            event["evidence_ids"], "event evidence_ids", allow_empty=False
        )
        if any(item not in all_evidence_ids for item in event_evidence_ids):
            raise ValueError("promotion event contains broken evidence")
        _text(event["reviewer"], "event reviewer")
        _time(event["timestamp"], "event timestamp")
        if event["event_type"] == "promoted":
            if event["to_scope"] not in RULE_SCOPES or event["reason"] is not None:
                raise ValueError("promotion event scope is invalid")
            expected_from = (
                "observed"
                if event["to_scope"] == "page"
                else RULE_SCOPES[RULE_SCOPES.index(event["to_scope"]) - 1]
            )
            if event["from_scope"] != expected_from:
                raise ValueError("promotion event skips a scope")
            target_rule = rules[event["rule_id"]]
            expected_evidence = (
                target_rule["source_rule_ids"]
                or target_rule["source_outcome_ids"]
            )
            if event_evidence_ids != expected_evidence:
                raise ValueError("promotion event evidence does not match its rule")
            if (
                event["reviewer"] != target_rule["promoted_by"]
                or event["timestamp"] != target_rule["promoted_at"]
            ):
                raise ValueError("promotion event review does not match its rule")
            promoted_event_counts[event["rule_id"]] += 1
        else:
            if event["to_scope"] != "revoked" or event["from_scope"] != rules[event["rule_id"]]["scope"]:
                raise ValueError("revocation event target is invalid")
            _text(event["reason"], "revocation reason")
            target_rule = rules[event["rule_id"]]
            if event_evidence_ids != [target_rule["rule_id"]] or (
                event["reviewer"] != target_rule["revoked_by"]
                or event["timestamp"] != target_rule["revoked_at"]
                or event["reason"] != target_rule["revocation_reason"]
            ):
                raise ValueError("revocation event does not match its rule")
            revoked_event_counts[event["rule_id"]] += 1
    for identifier, rule in rules.items():
        if promoted_event_counts[identifier] != 1:
            raise ValueError("every rule must have exactly one promotion event")
        expected_revocations = 1 if rule["revoked"] else 0
        if revoked_event_counts[identifier] != expected_revocations:
            raise ValueError("rule revocation audit count is inconsistent")
    return store
