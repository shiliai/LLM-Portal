#!/usr/bin/env python3
"""Deterministic state kernel for Outcome Kernel orchestration v2."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

STATE_SCHEMA_VERSION = "opd-state-v2"
CONTRACT_SCHEMA_VERSION = "opd-contract-v3"
BRIEF_SCHEMA_VERSION = "opd-coordinator-brief-v1"
PROMPT_CONTRACT_VERSION = "opd-coordinator-prompt-v1"
DECISION_IDENTITY = (
    "Delivery coordinator accountable for the Delivery Contract, observable outcomes, "
    "evidence honesty, and recovery path."
)
EVALUATION_ORDER = [
    "intent_subject_authority",
    "observable_outcome_and_evidence",
    "proportional_risk_and_process_cost",
    "recoverability",
    "critical_path_speed",
    "maintenance_and_reuse_value",
]
MAIN_STATES = {
    "draft", "approval_pending", "first_evidence", "delivery", "convergence",
    "closeout", "amendment_pending", "blocked", "passed", "accepted_risk",
    "degraded",
}
TERMINAL_STATES = {"passed", "accepted_risk", "degraded"}
IMMUTABLE_IDENTITY_KINDS = {
    "commit", "manifest", "artifact", "file_hash", "directory_hash", "runbook_hash",
    "command_hash", "deployment_revision", "external_revision", "suite", "user_message",
}
REVIEW_DIMENSIONS = {"business", "quality_security", "docs_operations"}
MATERIAL_EVENT_TYPES = {
    "material_progress", "external_mutation", "external_failure", "checkpoint_note",
    "environment_change", "risk_change", "decision", "heartbeat_change",
    "worker_context",
}
MUTATION_COMMANDS: set[str] = set()
DELIVERY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class OpdError(RuntimeError):
    """A deterministic business rejection; callers should not retry blindly."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _state_for_hash(state: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(state)
    value.pop("state_hash", None)
    return value


def state_hash(state: dict[str, Any]) -> str:
    return content_hash(_state_for_hash(state))


def with_state_hash(state: dict[str, Any]) -> dict[str, Any]:
    state["state_hash"] = state_hash(state)
    return state


def require(condition: bool, message: str) -> None:
    if not condition:
        raise OpdError(message)


def require_keys(value: dict[str, Any], keys: set[str], context: str) -> None:
    missing = sorted(key for key in keys if key not in value)
    require(not missing, f"{context} missing required fields: {', '.join(missing)}")


def reject_secrets(value: Any, context: str) -> None:
    forbidden = {"secret", "password", "token", "api_key", "private_key"}
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            require(normalized not in forbidden, f"{context} cannot contain secret field {key}")
            reject_secrets(item, context)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item, context)


def validate_identity(identity: Any, context: str = "identity") -> dict[str, str]:
    require(isinstance(identity, dict), f"{context} must be an object")
    require(set(identity) == {"kind", "value"}, f"{context} requires only kind and value")
    kind, value = identity.get("kind"), identity.get("value")
    require(kind in IMMUTABLE_IDENTITY_KINDS, f"{context} kind is not immutable")
    require(isinstance(value, str) and value.strip(), f"{context} value is required")
    return {"kind": kind, "value": value}


def validate_evidence(evidence: Any) -> dict[str, Any]:
    require(isinstance(evidence, dict), "evidence must be an object")
    required = {
        "id", "subject", "source", "collected_at", "verification", "result",
        "valid_when", "invalid_when", "reuse_scope",
    }
    require_keys(evidence, required, "evidence")
    require(isinstance(evidence["id"], str) and evidence["id"], "evidence id is required")
    validate_identity(evidence["subject"], "evidence subject")
    for field in required - {"subject", "result"}:
        require(evidence[field] not in (None, "", []), f"evidence {field} is required")
    reject_secrets(evidence, "evidence")
    return copy.deepcopy(evidence)


def validate_contract(contract: Any, *, allow_legacy: bool = False) -> dict[str, Any]:
    require(isinstance(contract, dict), "contract must be an object")
    reject_secrets(contract, "contract")
    required = {"intent", "subjects", "authority", "outcome", "recovery", "budgets", "reporting"}
    require_keys(contract, required, "contract")
    for key in required:
        require(isinstance(contract[key], dict), f"contract.{key} must be an object")
    intent = contract["intent"]
    required_intent = {"authority_sources", "baseline_revision", "delivery_profile", "goal", "non_goals"}
    if not allow_legacy:
        required_intent.add("confirmation_mode")
    require_keys(intent, required_intent, "contract.intent")
    require(intent["delivery_profile"] in {"standard", "fast"}, "delivery_profile must be standard or fast")
    confirmation_mode = intent.get("confirmation_mode", "interactive")
    require(confirmation_mode in {"interactive", "yolo"}, "confirmation_mode must be interactive or yolo")
    require(isinstance(intent["authority_sources"], list) and intent["authority_sources"], "authority_sources are required")
    for source in intent["authority_sources"]:
        require(isinstance(source, dict) and source.get("ref") and source.get("hash"), "authority source requires ref and hash")
    require(contract["subjects"].get("allowed_paths") is not None, "subjects.allowed_paths is required")
    require(contract["subjects"].get("excluded_paths") is not None, "subjects.excluded_paths is required")
    require_keys(contract["authority"], {"local_write", "external_mutation", "destructive_boundary", "provider_constraints"}, "contract.authority")
    require_keys(contract["outcome"], {"first_evidence", "acceptance", "final_evidence"}, "contract.outcome")
    require_keys(contract["recovery"], {"restore_point", "maintenance_window", "failure_strategy"}, "contract.recovery")
    require_keys(contract["budgets"], {"critical_path_minutes", "preparation_minutes", "host_attempts", "review"}, "contract.budgets")
    critical = contract["budgets"]["critical_path_minutes"]
    prep = contract["budgets"]["preparation_minutes"]
    require(isinstance(critical, (int, float)) and critical >= 0, "critical path budget must be non-negative")
    require(isinstance(prep, (int, float)) and prep >= 0, "preparation budget must be non-negative")
    require(prep <= min(critical * 0.2, 20), "preparation budget exceeds 20%/20 minute limit")
    require_keys(contract["reporting"], {"material_only", "heartbeat_minutes"}, "contract.reporting")
    if contract["authority"]["external_mutation"]:
        targets = contract["subjects"].get("external_targets", [])
        require(targets, "external mutation authority requires at least one exact external target")
        for target in targets:
            require_keys(target, {"id", "identity", "allowed_operations"}, "external target")
            validate_identity(target["identity"], "external target identity")
            require(target["allowed_operations"] and set(target["allowed_operations"]) <= {"reversible", "destructive"}, "external target operations are invalid")
        require(len({target["id"] for target in targets}) == len(targets), "external target IDs must be unique")
    if intent["delivery_profile"] == "fast":
        require_keys(contract, {"fast"}, "fast contract")
        fast = contract["fast"]
        require_keys(fast, {"explicit_user_choice", "observable_acceptance", "isolation_boundary", "single_writer", "single_subject", "exit_conditions"}, "contract.fast")
        require(fast["explicit_user_choice"] is True, "fast profile requires explicit user choice")
        require(fast["single_writer"] is True and fast["single_subject"] is True, "fast profile requires single writer and subject")
        require(len(contract["subjects"].get("integration_subjects", [])) == 1, "fast profile requires one integration subject")
        forbidden = ["production", "sensitive_data", "shared_persistent_state", "irreversible_mutation", "direct_release"]
        if not allow_legacy:
            require_keys(fast["isolation_boundary"], set(forbidden), "contract.fast.isolation_boundary")
        for field in forbidden:
            boundary = fast["isolation_boundary"].get(field, False)
            require(isinstance(boundary, bool), f"fast isolation field {field} must be boolean")
            require(not boundary, f"fast profile forbids {field}")
    if confirmation_mode == "yolo":
        require(intent["delivery_profile"] == "fast", "yolo mode requires fast profile")
        require_keys(contract, {"yolo"}, "yolo contract")
        yolo = contract["yolo"]
        require_keys(
            yolo,
            {"explicit_user_choice", "authorization_evidence", "coordinator_recommendation", "auto_approved_artifacts", "exit_conditions"},
            "contract.yolo",
        )
        require(yolo["explicit_user_choice"] is True, "yolo mode requires explicit user choice")
        validate_identity(yolo["authorization_evidence"], "yolo authorization evidence")
        require(yolo["authorization_evidence"]["kind"] == "user_message", "yolo authorization must be user-message evidence")
        require(isinstance(yolo["coordinator_recommendation"], dict) and yolo["coordinator_recommendation"], "yolo coordinator recommendation is required")
        required_artifacts = {"user_story_baseline", "compact_design", "delivery_contract", "reversible_execution"}
        require(set(yolo["auto_approved_artifacts"]) == required_artifacts, "yolo auto-approved artifacts are incomplete")
        required_exits = {
            "production", "sensitive_data", "shared_persistent_state", "irreversible_mutation",
            "direct_release", "scope_expansion", "risk_acceptance", "locked_provider_unavailable",
        }
        require(required_exits <= set(yolo["exit_conditions"]), "yolo exit conditions are incomplete")
        require(contract["authority"]["external_mutation"] is False, "yolo mode forbids external mutation")
        require(contract["authority"]["destructive_boundary"] == "none", "yolo mode forbids destructive authority")
    return copy.deepcopy(contract)


def _new_state(delivery_id: str, task_authorizations: list[dict[str, str]] | None = None) -> dict[str, Any]:
    authorizations = task_authorizations or []
    for authorization in authorizations:
        validate_identity(authorization, "task authorization")
        require(authorization["kind"] == "user_message", "task authorization must be user-message evidence")
    require(len({canonical_bytes(item) for item in authorizations}) == len(authorizations), "task authorizations must be unique")
    return with_state_hash({
        "schema_version": STATE_SCHEMA_VERSION,
        "delivery_id": delivery_id,
        "revision": 0,
        "state": "draft",
        "durable": False,
        "contract_version": None,
        "contract_hash": None,
        "contract": None,
        "draft_contract": None,
        "task_authorizations": copy.deepcopy(authorizations),
        "pending_amendment": None,
        "resume_state": None,
        "policies": {},
        "policy_evaluated": False,
        "policy_revision": None,
        "evidence": {},
        "work_items": {},
        "claims": {},
        "workers": {},
        "review": None,
        "fast": None,
        "convergence": None,
        "e2e": {"state": "not_started", "repairs": [], "targeted_closures": []},
        "subject_chain": [],
        "blocker": None,
        "retry_authorizations": [],
        "external_actions": {},
        "host_windows": {},
        "receipt": None,
        "superseded_work_items": [],
        "last_event": "init",
    })


class Store:
    def __init__(self, project: Path, delivery_id: str):
        require(bool(DELIVERY_ID.fullmatch(delivery_id)), "invalid delivery id")
        self.project = project.resolve()
        self.delivery_id = delivery_id
        self.root = self.project / "execution" / "delivery-v2"
        self.ephemeral = self.root / ".ephemeral" / f"{delivery_id}.json"
        self.delivery = self.root / delivery_id

    def exists(self) -> bool:
        return self.delivery.is_dir() or self.ephemeral.is_file()

    def load(self) -> dict[str, Any]:
        if self.delivery.is_dir():
            return replay_delivery(self.delivery)
        else:
            path = self.ephemeral
        require(path.is_file(), f"delivery does not exist: {self.delivery_id}")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OpdError(f"cannot read delivery state: {exc}") from exc
        validate_state(state)
        return state

    def create(self, task_authorizations: list[dict[str, str]] | None = None) -> dict[str, Any]:
        require(not self.exists(), f"delivery already exists: {self.delivery_id}")
        state = _new_state(self.delivery_id, task_authorizations)
        _atomic_json(self.ephemeral, state)
        return state

    def write_ephemeral(self, state: dict[str, Any]) -> None:
        require(not self.delivery.exists(), "durable delivery already exists")
        _atomic_json(self.ephemeral, state)

    def write_durable(
        self,
        old: dict[str, Any],
        state: dict[str, Any],
        event: dict[str, Any],
        crash_at: str | None = None,
    ) -> None:
        require(self.delivery.is_dir(), "durable delivery directory is missing")
        journal = self.delivery / "journal.jsonl"
        previous = journal.read_bytes()
        event_line = canonical_bytes(event) + b"\n"
        _atomic_bytes(journal, previous + event_line)
        if crash_at == "after_journal_commit":
            raise OSError("injected durable crash after_journal_commit")
        _materialize_derived(self.delivery, state)
        replayed = replay_delivery(self.delivery)
        require(replayed["state_hash"] == state["state_hash"], "post-write replay mismatch")


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_bytes(path, canonical_bytes(value) + b"\n")


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _materialize_derived(directory: Path, state: dict[str, Any]) -> None:
    _atomic_json(directory / "snapshot.json", state)
    _atomic_json(directory / "evidence-index.json", {
        "revision": state["revision"],
        "valid": {key: item for key, item in state["evidence"].items() if item["valid"]},
    })
    for version, contract in state.get("contracts", {}).items():
        path = directory / "contract" / f"v{version}.json"
        if path.exists():
            require(json.loads(path.read_text(encoding="utf-8")) == contract, "immutable contract version changed")
        else:
            _atomic_json(path, contract)
    for item_id, item in state["work_items"].items():
        _atomic_json(directory / "work-items" / f"{item_id}.json", item)
    for lineage, worker in state["workers"].items():
        for generation in worker["generations"]:
            _atomic_json(directory / "workers" / lineage / f"generation-{generation['generation']}.json", generation)
    if state["review"] is not None:
        _atomic_json(directory / "reviews" / "standard.json", state["review"])
    if state["fast"] is not None:
        _atomic_json(directory / "reviews" / "fast.json", state["fast"])
    if state["receipt"] is not None:
        _atomic_json(directory / "receipts" / "final.json", state["receipt"])


def _event(event_type: str, old: dict[str, Any], state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_schema_version": STATE_SCHEMA_VERSION,
        "type": event_type,
        "from_revision": old["revision"],
        "revision": state["revision"],
        "payload": copy.deepcopy(payload),
        "state": copy.deepcopy(state),
        "state_hash": state["state_hash"],
    }


def mutate(
    store: Store,
    expected_revision: int,
    event_type: str,
    payload: dict[str, Any],
    change: Callable[[dict[str, Any]], None],
    *,
    dry_run: bool = False,
    crash_at: str | None = None,
) -> dict[str, Any]:
    old = store.load()
    require(old["state"] not in TERMINAL_STATES, "terminal delivery is immutable")
    require(expected_revision == old["revision"], f"expected revision {expected_revision}, found {old['revision']}")
    state = copy.deepcopy(old)
    change(state)
    state["revision"] = old["revision"] + 1
    state["last_event"] = event_type
    with_state_hash(state)
    validate_state(state)
    event = _event(event_type, old, state, payload)
    if not dry_run:
        if old["durable"]:
            store.write_durable(old, state, event, crash_at)
        else:
            store.write_ephemeral(state)
    return state


def replay_delivery(directory: Path) -> dict[str, Any]:
    journal = directory / "journal.jsonl"
    require(journal.is_file(), "journal.jsonl is missing")
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    require(events and events[0]["type"] == "journal_bootstrap", "journal must begin with journal_bootstrap")
    first = events[0]
    pre = first["bootstrap_state"]
    require(state_hash(pre) == first["from_hash"], "journal bootstrap from_hash mismatch")
    state = first["state"]
    require(state["revision"] == pre["revision"] + 1, "journal activation revision mismatch")
    require(state_hash(state) == first["to_hash"] == state["state_hash"], "journal bootstrap to_hash mismatch")
    seen_activation = {first["activation_id"]}
    previous_revision = state["revision"]
    for event in events[1:]:
        require(event["type"] != "journal_bootstrap", "duplicate journal activation")
        require(event["from_revision"] == previous_revision, "journal revision gap")
        require(event["revision"] == previous_revision + 1, "journal revision must increase by one")
        state = event["state"]
        require(state["revision"] == event["revision"], "event state revision mismatch")
        require(state_hash(state) == event["state_hash"] == state["state_hash"], "event state hash mismatch")
        previous_revision = event["revision"]
    require(len(seen_activation) == 1, "journal must have one activation id")
    validate_state(state)
    return state


def validate_delivery_files(directory: Path, state: dict[str, Any]) -> None:
    replayed = replay_delivery(directory)
    require(replayed["state_hash"] == state["state_hash"], "snapshot does not match journal replay")
    for version, contract in state.get("contracts", {}).items():
        path = directory / "contract" / f"v{version}.json"
        require(path.is_file(), f"contract v{version} file is missing")
        require(json.loads(path.read_text(encoding="utf-8")) == contract, f"contract v{version} file mismatch")
    index = json.loads((directory / "evidence-index.json").read_text(encoding="utf-8"))
    expected_valid = {key: item for key, item in state["evidence"].items() if item["valid"]}
    require(index == {"revision": state["revision"], "valid": expected_valid}, "evidence index mismatch")
    for item_id, item in state["work_items"].items():
        path = directory / "work-items" / f"{item_id}.json"
        require(path.is_file() and json.loads(path.read_text(encoding="utf-8")) == item, f"work item file mismatch: {item_id}")
    for lineage, worker in state["workers"].items():
        for generation in worker["generations"]:
            path = directory / "workers" / lineage / f"generation-{generation['generation']}.json"
            require(path.is_file() and json.loads(path.read_text(encoding="utf-8")) == generation, f"worker generation file mismatch: {lineage}")


def derived_files_consistent(directory: Path, state: dict[str, Any]) -> bool:
    try:
        validate_delivery_files(directory, state)
    except (OpdError, OSError, json.JSONDecodeError):
        return False
    return True


def materialize_replayed_state(directory: Path, state: dict[str, Any]) -> None:
    require(replay_delivery(directory)["state_hash"] == state["state_hash"], "cannot materialize a non-authoritative state")
    _materialize_derived(directory, state)
    validate_delivery_files(directory, state)


def activate_journal(store: Store, expected_revision: int, trigger: str, activation_id: str | None = None, crash_at: str | None = None) -> dict[str, Any]:
    if store.delivery.is_dir():
        replayed = replay_delivery(store.delivery)
        first = json.loads((store.delivery / "journal.jsonl").read_text(encoding="utf-8").splitlines()[0])
        require(activation_id and activation_id == first["activation_id"], "activation identity does not match committed Journal")
        require(expected_revision == first["from_revision"], "activation expected revision does not match committed Journal")
        require(first["to_hash"] == replayed["state_hash"] if replayed["revision"] == first["revision"] else True, "activation target hash mismatch")
        return replayed
    old = store.load()
    require(expected_revision == old["revision"], f"expected revision {expected_revision}, found {old['revision']}")
    require(old["state"] not in TERMINAL_STATES, "terminal delivery cannot activate journal")
    require(old["policy_evaluated"], "persistence policy has not been evaluated")
    require(trigger in old["policies"].get("journal_triggers", []), "persistence policy trigger is not satisfied")
    validate_state(old)
    activation_id = activation_id or str(uuid.uuid4())
    target = copy.deepcopy(old)
    target["durable"] = True
    target["revision"] += 1
    target["last_event"] = "journal_bootstrap"
    with_state_hash(target)
    bootstrap = {
        "event_schema_version": STATE_SCHEMA_VERSION,
        "type": "journal_bootstrap",
        "activation_id": activation_id,
        "from_revision": old["revision"],
        "revision": target["revision"],
        "from_hash": state_hash(old),
        "to_hash": target["state_hash"],
        "valid_evidence_refs": sorted(key for key, item in old["evidence"].items() if item["valid"]),
        "bootstrap_state": old,
        "state": target,
    }
    store.root.mkdir(parents=True, exist_ok=True)
    temporary = store.root / f".{store.delivery_id}.activate-{activation_id}"
    require(not temporary.exists(), "activation temporary directory already exists")
    try:
        temporary.mkdir()
        if crash_at == "after_mkdir":
            raise OSError("injected activation crash after_mkdir")
        _atomic_bytes(temporary / "journal.jsonl", canonical_bytes(bootstrap) + b"\n")
        _materialize_derived(temporary, target)
        if crash_at == "before_replay":
            raise OSError("injected activation crash before_replay")
        require(replay_delivery(temporary)["state_hash"] == target["state_hash"], "activation replay mismatch")
        _fsync_dir(temporary)
        if crash_at == "before_rename":
            raise OSError("injected activation crash before_rename")
        os.rename(temporary, store.delivery)
        if crash_at == "after_rename":
            raise OSError("injected activation crash after_rename")
        _fsync_dir(store.root)
        store.ephemeral.unlink(missing_ok=True)
        return target
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def _active_claims_for_subject(state: dict[str, Any], subject: dict[str, str]) -> list[dict[str, Any]]:
    return [claim for claim in state["claims"].values() if claim["active"] and claim["subject"] == subject]


def _profile(state: dict[str, Any]) -> str | None:
    return state["contract"]["intent"]["delivery_profile"] if state["contract"] else None


def contract_change_invalidates_evidence(old: dict[str, Any], new: dict[str, Any]) -> bool:
    old_material = copy.deepcopy(old)
    new_material = copy.deepcopy(new)
    for value in (old_material, new_material):
        value.pop("reporting", None)
        value.pop("accepted_risks", None)
    return old_material != new_material


def reset_for_material_amendment(state: dict[str, Any], old_contract_version: int) -> None:
    for item in state["evidence"].values():
        item["valid"] = False
        item["invalidated_by"] = "contract_amendment"
    for claim in state["claims"].values():
        claim["active"] = False
    for worker in state["workers"].values():
        for generation in worker["generations"]:
            generation["active"] = False
    state["superseded_work_items"].append({
        "contract_version": old_contract_version,
        "work_items": copy.deepcopy(state["work_items"]),
        "workers": copy.deepcopy(state["workers"]),
        "review": copy.deepcopy(state["review"]),
        "fast": copy.deepcopy(state["fast"]),
        "convergence": copy.deepcopy(state["convergence"]),
        "e2e": copy.deepcopy(state["e2e"]),
        "subject_chain": copy.deepcopy(state["subject_chain"]),
    })
    state["work_items"] = {}
    state["claims"] = {}
    state["workers"] = {}
    state["review"] = None
    state["fast"] = None
    state["convergence"] = None
    state["e2e"] = {"state": "not_started", "repairs": [], "targeted_closures": []}
    state["subject_chain"] = []
    state["receipt"] = None
    state.pop("pending_disposition", None)
    state["policies"] = {}
    state["policy_evaluated"] = False
    state["policy_revision"] = None


def validate_state(state: Any) -> None:
    require(isinstance(state, dict), "snapshot must be an object")
    require(state.get("schema_version") == STATE_SCHEMA_VERSION, "unknown state schema version")
    require(state.get("state") in MAIN_STATES, "unknown main state")
    require(isinstance(state.get("revision"), int) and state["revision"] >= 0, "invalid revision")
    require(state.get("state_hash") == state_hash(state), "snapshot state hash mismatch")
    require(isinstance(state.get("task_authorizations", []), list), "task authorizations must be a list")
    for authorization in state.get("task_authorizations", []):
        validate_identity(authorization, "task authorization")
        require(authorization["kind"] == "user_message", "task authorization must be user-message evidence")
    if state["contract"] is not None:
        validate_contract(state["contract"], allow_legacy=True)
        require(state["contract_hash"] == content_hash(state["contract"]), "contract hash mismatch")
        require(state["contract_version"] >= 1, "contract version is invalid")
    if state["policy_evaluated"]:
        require(state["policies"] and state["policy_revision"] is not None, "activated policy decision is incomplete")
        require(state["policy_revision"] <= state["revision"], "policy revision cannot be in the future")
    subjects: dict[str, str] = {}
    active_lineages: set[tuple[str, str]] = set()
    for item_id, claim in state["claims"].items():
        require(item_id in state["work_items"], "claim references unknown work item")
        if claim["active"]:
            key = canonical_bytes(claim["subject"]).decode()
            require(key not in subjects, "subject has more than one active writer")
            subjects[key] = item_id
            pair = (claim["lineage_id"], str(claim["generation"]))
            require(pair not in active_lineages, "worker generation has overlapping claims")
            active_lineages.add(pair)
    for lineage, worker in state["workers"].items():
        require(worker["work_item_id"] in state["work_items"], f"worker lineage references unknown Work Item: {lineage}")
        require(worker["correction_rounds"] >= 0 and worker["compactions"] >= 0, "worker context counters cannot be negative")
        require(len({generation["session"] for generation in worker["generations"]}) == len(worker["generations"]), "worker generation sessions must be unique")
    for evidence in state["evidence"].values():
        validate_evidence(evidence["record"])
    if _profile(state) == "fast":
        require(len(state["work_items"]) <= 1, "fast profile allows one work item")
        require(len(state["workers"]) <= 1, "fast profile allows one writer lineage")
        require(state["review"] is None, "fast profile cannot create standard review campaign")
        if state["fast"]:
            require(state["fast"]["review_count"] <= 1, "fast review budget exceeded")
            require(state["fast"]["fix_batch_count"] <= 1, "fast fix budget exceeded")
            require(state["fast"]["closure_count"] == 0, "fast closure is forbidden")
    if state["review"]:
        campaign = state["review"]
        require(campaign["state"] in {"collecting", "plan_pending", "plan_approved", "fix_pending", "closure_pending", "fixed", "passed", "stopped"}, "invalid review campaign state")
        require(campaign["plan_revision"] <= 1, "review plan revision budget exceeded")
        if campaign["state"] == "fixed":
            require(campaign.get("fix_subject"), "fixed review requires a fix subject")
            require(campaign.get("fix_record"), "fixed review requires deterministic fix evidence")
            fix_record = campaign["fix_record"]
            require(
                fix_record.get("from") == campaign["subject"]
                and fix_record.get("to") == campaign["fix_subject"],
                "fixed review subject chain is inconsistent",
            )
            findings = {finding["id"] for finding in campaign.get("findings") or []}
            mappings = fix_record.get("finding_mappings")
            require(isinstance(mappings, list), "fixed review finding mappings are invalid")
            require(
                len(mappings) == len(findings)
                and {mapping.get("finding_id") for mapping in mappings} == findings,
                "fixed review finding mappings are incomplete",
            )
            for mapping in mappings:
                require(mapping.get("change_refs"), "fixed review finding mapping requires changes")
                _passed_refs_for_subject(state, mapping.get("evidence_refs", []), campaign["fix_subject"])
    if state["convergence"] and state["subject_chain"]:
        previous = state["subject_chain"][0]["to"]
        for edge in state["subject_chain"][1:]:
            require(edge.get("from") == previous, "subject chain is discontinuous")
            previous = edge.get("to")
        require(previous == state["convergence"]["subject"], "subject chain does not end at convergence subject")
    if state["e2e"]["state"] != "not_started":
        require(state["e2e"].get("subject") == state["convergence"]["subject"], "E2E subject must match convergence subject")
    if state["e2e"]["state"] == "passed":
        passed_ref = state["e2e"].get("passed_evidence_ref")
        _passed_refs_for_subject(state, [passed_ref] if passed_ref else [], state["e2e"]["subject"])
    if state["e2e"]["state"] == "recheck_pending" and state["e2e"].get("p0_required"):
        require(state["e2e"]["targeted_closures"] and state["e2e"]["targeted_closures"][-1]["passed"], "P0 repair requires deterministic P0 evidence")


def _add_evidence(state: dict[str, Any], evidence: dict[str, Any]) -> None:
    evidence = validate_evidence(evidence)
    require(evidence["id"] not in state["evidence"], "evidence id already exists")
    state["evidence"][evidence["id"]] = {"record": evidence, "valid": True, "invalidated_by": None}


def _invalidate(state: dict[str, Any], refs: list[str], reason: str) -> None:
    require(refs, "invalidation refs are required")
    for ref in refs:
        require(ref in state["evidence"] and state["evidence"][ref]["valid"], f"evidence is not valid: {ref}")
        state["evidence"][ref]["valid"] = False
        state["evidence"][ref]["invalidated_by"] = reason


def _work_item(state: dict[str, Any], item_id: str) -> dict[str, Any]:
    require(item_id in state["work_items"], f"unknown work item: {item_id}")
    return state["work_items"][item_id]


def _fast_lineage(state: dict[str, Any]) -> str:
    require(_profile(state) == "fast" and len(state["work_items"]) == 1, "operation requires the Fast Work Item")
    lineage = next(iter(state["work_items"].values())).get("worker_lineage_id")
    require(bool(lineage), "Fast Work Item has no writer lineage")
    return lineage


def _integration_owner(state: dict[str, Any]) -> str:
    convergence = state.get("convergence") or {}
    lineage = convergence.get("integration_owner")
    require(lineage in state["workers"], "convergence integration owner is missing")
    return lineage


def _require_active_lineage(state: dict[str, Any], lineage: str) -> None:
    require(any(claim["active"] and claim["lineage_id"] == lineage for claim in state["claims"].values()), "original worker lineage must have an active claim")


def _valid_refs(state: dict[str, Any], refs: list[str]) -> None:
    require(refs, "evidence refs are required")
    for ref in refs:
        require(ref in state["evidence"] and state["evidence"][ref]["valid"], f"invalid evidence ref: {ref}")


def _refs_for_subject(state: dict[str, Any], refs: list[str], subject: dict[str, str]) -> None:
    _valid_refs(state, refs)
    for ref in refs:
        require(state["evidence"][ref]["record"]["subject"] == subject, f"evidence subject mismatch: {ref}")


def _passed_refs_for_subject(state: dict[str, Any], refs: list[str], subject: dict[str, str]) -> None:
    _refs_for_subject(state, refs, subject)
    for ref in refs:
        result = state["evidence"][ref]["record"]["result"]
        passed = result is True or (
            isinstance(result, str) and result.lower() in {"passed", "pass", "succeeded", "success"}
        ) or (
            isinstance(result, dict)
            and (
                result.get("passed") is True
                or str(result.get("conclusion", "")).lower() in {"passed", "pass", "succeeded", "success"}
            )
        )
        require(passed, f"evidence result is not passed: {ref}")


def _required_story_ids(state: dict[str, Any]) -> list[str]:
    return sorted({story_id for item in state["work_items"].values() for story_id in item["story_ids"]})


def _journal_activation_required(state: dict[str, Any]) -> bool:
    return (
        state["state"] in {"first_evidence", "delivery"}
        and state["policy_evaluated"]
        and state["policies"].get("durable_required") is True
        and not state["durable"]
    )


def validate_alignment(
    state: dict[str, Any],
    alignment: Any,
    required_story_ids: list[str],
    subject: dict[str, str],
    *,
    passed_gate: bool,
) -> dict[str, Any]:
    require(isinstance(alignment, dict), "alignment must be an object")
    require_keys(alignment, {"baseline_revision", "stories", "drift_score", "accepted_deviation"}, "alignment")
    require(alignment["baseline_revision"] == state["contract"]["intent"]["baseline_revision"], "alignment baseline revision mismatch")
    require(isinstance(alignment["stories"], list), "alignment stories must be a list")
    require(all(isinstance(story, dict) for story in alignment["stories"]), "alignment stories must contain objects")
    story_ids = [story.get("id") for story in alignment["stories"]]
    require(all(isinstance(story_id, str) and story_id for story_id in story_ids), "alignment story ID is required")
    require(len(story_ids) == len(set(story_ids)), "alignment story IDs must be unique")
    require(set(story_ids) == set(required_story_ids), "alignment story IDs mismatch")
    drift_score = alignment["drift_score"]
    require(type(drift_score) is int, "alignment drift score must be an integer")
    require(drift_score >= 0, "alignment drift score cannot be negative")
    incomplete: list[str] = []
    for story in alignment["stories"]:
        require_keys(story, {"id", "role", "goal", "value", "given", "when", "then", "evidence_refs", "status"}, "alignment story")
        for field in ("role", "goal", "value", "given", "when", "then"):
            require(isinstance(story[field], str) and story[field].strip(), f"alignment story {field} is required")
        require(story["status"] in {"Complete", "Partial", "Missing"}, "alignment story status is invalid")
        _refs_for_subject(state, story["evidence_refs"], subject)
        if story["status"] != "Complete":
            incomplete.append(story["id"])
    accepted = alignment["accepted_deviation"]
    if incomplete or drift_score != 0:
        require(isinstance(accepted, dict), "alignment drift requires exact accepted deviation")
        require_keys(accepted, {"authorized_by", "evidence", "story_ids", "details"}, "accepted deviation")
        for field in ("authorized_by", "evidence", "details"):
            require(isinstance(accepted[field], str) and accepted[field].strip(), f"accepted deviation {field} is required")
        accepted_story_ids = accepted["story_ids"]
        require(isinstance(accepted_story_ids, list), "accepted deviation story_ids must be a list")
        require(all(isinstance(story_id, str) and story_id for story_id in accepted_story_ids), "accepted deviation story ID is required")
        require(set(accepted_story_ids) == set(incomplete or required_story_ids), "accepted deviation story IDs mismatch")
    else:
        require(accepted is None, "drift-zero alignment cannot carry accepted deviation")
    if passed_gate:
        require(not incomplete and drift_score == 0, "passed requires all stories Complete with drift zero")
    return copy.deepcopy(alignment)


def _validate_actual_provider(contract: dict[str, Any], actual: dict[str, Any]) -> None:
    constraints = contract["authority"]["provider_constraints"]
    if not constraints.get("locked"):
        return
    require(actual.get("provider") == constraints.get("provider"), "locked provider mismatch requires amendment")
    if constraints.get("model"):
        require(actual.get("model") == constraints["model"], "locked model mismatch requires amendment")


def _standard_ready_for_closeout(state: dict[str, Any]) -> None:
    if state["policies"].get("review_required"):
        require(state["review"] and state["review"]["state"] in {"fixed", "passed"}, "required review fixes have not been recorded")
    require(state["e2e"]["state"] == "passed", "final subject E2E has not passed")


def exact_next_action(state: dict[str, Any]) -> str:
    main = state["state"]
    if main == "draft": return "submit-contract"
    if main == "approval_pending": return "freeze-contract"
    if _journal_activation_required(state): return "activate-journal"
    if main == "first_evidence": return "evaluate-policies" if not state["policies"] else "record-first-evidence"
    if main == "amendment_pending":
        pending = state.get("pending_amendment") or {}
        return "approve-amendment" if isinstance(pending.get("contract"), dict) else "amend-contract"
    if main == "blocked": return "authorize-retry"
    if main == "delivery":
        active = [key for key, item in state["work_items"].items() if item["state"] == "active"]
        if active: return f"record-work-item-evidence:{min(active)}"
        claimable = [key for key, item in state["work_items"].items() if item["state"] == "claimable"]
        if claimable: return f"claim:{min(claimable)}"
        return "begin-convergence"
    if main == "convergence":
        if _profile(state) == "fast":
            sub = state["fast"]["state"]
            return {
                "candidate_ready": "record-fast-check", "checked": "start-fast-review",
                "review_pending": "record-fast-review", "findings_frozen": "begin-fast-fix" if state["fast"]["blocking_findings"] else "begin-fast-final-verification",
                "fix_pending": "record-fast-fix", "final_verification": "record-fast-final-verification",
                "verified": "integrate-work-item", "failed": "begin-closeout",
            }[sub]
        if state["policies"].get("review_required") and state["review"] is None: return "start-review"
        if state["review"]:
            review_state = state["review"]["state"]
            if review_state in {"fix_pending", "closure_pending"}:
                return "record-fix"
            if review_state not in {"fixed", "passed"}:
                return f"review:{review_state}"
        if state["e2e"]["state"] in {"not_started", "recheck_pending"}: return "start-e2e"
        if state["e2e"]["state"] == "running": return "record-e2e"
        if state["e2e"]["state"] == "failed": return "begin-e2e-fix"
        if state["e2e"]["state"] == "fix_pending": return "record-e2e-fix"
        if state["e2e"]["state"] == "p0_closure_pending": return "record-p0-evidence"
        return "begin-closeout"
    if main == "closeout": return "closeout"
    return "none"


def build_coordinator_brief(state: dict[str, Any], environment_facts: dict[str, Any] | None = None) -> dict[str, Any]:
    validate_state(state)
    reject_secrets(environment_facts or {}, "environment facts")
    capsules = []
    for lineage, worker in sorted(state["workers"].items()):
        current = worker["generations"][-1]
        capsules.append({"lineage_id": lineage, "generation": current["generation"], "capsule": current["capsule"]})
    brief = {
        "brief_schema_version": BRIEF_SCHEMA_VERSION,
        "decision_identity": DECISION_IDENTITY,
        "objective": state["contract"]["intent"]["goal"] if state["contract"] else None,
        "contract_ref": {"version": state["contract_version"], "hash": state["contract_hash"]},
        "current_state": {"delivery_id": state["delivery_id"], "revision": state["revision"], "state": state["state"], "profile": _profile(state), "substate": state["fast"]["state"] if state["fast"] else None},
        "valid_evidence_refs": sorted(key for key, item in state["evidence"].items() if item["valid"]),
        "active_worker_capsules": capsules,
        "environment_facts": environment_facts or {},
        "authority_and_constraints": state["contract"]["authority"] if state["contract"] else {},
        "evaluation_order": EVALUATION_ORDER,
        "exact_next_decision": exact_next_action(state),
        "output_contract": ["decision", "evidence_refs", "assumptions_and_unknowns", "actions", "state_command", "material_user_update"],
    }
    require("transcript" not in canonical_bytes(brief).decode().lower(), "brief cannot include transcript")
    return {"brief": brief, "brief_hash": content_hash(brief)}


def validate_decision_proposal(state: dict[str, Any], proposal: Any) -> dict[str, Any]:
    required = {"decision", "evidence_refs", "assumptions_and_unknowns", "actions", "state_command", "material_user_update"}
    require(isinstance(proposal, dict) and set(proposal) == required, "decision proposal schema mismatch")
    require(isinstance(proposal["evidence_refs"], list), "evidence_refs must be a list")
    for ref in proposal["evidence_refs"]:
        require(ref in state["evidence"] and state["evidence"][ref]["valid"], f"proposal references invalid evidence: {ref}")
    command = proposal["state_command"]
    require(isinstance(command, dict), "state_command must be an object")
    require(set(command) <= {"command", "expected_revision", "data"} and {"command", "expected_revision"} <= set(command), "state_command schema mismatch")
    require(command["command"] in MUTATION_COMMANDS | {"activate-journal"}, "unknown or read-only state command")
    require(command["expected_revision"] == state["revision"], "proposal expected revision mismatch")
    if command["command"] == "activate-journal":
        require(not state["durable"] and state["state"] not in TERMINAL_STATES, "journal activation is not legal")
        trigger = command.get("data", {}).get("trigger")
        require(state["policy_evaluated"] and trigger in state["policies"].get("journal_triggers", []), "persistence policy trigger is not satisfied")
    else:
        candidate = copy.deepcopy(state)
        command_change(command["command"], command.get("data", {}))(candidate)
        candidate["revision"] += 1
        candidate["last_event"] = command["command"]
        with_state_hash(candidate)
        validate_state(candidate)
    if proposal["decision"] in {"external_wait", "no_state_change"}:
        require(not proposal["material_user_update"], "unchanged waits cannot produce a user update")
    return {"valid": True, "command": copy.deepcopy(command)}


def _policy_decisions(contract: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    profile = contract["intent"]["delivery_profile"]
    integration_subjects = facts.get("integration_subjects", contract["subjects"].get("integration_subjects", []))
    durable_reasons = []
    if contract["authority"]["external_mutation"]: durable_reasons.append("external_mutation")
    if facts.get("multi_agent"): durable_reasons.append("multi_agent")
    if contract["budgets"]["critical_path_minutes"] > 30: durable_reasons.append("over_30_minutes")
    if len(contract["subjects"].get("repos", [])) > 1 or len(integration_subjects) > 1: durable_reasons.append("cross_repo")
    triggers = {
        "p0", "identity", "approval", "security", "credentials", "protected_data",
        "irreversible_mutation", "cross_service_contract", "rollback_boundary_change",
        "explicit_broad_review",
    }
    active = sorted(key for key in triggers if facts.get(key))
    if profile == "fast":
        exits = [key for key in ["production", "sensitive_data", "shared_persistent_state", "irreversible_mutation", "direct_release", "second_writer_lineage", "overlapping_generation", "multiple_integration_subjects"] if facts.get(key)]
        if active:
            exits.extend(f"standard_review:{reason}" for reason in active)
        if contract["intent"].get("confirmation_mode", "interactive") == "yolo":
            exits.extend(
                key for key in ["scope_expansion", "risk_acceptance", "locked_provider_unavailable"]
                if facts.get(key)
            )
    frozen_target = facts.get("frozen_pr_target")
    requested_target = facts.get("requested_pr_target")
    if frozen_target and requested_target:
        require(frozen_target == requested_target, "requested PR target differs from frozen Contract target")
    if facts.get("actual_provider") or facts.get("actual_model"):
        _validate_actual_provider(contract, {"provider": facts.get("actual_provider"), "model": facts.get("actual_model")})
    route = facts.get("execution_route", "direct_command")
    require(route in {"direct_command", "runbook", "thin_script", "full_automation"}, "invalid execution route")
    preparation_spent = facts.get("preparation_spent_minutes", 0)
    preparation_limit = min(contract["budgets"]["critical_path_minutes"] * 0.2, 20)
    require(
        not (facts.get("expand_preparation") and preparation_spent >= preparation_limit),
        "preparation budget exhausted; reduce, reuse, or reroute",
    )
    journal_triggers = list(durable_reasons)
    if active or profile == "fast":
        journal_triggers.append("multi_agent_review")
    return {
        "execution_route": route,
        "durable_required": bool(durable_reasons),
        "durable_reasons": durable_reasons,
        "journal_triggers": sorted(set(journal_triggers)),
        "review_required": bool(active),
        "review_reasons": active,
        "preparation_limit_minutes": preparation_limit,
        "first_evidence_route": facts.get("first_evidence_route", contract["outcome"]["first_evidence"]),
        "profile_exit_reasons": exits if profile == "fast" else [],
    }


def command_change(command: str, payload: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def change(state: dict[str, Any]) -> None:
        profile = _profile(state)
        if command == "submit-contract":
            require(state["state"] == "draft", "submit-contract requires draft")
            state["draft_contract"] = validate_contract(payload["contract"])
            state["state"] = "approval_pending"
        elif command == "freeze-contract":
            require(state["state"] == "approval_pending", "freeze-contract requires approval_pending")
            contract = state["draft_contract"]
            if contract["intent"].get("confirmation_mode", "interactive") == "yolo":
                require(
                    payload.get("authorization_evidence") == contract["yolo"]["authorization_evidence"],
                    "yolo freeze requires the exact task-scoped authorization evidence",
                )
                require(
                    payload["authorization_evidence"] in state["task_authorizations"],
                    "yolo authorization was not supplied by the task boundary",
                )
            else:
                require(payload.get("approval_evidence"), "approval evidence is required")
            state["contract_version"] = 1
            state["contract"] = contract
            state["contract_hash"] = content_hash(contract)
            state["contracts"] = {"1": contract}
            state["draft_contract"] = None
            state["state"] = "first_evidence"
        elif command == "evaluate-policies":
            require(state["state"] == "first_evidence", "evaluate-policies requires first_evidence")
            require(not state["policy_evaluated"], "policy decisions are already activated and immutable")
            state["policies"] = _policy_decisions(state["contract"], payload.get("facts", {}))
            state["policy_evaluated"] = True
            state["policy_revision"] = state["revision"] + 1
            if state["policies"].get("profile_exit_reasons"):
                state["resume_state"] = "first_evidence"
                state["pending_amendment"] = {
                    "type": "profile_exit",
                    "reasons": state["policies"]["profile_exit_reasons"],
                }
                state["state"] = "amendment_pending"
        elif command == "record-safety-facts":
            require(state["policy_evaluated"], "safety facts require evaluated policies")
            require(_profile(state) == "fast", "safety facts exit is only defined for fast delivery")
            facts = payload.get("facts")
            require(isinstance(facts, dict) and facts, "safety facts are required")
            exits = _policy_decisions(state["contract"], facts)["profile_exit_reasons"]
            require(exits, "safety facts do not require a profile exit")
            state["resume_state"] = state["state"]
            state["state"] = "amendment_pending"
            state["pending_amendment"] = {"type": "profile_exit", "reasons": exits, "facts": copy.deepcopy(facts)}
        elif command == "record-first-evidence":
            require(state["state"] == "first_evidence", "record-first-evidence requires first_evidence")
            require(state["policy_evaluated"], "record-first-evidence requires activated policy decisions")
            require(not _journal_activation_required(state), "activate-journal is required before delivery or worker dispatch")
            _add_evidence(state, payload["evidence"])
            state["state"] = "delivery"
        elif command == "record-evidence":
            require(state["contract"] is not None and state["state"] in {"first_evidence", "delivery", "convergence"}, "record-evidence requires a frozen Contract and active delivery phase")
            candidate = payload["evidence"]
            manifest = payload.get("full_artifact_manifest")
            if manifest:
                validate_identity(manifest, "artifact manifest")
                reusable = [
                    key
                    for key, item in state["evidence"].items()
                    if item["valid"]
                    and item["record"].get("full_artifact_manifest") == manifest
                ]
                if reusable:
                    raise OpdError(f"full artifact evidence remains reusable: {reusable[0]}")
                candidate = copy.deepcopy(candidate)
                candidate["full_artifact_manifest"] = manifest
            _add_evidence(state, candidate)
        elif command == "invalidate-evidence":
            require(state["contract"] is not None and state["state"] in {"first_evidence", "delivery", "convergence"}, "invalidate-evidence requires a frozen Contract and active delivery phase")
            _invalidate(state, payload["refs"], payload["reason"])
        elif command == "add-work-item":
            require(state["state"] == "delivery", "add-work-item requires delivery")
            require(state["policy_evaluated"], "add-work-item requires activated policy decisions")
            item = copy.deepcopy(payload["work_item"])
            required = {"id", "subject", "start_subject", "allowed_paths", "excluded_paths", "story_ids", "acceptance", "mutation_authority", "exact_next_action", "dependencies"}
            require_keys(item, required, "work item")
            require(item["id"] not in state["work_items"], "work item already exists")
            validate_identity(item["subject"], "work item subject")
            validate_identity(item["start_subject"], "work item start subject")
            require(1 <= len(item["story_ids"]) <= 3, "work item requires 1 to 3 story IDs")
            require(item["id"] not in item["dependencies"] and all(dependency in state["work_items"] for dependency in item["dependencies"]), "work item dependencies must already exist and cannot include itself")
            require(set(item["allowed_paths"]) <= set(state["contract"]["subjects"]["allowed_paths"]), "work item allowed paths exceed Contract")
            require(set(state["contract"]["subjects"]["excluded_paths"]) <= set(item["excluded_paths"]), "work item must preserve Contract exclusions")
            require(item["mutation_authority"] in {"local_write", "external_mutation", "read_only"}, "Work Item mutation authority is invalid")
            if item["mutation_authority"] == "local_write":
                require(state["contract"]["authority"]["local_write"] is True, "Contract does not authorize local writes")
            elif item["mutation_authority"] == "external_mutation":
                require(state["contract"]["authority"]["external_mutation"] is True, "Contract does not authorize external mutation")
            if profile == "fast": require(not state["work_items"], "fast profile allows exactly one work item")
            deps_ready = all(dep in state["work_items"] and state["work_items"][dep]["state"] == "completed" for dep in item["dependencies"])
            item.update({"state": "claimable" if deps_ready else "pending", "worker_lineage_id": None, "generation": 0, "evidence_refs": [], "alignment": None, "receipt": None})
            state["work_items"][item["id"]] = item
        elif command == "claim":
            require(state["state"] == "delivery", "claim requires delivery")
            item = _work_item(state, payload["work_item_id"])
            require(item["state"] == "claimable", "work item is not claimable")
            lineage = payload["lineage_id"]
            if profile == "fast" and state["workers"]: require(lineage in state["workers"], "fast profile forbids a second lineage")
            if any(worker.get("work_item_id") != item["id"] for worker in state["workers"].values()):
                require(state["durable"], "Journal must be active before additional worker dispatch")
            require(not _active_claims_for_subject(state, item["subject"]), "subject already has an active writer")
            require(lineage not in state["workers"], "worker lineage is already bound; claim cannot overwrite its history")
            require(payload.get("provider") and payload.get("model") and payload.get("session"), "actual provider/model/session are required")
            _validate_actual_provider(state["contract"], payload)
            item["state"] = "active"; item["worker_lineage_id"] = lineage; item["generation"] = 1
            capsule = payload["capsule"]
            require_keys(capsule, {"contract_version", "contract_hash", "story_ids", "subject", "allowed_paths", "valid_evidence_refs", "open_blockers", "safety_constraints", "exact_next_action"}, "worker capsule")
            require(capsule["contract_version"] == state["contract_version"] and capsule["contract_hash"] == state["contract_hash"], "Capsule Contract identity mismatch")
            require(capsule["story_ids"] == item["story_ids"] and capsule["subject"] == item["subject"], "Capsule Work Item identity mismatch")
            if capsule["valid_evidence_refs"]:
                _valid_refs(state, capsule["valid_evidence_refs"])
            state["workers"][lineage] = {
                "work_item_id": item["id"],
                "correction_rounds": 0,
                "compactions": 0,
                "generations": [{
                    "generation": 1,
                    "active": True,
                    "provider": payload["provider"],
                    "model": payload["model"],
                    "session": payload["session"],
                    "capsule": capsule,
                }],
            }
            state["claims"][item["id"]] = {"lineage_id": lineage, "generation": 1, "subject": item["subject"], "active": True}
        elif command == "record-work-item-evidence":
            item = _work_item(state, payload["work_item_id"])
            require(item["state"] == "active", "work item evidence requires active state")
            require(payload["evidence"]["subject"] == item["subject"], "work item evidence subject mismatch")
            _add_evidence(state, payload["evidence"])
            item["evidence_refs"].append(payload["evidence"]["id"]); item["state"] = "evidence_ready"
        elif command == "integrate-work-item":
            item = _work_item(state, payload["work_item_id"])
            require(item["state"] == "evidence_ready", "integration requires evidence_ready")
            validate_identity(payload["subject"], "integrated subject")
            _valid_refs(state, item["evidence_refs"])
            if profile == "fast": require(state["fast"] and state["fast"]["state"] == "verified", "fast outcome must be verified before integration")
            item["subject"] = payload["subject"]; item["state"] = "integrated"
            state["claims"][item["id"]]["subject"] = payload["subject"]
        elif command == "record-alignment":
            item = _work_item(state, payload["work_item_id"])
            require(item["state"] == "integrated", "alignment requires integrated work item")
            alignment = validate_alignment(
                state,
                payload["alignment"],
                item["story_ids"],
                item["subject"],
                passed_gate=False,
            )
            item["alignment"] = alignment; item["state"] = "aligned"
        elif command == "complete-work-item":
            item = _work_item(state, payload["work_item_id"])
            require(item["state"] == "aligned", "completion requires aligned work item")
            require_keys(payload["receipt"], {"outcome", "subject", "evidence_refs", "remaining_risks"}, "work item receipt")
            require(payload["receipt"]["subject"] == item["subject"], "Work Item receipt subject mismatch")
            _refs_for_subject(state, payload["receipt"]["evidence_refs"], item["subject"])
            item["receipt"] = copy.deepcopy(payload["receipt"]); item["state"] = "completed"
            for pending in state["work_items"].values():
                if pending["state"] == "pending" and all(state["work_items"][dependency]["state"] == "completed" for dependency in pending["dependencies"]):
                    pending["state"] = "claimable"
        elif command == "rotate-worker":
            item = _work_item(state, payload["work_item_id"])
            require(item["state"] in {"active", "blocked"}, "worker rotation requires active or blocked work item")
            lineage = item["worker_lineage_id"]; worker = state["workers"][lineage]
            require(payload["trigger"] in {"two_correction_rounds", "two_compactions", "no_progress", "contract_misunderstanding", "approved_fix_plan"}, "invalid rotation trigger")
            require(payload.get("trigger_evidence"), "worker rotation requires trigger evidence")
            if payload["trigger"] == "two_correction_rounds":
                require(worker["correction_rounds"] >= 2, "two correction rounds have not been recorded")
            elif payload["trigger"] == "two_compactions":
                require(worker["compactions"] >= 2, "two compactions have not been recorded")
            require(len(worker["generations"]) < 3, "worker generation budget exhausted")
            require(payload.get("provider") and payload.get("model") and payload.get("session"), "new generation provider/model/session are required")
            _validate_actual_provider(state["contract"], payload)
            require(payload["session"] not in {generation["session"] for generation in worker["generations"]}, "new generation requires a new session")
            capsule = payload["capsule"]
            require_keys(capsule, {"contract_version", "contract_hash", "story_ids", "subject", "allowed_paths", "valid_evidence_refs", "open_blockers", "safety_constraints", "exact_next_action"}, "worker capsule")
            require(capsule["contract_version"] == state["contract_version"] and capsule["contract_hash"] == state["contract_hash"], "Capsule Contract identity mismatch")
            require(capsule["story_ids"] == item["story_ids"] and capsule["subject"] == item["subject"], "Capsule Work Item identity mismatch")
            if capsule["valid_evidence_refs"]:
                _valid_refs(state, capsule["valid_evidence_refs"])
            worker["generations"][-1]["active"] = False
            state["claims"][item["id"]]["active"] = False
            generation = len(worker["generations"]) + 1
            worker["generations"].append({
                "generation": generation,
                "active": True,
                "provider": payload["provider"],
                "model": payload["model"],
                "session": payload["session"],
                "trigger": payload["trigger"],
                "trigger_evidence": copy.deepcopy(payload["trigger_evidence"]),
                "capsule": capsule,
            })
            item["generation"] = generation
            state["claims"][item["id"]] = {"lineage_id": lineage, "generation": generation, "subject": item["subject"], "active": True}
        elif command == "begin-convergence":
            require(state["state"] == "delivery", "begin-convergence requires delivery")
            if profile == "fast":
                require(len(state["work_items"]) == 1, "fast convergence requires one work item")
                item = next(iter(state["work_items"].values()))
                require(item["state"] == "evidence_ready", "fast candidate requires evidence_ready")
                candidate = validate_identity(payload["candidate_subject"], "fast candidate")
                require(candidate == item["subject"], "fast candidate must equal the evidence-ready Work Item subject")
                _refs_for_subject(state, item["evidence_refs"], candidate)
                state["fast"] = {"state": "candidate_ready", "candidate_subject": payload["candidate_subject"], "review_count": 0, "fix_batch_count": 0, "closure_count": 0, "reviewer": None, "check_failures": [], "findings": [], "blocking_findings": [], "invalidated_refs": [], "replacement_refs": []}
                state["convergence"] = {
                    "subject": candidate,
                    "integration_owner": item["worker_lineage_id"],
                    "integration_evidence_refs": list(item["evidence_refs"]),
                    "work_item_subjects": {item["id"]: item["subject"]},
                    "required_story_ids": list(item["story_ids"]),
                    "required_final_evidence_refs": [],
                }
            else:
                require(state["work_items"] and all(item["state"] == "completed" for item in state["work_items"].values()), "standard convergence requires completed work items")
                subject = validate_identity(payload["subject"], "convergence subject")
                owner = payload["integration_owner"]
                require(owner in state["workers"], "integration owner must be an existing Work Item lineage")
                _require_active_lineage(state, owner)
                expected_subjects = {item_id: item["subject"] for item_id, item in state["work_items"].items()}
                require(payload["work_item_subjects"] == expected_subjects, "convergence Work Item subject set mismatch")
                integration_evidence = payload["integration_evidence"]
                require(integration_evidence["subject"] == subject, "integration evidence subject mismatch")
                _add_evidence(state, integration_evidence)
                state["convergence"] = {
                    "subject": subject,
                    "integration_owner": owner,
                    "integration_evidence_refs": [integration_evidence["id"]],
                    "work_item_subjects": expected_subjects,
                    "required_story_ids": _required_story_ids(state),
                    "required_final_evidence_refs": [],
                }
                state["subject_chain"].append({"kind": "integration", "from": list(expected_subjects.values()), "to": subject, "evidence_refs": [integration_evidence["id"]]})
            state["state"] = "convergence"
        elif command == "record-fast-check":
            require(profile == "fast" and state["fast"]["state"] == "candidate_ready", "fast check requires candidate_ready")
            require(payload["subject"] == state["fast"]["candidate_subject"], "fast check subject mismatch")
            require(payload["evidence"]["subject"] == state["fast"]["candidate_subject"], "fast check evidence subject mismatch")
            _add_evidence(state, payload["evidence"])
            state["fast"]["check_failures"] = copy.deepcopy(payload.get("failure_ids", [])); state["fast"]["state"] = "checked"
        elif command == "start-fast-review":
            require(profile == "fast" and state["fast"]["state"] == "checked", "fast review requires checked")
            require(state["durable"], "journal must be active before fast reviewer dispatch")
            require(state["fast"]["review_count"] == 0, "fast review budget exhausted")
            reviewer = payload["reviewer"]
            require_keys(reviewer, {"identity", "provider", "model", "session", "capability"}, "fast reviewer")
            require(reviewer["identity"] != _fast_lineage(state), "fast reviewer must be independent")
            worker_sessions = {generation["session"] for worker in state["workers"].values() for generation in worker["generations"]}
            require(reviewer["session"] not in worker_sessions, "fast reviewer session must be independent from workers")
            _validate_actual_provider(state["contract"], reviewer)
            state["fast"]["reviewer"] = copy.deepcopy(reviewer); state["fast"]["review_count"] = 1; state["fast"]["state"] = "review_pending"
        elif command == "record-fast-review":
            require(profile == "fast" and state["fast"]["state"] == "review_pending", "fast review report requires review_pending")
            require(payload["reviewer_identity"] == state["fast"]["reviewer"]["identity"], "fast reviewer identity mismatch")
            require(payload["subject"] == state["fast"]["candidate_subject"], "fast review subject mismatch")
            findings = copy.deepcopy(payload.get("findings", []))
            blocking = [finding for finding in findings if finding.get("blocking")]
            blocking += [{"id": failure, "blocking": True, "source": "candidate_check"} for failure in state["fast"]["check_failures"]]
            state["fast"]["findings"] = findings; state["fast"]["blocking_findings"] = blocking; state["fast"]["state"] = "findings_frozen"
        elif command == "begin-fast-fix":
            require(profile == "fast" and state["fast"]["state"] == "findings_frozen", "fast fix requires findings_frozen")
            require(state["fast"]["blocking_findings"], "fast fix requires blocking findings")
            require(state["fast"]["fix_batch_count"] == 0, "fast fix budget exhausted")
            _require_active_lineage(state, _fast_lineage(state))
            state["fast"]["fix_batch_count"] = 1; state["fast"]["state"] = "fix_pending"
        elif command == "record-fast-fix":
            require(profile == "fast" and state["fast"]["state"] == "fix_pending", "fast fix result requires fix_pending")
            require(payload["lineage_id"] == _fast_lineage(state), "fast fix must use original lineage")
            old_subject = state["fast"]["candidate_subject"]
            validate_identity(payload["new_subject"], "fast fixed subject")
            _invalidate(state, payload["invalidated_evidence_refs"], "fast_fix")
            state["fast"]["invalidated_refs"] = list(payload["invalidated_evidence_refs"])
            state["fast"]["candidate_subject"] = payload["new_subject"]
            next(iter(state["work_items"].values()))["subject"] = payload["new_subject"]
            state["convergence"]["subject"] = payload["new_subject"]
            state["subject_chain"].append({
                "kind": "fast_fix",
                "from": old_subject,
                "to": payload["new_subject"],
                "finding_ids": [finding["id"] for finding in state["fast"]["blocking_findings"]],
                "invalidation_refs": list(payload["invalidated_evidence_refs"]),
            })
            state["fast"]["state"] = "final_verification"
        elif command == "begin-fast-final-verification":
            require(profile == "fast" and state["fast"]["state"] == "findings_frozen", "fast final verification requires findings_frozen")
            require(not state["fast"]["blocking_findings"], "blocking findings require the one fix batch")
            state["fast"]["state"] = "final_verification"
        elif command == "record-fast-final-verification":
            require(profile == "fast" and state["fast"]["state"] == "final_verification", "fast final result requires final_verification")
            for evidence in payload.get("evidence", []):
                require(evidence["subject"] == state["fast"]["candidate_subject"], "fast final evidence subject mismatch")
                _add_evidence(state, evidence)
            replacements = payload.get("evidence_refs", [])
            _refs_for_subject(state, replacements, state["fast"]["candidate_subject"])
            if state["fast"]["invalidated_refs"]: require(replacements, "invalidated fast evidence must be rebuilt")
            state["fast"]["replacement_refs"] = replacements
            state["convergence"]["required_final_evidence_refs"] = list(replacements)
            state["fast"]["state"] = "verified" if payload["passed"] else "failed"
        elif command == "start-review":
            require(profile == "standard" and state["state"] == "convergence", "standard review requires standard convergence")
            require(state["policies"].get("review_required"), "risk policy did not require review")
            require(state["durable"], "journal must be active before standard reviewer dispatch")
            require(state["review"] is None, "only one broad review campaign is allowed")
            subject = validate_identity(payload["subject"], "review subject")
            require(subject == state["convergence"]["subject"], "review subject must equal the convergence subject")
            reviewers = payload["reviewers"]
            require(len(reviewers) == 3, "standard review requires three reviewers")
            for reviewer in reviewers:
                require_keys(reviewer, {"identity", "dimension", "provider", "model", "session"}, "standard reviewer")
                _validate_actual_provider(state["contract"], reviewer)
            require({item["dimension"] for item in reviewers} == REVIEW_DIMENSIONS, "review dimensions are incomplete")
            require(len({item["identity"] for item in reviewers}) == 3, "reviewers must be independent")
            require(len({item["session"] for item in reviewers}) == 3, "reviewer sessions must be independent")
            require(not ({item["identity"] for item in reviewers} & set(state["workers"])), "reviewer identities cannot overlap worker lineages")
            worker_sessions = {generation["session"] for worker in state["workers"].values() for generation in worker["generations"]}
            require(not ({item["session"] for item in reviewers} & worker_sessions), "reviewer sessions cannot overlap worker sessions")
            state["review"] = {"state": "collecting", "subject": subject, "reviewers": copy.deepcopy(reviewers), "findings": None, "plan_revision": 0, "fix_plan": None, "fix_subject": None, "fix_record": None}
        elif command == "freeze-findings":
            require(profile == "standard" and state["review"] and state["review"]["state"] == "collecting", "freeze-findings requires collecting campaign")
            reports = payload["reports"]; campaign = state["review"]
            require(len(reports) == 3, "all three review reports are required")
            require({r["reviewer_identity"] for r in reports} == {r["identity"] for r in campaign["reviewers"]}, "review report identities mismatch")
            require(all(r["subject"] == campaign["subject"] for r in reports), "review reports must bind the frozen subject")
            findings = payload["findings"]
            for finding in findings:
                require_keys(finding, {"id", "reviewer_identity", "subject", "path_or_evidence", "story_or_invariant", "severity", "closure_condition"}, "standard finding")
                require(finding["subject"] == campaign["subject"], "finding subject mismatch")
                require(finding["reviewer_identity"] in {reviewer["identity"] for reviewer in campaign["reviewers"]}, "finding reviewer identity mismatch")
            require(len({finding["id"] for finding in findings}) == len(findings), "finding IDs must be unique")
            campaign["findings"] = copy.deepcopy(findings)
            if findings:
                campaign["state"] = "plan_pending"
            else:
                campaign["fix_subject"] = copy.deepcopy(campaign["subject"])
                campaign["fix_record"] = {
                    "from": copy.deepcopy(campaign["subject"]),
                    "to": copy.deepcopy(campaign["subject"]),
                    "diff_identity": "none:no-findings",
                    "finding_mappings": [],
                }
                campaign["state"] = "fixed"
        elif command == "approve-fix-plan":
            campaign = state["review"]
            require(campaign and campaign["state"] == "plan_pending", "fix plan approval requires plan_pending")
            decisions = payload["decisions"]
            require(len(decisions) == 3 and {d["reviewer_identity"] for d in decisions} == {r["identity"] for r in campaign["reviewers"]}, "three original reviewer decisions are required")
            if all(d["approved"] for d in decisions):
                plan = payload["fix_plan"]
                finding_ids = {finding["id"] for finding in campaign["findings"]}
                require(isinstance(plan, dict) and set(plan) == finding_ids, "fix plan must map every frozen finding exactly once")
                for mapping in plan.values():
                    require_keys(mapping, {"changes", "tests", "docs", "evidence"}, "fix plan mapping")
                campaign["fix_plan"] = copy.deepcopy(plan); campaign["state"] = "plan_approved"
            elif campaign["plan_revision"] == 0:
                campaign["plan_revision"] = 1
            else:
                campaign["state"] = "stopped"; state["resume_state"] = "convergence"; state["state"] = "blocked"; state["blocker"] = {"type": "review_plan_stopped", "evidence": decisions}
        elif command == "begin-fix":
            campaign = state["review"]
            require(campaign and campaign["state"] == "plan_approved", "begin-fix requires approved plan")
            require(payload["lineage_id"] == _integration_owner(state), "standard fix must use the convergence integration owner")
            _require_active_lineage(state, payload["lineage_id"])
            campaign["state"] = "fix_pending"
        elif command == "record-fix":
            campaign = state["review"]
            require(campaign and campaign["state"] in {"fix_pending", "closure_pending"}, "fix record requires the one pending fix batch")
            require_keys(payload, {"lineage_id", "new_subject", "diff_identity", "finding_mappings", "invalidated_evidence_refs"}, "fix record")
            require(payload["lineage_id"] == _integration_owner(state), "standard fix must use the convergence integration owner")
            _require_active_lineage(state, payload["lineage_id"])
            new_subject = validate_identity(payload["new_subject"], "fixed subject")
            require(new_subject != campaign["subject"], "fix must produce a new immutable subject")
            require(payload.get("diff_identity"), "fix diff identity is required")
            mappings = payload["finding_mappings"]
            findings = {finding["id"]: finding for finding in campaign["findings"]}
            require(isinstance(mappings, list) and len(mappings) == len(findings), "fix must map every frozen finding exactly once")
            require({mapping.get("finding_id") for mapping in mappings} == set(findings), "fix finding mapping mismatch")
            mapped_evidence_refs = {
                ref for mapping in mappings for ref in mapping.get("evidence_refs", [])
            }
            require(
                not (mapped_evidence_refs & set(payload["invalidated_evidence_refs"])),
                "fix evidence cannot be invalidated by the same mutation",
            )
            for mapping in mappings:
                require_keys(mapping, {"finding_id", "change_refs", "evidence_refs"}, "fix finding mapping")
                require(mapping["change_refs"], "fix finding mapping requires changes")
                _passed_refs_for_subject(state, mapping["evidence_refs"], new_subject)
            _invalidate(state, payload["invalidated_evidence_refs"], "review_fix")
            campaign["fix_subject"] = new_subject
            campaign["fix_record"] = {
                "from": campaign["subject"], "to": new_subject,
                "diff_identity": payload["diff_identity"], "finding_mappings": copy.deepcopy(mappings),
            }
            campaign["state"] = "fixed"
            state["subject_chain"].append({"from": campaign["subject"], "to": new_subject, "kind": "review_fix", "diff_identity": payload["diff_identity"]})
            state["convergence"]["subject"] = new_subject
        elif command == "record-closure":
            raise OpdError("reviewer closure is retired; record deterministic fix evidence instead")
        elif command == "start-e2e":
            require(profile == "standard" and state["state"] == "convergence", "E2E requires standard convergence")
            if state["policies"].get("review_required"): require(state["review"] and state["review"]["state"] in {"fixed", "passed"}, "review fixes must be recorded before E2E")
            require(state["e2e"]["state"] in {"not_started", "recheck_pending"}, "E2E cannot start from current state")
            subject = validate_identity(payload["subject"], "E2E subject")
            require(subject == state["convergence"]["subject"], "E2E subject must equal the current convergence subject")
            if state["subject_chain"]: require(state["subject_chain"][-1]["to"] == subject, "E2E subject breaks identity chain")
            state["e2e"].update({"state": "running", "suite": validate_identity(payload["suite"], "E2E suite"), "subject": subject, "p0_required": False})
        elif command == "record-e2e":
            require(state["e2e"]["state"] == "running", "record-e2e requires running")
            require(payload["suite"] == state["e2e"]["suite"] and payload["subject"] == state["e2e"]["subject"], "E2E identity mismatch")
            require(payload["evidence"]["subject"] == state["e2e"]["subject"], "E2E evidence subject mismatch")
            _add_evidence(state, payload["evidence"])
            if payload["passed"]:
                _passed_refs_for_subject(state, [payload["evidence"]["id"]], state["e2e"]["subject"])
                state["e2e"]["state"] = "passed"; state["e2e"]["failure_ids"] = []
                state["e2e"]["passed_evidence_ref"] = payload["evidence"]["id"]
                state["convergence"]["required_final_evidence_refs"] = [payload["evidence"]["id"]]
            else:
                require(payload.get("failure_ids"), "failed E2E requires real failure IDs")
                state["e2e"]["state"] = "failed"; state["e2e"]["failure_ids"] = list(payload["failure_ids"])
                state["convergence"]["required_final_evidence_refs"] = [payload["evidence"]["id"]]
        elif command == "begin-e2e-fix":
            require(state["e2e"]["state"] == "failed", "E2E fix requires failed")
            require(payload["lineage_id"] == _integration_owner(state), "E2E fix must use convergence integration owner")
            _require_active_lineage(state, payload["lineage_id"])
            mappings = payload["failure_mappings"]
            for mapping in mappings:
                require_keys(mapping, {"failure_id", "action"}, "E2E failure mapping")
                require(mapping["action"], "E2E failure mapping requires an action")
            require(
                mappings
                and len(mappings) == len(state["e2e"]["failure_ids"])
                and {m["failure_id"] for m in mappings} == set(state["e2e"]["failure_ids"]),
                "E2E fix must map every real failure ID exactly once",
            )
            state["e2e"]["state"] = "fix_pending"; state["e2e"]["pending_mappings"] = copy.deepcopy(mappings)
        elif command == "record-e2e-fix":
            require(state["e2e"]["state"] == "fix_pending", "E2E fix result requires fix_pending")
            old_subject = state["e2e"]["subject"]; new_subject = validate_identity(payload["new_subject"], "E2E repaired subject")
            require(new_subject != old_subject, "E2E fix must produce a new immutable subject")
            require(payload.get("diff_identity"), "E2E fix diff identity is required")
            mapped = {m["failure_id"] for m in state["e2e"]["pending_mappings"]}
            require(set(payload["failure_ids"]) == mapped, "E2E fix result must preserve failure mapping")
            _invalidate(state, payload["invalidated_evidence_refs"], "e2e_fix")
            repair = {"from": old_subject, "to": new_subject, "diff_identity": payload["diff_identity"], "failure_ids": payload["failure_ids"], "touches_p0": payload["touches_p0"]}
            state["e2e"]["repairs"].append(repair); state["subject_chain"].append(repair); state["e2e"]["subject"] = new_subject; state["e2e"]["p0_required"] = payload["touches_p0"]
            state["convergence"]["subject"] = new_subject
            state["convergence"]["required_final_evidence_refs"] = []
            state["e2e"]["state"] = "p0_closure_pending" if payload["touches_p0"] else "recheck_pending"
        elif command == "record-p0-evidence":
            require(state["e2e"]["state"] == "p0_closure_pending", "P0 evidence requires p0_closure_pending")
            require_keys(payload, {"invariant", "failure_ids", "diff_identity", "passed", "evidence_refs"}, "P0 evidence")
            require(isinstance(payload["passed"], bool), "P0 evidence passed must be boolean")
            require(payload["failure_ids"] == state["e2e"]["repairs"][-1]["failure_ids"] and payload["diff_identity"] == state["e2e"]["repairs"][-1]["diff_identity"], "P0 evidence identity mismatch")
            if payload["passed"]:
                _passed_refs_for_subject(state, payload["evidence_refs"], state["e2e"]["subject"])
            else:
                _refs_for_subject(state, payload["evidence_refs"], state["e2e"]["subject"])
            receipt = copy.deepcopy(payload); state["e2e"]["targeted_closures"].append(receipt)
            if payload["passed"]: state["e2e"]["state"] = "recheck_pending"
            else: state["e2e"]["state"] = "stopped"; state["resume_state"] = "convergence"; state["state"] = "blocked"; state["blocker"] = {"type": "p0_evidence_failed", "evidence": receipt}
        elif command == "begin-closeout":
            require(state["state"] == "convergence", "begin-closeout requires convergence")
            disposition = payload["disposition"]
            require(disposition in TERMINAL_STATES, "invalid closeout disposition")
            if disposition == "passed":
                require(all(item["state"] == "completed" for item in state["work_items"].values()), "passed closeout requires completed work items")
                if profile == "standard": _standard_ready_for_closeout(state)
                else: require(state["fast"]["state"] == "verified", "fast closeout requires verified")
            else:
                _valid_refs(state, payload["failure_evidence_refs"])
            state["pending_disposition"] = disposition; state["state"] = "closeout"
        elif command == "closeout":
            require(state["state"] == "closeout", "closeout requires closeout state")
            disposition = payload["disposition"]
            require(disposition == state["pending_disposition"], "closeout disposition changed")
            require_keys(payload["receipt"], {"contract_ref", "final_subject", "evidence_refs", "story_alignment", "risk_disposition", "recovery"}, "final receipt")
            _refs_for_subject(state, payload["receipt"]["evidence_refs"], state["convergence"]["subject"])
            require(payload["receipt"]["contract_ref"] == {"version": state["contract_version"], "hash": state["contract_hash"]}, "receipt contract identity mismatch")
            require(payload["receipt"]["risk_disposition"] == disposition, "receipt risk disposition mismatch")
            final_subject = state["convergence"]["subject"]
            require(payload["receipt"]["final_subject"] == final_subject, "receipt final subject must match convergence subject")
            alignment = validate_alignment(
                state,
                payload["receipt"]["story_alignment"],
                state["convergence"]["required_story_ids"],
                final_subject,
                passed_gate=disposition == "passed",
            )
            required_final_refs = set(state["convergence"]["required_final_evidence_refs"])
            require(required_final_refs and required_final_refs <= set(payload["receipt"]["evidence_refs"]), "receipt is missing required final verification evidence")
            _refs_for_subject(state, list(required_final_refs), final_subject)
            recovery = payload["receipt"]["recovery"]
            require_keys(recovery, {"restore_point", "status", "evidence_refs"}, "receipt recovery")
            require(recovery["restore_point"] == state["contract"]["recovery"]["restore_point"], "receipt recovery point mismatch")
            require(recovery["status"] in {"verified", "not_applicable", "degraded"}, "receipt recovery status is invalid")
            if recovery["evidence_refs"]:
                _valid_refs(state, recovery["evidence_refs"])
            if profile == "fast":
                boundary = payload["receipt"].get("delivery_boundary")
                require(isinstance(boundary, dict), "Fast receipt requires delivery boundary")
                require(boundary == {
                    "profile": "fast",
                    "production_ready": False,
                    "isolation_boundary": state["contract"]["fast"]["isolation_boundary"],
                }, "Fast receipt cannot claim production readiness or alter isolation boundary")
            if disposition == "accepted_risk":
                acceptance = payload["receipt"].get("risk_acceptance", {})
                require(acceptance.get("authorized_by") and acceptance.get("evidence"), "accepted risk requires explicit authorization")
                require(state["contract_version"] > 1 and state["contract"].get("accepted_risks"), "accepted risk requires an approved Contract amendment")
            state["receipt"] = copy.deepcopy(payload["receipt"])
            state["receipt"]["story_alignment"] = alignment
            state["state"] = disposition
            for claim in state["claims"].values():
                claim["active"] = False
            for worker in state["workers"].values():
                for generation in worker["generations"]:
                    generation["active"] = False
        elif command == "amend-contract":
            require(state["state"] not in TERMINAL_STATES, "terminal delivery cannot be amended")
            validate_contract(payload["contract"])
            if state["state"] != "amendment_pending":
                state["resume_state"] = state["state"]
                state["state"] = "amendment_pending"
            state["pending_amendment"] = copy.deepcopy(payload)
        elif command == "approve-amendment":
            require(state["state"] == "amendment_pending", "approve-amendment requires amendment_pending")
            require(payload.get("approval_evidence"), "amendment approval evidence is required")
            require(isinstance(state["pending_amendment"], dict) and isinstance(state["pending_amendment"].get("contract"), dict), "approved amendment requires a complete replacement Contract")
            amendment = state["pending_amendment"]; contract = validate_contract(amendment["contract"])
            invalidates = contract_change_invalidates_evidence(state["contract"], contract)
            old_contract_version = state["contract_version"]
            version = state["contract_version"] + 1
            state["contracts"][str(version)] = contract; state["contract_version"] = version; state["contract"] = contract; state["contract_hash"] = content_hash(contract)
            if invalidates:
                reset_for_material_amendment(state, old_contract_version)
            state["state"] = "first_evidence" if invalidates else state["resume_state"]
            state["pending_amendment"] = None; state["resume_state"] = None
        elif command == "stop":
            require(payload.get("blocker") and payload.get("evidence"), "stop requires blocker and evidence")
            state["resume_state"] = state["state"]; state["state"] = "blocked"; state["blocker"] = copy.deepcopy(payload)
        elif command == "authorize-retry":
            require(state["state"] == "blocked", "authorize-retry requires blocked")
            require_keys(payload, {"authorized_by", "reason", "scope", "evidence"}, "retry authorization")
            require(state["e2e"]["state"] != "stopped", "stopped E2E requires amendment or new delivery")
            state["retry_authorizations"].append(copy.deepcopy(payload)); state["state"] = state["resume_state"]; state["resume_state"] = None; state["blocker"] = None
        elif command == "record-event":
            require(state["durable"], "record-event requires durable journal")
            require(payload.get("event_type") in MATERIAL_EVENT_TYPES, "event type is not in material allowlist")
            require(not any(key in payload.get("data", {}) for key in ("state", "revision", "work_items", "claims", "evidence", "review", "fast", "e2e")), "record-event cannot modify state fields")
            data = payload.get("data", {})
            if payload["event_type"] == "external_mutation":
                require(state["state"] in {"delivery", "convergence"}, "external mutation requires delivery or convergence")
                require(state["contract"]["authority"]["external_mutation"] is True, "Contract does not authorize external mutation")
                require("external_mutation" in state["policies"].get("journal_triggers", []), "external mutation policy is not activated")
                require_keys(data, {"idempotency_key", "automation_identity", "target_id", "target", "operation", "authorization_ref"}, "external mutation")
                validate_identity(data["automation_identity"], "external automation identity")
                validate_identity(data["target"], "external mutation target")
                targets = {target["id"]: target for target in state["contract"]["subjects"]["external_targets"]}
                require(data["target_id"] in targets, "external mutation target is outside Contract")
                target = targets[data["target_id"]]
                require(data["target"] == target["identity"], "external mutation target identity mismatch")
                require(data["operation"] in target["allowed_operations"], "external mutation operation is outside target authority")
                if data["operation"] == "destructive":
                    boundary = state["contract"]["authority"]["destructive_boundary"]
                    require(isinstance(boundary, dict) and boundary.get("authorization_ref"), "destructive mutation lacks Contract boundary authorization")
                    require(data["authorization_ref"] == boundary["authorization_ref"], "destructive authorization mismatch")
                else:
                    require(data["authorization_ref"] is None, "reversible mutation cannot consume destructive authorization")
                require(isinstance(data["idempotency_key"], str) and data["idempotency_key"].strip(), "external mutation idempotency key is required")
                require(data["idempotency_key"] not in state["external_actions"], "external mutation was already recorded")
                state["external_actions"][data["idempotency_key"]] = copy.deepcopy(data)
            elif payload["event_type"] == "decision":
                require_keys(data, {"skill_commit", "prompt_contract_version", "brief_schema_version", "brief_hash", "provider", "model", "session"}, "decision event")
                require(data["prompt_contract_version"] == PROMPT_CONTRACT_VERSION, "decision prompt contract version mismatch")
                require(data["brief_schema_version"] == BRIEF_SCHEMA_VERSION, "decision Brief schema version mismatch")
            elif payload["event_type"] == "external_failure":
                require_keys(data, {"window_id", "host", "failure_id", "root_cause_localized", "restore_reason"}, "external failure")
                require(data["restore_reason"] in {None, "window_exit", "safety", "deadline"}, "continuous-window failure cannot restore the original service")
                window = state["host_windows"].setdefault(data["window_id"], {"failures": [], "localized_retries": 0})
                require(data["failure_id"] not in {item["failure_id"] for item in window["failures"]}, "external failure ID already recorded")
                window["failures"].append(copy.deepcopy(data))
                if data["root_cause_localized"]:
                    window["localized_retries"] += 1
                    if window["localized_retries"] > 1:
                        state["resume_state"] = state["state"]
                        state["state"] = "blocked"
                        state["blocker"] = {"type": "host_retry_exhausted", "evidence": data["failure_id"]}
            elif payload["event_type"] == "worker_context":
                require_keys(data, {"lineage_id", "counter", "proof"}, "worker context event")
                require(data["lineage_id"] in state["workers"], "worker context lineage is unknown")
                require(data["counter"] in {"correction_rounds", "compactions"}, "worker context counter is invalid")
                require(data["proof"], "worker context counter requires evidence")
                state["workers"][data["lineage_id"]][data["counter"]] += 1
        elif command == "checkpoint":
            require(state["durable"], "checkpoint requires durable journal")
            require(payload.get("material_event_ref"), "checkpoint requires a material event reference")
        else:
            raise OpdError(f"unknown mutation command: {command}")
    return change


MUTATION_COMMANDS.update({
    "submit-contract", "freeze-contract", "evaluate-policies", "record-safety-facts", "record-first-evidence",
    "record-evidence", "invalidate-evidence", "add-work-item", "claim",
    "record-work-item-evidence", "integrate-work-item", "record-alignment",
    "complete-work-item", "rotate-worker", "begin-convergence", "record-fast-check",
    "start-fast-review", "record-fast-review", "begin-fast-fix", "record-fast-fix",
    "begin-fast-final-verification", "record-fast-final-verification", "start-review",
    "freeze-findings", "approve-fix-plan", "begin-fix", "record-fix", "record-closure", "start-e2e",
    "record-e2e", "begin-e2e-fix", "record-e2e-fix", "record-p0-evidence",
    "begin-closeout", "closeout", "amend-contract", "approve-amendment", "stop",
    "authorize-retry", "record-event", "checkpoint",
})


def _read_data(value: str | None) -> dict[str, Any]:
    if value is None: return {}
    if value.startswith("@"):
        text = Path(value[1:]).read_text(encoding="utf-8")
    else: text = value
    parsed = json.loads(text)
    require(isinstance(parsed, dict), "--data must contain a JSON object")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init"); init.add_argument("project", type=Path); init.add_argument("delivery_id")
    for name in sorted(MUTATION_COMMANDS):
        command = sub.add_parser(name); command.add_argument("project", type=Path); command.add_argument("delivery_id"); command.add_argument("--expected-revision", type=int, required=True); command.add_argument("--data")
    activate = sub.add_parser("activate-journal"); activate.add_argument("project", type=Path); activate.add_argument("delivery_id"); activate.add_argument("--expected-revision", type=int, required=True); activate.add_argument("--trigger", required=True); activate.add_argument("--activation-id"); activate.add_argument("--crash-at", choices=["after_mkdir", "before_replay", "before_rename", "after_rename"])
    for name in ["status", "resume", "validate", "build-brief"]:
        command = sub.add_parser(name); command.add_argument("project", type=Path); command.add_argument("delivery_id")
        if name == "build-brief": command.add_argument("--environment-facts")
    decision = sub.add_parser("validate-decision"); decision.add_argument("project", type=Path); decision.add_argument("delivery_id"); decision.add_argument("--data", required=True)
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    store = Store(args.project, args.delivery_id)
    if args.command == "init": return store.create()
    if args.command == "activate-journal": return activate_journal(store, args.expected_revision, args.trigger, args.activation_id, args.crash_at)
    state = store.load()
    if args.command == "status":
        return {
            "delivery_id": state["delivery_id"],
            "revision": state["revision"],
            "state": state["state"],
            "durable": state["durable"],
            "derived_files_consistent": derived_files_consistent(store.delivery, state) if state["durable"] else True,
            "claims": state["claims"],
            "valid_evidence_refs": sorted(key for key, item in state["evidence"].items() if item["valid"]),
            "blocker": state["blocker"],
        }
    if args.command == "resume":
        require(state["durable"] and state["state"] not in TERMINAL_STATES, "resume requires durable nonterminal delivery")
        consistent = derived_files_consistent(store.delivery, state)
        return {
            "delivery_id": state["delivery_id"],
            "revision": state["revision"],
            "derived_files_consistent": consistent,
            "exact_next_action": exact_next_action(state) if consistent else "checkpoint:materialize-replayed-state",
            "claims": state["claims"],
        }
    if args.command == "validate":
        validate_state(state)
        return {
            "valid": True,
            "revision": state["revision"],
            "state_hash": state["state_hash"],
            "derived_files_consistent": derived_files_consistent(store.delivery, state) if state["durable"] else True,
        }
    if args.command == "build-brief": return build_coordinator_brief(state, _read_data(args.environment_facts))
    if args.command == "validate-decision": return validate_decision_proposal(state, _read_data(args.data))
    payload = _read_data(args.data)
    return mutate(store, args.expected_revision, args.command, payload, command_change(args.command, payload))


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(build_parser().parse_args(argv))
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        return 0
    except (OpdError, KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"opd: rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
