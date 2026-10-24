#!/usr/bin/env python3
"""受控源码发布：默认只读；不启动容器，不自动恢复数据库。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile


ACK = "SOURCE_WRITE_AUTHORIZED"
DEFAULT_PROTECTED = (
    "vps/.env", "vps/docker-compose.override.yml", "vps/docker-compose.override.yaml",
    "vps/compose.override.yml", "vps/compose.override.yaml",
)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or ".git" in path.parts:
        raise ValueError("unsafe relative path")
    return str(path)


def regular(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError("required regular file missing or symlinked")
    return path


def metadata(path: Path) -> dict:
    regular(path)
    info = path.stat()
    return {"sha256": digest(path), "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid, "gid": info.st_gid}


def protected(root: Path, extra: list[str]) -> dict:
    if (root / "vps").is_symlink():
        raise ValueError("vps must not be a symlink")
    if any(not re.fullmatch(r"[A-Za-z0-9_./-]+", name) for name in extra):
        raise ValueError("protected paths may not contain rsync pattern characters")
    names = set(DEFAULT_PROTECTED) | {relative(name) for name in extra}
    names.update(str(p.relative_to(root)) for p in (root / "vps").glob(".env*")
                 if p.name != ".env.example")
    result = {}
    for name in sorted(names):
        path = root / name
        # 缺失的 override 同样受保护，不能被发布包意外创建。
        result[name] = metadata(path) if path.exists() or path.is_symlink() else None
    if result.get("vps/.env") is None or result["vps/.env"]["mode"] != 0o600:
        raise ValueError("existing vps/.env must be a regular 0600 file")
    return result


def release_files(directory: Path, commit: str) -> tuple[Path, dict]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("expected full lowercase commit SHA")
    prefix = f"private-llm-{commit}"
    archive = regular(directory / f"{prefix}.tar")
    declared = regular(directory / f"{prefix}.commit").read_text().strip()
    if declared != commit:
        raise ValueError("release commit mismatch")
    checksum = regular(directory / f"{prefix}.tar.sha256").read_text().strip().split()
    if checksum != [digest(archive), archive.name]:
        raise ValueError("release archive checksum mismatch")
    inventory = {}
    for line in regular(directory / f"{prefix}.files.sha256").read_text().splitlines():
        value, name = line.split("  ", 1)
        name = relative(name)
        if name in inventory or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("invalid or duplicate manifest entry")
        inventory[name] = value
    if not {"vps/deploy.sh", "vps/docker-compose.yml"} <= inventory.keys():
        raise ValueError("release lacks required deployment files")
    with tarfile.open(archive) as tar:
        actual = {}
        for member in tar.getmembers():
            name = relative(member.name)
            if member.isdir():
                continue
            if not member.isfile() or name in actual:
                raise ValueError("release may contain only regular files and directories")
            stream = tar.extractfile(member)
            actual[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != inventory:
            raise ValueError("archive file inventory mismatch")
    return archive, inventory


def private_json(path: Path, body: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(body, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def source_inventory(root: Path) -> dict:
    result = {}
    for path in root.rglob("*"):
        name = path.relative_to(root)
        if name.parts[0] == ".git":
            continue
        if path.is_symlink():
            raise ValueError("source symlinks are not supported")
        if path.is_file():
            result[str(name)] = digest(path)
        elif not path.is_dir():
            raise ValueError("special source files are not supported")
    return result


def authorize(args) -> None:
    if not args.apply or args.ack != ACK:
        raise ValueError(f"write requires --apply --ack {ACK}")
    tmp = os.environ.get("TMPDIR", "")
    if not tmp or not Path(tmp).is_absolute() or not Path(tmp).is_dir():
        raise ValueError("explicit existing absolute TMPDIR required for writes")


def recoveries(database: Path | None, state: Path | None, verified: bool) -> dict:
    if not verified or database is None or state is None:
        raise ValueError("operator-verified database and persistent-state recovery files required")
    result = {}
    if database.resolve() == state.resolve():
        raise ValueError("database and state recovery files must be distinct")
    for name, path in (("database", database), ("state", state)):
        regular(path)
        if not path.stat().st_size or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("recovery file must be nonempty and private")
        result[name] = {"path": str(path.resolve()), "sha256": digest(path)}
    return result


def verify_recoveries(values: dict) -> None:
    if set(values) != {"database", "state"}:
        raise ValueError("incomplete recovery receipt")
    for item in values.values():
        path = regular(Path(item["path"]))
        if stat.S_IMODE(path.stat().st_mode) & 0o077 or digest(path) != item["sha256"]:
            raise ValueError("recovery file checksum mismatch")


def snapshot(args, root: Path, config: dict) -> dict:
    recovery = recoveries(args.database_backup, args.state_backup, args.recovery_verified)
    destination = args.receipt.resolve()
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError("recovery directory must be outside deployment source")
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    archive = destination / "source.tar"
    try:
        inventory = source_inventory(root)
        with archive.open("xb") as stream:
            os.chmod(archive, 0o600)
            with tarfile.open(fileobj=stream, mode="w") as tar:
                for entry in sorted(root.iterdir()):
                    if entry.name == ".git":
                        continue
                    tar.add(entry, arcname=entry.name, recursive=True)
            stream.flush()
            os.fsync(stream.fileno())
        with tarfile.open(archive) as tar:
            archived_files = {}
            for member in tar.getmembers():
                relative(member.name)
                if not member.isfile() and not member.isdir():
                    raise ValueError("source recovery contains unsupported links or special files")
                if member.isfile():
                    archived_files[member.name] = hashlib.file_digest(
                        tar.extractfile(member), "sha256").hexdigest()
            if archived_files != inventory:
                raise ValueError("source recovery file inventory mismatch")
        body = {"version": 2, "root": str(root), "source_sha256": digest(archive),
                "source_files": inventory, "protected": config, "recovery": recovery,
                "target_commit": args.commit}
        if protected(root, args.protect) != config or source_inventory(root) != inventory:
            raise ValueError("deployment source or configuration changed during backup")
        private_json(destination / "receipt.json", body)
        return body
    except Exception:
        # 失败目录保留，不能把不完整的备份误标为可恢复。
        raise


def receipt(args, root: Path, config: dict) -> dict:
    body = json.loads(regular(args.receipt / "receipt.json").read_text())
    if body.get("version") != 2 or body.get("root") != str(root):
        raise ValueError("receipt deployment identity mismatch")
    if digest(regular(args.receipt / "source.tar")) != body["source_sha256"]:
        raise ValueError("source recovery checksum mismatch")
    verify_recoveries(body["recovery"])
    if config != body["protected"]:
        raise ValueError("requested protected paths or configuration differ from recovery point")
    return body


def source_intent(action: str, body: dict, before: dict, target: dict) -> dict:
    return {"version": 1, "action": action, "root": body["root"],
            "target_commit": body["target_commit"], "source_sha256": body["source_sha256"],
            "before_source_files": before, "target_source_files": target}


def read_intent(path: Path, action: str, body: dict) -> dict:
    value = json.loads(regular(path).read_text())
    expected = source_intent(action, body, value["before_source_files"], value["target_source_files"])
    if value != expected:
        raise ValueError("source intent identity mismatch")
    for inventory in (value["before_source_files"], value["target_source_files"]):
        if not isinstance(inventory, dict):
            raise ValueError("invalid source intent inventory")
        for name, checksum in inventory.items():
            if relative(name) != name or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ValueError("invalid source intent entry")
    if action == "stage" and value["before_source_files"] != body["source_files"]:
        raise ValueError("stage intent baseline mismatch")
    if action == "restore-source" and value["target_source_files"] != body["source_files"]:
        raise ValueError("restore intent target mismatch")
    return value


def explained_mix(actual: dict, before: dict, target: dict) -> bool:
    # 缺失也属于状态：只有某一已知端点本来就没有该文件，才允许中断时缺失。
    return all(actual.get(name) in (before.get(name), target.get(name))
               for name in actual.keys() | before.keys() | target.keys())


def restore_source_allowed(directory: Path, body: dict, actual: dict) -> None:
    baseline = body["source_files"]
    if actual == baseline:
        return
    intent_path = directory / "stage-intent.json"
    intent = read_intent(intent_path, "stage", body)
    target = intent["target_source_files"]
    completion_path = directory / "stage-complete.json"
    if completion_path.exists():
        completion = json.loads(regular(completion_path).read_text())
        if completion.get("intent_sha256") != digest(intent_path) or \
                completion.get("target_source_files") != target:
            raise ValueError("stage completion does not match durable intent")
        if actual == target:
            return
        # 已完成发布后的混合状态，必须另有显式恢复的中断记录，不能推断为新写入可覆盖。
        restore = read_intent(directory / "restore-source-intent.json", "restore-source", body)
        if not explained_mix(restore["before_source_files"], baseline, target) or \
                not explained_mix(actual, restore["before_source_files"], baseline):
            raise ValueError("source changes are not explained by restore intent")
    elif not explained_mix(actual, baseline, target):
        raise ValueError("source changes are not explained by stage intent")


def extract(archive: Path, stage: Path) -> None:
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            relative(member.name)
            if member.issym() or member.islnk():
                # 源码恢复包也不允许链接绕过保护路径。
                raise ValueError("symlink/hardlink archive entries are not supported")
            if not member.isdir() and not member.isfile():
                raise ValueError("special archive entry is not supported")
        tar.extractall(stage, filter="data")


def sync_source(stage: Path, root: Path, names: dict) -> None:
    for name in names:
        # rsync 排除目录本身之外，还必须防止发布包用其祖先的链接替换。
        current = root
        for part in Path(name).parts[:-1]:
            current /= part
            if current.is_symlink():
                raise ValueError("protected file has a symlinked parent")
    command = ["rsync", "-a", "--delete", "--exclude=/.git"]
    command.extend(f"--exclude=/{name}" for name in names)
    command.extend([str(stage) + "/", str(root) + "/"])
    # 不打印子进程输出，避免任意文件名或现场路径进入公开日志。
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(f"source sync failed (exit {result.returncode}); recovery retained")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="check",
                        choices=("check", "backup", "stage", "restore-source"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--release-dir", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--protect", action="append", default=[])
    parser.add_argument("--database-backup", type=Path)
    parser.add_argument("--state-backup", type=Path)
    parser.add_argument("--recovery-verified", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--ack", default="")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        if not root.is_dir() or root == Path("/"):
            raise ValueError("invalid deployment root")
        config = protected(root, args.protect)
        archive, inventory = None, None
        if args.action != "restore-source":
            if args.release_dir is None or args.commit is None:
                raise ValueError("release directory and commit required")
            archive, inventory = release_files(args.release_dir, args.commit)
        if args.action == "check":
            print(json.dumps({"status": "checked", "commit": args.commit,
                              "files": len(inventory), "protected_files": len(config),
                              "environment_write": False}))
            return 0
        authorize(args)
        if args.receipt is None:
            raise ValueError("receipt directory required")
        if args.action == "backup":
            snapshot(args, root, config)
        else:
            body = receipt(args, root, config)
            if (args.receipt / f"{args.action}-complete.json").exists():
                raise ValueError("source action already completed; inspect existing receipt")
            if args.action == "stage" and body["target_commit"] != args.commit:
                raise ValueError("receipt target commit mismatch")
            if args.action == "restore-source":
                archive = args.receipt / "source.tar"
            stage = Path(tempfile.mkdtemp(prefix="portal-source-", dir=os.environ["TMPDIR"]))
            # 暂存目录即使失败也保留；现场核验无进程引用后由协调者清理。
            extract(archive, stage)
            before = source_inventory(root)
            intent_path = args.receipt / f"{args.action}-intent.json"
            if args.action == "stage":
                if before != body["source_files"]:
                    raise ValueError("source changed since recovery point")
                target = {name: checksum for name, checksum in inventory.items()
                          if name not in body["protected"]}
                target.update({name: before[name] for name in body["protected"] if name in before})
                private_json(intent_path, source_intent(args.action, body, before, target))
            else:
                restore_source_allowed(args.receipt, body, before)
                target = body["source_files"]
                if intent_path.exists():
                    intent = read_intent(intent_path, args.action, body)
                    if not explained_mix(before, intent["before_source_files"], target):
                        raise ValueError("source differs from interrupted restore intent")
                else:
                    private_json(intent_path, source_intent(args.action, body, before, target))
            if source_inventory(root) != before or protected(root, args.protect) != body["protected"]:
                raise ValueError("source or protected configuration changed before sync")
            sync_source(stage, root, body["protected"])
            if protected(root, args.protect) != body["protected"]:
                raise ValueError("protected configuration changed during source sync")
            actual = source_inventory(root)
            if actual != target:
                raise ValueError("source target inventory mismatch")
            private_json(args.receipt / f"{args.action}-complete.json",
                         {"status": "source-only", "deployment_executed": False,
                          "intent_sha256": digest(intent_path), "target_source_files": actual})
        print(json.dumps({"status": args.action, "deployment_executed": False}))
        return 0
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, RuntimeError):
        # 不回显异常：解析失败的输入可能包含真实配置或凭据。
        print(json.dumps({"status": "failed", "action": args.action,
                          "recovery_retained": True}), file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
