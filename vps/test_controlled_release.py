from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest


HERE = Path(__file__).parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def release(tmp_path, monkeypatch):
    mod = load("controlled_release")
    root = tmp_path / "deployed"
    (root / "vps").mkdir(parents=True)
    (root / "vps/.env").write_text("PLACEHOLDER=unchanged\n")
    (root / "vps/.env").chmod(0o600)
    (root / "vps/docker-compose.override.yml").write_text("services: {}\n")
    (root / "vps/docker-compose.override.yml").chmod(0o640)
    (root / "old.txt").write_text("original source\n")
    (root / ".git").mkdir()
    (root / ".git/private").write_text("must not be archived")
    package = tmp_path / "package"
    package.mkdir()
    commit = "a" * 40
    prefix = f"private-llm-{commit}"
    files = {"vps/deploy.sh": b"#!/bin/sh\nexit 99\n",
             "vps/docker-compose.yml": b"services: {}\n",
             "vps/docker-compose.override.yml": b"must not replace override\n",
             "new.txt": b"new source\n"}
    archive = package / f"{prefix}.tar"
    with tarfile.open(archive, "w") as tar:
        for name, body in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            tar.addfile(member, io.BytesIO(body))
    (package / f"{prefix}.tar.sha256").write_text(f"{mod.digest(archive)}  {archive.name}\n")
    (package / f"{prefix}.files.sha256").write_text("".join(
        f"{hashlib.sha256(body).hexdigest()}  ./{name}\n" for name, body in files.items()))
    (package / f"{prefix}.commit").write_text(commit + "\n")
    database, state = tmp_path / "database.dump", tmp_path / "state.tar"
    for path in (database, state):
        path.write_bytes(b"operator-verified recovery artifact")
        path.chmod(0o600)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    common = ["--root", str(root), "--release-dir", str(package), "--commit", commit]
    receipt = tmp_path / "recovery"
    write = ["--apply", "--ack", mod.ACK, "--receipt", str(receipt)]
    backups = ["--database-backup", str(database), "--state-backup", str(state),
               "--recovery-verified"]
    return mod, root, package, commit, common, write, backups, receipt


def test_check_is_default_and_read_only(release):
    mod, root, _, _, common, _, _, receipt = release
    initial = mod.protected(root, [])
    assert mod.main(common) == 0
    assert mod.protected(root, []) == initial
    assert not receipt.exists()
    assert not (root / "new.txt").exists()


@pytest.mark.parametrize("gate", [[], ["--apply"], ["--ack", "SOURCE_WRITE_AUTHORIZED"]])
def test_backup_requires_both_write_gates(release, gate):
    mod, _, _, _, common, _, backups, receipt = release
    assert mod.main(["backup", *common, "--receipt", str(receipt), *backups, *gate]) == 1
    assert not receipt.exists()


def test_backup_requires_existing_absolute_tmpdir(release, monkeypatch):
    mod, _, _, _, common, write, backups, receipt = release
    monkeypatch.setenv("TMPDIR", str(receipt / "absent"))
    assert mod.main(["backup", *common, *write, *backups]) == 1
    assert not receipt.exists()


def test_stage_and_restore_preserve_config_and_recovery(release):
    mod, root, _, _, common, write, backups, receipt = release
    initial = mod.protected(root, [])
    assert mod.main(["backup", *common, *write, *backups]) == 0
    assert (receipt / "source.tar").stat().st_mode & 0o777 == 0o600
    assert (receipt / "receipt.json").stat().st_mode & 0o777 == 0o600
    with tarfile.open(receipt / "source.tar") as tar:
        assert not any(member.name.startswith(".git/") for member in tar.getmembers())
    assert mod.main(["stage", *common, *write]) == 0
    assert (root / "new.txt").read_bytes() == b"new source\n"
    assert not (root / "old.txt").exists()
    assert mod.protected(root, []) == initial
    assert (root / ".git/private").exists()
    assert not (root / "vps/compose.override.yml").exists()
    assert mod.main(["restore-source", *common, *write]) == 0
    assert (root / "old.txt").read_text() == "original source\n"
    assert not (root / "new.txt").exists()
    assert mod.protected(root, []) == initial
    assert (receipt / "stage-complete.json").exists()
    assert (receipt / "restore-source-complete.json").exists()


def test_modified_protected_config_stops_before_source_sync(release):
    mod, root, _, _, common, write, backups, _ = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    (root / "vps/.env").write_text("changed independently\n")
    assert mod.main(["stage", *common, *write]) == 1
    assert (root / "old.txt").exists()
    assert not (root / "new.txt").exists()


def test_corrupt_recovery_stops_before_source_sync(release):
    mod, root, _, _, common, write, backups, receipt = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    (receipt / "source.tar").write_bytes(b"corrupt")
    assert mod.main(["stage", *common, *write]) == 1
    assert (root / "old.txt").exists()


def test_changed_database_recovery_stops_before_source_sync(release):
    mod, root, _, _, common, write, backups, receipt = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    body = json.loads((receipt / "receipt.json").read_text())
    Path(body["recovery"]["database"]["path"]).write_text("changed")
    assert mod.main(["stage", *common, *write]) == 1
    assert (root / "old.txt").exists()


def test_existing_completion_stops_before_source_sync(release):
    mod, root, _, _, common, write, backups, receipt = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    (receipt / "stage-complete.json").write_text("{}")
    assert mod.main(["stage", *common, *write]) == 1
    assert (root / "old.txt").exists()


def test_nonstandard_override_must_be_explicitly_protected(release):
    mod, root, _, _, common, write, backups, _ = release
    (root / "vps/custom.yml").write_text("local override")
    common = [*common, "--protect", "vps/custom.yml"]
    assert mod.main(["backup", *common, *write, *backups]) == 0
    assert mod.main(["stage", *common, *write]) == 0
    assert (root / "vps/custom.yml").read_text() == "local override"


def test_protected_rsync_patterns_are_rejected(release):
    mod, root, *_ = release
    with pytest.raises(ValueError):
        mod.protected(root, ["vps/*"])


def test_bad_archive_hash_is_rejected_before_backup(release):
    mod, _, package, commit, common, write, backups, receipt = release
    (package / f"private-llm-{commit}.tar").write_bytes(b"tampered")
    assert mod.main(["backup", *common, *write, *backups]) == 1
    assert not receipt.exists()


def test_failed_sync_retains_source_backup_and_stage(release, monkeypatch):
    mod, _, _, _, common, write, backups, receipt = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    monkeypatch.setattr(mod, "sync_source", lambda *args: (_ for _ in ()).throw(RuntimeError()))
    assert mod.main(["stage", *common, *write]) == 1
    assert (receipt / "source.tar").exists()
    assert not (receipt / "stage-complete.json").exists()


@pytest.mark.parametrize("change", ["modified", "new", "deleted"])
def test_review_stage_rejects_source_drift_since_backup(release, change):
    mod, root, _, _, common, write, backups, receipt = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    path = root / ("unreviewed.py" if change == "new" else "old.txt")
    if change == "deleted":
        path.unlink()
    else:
        path.write_text("new user work")
    before = mod.source_inventory(root)
    assert mod.main(["stage", *common, *write]) == 1
    assert mod.source_inventory(root) == before
    assert not (receipt / "stage-intent.json").exists()


def test_review_restore_rejects_unexplained_source_without_stage_intent(release):
    mod, root, _, _, common, write, backups, _ = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    (root / "unreviewed.py").write_text("new user work")
    before = mod.source_inventory(root)
    assert mod.main(["restore-source", *common, *write]) == 1
    assert mod.source_inventory(root) == before


@pytest.mark.parametrize("change", ["modified", "new", "deleted"])
def test_review_restore_rejects_source_drift_after_completed_stage(release, change):
    mod, root, _, _, common, write, backups, _ = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    assert mod.main(["stage", *common, *write]) == 0
    path = root / ("unreviewed.py" if change == "new" else "vps/deploy.sh")
    if change == "deleted":
        path.unlink()
    else:
        path.write_text("new user work")
    before = mod.source_inventory(root)
    assert mod.main(["restore-source", *common, *write]) == 1
    assert mod.source_inventory(root) == before


@pytest.mark.parametrize("action", ["stage", "restore-source"])
def test_review_extra_protection_request_must_match_backup(release, action):
    mod, root, _, _, common, write, backups, receipt = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    before = mod.source_inventory(root)
    assert mod.main([action, *common, *write, "--protect", "old.txt"]) == 1
    assert mod.source_inventory(root) == before
    assert not (receipt / f"{action}-intent.json").exists()


def test_review_missing_protection_request_must_match_backup(release):
    mod, root, _, _, common, write, backups, _ = release
    (root / "vps/custom.yml").write_text("local configuration")
    assert mod.main(["backup", *common, *write, *backups,
                     "--protect", "vps/custom.yml"]) == 0
    before = mod.source_inventory(root)
    assert mod.main(["stage", *common, *write]) == 1
    assert mod.source_inventory(root) == before


def test_review_git_worktree_pointer_survives_stage_and_restore(release):
    mod, root, _, _, common, write, backups, _ = release
    shutil.rmtree(root / ".git")
    (root / ".git").write_text("gitdir: /example/managed-worktree\n")
    before = mod.metadata(root / ".git")
    assert mod.main(["backup", *common, *write, *backups]) == 0
    assert mod.main(["stage", *common, *write]) == 0
    assert mod.metadata(root / ".git") == before
    assert mod.main(["restore-source", *common, *write]) == 0
    assert mod.metadata(root / ".git") == before


def test_review_partial_stage_has_durable_intent_and_can_be_explicitly_restored(release, monkeypatch):
    mod, root, _, _, common, write, backups, receipt = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    baseline = mod.source_inventory(root)
    sync = mod.sync_source

    def fail_mid_stage(stage, target, names):
        intent = json.loads((receipt / "stage-intent.json").read_text())
        assert intent["before_source_files"] == baseline
        assert intent["target_source_files"]["new.txt"] == mod.digest(stage / "new.txt")
        assert (receipt / "stage-intent.json").stat().st_mode & 0o777 == 0o600
        (target / "old.txt").unlink()
        shutil.copy2(stage / "new.txt", target / "new.txt")
        raise RuntimeError("simulated rsync failure")

    monkeypatch.setattr(mod, "sync_source", fail_mid_stage)
    assert mod.main(["stage", *common, *write]) == 1
    assert not (receipt / "stage-complete.json").exists()
    monkeypatch.setattr(mod, "sync_source", sync)
    assert mod.main(["restore-source", *common, *write]) == 0
    assert mod.source_inventory(root) == baseline
    completion = json.loads((receipt / "restore-source-complete.json").read_text())
    assert completion["target_source_files"] == baseline


def test_review_completed_stage_records_actual_target_inventory(release):
    mod, root, _, _, common, write, backups, receipt = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    assert mod.main(["stage", *common, *write]) == 0
    intent = json.loads((receipt / "stage-intent.json").read_text())
    completion = json.loads((receipt / "stage-complete.json").read_text())
    assert completion["target_source_files"] == mod.source_inventory(root)
    assert completion["target_source_files"] == intent["target_source_files"]
    assert completion["intent_sha256"] == mod.digest(receipt / "stage-intent.json")


def test_review_unknown_content_after_partial_stage_is_not_overwritten(release, monkeypatch):
    mod, root, _, _, common, write, backups, _ = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    sync = mod.sync_source

    def partial(stage, target, names):
        shutil.copy2(stage / "new.txt", target / "new.txt")
        raise RuntimeError("interrupted")

    monkeypatch.setattr(mod, "sync_source", partial)
    assert mod.main(["stage", *common, *write]) == 1
    (root / "new.txt").write_text("unknown user edits")
    before = mod.source_inventory(root)
    monkeypatch.setattr(mod, "sync_source", sync)
    assert mod.main(["restore-source", *common, *write]) == 1
    assert mod.source_inventory(root) == before


def test_review_partial_restore_can_be_explicitly_resumed(release, monkeypatch):
    mod, root, _, _, common, write, backups, receipt = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    baseline = mod.source_inventory(root)
    assert mod.main(["stage", *common, *write]) == 0
    target = mod.source_inventory(root)
    sync = mod.sync_source

    def partial(stage, destination, names):
        intent = json.loads((receipt / "restore-source-intent.json").read_text())
        assert intent["before_source_files"] == target
        assert intent["target_source_files"] == baseline
        shutil.copy2(stage / "old.txt", destination / "old.txt")
        (destination / "new.txt").unlink()
        raise RuntimeError("interrupted restore")

    monkeypatch.setattr(mod, "sync_source", partial)
    assert mod.main(["restore-source", *common, *write]) == 1
    intent_hash = mod.digest(receipt / "restore-source-intent.json")
    assert not (receipt / "restore-source-complete.json").exists()
    monkeypatch.setattr(mod, "sync_source", sync)
    assert mod.main(["restore-source", *common, *write]) == 0
    assert mod.source_inventory(root) == baseline
    assert mod.digest(receipt / "restore-source-intent.json") == intent_hash


def test_review_unknown_content_after_partial_restore_is_not_overwritten(release, monkeypatch):
    mod, root, _, _, common, write, backups, _ = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    assert mod.main(["stage", *common, *write]) == 0
    sync = mod.sync_source

    def partial(stage, destination, names):
        shutil.copy2(stage / "old.txt", destination / "old.txt")
        raise RuntimeError("interrupted restore")

    monkeypatch.setattr(mod, "sync_source", partial)
    assert mod.main(["restore-source", *common, *write]) == 1
    (root / "old.txt").write_text("unknown user edits")
    before = mod.source_inventory(root)
    monkeypatch.setattr(mod, "sync_source", sync)
    assert mod.main(["restore-source", *common, *write]) == 1
    assert mod.source_inventory(root) == before


def test_review_source_drift_during_intent_write_stops_sync(release, monkeypatch):
    mod, root, _, _, common, write, backups, _ = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    write_json = mod.private_json

    def changed_after_intent(path, body):
        write_json(path, body)
        (root / "old.txt").write_text("concurrent user work")

    monkeypatch.setattr(mod, "private_json", changed_after_intent)
    assert mod.main(["stage", *common, *write]) == 1
    assert (root / "old.txt").read_text() == "concurrent user work"
    assert not (root / "new.txt").exists()


def test_review_tampered_stage_intent_or_completion_cannot_authorize_restore(release):
    mod, root, _, _, common, write, backups, receipt = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    assert mod.main(["stage", *common, *write]) == 0
    intent_path = receipt / "stage-intent.json"
    intent = json.loads(intent_path.read_text())
    intent["target_commit"] = "b" * 40
    intent_path.write_text(json.dumps(intent))
    before = mod.source_inventory(root)
    assert mod.main(["restore-source", *common, *write]) == 1
    assert mod.source_inventory(root) == before


def test_review_old_receipt_requires_new_recovery_directory(release):
    mod, root, _, _, common, write, backups, receipt = release
    assert mod.main(["backup", *common, *write, *backups]) == 0
    path = receipt / "receipt.json"
    body = json.loads(path.read_text())
    body["version"] = 1
    path.write_text(json.dumps(body))
    before = mod.source_inventory(root)
    assert mod.main(["stage", *common, *write]) == 1
    assert mod.source_inventory(root) == before


@pytest.mark.parametrize("name", ["../outside", "/outside", "a/../../outside", ".git/config"])
def test_unsafe_paths_are_rejected(name):
    with pytest.raises(ValueError):
        load("controlled_release").relative(name)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE])
def test_extract_rejects_links_and_devices(tmp_path, kind):
    mod = load("controlled_release")
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tar:
        member = tarfile.TarInfo("entry")
        member.type, member.linkname = kind, "../outside"
        tar.addfile(member)
    with pytest.raises(ValueError):
        mod.extract(archive, tmp_path / "stage")


def test_recovery_artifacts_must_be_private_and_distinct(tmp_path):
    mod = load("controlled_release")
    database, state = tmp_path / "db", tmp_path / "state"
    for path in (database, state):
        path.write_text("recovery")
        path.chmod(0o600)
    with pytest.raises(ValueError):
        mod.recoveries(database, database, True)
    state.chmod(0o644)
    with pytest.raises(ValueError):
        mod.recoveries(database, state, True)


def mapping_snapshot():
    return {"data": [
        {"model_name": "demo-model", "model_info": {"id": "selected", "max_input_tokens": 1000,
         "max_output_tokens": 100, "updated_at": "before"},
         "litellm_params": {"model": "openai/demo-model", "api_base": "http://192.0.2.1/v1",
         "api_key": "masked", "tags": ["demo"], "rpm": 20, "timeout": 600}},
        {"model_name": "other-model", "model_info": {"id": "other"},
         "litellm_params": {"model": "openai/other-model", "api_base": "http://192.0.2.3/v1"}},
    ]}


def plan_and_after(mod):
    before = mapping_snapshot()
    plan = mod.make_plan(before, "selected", "demo-model", "http://192.0.2.1/v1",
                         "http://192.0.2.2/v1")
    after = copy.deepcopy(before)
    after["data"][0]["litellm_params"]["api_base"] = "http://192.0.2.2/v1"
    return before, plan, after


def test_mapping_plan_is_endpoint_only_and_audit_fields_may_change():
    mod = load("model_mapping_plan")
    before, plan, after = plan_and_after(mod)
    assert plan["body"] == {"litellm_params": {"api_base": "http://192.0.2.2/v1"}}
    assert plan["path"] == "/model/selected/update"
    assert plan["http_method"] == "PATCH"
    assert "api_key" not in json.dumps(plan)
    after["data"][0]["model_info"]["updated_at"] = "after"
    after["data"][0]["model_info"]["updated_by"] = "operator"
    mod.verify(before, after, plan)


def test_mapping_default_check_never_creates_plan(tmp_path):
    mod = load("model_mapping_plan")
    before, output = tmp_path / "before.json", tmp_path / "plan.json"
    before.write_text(json.dumps(mapping_snapshot()))
    args = ["--before", str(before), "--deployment-id", "selected", "--model", "demo-model",
            "--old-api-base", "http://192.0.2.1/v1", "--new-api-base", "http://192.0.2.2/v1",
            "--plan", str(output)]
    assert mod.main(args) == 0
    assert not output.exists()
    assert mod.main([*args, "--apply"]) == 1
    assert not output.exists()
    assert mod.main([*args, "--apply", "--ack", mod.ACK]) == 0
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("change", ["budget", "key", "other-model", "delete", "new"])
def test_mapping_verify_rejects_any_unrelated_change(change):
    mod = load("model_mapping_plan")
    before, plan, after = plan_and_after(mod)
    if change == "budget":
        after["data"][0]["model_info"]["max_input_tokens"] = 999
    elif change == "key":
        after["data"][0]["litellm_params"]["api_key"] = "different"
    elif change == "other-model":
        after["data"][1]["litellm_params"]["api_base"] = "http://192.0.2.4/v1"
    elif change == "delete":
        after["data"].pop()
    else:
        extra = copy.deepcopy(after["data"][1])
        extra["model_info"]["id"] = "new"
        after["data"].append(extra)
    with pytest.raises(ValueError):
        mod.verify(before, after, plan)


@pytest.mark.parametrize("url", ["file:///v1", "http://user:pass@example.com/v1",
                                 "http://example.com/v1?token=placeholder", "http://example.com"])
def test_mapping_rejects_credentialed_or_non_api_endpoint(url):
    with pytest.raises(ValueError):
        load("model_mapping_plan").endpoint(url)


def test_vm_bind_overrides_remain_optional_and_backward_compatible():
    source = (HERE / "docker-compose.yml").read_text()
    assert '${VM_BIND_HOST:-${WG_VPS_IP:-10.77.0.1}}:${VM_BIND_PORT:-8428}:8428' in source


@pytest.mark.parametrize("override,host,port", [
    ({}, "192.0.2.10", "8428"),
    ({"VM_BIND_HOST": "127.0.0.1", "VM_BIND_PORT": "18428"}, "127.0.0.1", "18428"),
])
def test_compose_vm_bind_rendering_without_starting_containers(override, host, port):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI not installed")
    if subprocess.run(["docker", "compose", "version"], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        pytest.skip("Docker Compose CLI not installed")
    env = dict(os.environ)
    env.pop("VM_BIND_HOST", None)
    env.pop("VM_BIND_PORT", None)
    env["WG_VPS_IP"] = "192.0.2.10"
    env.update(override)
    config = json.loads(subprocess.check_output(
        ["docker", "compose", "-f", str(HERE / "docker-compose.yml"),
         "--env-file", str(HERE / ".env.example"), "config", "--format", "json"],
        env=env, text=True))
    ports = config["services"]["victoriametrics"]["ports"]
    assert len(ports) == 1
    assert ports[0]["host_ip"] == host
    assert ports[0]["published"] == port
    assert ports[0]["target"] == 8428
