#!/usr/bin/env python3
"""Build and verify deterministic BookApp source release artifacts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


_FORMAT_VERSION = 1
_RELEASE_ID_PATTERN = re.compile(r"(?:[a-z0-9]|[a-z0-9][a-z0-9._-]{0,62}[a-z0-9])\Z")
_HEX_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_DEPLOYMENT_SCRIPT_SUFFIXES = frozenset({".sh"})
_FORBIDDEN_ROOTS = frozenset(
    {
        ".beads",
        ".claude",
        ".git",
        ".github",
        ".idea",
        ".venv",
        ".vscode",
        "archives",
        "backup",
        "backups",
        "credentials",
        "keys",
        "logs",
        "media",
        "secret",
        "secrets",
        "state",
        "tests",
        "venv",
    }
)
_FORBIDDEN_COMPONENTS = frozenset({"__pycache__", ".mypy_cache", ".pytest_cache"})
_FORBIDDEN_SUFFIXES = frozenset(
    {
        ".db",
        ".key",
        ".pem",
        ".sqlite",
        ".sqlite3",
    }
)


class ReleaseError(ValueError):
    """Raised when release input or artifact integrity is invalid."""


@dataclass(frozen=True)
class ReleaseFile:
    path: PurePosixPath
    source_path: Path
    mode: int
    size: int
    sha256: str

    @property
    def archive_path(self) -> str:
        return self.path.as_posix()


@dataclass(frozen=True)
class GitMetadata:
    head: str
    dirty: bool


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular_file(path: Path, *, description: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ReleaseError(f"{description} is missing") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ReleaseError(f"{description} must not be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise ReleaseError(f"{description} must be a regular file")
    return metadata


def _require_directory(path: Path, *, description: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ReleaseError(f"{description} is missing") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseError(f"{description} must be a real directory")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_release_id(value: str) -> str:
    if not _RELEASE_ID_PATTERN.fullmatch(value) or value in {".", ".."}:
        raise ReleaseError("release ID is invalid")
    return value


def _validate_source_date_epoch(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ReleaseError("SOURCE_DATE_EPOCH must be a non-negative integer")
    return value


def _relative_path(value: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise ReleaseError("allowlist path is invalid")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ReleaseError("allowlist path is invalid")
    if candidate.as_posix() != value:
        raise ReleaseError("allowlist path is not canonical")
    return candidate


def _is_forbidden_path(path: PurePosixPath) -> bool:
    parts = path.parts
    lower_parts = tuple(part.lower() for part in parts)
    first = lower_parts[0]
    name = lower_parts[-1]
    if first in _FORBIDDEN_ROOTS or first.startswith(".env"):
        return True
    if any(part in _FORBIDDEN_COMPONENTS for part in lower_parts):
        return True
    if name == "data.db" or name.startswith(".env"):
        return True
    if name.endswith(("-wal", "-shm")) and any(
        name[: -len(suffix)].endswith((".db", ".sqlite", ".sqlite3"))
        for suffix in ("-wal", "-shm")
    ):
        return True
    return any(name.endswith(suffix) for suffix in _FORBIDDEN_SUFFIXES)


def _load_allowlist(path: Path) -> tuple[PurePosixPath, ...]:
    _require_regular_file(path, description="release allowlist")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseError("release allowlist must be UTF-8") from exc

    entries: list[PurePosixPath] = []
    seen: set[str] = set()
    for line in content.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        candidate = _relative_path(value)
        key = candidate.as_posix()
        if key in seen:
            raise ReleaseError("release allowlist contains a duplicate path")
        if _is_forbidden_path(candidate):
            raise ReleaseError("release allowlist contains a forbidden path")
        entries.append(candidate)
        seen.add(key)

    if not entries:
        raise ReleaseError("release allowlist is empty")
    if [entry.as_posix() for entry in entries] != sorted(seen):
        raise ReleaseError("release allowlist must be sorted")
    return tuple(entries)


def _deployment_mode(path: PurePosixPath) -> int:
    return 0o755 if path.suffix in _DEPLOYMENT_SCRIPT_SUFFIXES else 0o644


def _collect_release_files(repo_root: Path, allowlist: Iterable[PurePosixPath]) -> tuple[ReleaseFile, ...]:
    root = repo_root.resolve(strict=True)
    collected: list[ReleaseFile] = []
    casefolded_paths: set[str] = set()
    for relative_path in allowlist:
        source_path = root.joinpath(*relative_path.parts)
        if not _is_relative_to(source_path.resolve(strict=False), root):
            raise ReleaseError("release source escapes repository root")
        metadata = _require_regular_file(source_path, description="release source")
        resolved_source = source_path.resolve(strict=True)
        if not _is_relative_to(resolved_source, root):
            raise ReleaseError("release source escapes repository root")
        folded_path = relative_path.as_posix().casefold()
        if folded_path in casefolded_paths:
            raise ReleaseError("release allowlist has case-colliding paths")
        casefolded_paths.add(folded_path)
        if relative_path.suffix in _DEPLOYMENT_SCRIPT_SUFFIXES and b"\r\n" in source_path.read_bytes():
            raise ReleaseError("release shell scripts must use LF line endings")
        collected.append(
            ReleaseFile(
                path=relative_path,
                source_path=source_path,
                mode=_deployment_mode(relative_path),
                size=metadata.st_size,
                sha256=_sha256_file(source_path),
            )
        )
    return tuple(collected)


def _git_metadata(repo_root: Path) -> GitMetadata:
    def run_git(*arguments: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", os.fspath(repo_root), *arguments],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ReleaseError("release source must be a Git worktree") from exc
        return completed.stdout

    head = run_git("rev-parse", "HEAD").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ReleaseError("Git HEAD is invalid")
    dirty = bool(run_git("status", "--porcelain=v1", "--untracked-files=all"))
    return GitMetadata(head=head, dirty=dirty)


def _artifact_paths(output_directory: Path, release_id: str) -> tuple[Path, Path, Path]:
    stem = f"bookapp-{release_id}"
    return (
        output_directory / f"{stem}.tar.gz",
        output_directory / f"{stem}.manifest.json",
        output_directory / f"{stem}.sha256",
    )


def _assert_output_directory(repo_root: Path, output_directory: Path) -> Path:
    if not output_directory.is_absolute():
        raise ReleaseError("output directory must be absolute")
    _require_directory(output_directory, description="output directory")
    resolved_output = output_directory.resolve(strict=True)
    resolved_repo = repo_root.resolve(strict=True)
    if _is_relative_to(resolved_output, resolved_repo):
        raise ReleaseError("output directory must be outside repository root")
    if any(resolved_output.iterdir()):
        raise ReleaseError("output directory must be empty")
    return resolved_output


def _archive_directory_names(top_level: str, files: Iterable[ReleaseFile]) -> tuple[str, ...]:
    names = {top_level}
    for item in files:
        parent = PurePosixPath(top_level, item.path).parent
        while parent != PurePosixPath("."):
            names.add(parent.as_posix())
            parent = parent.parent
    return tuple(sorted(names, key=lambda name: (len(PurePosixPath(name).parts), name)))


def _tarinfo(name: str, *, mode: int, size: int, mtime: int, directory: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = mtime
    if directory:
        info.type = tarfile.DIRTYPE
        info.size = 0
    else:
        info.size = size
    return info


def _write_archive(
    artifact_path: Path,
    *,
    top_level: str,
    files: tuple[ReleaseFile, ...],
    source_date_epoch: int,
) -> None:
    with artifact_path.open("xb") as target:
        with gzip.GzipFile(
            fileobj=target,
            mode="wb",
            filename="",
            mtime=source_date_epoch,
            compresslevel=9,
        ) as gzip_output:
            with tarfile.open(fileobj=gzip_output, mode="w", format=tarfile.GNU_FORMAT) as archive:
                for directory in _archive_directory_names(top_level, files):
                    archive.addfile(
                        _tarinfo(
                            directory,
                            mode=0o755,
                            size=0,
                            mtime=source_date_epoch,
                            directory=True,
                        )
                    )
                for item in files:
                    archive_name = PurePosixPath(top_level, item.path).as_posix()
                    with item.source_path.open("rb") as source:
                        archive.addfile(
                            _tarinfo(
                                archive_name,
                                mode=item.mode,
                                size=item.size,
                                mtime=source_date_epoch,
                            ),
                            source,
                        )


def _manifest(
    *,
    artifact_path: Path,
    release_id: str,
    source_date_epoch: int,
    git: GitMetadata,
    allowlist_path: Path,
    files: tuple[ReleaseFile, ...],
) -> dict[str, object]:
    return {
        "allowlist_sha256": _sha256_file(allowlist_path),
        "artifact": {
            "filename": artifact_path.name,
            "sha256": _sha256_file(artifact_path),
            "size": artifact_path.stat().st_size,
        },
        "files": [
            {
                "mode": f"{item.mode:04o}",
                "path": item.archive_path,
                "sha256": item.sha256,
                "size": item.size,
            }
            for item in files
        ],
        "format_version": _FORMAT_VERSION,
        "git": {"dirty": git.dirty, "head": git.head},
        "release_id": release_id,
        "source_date_epoch": source_date_epoch,
    }


def _write_sidecar(path: Path, content: bytes) -> None:
    with path.open("xb") as output:
        output.write(content)


def build_release(
    *,
    repo_root: Path,
    output_directory: Path,
    release_id: str,
    source_date_epoch: int,
    allowlist_path: Path | None = None,
    require_clean_tree: bool = False,
) -> tuple[Path, Path, Path]:
    """Build a deterministic source archive and its integrity sidecars."""
    release_id = _validate_release_id(release_id)
    source_date_epoch = _validate_source_date_epoch(source_date_epoch)
    _require_directory(repo_root, description="repository root")
    root = repo_root.resolve(strict=True)
    output = _assert_output_directory(root, output_directory)
    allowlist = allowlist_path or root / "deploy" / "release-allowlist.txt"
    _require_regular_file(allowlist, description="release allowlist")
    allowlist = allowlist.resolve(strict=True)
    if not _is_relative_to(allowlist, root):
        raise ReleaseError("release allowlist must be inside repository root")
    selected_paths = _load_allowlist(allowlist)
    files = _collect_release_files(root, selected_paths)
    git = _git_metadata(root)
    if require_clean_tree and git.dirty:
        raise ReleaseError("release source worktree is not clean")

    artifact_path, manifest_path, checksum_path = _artifact_paths(output, release_id)
    top_level = f"bookapp-{release_id}"
    created: list[Path] = [artifact_path, manifest_path, checksum_path]
    try:
        _write_archive(
            artifact_path,
            top_level=top_level,
            files=files,
            source_date_epoch=source_date_epoch,
        )
        manifest = _manifest(
            artifact_path=artifact_path,
            release_id=release_id,
            source_date_epoch=source_date_epoch,
            git=git,
            allowlist_path=allowlist,
            files=files,
        )
        _write_sidecar(manifest_path, _canonical_json(manifest))
        _write_sidecar(
            checksum_path,
            f"{manifest['artifact']['sha256']}  {artifact_path.name}\n".encode("ascii"),
        )
    except Exception:
        for created_path in reversed(created):
            created_path.unlink(missing_ok=True)
        raise
    return artifact_path, manifest_path, checksum_path


def _load_json(path: Path) -> dict[str, Any]:
    _require_regular_file(path, description="release manifest")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("release manifest is invalid") from exc
    if not isinstance(value, dict):
        raise ReleaseError("release manifest is invalid")
    return value


def _expected_manifest_keys(manifest: dict[str, Any]) -> None:
    expected = {
        "allowlist_sha256",
        "artifact",
        "files",
        "format_version",
        "git",
        "release_id",
        "source_date_epoch",
    }
    if set(manifest) != expected or manifest["format_version"] != _FORMAT_VERSION:
        raise ReleaseError("release manifest schema is invalid")


def _parse_manifest_files(manifest: dict[str, Any]) -> tuple[ReleaseFile, ...]:
    entries = manifest["files"]
    if not isinstance(entries, list):
        raise ReleaseError("release manifest file list is invalid")
    files: list[ReleaseFile] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"mode", "path", "sha256", "size"}:
            raise ReleaseError("release manifest file entry is invalid")
        path = entry["path"]
        mode = entry["mode"]
        size = entry["size"]
        digest = entry["sha256"]
        if not isinstance(path, str) or not isinstance(mode, str) or type(size) is not int or not isinstance(digest, str):
            raise ReleaseError("release manifest file entry is invalid")
        relative_path = _relative_path(path)
        if _is_forbidden_path(relative_path) or mode != f"{_deployment_mode(relative_path):04o}" or size < 0:
            raise ReleaseError("release manifest file entry is invalid")
        if not _HEX_DIGEST_PATTERN.fullmatch(digest) or path in seen:
            raise ReleaseError("release manifest file entry is invalid")
        files.append(
            ReleaseFile(
                path=relative_path,
                source_path=Path(),
                mode=int(mode, 8),
                size=size,
                sha256=digest,
            )
        )
        seen.add(path)
    if [item.archive_path for item in files] != sorted(seen):
        raise ReleaseError("release manifest file list is not sorted")
    return tuple(files)


def _validate_manifest(
    manifest: dict[str, Any],
    *,
    artifact_path: Path,
    allowlist_path: Path,
) -> tuple[str, tuple[ReleaseFile, ...]]:
    _expected_manifest_keys(manifest)
    release_id = manifest["release_id"]
    epoch = manifest["source_date_epoch"]
    artifact = manifest["artifact"]
    git = manifest["git"]
    allowlist_digest = manifest["allowlist_sha256"]
    if not isinstance(release_id, str) or _validate_release_id(release_id) != release_id:
        raise ReleaseError("release manifest release ID is invalid")
    if not isinstance(epoch, int) or type(epoch) is bool:
        raise ReleaseError("release manifest epoch is invalid")
    _validate_source_date_epoch(epoch)
    if not isinstance(artifact, dict) or set(artifact) != {"filename", "sha256", "size"}:
        raise ReleaseError("release manifest artifact is invalid")
    if artifact.get("filename") != artifact_path.name or type(artifact.get("size")) is not int:
        raise ReleaseError("release manifest artifact is invalid")
    if artifact["size"] < 0 or not isinstance(artifact.get("sha256"), str) or not _HEX_DIGEST_PATTERN.fullmatch(artifact["sha256"]):
        raise ReleaseError("release manifest artifact is invalid")
    if not isinstance(git, dict) or set(git) != {"dirty", "head"} or not isinstance(git["dirty"], bool):
        raise ReleaseError("release manifest Git metadata is invalid")
    if not isinstance(git["head"], str) or not re.fullmatch(r"[0-9a-f]{40}", git["head"]):
        raise ReleaseError("release manifest Git metadata is invalid")
    if not isinstance(allowlist_digest, str) or not _HEX_DIGEST_PATTERN.fullmatch(allowlist_digest):
        raise ReleaseError("release manifest allowlist digest is invalid")
    if allowlist_digest != _sha256_file(allowlist_path):
        raise ReleaseError("release allowlist does not match manifest")
    return release_id, _parse_manifest_files(manifest)


def _read_checksum(path: Path, expected_filename: str) -> str:
    _require_regular_file(path, description="release checksum")
    try:
        content = path.read_text(encoding="ascii")
    except UnicodeDecodeError as exc:
        raise ReleaseError("release checksum is invalid") from exc
    match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\\r\n]+)\n", content)
    if match is None or match.group(2) != expected_filename:
        raise ReleaseError("release checksum is invalid")
    return match.group(1)


def _validate_member_name(name: str) -> PurePosixPath:
    if not name or "\\" in name:
        raise ReleaseError("release archive member name is invalid")
    value = PurePosixPath(name)
    if value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
        raise ReleaseError("release archive member name is invalid")
    if value.as_posix() != name:
        raise ReleaseError("release archive member name is invalid")
    return value


def _verify_archive(
    *,
    artifact_path: Path,
    top_level: str,
    files: tuple[ReleaseFile, ...],
    source_date_epoch: int,
) -> None:
    expected_file_members = {
        PurePosixPath(top_level, item.path).as_posix(): item
        for item in files
    }
    expected_directories = set(_archive_directory_names(top_level, files))
    seen_members: set[str] = set()
    try:
        archive = tarfile.open(artifact_path, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseError("release archive is invalid") from exc
    with archive:
        for member in archive:
            name = _validate_member_name(member.name)
            archive_name = name.as_posix()
            if archive_name in seen_members:
                raise ReleaseError("release archive contains duplicate members")
            seen_members.add(archive_name)
            if member.uid != 0 or member.gid != 0 or member.uname != "root" or member.gname != "root":
                raise ReleaseError("release archive member ownership is invalid")
            if member.mtime != source_date_epoch:
                raise ReleaseError("release archive member timestamp is invalid")
            if member.isdir():
                if archive_name not in expected_directories or member.mode != 0o755 or member.size != 0:
                    raise ReleaseError("release archive directory is invalid")
                continue
            if not member.isreg() or member.issym() or member.islnk():
                raise ReleaseError("release archive contains a non-regular file")
            expected = expected_file_members.get(archive_name)
            if expected is None:
                raise ReleaseError("release archive contains an unexpected file")
            if member.mode != expected.mode or member.size != expected.size:
                raise ReleaseError("release archive file metadata is invalid")
            source = archive.extractfile(member)
            if source is None:
                raise ReleaseError("release archive file is invalid")
            digest = hashlib.sha256()
            with source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected.sha256:
                raise ReleaseError("release archive file checksum is invalid")
    if seen_members != expected_directories | set(expected_file_members):
        raise ReleaseError("release archive member set is invalid")


def verify_release(
    *,
    repo_root: Path,
    artifact_path: Path,
    manifest_path: Path | None = None,
    checksum_path: Path | None = None,
    allowlist_path: Path | None = None,
) -> None:
    """Verify a release artifact without extracting its contents."""
    _require_directory(repo_root, description="repository root")
    root = repo_root.resolve(strict=True)
    _require_regular_file(artifact_path, description="release artifact")
    artifact = artifact_path.resolve(strict=True)
    if artifact_path.is_symlink():
        raise ReleaseError("release artifact must not be a symbolic link")
    stem = artifact.name.removesuffix(".tar.gz")
    if stem == artifact.name:
        raise ReleaseError("release artifact filename is invalid")
    manifest = manifest_path or artifact.with_name(f"{stem}.manifest.json")
    checksum = checksum_path or artifact.with_name(f"{stem}.sha256")
    selected_allowlist = allowlist_path or root / "deploy" / "release-allowlist.txt"
    _require_regular_file(selected_allowlist, description="release allowlist")
    selected_allowlist = selected_allowlist.resolve(strict=True)
    if not _is_relative_to(selected_allowlist, root):
        raise ReleaseError("release allowlist must be inside repository root")
    expected_paths = _load_allowlist(selected_allowlist)
    document = _load_json(manifest)
    release_id, files = _validate_manifest(
        document,
        artifact_path=artifact,
        allowlist_path=selected_allowlist,
    )
    if release_id != stem.removeprefix("bookapp-") or stem != f"bookapp-{release_id}":
        raise ReleaseError("release artifact filename does not match manifest")
    if tuple(item.path for item in files) != expected_paths:
        raise ReleaseError("release manifest does not match release allowlist")
    artifact_metadata = document["artifact"]
    assert isinstance(artifact_metadata, dict)
    actual_digest = _sha256_file(artifact)
    if actual_digest != artifact_metadata["sha256"] or artifact.stat().st_size != artifact_metadata["size"]:
        raise ReleaseError("release artifact checksum is invalid")
    if _read_checksum(checksum, artifact.name) != actual_digest:
        raise ReleaseError("release checksum does not match artifact")
    epoch = document["source_date_epoch"]
    assert isinstance(epoch, int)
    _verify_archive(
        artifact_path=artifact,
        top_level=f"bookapp-{release_id}",
        files=files,
        source_date_epoch=epoch,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    build = subcommands.add_parser("build", help="build a deterministic source release")
    build.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    build.add_argument("--output-directory", type=Path, required=True)
    build.add_argument("--release-id", required=True)
    build.add_argument("--source-date-epoch", type=int)
    build.add_argument("--allowlist", type=Path)
    build.add_argument("--require-clean-tree", action="store_true")
    verify = subcommands.add_parser("verify", help="verify a release without extraction")
    verify.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    verify.add_argument("--artifact", type=Path, required=True)
    verify.add_argument("--manifest", type=Path)
    verify.add_argument("--checksum", type=Path)
    verify.add_argument("--allowlist", type=Path)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    try:
        if options.command == "build":
            source_date_epoch = options.source_date_epoch
            if source_date_epoch is None:
                raw_epoch = os.getenv("SOURCE_DATE_EPOCH")
                if raw_epoch is None:
                    raise ReleaseError("SOURCE_DATE_EPOCH or --source-date-epoch is required")
                try:
                    source_date_epoch = int(raw_epoch)
                except ValueError as exc:
                    raise ReleaseError("SOURCE_DATE_EPOCH is invalid") from exc
            artifact, manifest, checksum = build_release(
                repo_root=options.repo_root,
                output_directory=options.output_directory,
                release_id=options.release_id,
                source_date_epoch=source_date_epoch,
                allowlist_path=options.allowlist,
                require_clean_tree=options.require_clean_tree,
            )
            print(artifact)
            print(manifest)
            print(checksum)
        else:
            verify_release(
                repo_root=options.repo_root,
                artifact_path=options.artifact,
                manifest_path=options.manifest,
                checksum_path=options.checksum,
                allowlist_path=options.allowlist,
            )
    except ReleaseError as exc:
        print(f"release error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
