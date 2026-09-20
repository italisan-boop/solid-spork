from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
import stat
from pathlib import Path
import time
from typing import Iterator, Protocol

from controlplane.deployments import (
    DeploymentFailure,
    DeploymentJob,
    claim_next_deployment_job,
    complete_deployment_job,
    complete_teardown_job,
    recover_abandoned_deployment_jobs,
)
from controlplane.host_operations import UnixSocketPrivilegedOperations
from controlplane.materializer import ManifestSigner
from controlplane.root_adapter import DeploymentReconciliation, RootDeploymentAdapter
from controlplane.secret_envelopes import EnvelopeCipher
from controlplane.tenants import queue_missing_delivery_encryption_reconciliations
from controlplane.unit_templates import ControllerPaths


def _controller_credential_path(name: str) -> Path:
    directory_value = os.getenv("CREDENTIALS_DIRECTORY", "").strip()
    directory = Path(directory_value)
    credential = directory / name
    if (
        not directory_value
        or not directory.is_absolute()
        or directory.is_symlink()
        or not directory.is_dir()
        or credential.is_symlink()
        or not credential.is_file()
    ):
        raise ValueError("managed controller credential is unavailable")
    return credential


@contextmanager
def controller_singleton_lock(path: str | Path) -> Iterator[None]:
    if os.name != "posix":
        raise RuntimeError("managed controller requires POSIX locking")
    import fcntl

    lock_path = Path(path)
    if not lock_path.is_absolute():
        raise ValueError("controller lock path must be absolute")
    lock_path.parent.mkdir(mode=0o770, parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("managed controller is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
def wait_for_host_operations_socket(socket_path: Path, poll_seconds: int) -> None:
    while True:
        try:
            if stat.S_ISSOCK(socket_path.stat().st_mode):
                return
        except FileNotFoundError:
            pass
        time.sleep(poll_seconds)


class DeploymentAdapter(Protocol):
    def reconcile(self, job: DeploymentJob) -> str | DeploymentReconciliation:
        ...


class DeploymentController:
    def __init__(self, control_database_path: str | Path, adapter: DeploymentAdapter):
        self.control_database_path = Path(control_database_path)
        self.adapter = adapter

    def run_once(self) -> bool:
        job = claim_next_deployment_job(self.control_database_path)
        if job is None:
            return False
        try:
            result = self.adapter.reconcile(job)
        except DeploymentFailure as exc:
            if job.operation == "teardown":
                complete_teardown_job(
                    self.control_database_path,
                    job=job,
                    succeeded=False,
                    stage=exc.stage,
                    error_type=exc.error_type,
                )
            else:
                complete_deployment_job(
                    self.control_database_path,
                    job=job,
                    succeeded=False,
                    stage=exc.stage,
                    error_type=exc.error_type,
                )
        except Exception as exc:
            if job.operation == "teardown":
                complete_teardown_job(
                    self.control_database_path,
                    job=job,
                    succeeded=False,
                    stage="teardown_material",
                    error_type=type(exc).__name__,
                )
            else:
                complete_deployment_job(
                    self.control_database_path,
                    job=job,
                    succeeded=False,
                    stage="identity_allocated",
                    error_type=type(exc).__name__,
                )
        else:
            if isinstance(result, DeploymentReconciliation):
                stage = result.stage
                applied_generation = result.applied_generation
                applied_hosts = list(result.applied_hosts) or None
            else:
                stage = result
                applied_generation = job.desired_generation
                applied_hosts = None
            if job.operation == "teardown":
                complete_teardown_job(
                    self.control_database_path,
                    job=job,
                    succeeded=True,
                    stage=stage,
                )
            else:
                complete_deployment_job(
                    self.control_database_path,
                    job=job,
                    succeeded=True,
                    stage=stage,
                    applied_generation=applied_generation,
                    applied_hosts=applied_hosts,
                )
        return True


@dataclass(frozen=True)
class RootControllerSettings:
    database_path: Path
    release_root: Path
    credential_root: Path
    runtime_root: Path
    tenant_data_root: Path
    tenant_backup_root: Path
    public_key_file: Path
    controller_kek_file: Path
    controller_kek_version: str
    controller_signing_key_file: Path
    host_operations_socket: Path
    poll_seconds: int
    controller_lock_file: Path | None = None

    @classmethod
    def from_environment(cls) -> "RootControllerSettings":
        values = {
            name: os.getenv(name, "").strip()
            for name in (
                "PLATFORM_CONTROLLER_DATABASE_PATH",
                "PLATFORM_CONTROLLER_RELEASE_ROOT",
                "PLATFORM_CONTROLLER_CREDENTIAL_ROOT",
                "PLATFORM_CONTROLLER_RUNTIME_ROOT",
                "PLATFORM_CONTROLLER_TENANT_DATA_ROOT",
                "PLATFORM_CONTROLLER_TENANT_BACKUP_ROOT",
                "PLATFORM_CONTROLLER_MANIFEST_PUBLIC_KEY_FILE",
                "PLATFORM_CONTROLLER_KEK_FILE",
                "PLATFORM_CONTROLLER_SIGNING_KEY_FILE",
                "PLATFORM_CONTROLLER_HOST_OPERATIONS_SOCKET",
            )
        }
        if os.getenv("BOOKAPP_MANAGED_CONTROLLER", "").strip() == "1":
            values["PLATFORM_CONTROLLER_KEK_FILE"] = str(
                _controller_credential_path("controller_kek")
            )
            values["PLATFORM_CONTROLLER_SIGNING_KEY_FILE"] = str(
                _controller_credential_path("controller_signing_key")
            )
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError("missing root controller settings")
        raw_paths = {name: Path(value) for name, value in values.items()}
        if any(not path.is_absolute() for path in raw_paths.values()):
            raise ValueError("root controller paths must be absolute")
        paths = {name: path.resolve() for name, path in raw_paths.items()}
        lock_value = os.getenv("PLATFORM_CONTROLLER_LOCK_FILE", "").strip()
        lock_path = (
            Path(lock_value)
            if lock_value
            else paths["PLATFORM_CONTROLLER_DATABASE_PATH"].parent / "controller.lock"
        )
        if not lock_path.is_absolute():
            raise ValueError("root controller paths must be absolute")
        try:
            poll_seconds = int(os.getenv("PLATFORM_CONTROLLER_POLL_SECONDS", "5"))
        except ValueError as exc:
            raise ValueError("invalid root controller poll interval") from exc
        if poll_seconds < 1:
            raise ValueError("invalid root controller poll interval")
        return cls(
            database_path=paths["PLATFORM_CONTROLLER_DATABASE_PATH"],
            release_root=paths["PLATFORM_CONTROLLER_RELEASE_ROOT"],
            credential_root=paths["PLATFORM_CONTROLLER_CREDENTIAL_ROOT"],
            runtime_root=paths["PLATFORM_CONTROLLER_RUNTIME_ROOT"],
            tenant_data_root=paths["PLATFORM_CONTROLLER_TENANT_DATA_ROOT"],
            tenant_backup_root=paths["PLATFORM_CONTROLLER_TENANT_BACKUP_ROOT"],
            public_key_file=paths["PLATFORM_CONTROLLER_MANIFEST_PUBLIC_KEY_FILE"],
            controller_kek_file=paths["PLATFORM_CONTROLLER_KEK_FILE"],
            controller_kek_version=os.getenv("PLATFORM_CONTROLLER_KEK_VERSION", "v1"),
            controller_signing_key_file=paths["PLATFORM_CONTROLLER_SIGNING_KEY_FILE"],
            host_operations_socket=paths["PLATFORM_CONTROLLER_HOST_OPERATIONS_SOCKET"],
            poll_seconds=poll_seconds,
            controller_lock_file=lock_path.resolve(),
        )


def root_controller(settings: RootControllerSettings) -> DeploymentController:
    controller_paths = ControllerPaths(
        release_root=settings.release_root,
        credential_root=settings.credential_root,
        runtime_root=settings.runtime_root,
        tenant_data_root=settings.tenant_data_root,
        tenant_backup_root=settings.tenant_backup_root,
        public_key_file=settings.public_key_file,
    )
    operations = UnixSocketPrivilegedOperations(settings.host_operations_socket)
    adapter = RootDeploymentAdapter(
        settings.database_path,
        paths=controller_paths,
        cipher=EnvelopeCipher.from_key_file(
            settings.controller_kek_file, settings.controller_kek_version
        ),
        signer=ManifestSigner.from_key_file(settings.controller_signing_key_file),
        operations=operations,
    )
    return DeploymentController(settings.database_path, adapter)


def main() -> int:
    settings = RootControllerSettings.from_environment()
    lock_path = settings.controller_lock_file or settings.database_path.parent / "controller.lock"
    try:
        with controller_singleton_lock(lock_path):
            wait_for_host_operations_socket(
                settings.host_operations_socket, settings.poll_seconds
            )
            recover_abandoned_deployment_jobs(settings.database_path)
            queue_missing_delivery_encryption_reconciliations(settings.database_path)
            controller = root_controller(settings)
            while True:
                if not controller.run_once():
                    time.sleep(settings.poll_seconds)
    except RuntimeError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
