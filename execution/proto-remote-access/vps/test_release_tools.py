from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path


HERE = Path(__file__).parent


def _rollback_module():
    spec = importlib.util.spec_from_file_location(
        "prepare_legacy_mcp_rollback", HERE / "prepare_legacy_mcp_rollback.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_legacy_rollback_keeps_only_explicitly_safe_global_entries(tmp_path):
    mod = _rollback_module()
    entries = [
        {"name": "absent", "url": "https://a.invalid"},
        {"name": "empty", "url": "https://b.invalid", "groups": []},
        {"name": "restricted", "url": "https://c.invalid", "groups": ["home"]},
        {"name": "null", "url": "https://d.invalid", "groups": None},
        {"name": "false", "url": "https://e.invalid", "groups": False},
        {"name": "zero", "url": "https://f.invalid", "groups": 0},
        {"name": "string", "url": "https://g.invalid", "groups": ""},
        {"name": "object", "url": "https://h.invalid", "groups": {}},
        {"name": "bad-list", "url": "https://i.invalid", "groups": [0]},
        "structurally-invalid",
    ]
    registry = tmp_path / "external-mcp.json"
    registry.write_text(json.dumps(entries))
    registry.chmod(0o640)
    inode = registry.stat().st_ino
    result = mod.prepare(registry, tmp_path / "backup", tmp_path / "quarantine")
    assert registry.stat().st_ino == inode
    assert registry.stat().st_mode & 0o777 == 0o640
    assert [e["name"] for e in json.loads(registry.read_text())] == ["absent", "empty"]
    assert result["kept"] == 2 and result["quarantined"] == 8


def test_legacy_rollback_aborts_without_mutating_invalid_registry(tmp_path):
    mod = _rollback_module()
    for index, raw in enumerate((b"not-json\n", b'{"name":"not-a-list"}\n')):
        registry = tmp_path / f"external-mcp-{index}.json"
        registry.write_bytes(raw)
        try:
            mod.prepare(registry, tmp_path / f"backup-{index}",
                        tmp_path / f"quarantine-{index}")
        except RuntimeError as exc:
            assert "parse failed" in str(exc) or "root must be a list" in str(exc)
        else:
            raise AssertionError("invalid registry must abort")
        assert registry.read_bytes() == raw


def test_restore_file_preserves_target_inode_and_backup_bytes(tmp_path):
    target, backup = tmp_path / "nginx.conf", tmp_path / "nginx.conf.backup"
    target.write_text("candidate\n")
    backup.write_text("previous\n")
    inode = target.stat().st_ino
    subprocess.run(["bash", "-c",
                    'source "$1"; restore_file_same_inode "$2" "$3"', "bash",
                    str(HERE / "deploy-lib.sh"), str(backup), str(target)], check=True)
    assert target.stat().st_ino == inode
    assert target.read_bytes() == backup.read_bytes()


def test_release_package_binds_commit_and_has_file_inventory(tmp_path):
    root = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=HERE,
                                   text=True).strip()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                     text=True).strip()
    subprocess.run([str(HERE / "release-package.sh"), commit, str(tmp_path)],
                   cwd=root, check=True)
    assert (tmp_path / f"private-llm-{commit}.commit").read_text().strip() == commit
    checksum = tmp_path / f"private-llm-{commit}.tar.sha256"
    assert checksum.read_text().split()[1] == f"private-llm-{commit}.tar"
    inventory = (tmp_path / f"private-llm-{commit}.files.sha256").read_text()
    assert "./console/console.py" in inventory
    assert "vps/.env\n" not in inventory

    relocated = tmp_path / "relocated"
    relocated.mkdir()
    shutil.copy2(tmp_path / f"private-llm-{commit}.tar", relocated)
    shutil.copy2(checksum, relocated)
    subprocess.run(["shasum", "-a", "256", "-c", checksum.name],
                   cwd=relocated, check=True)
