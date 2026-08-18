#!/usr/bin/env python3
"""Quarantine policy-bearing MCP entries before rolling back to pre-#51 code."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from pathlib import Path


def legacy_safe(entry: object) -> bool:
    return (isinstance(entry, dict)
            and isinstance(entry.get("name"), str) and bool(entry["name"])
            and isinstance(entry.get("url"), str) and bool(entry["url"])
            and ("groups" not in entry or entry["groups"] == []))


def write_same_inode(path: Path, data: bytes, mode: int) -> None:
    with path.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(path, mode)


def prepare(path: Path, backup: Path | None = None, quarantine: Path | None = None) -> dict:
    if not path.is_file():
        raise RuntimeError(f"registry does not exist: {path}")
    raw = path.read_bytes()
    mode = path.stat().st_mode & 0o777
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup or path.with_name(f"{path.name}.pre-legacy-{stamp}.bak")
    quarantine = quarantine or path.with_name(f"{path.name}.legacy-quarantine-{stamp}.json")
    if backup.exists() or quarantine.exists():
        raise RuntimeError("backup or quarantine path already exists")
    shutil.copyfile(path, backup)
    os.chmod(backup, mode)
    if backup.read_bytes() != raw:
        raise RuntimeError("backup verification failed")
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"registry parse failed: {exc}") from exc
    if not isinstance(entries, list):
        raise RuntimeError("registry root must be a list")

    kept = [entry for entry in entries if legacy_safe(entry)]
    blocked = [entry for entry in entries if not legacy_safe(entry)]
    quarantine.write_text(json.dumps(blocked, ensure_ascii=False, indent=2) + "\n")
    os.chmod(quarantine, mode)
    replacement = (json.dumps(kept, ensure_ascii=False, indent=2) + "\n").encode()
    try:
        write_same_inode(path, replacement, mode)
        verified = json.loads(path.read_text())
        if not isinstance(verified, list) or not all(legacy_safe(entry) for entry in verified):
            raise RuntimeError("post-write compatibility verification failed")
    except Exception:
        write_same_inode(path, raw, mode)
        raise
    return {"kept": len(kept), "quarantined": len(blocked),
            "backup": str(backup), "quarantine": str(quarantine)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("registry", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--quarantine", type=Path)
    args = parser.parse_args()
    result = prepare(args.registry, args.backup, args.quarantine)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
