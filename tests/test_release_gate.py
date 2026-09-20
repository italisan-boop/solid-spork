from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from unittest.mock import patch

from deploy.release import ReleaseError, build_release, main, verify_release


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.allowlist = self.repository / "deploy" / "release-allowlist.txt"
        self._write("app.py", b"print('application')\n")
        self._write("deploy/start.sh", b"#!/usr/bin/env bash\nexec python app.py\n")
        self._write("requirements.txt", b"flask==3.0.0\n")
        self._write_allowlist(
            "app.py\n"
            "deploy/start.sh\n"
            "requirements.txt\n"
        )
        self._git("init")
        self._git("config", "user.email", "release-gate@example.invalid")
        self._git("config", "user.name", "Release Gate")
        self._git("add", ".")
        self._git("commit", "-m", "release fixture")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", os.fspath(self.repository), *arguments],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _write(self, relative_path: str, content: bytes) -> Path:
        target = self.repository / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def _write_allowlist(self, content: str) -> None:
        self.allowlist.parent.mkdir(parents=True, exist_ok=True)
        self.allowlist.write_text(content, encoding="utf-8")

    def _output_directory(self, name: str) -> Path:
        output = self.root / name
        output.mkdir()
        return output

    def _build(self, output: Path, *, require_clean_tree: bool = False):
        return build_release(
            repo_root=self.repository,
            output_directory=output,
            release_id="release-20260919",
            source_date_epoch=1_789_753_600,
            require_clean_tree=require_clean_tree,
        )

    def _update_integrity_sidecars(self, artifact: Path, manifest: Path, checksum: Path) -> None:
        document = json.loads(manifest.read_text(encoding="utf-8"))
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        document["artifact"] = {
            "filename": artifact.name,
            "sha256": digest,
            "size": artifact.stat().st_size,
        }
        manifest.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        checksum.write_text(f"{digest}  {artifact.name}\n", encoding="ascii")

    @staticmethod
    def _tar_info(name: str, *, epoch: int, directory: bool = False) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.mode = 0o755 if directory else 0o644
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        info.mtime = epoch
        if directory:
            info.type = tarfile.DIRTYPE
        return info

    def _replace_with_hostile_archive(
        self,
        artifact: Path,
        manifest: Path,
        checksum: Path,
        *,
        member: tarfile.TarInfo,
    ) -> None:
        epoch = 1_789_753_600
        with artifact.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=epoch) as zipped:
                with tarfile.open(fileobj=zipped, mode="w", format=tarfile.GNU_FORMAT) as archive:
                    archive.addfile(self._tar_info("bookapp-release-20260919", epoch=epoch, directory=True))
                    archive.addfile(member)
        self._update_integrity_sidecars(artifact, manifest, checksum)

    def test_build_is_deterministic_and_verifies_exact_members(self):
        first = self._build(self._output_directory("first"), require_clean_tree=True)
        second = self._build(self._output_directory("second"), require_clean_tree=True)
        for first_path, second_path in zip(first, second):
            self.assertEqual(first_path.read_bytes(), second_path.read_bytes())

        artifact, manifest, checksum = first
        verify_release(repo_root=self.repository, artifact_path=artifact)
        document = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertFalse(document["git"]["dirty"])
        self.assertEqual("release-20260919", document["release_id"])
        self.assertEqual(1_789_753_600, document["source_date_epoch"])
        self.assertEqual(
            ["app.py", "deploy/start.sh", "requirements.txt"],
            [item["path"] for item in document["files"]],
        )
        self.assertEqual(
            f"{document['artifact']['sha256']}  {artifact.name}\n",
            checksum.read_text(encoding="ascii"),
        )
        with tarfile.open(artifact, mode="r:gz") as archive:
            members = {member.name: member for member in archive.getmembers()}
        self.assertEqual(
            {
                "bookapp-release-20260919",
                "bookapp-release-20260919/app.py",
                "bookapp-release-20260919/deploy",
                "bookapp-release-20260919/deploy/start.sh",
                "bookapp-release-20260919/requirements.txt",
            },
            set(members),
        )
        self.assertEqual(0o755, members["bookapp-release-20260919/deploy/start.sh"].mode)
        self.assertEqual(0o644, members["bookapp-release-20260919/app.py"].mode)

    def test_dirty_tree_is_recorded_and_can_be_required_clean(self):
        self._write("app.py", b"print('changed')\n")
        artifact, manifest, _ = self._build(self._output_directory("dirty"))
        self.assertTrue(json.loads(manifest.read_text(encoding="utf-8"))["git"]["dirty"])
        verify_release(repo_root=self.repository, artifact_path=artifact)

        clean_output = self._output_directory("clean-required")
        with self.assertRaisesRegex(ReleaseError, "not clean"):
            self._build(clean_output, require_clean_tree=True)
        self.assertEqual([], list(clean_output.iterdir()))

    def test_cli_accepts_source_date_epoch_environment(self):
        output = self._output_directory("environment")
        with patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1789753600"}):
            result = main(
                [
                    "build",
                    "--repo-root",
                    str(self.repository),
                    "--output-directory",
                    str(output),
                    "--release-id",
                    "environment-20260919",
                    "--require-clean-tree",
                ]
            )
        self.assertEqual(0, result)
        self.assertTrue((output / "bookapp-environment-20260919.tar.gz").is_file())

    def test_forbidden_or_noncanonical_allowlist_paths_are_rejected(self):
        self._write_allowlist(".env\n")
        with self.assertRaisesRegex(ReleaseError, "forbidden"):
            self._build(self._output_directory("forbidden"))

        self._write_allowlist("requirements.txt\napp.py\n")
        with self.assertRaisesRegex(ReleaseError, "sorted"):
            self._build(self._output_directory("unsorted"))

        self._write_allowlist("../outside.py\n")
        with self.assertRaisesRegex(ReleaseError, "invalid"):
            self._build(self._output_directory("traversal"))

    def test_rejects_crlf_shell_script_input(self):
        self._write("deploy/start.sh", b"#!/usr/bin/env bash\r\nexec python app.py\r\n")
        with self.assertRaisesRegex(ReleaseError, "LF line endings"):
            self._build(self._output_directory("crlf-script"), require_clean_tree=False)

    def test_symbolic_release_input_is_rejected(self):
        target = self.repository / "linked.py"
        try:
            target.symlink_to(self.repository / "app.py")
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")
        self._write_allowlist("linked.py\n")
        with self.assertRaisesRegex(ReleaseError, "symbolic link"):
            self._build(self._output_directory("symlink"))

    def test_verify_rejects_tampered_artifact_and_allowlist(self):
        artifact, manifest, _ = self._build(self._output_directory("tampered"), require_clean_tree=True)
        with artifact.open("ab") as output:
            output.write(b"tampered")
        with self.assertRaisesRegex(ReleaseError, "checksum"):
            verify_release(repo_root=self.repository, artifact_path=artifact)

        artifact, _, _ = self._build(self._output_directory("allowlist"), require_clean_tree=True)
        self._write_allowlist(
            "app.py\n"
            "deploy/start.sh\n"
            "requirements.txt\n"
            "unlisted.py\n"
        )
        self._write("unlisted.py", b"print('not included')\n")
        with self.assertRaisesRegex(ReleaseError, "allowlist"):
            verify_release(repo_root=self.repository, artifact_path=artifact)

    def test_verify_rejects_archive_traversal_and_hardlinks(self):
        for index, hostile in enumerate(("traversal", "hardlink")):
            with self.subTest(hostile=hostile):
                output = self._output_directory(f"hostile-{index}")
                artifact, manifest, checksum = self._build(output, require_clean_tree=True)
                if hostile == "traversal":
                    member = self._tar_info("../outside", epoch=1_789_753_600)
                else:
                    member = self._tar_info(
                        "bookapp-release-20260919/app.py",
                        epoch=1_789_753_600,
                    )
                    member.type = tarfile.LNKTYPE
                    member.linkname = "bookapp-release-20260919/app.py"
                    member.size = 0
                self._replace_with_hostile_archive(
                    artifact,
                    manifest,
                    checksum,
                    member=member,
                )
                with self.assertRaises(ReleaseError):
                    verify_release(repo_root=self.repository, artifact_path=artifact)
    def test_failed_sidecar_write_cleans_partial_output(self):
        output = self._output_directory("partial-sidecar")

        def fail_after_creating(path, _data):
            path.write_bytes(b"partial")
            raise OSError("disk full")

        with patch("deploy.release._write_sidecar", side_effect=fail_after_creating):
            with self.assertRaises(OSError):
                self._build(output, require_clean_tree=True)
        self.assertEqual([], list(output.iterdir()))


if __name__ == "__main__":
    unittest.main()
