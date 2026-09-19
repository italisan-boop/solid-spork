from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from controlplane.deployments import (
    DeploymentFailure,
    DeploymentJob,
    published_route_hosts,
    tenant_system_user,
)
from controlplane.materializer import ManifestSigner, materialize_runtime
from controlplane.secret_envelopes import EnvelopeCipher
from controlplane.route_templates import tenant_socket
from controlplane.schema import connect
from controlplane.tenants import (
    activate_managed_after_owner_claim,
    complete_managed_provisioning,
    effective_tenant_entitlements,
    get_tenant,
)
from controlplane.unit_templates import ControllerPaths, TenantStoragePaths


@dataclass(frozen=True)
class DeploymentReconciliation:
    stage: str
    applied_generation: int
    applied_hosts: tuple[str, ...] = ()


class PrivilegedDeploymentOperations(Protocol):
    def ensure_tenant_identity(self, tenant_id: str) -> int:
        ...

    def prepare_tenant_storage(self, *, tenant_id: str, uid: int) -> None:
        ...

    def initialize_tenant_database(
        self,
        *,
        tenant_id: str,
        uid: int,
        canonical_host: str,
        owner_telegram_id: int,
        display_name: str,
    ) -> None:
        ...

    def owner_claim_verdict(self, *, tenant_id: str, owner_telegram_id: int) -> str:
        ...

    def write_runtime_material(
        self,
        *,
        tenant_id: str,
        runtime_generation: int,
        credentials: dict[str, bytes],
        manifest: bytes,
        signature: bytes,
        public_key: bytes,
    ) -> None:
        ...

    def install_tenant_unit(self, *, tenant_id: str) -> None:
        ...

    def start_tenant_unit(self, tenant_id: str) -> None:
        ...

    def check_tenant_health(self, tenant_id: str, runtime_generation: int) -> None:
        ...

    def publish_tenant_route(self, *, tenant_id: str, hosts: list[str]) -> None:
        ...


class RootDeploymentAdapter:
    def __init__(
        self,
        control_database_path: str | Path,
        *,
        paths: ControllerPaths,
        cipher: EnvelopeCipher,
        signer: ManifestSigner,
        operations: PrivilegedDeploymentOperations,
    ):
        self.control_database_path = Path(control_database_path)
        self.paths = paths
        self.cipher = cipher
        self.signer = signer
        self.operations = operations

    @staticmethod
    def _run_stage(stage: str, operation):
        try:
            return operation()
        except DeploymentFailure:
            raise
        except Exception as exc:
            raise DeploymentFailure(stage, type(exc).__name__) from exc

    def _current_tenant(self, tenant, generation: int):
        current = get_tenant(self.control_database_path, tenant.id)
        if current is None:
            raise DeploymentFailure("credentials_materialized", "TenantMissing")
        if current.runtime_generation != generation:
            raise DeploymentFailure("credentials_materialized", "GenerationSuperseded")
        return current

    def _publish_route_if_current(self, tenant, generation: int, hosts: list[str]) -> None:
        database = connect(self.control_database_path)
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT runtime_generation FROM platform_tenants WHERE id = ?",
                (tenant.id,),
            ).fetchone()
            if row is None or row[0] != generation:
                raise DeploymentFailure("routing_published", "GenerationSuperseded")
            self.operations.publish_tenant_route(tenant_id=tenant.id, hosts=hosts)
            database.commit()
        except Exception:
            database.rollback()
            raise
        finally:
            database.close()

    def reconcile(self, job: DeploymentJob) -> DeploymentReconciliation:
        tenant = get_tenant(self.control_database_path, job.tenant_id)
        if tenant is None:
            raise ValueError("tenant not found")
        storage = self._validated_storage_paths(tenant)

        if job.operation == "provision":
            if tenant.lifecycle_state not in {"draft", "migration_failed"}:
                raise ValueError("tenant is not ready for managed provisioning")
            tenant = self._provision(job, tenant, storage)
        elif job.operation == "activate":
            if tenant.lifecycle_state != "awaiting_owner_claim":
                raise ValueError("tenant is not awaiting owner claim")
            claim_verdict = self._run_stage(
                "schema_initialized",
                lambda: self.operations.owner_claim_verdict(
                    tenant_id=tenant.id,
                    owner_telegram_id=tenant.owner_telegram_id,
                ),
            )
            tenant = self._run_stage(
                "schema_initialized",
                lambda: activate_managed_after_owner_claim(
                    self.control_database_path,
                    tenant_id=tenant.id,
                    actor_telegram_id=job.actor_telegram_id,
                    claim_verdict=claim_verdict,
                ),
            )
        elif job.operation != "redeploy":
            raise ValueError("unsupported deployment operation")
        elif tenant.lifecycle_state not in {"awaiting_owner_claim", "active"}:
            raise ValueError("tenant is not ready for managed redeployment")

        latest = get_tenant(self.control_database_path, tenant.id)
        if latest is None:
            raise ValueError("tenant not found")
        if latest.lifecycle_state not in {"awaiting_owner_claim", "active"}:
            raise ValueError("tenant is not ready for managed redeployment")
        return self._deploy_runtime(
            latest,
            self._validated_storage_paths(latest),
            previous_hosts=published_route_hosts(self.control_database_path, latest.id),
            route_first=job.operation == "redeploy",
        )

    def _validated_storage_paths(self, tenant) -> TenantStoragePaths:
        tenant_id = tenant_system_user(tenant.id).removeprefix("tenant-")
        data_root = self.paths.tenant_data_root.resolve()
        backup_root = self.paths.tenant_backup_root.resolve()
        data_directory = (data_root / tenant.id).resolve()
        backup_directory = (backup_root / tenant.id).resolve()
        if (
            len(tenant_id) != 16
            or data_directory.parent != data_root
            or backup_directory.parent != backup_root
        ):
            raise ValueError("invalid tenant storage identity")
        storage = TenantStoragePaths(
            database_path=data_directory / "app.sqlite",
            media_root=data_directory / "media",
            backup_root=backup_directory / "backups",
        )
        if (
            tenant.database_path.resolve() != storage.database_path
            or tenant.media_root.resolve() != storage.media_root
            or tenant.backup_root.resolve() != storage.backup_root
        ):
            raise ValueError("tenant storage paths are invalid")
        return storage

    def _provision(self, job: DeploymentJob, tenant, storage: TenantStoragePaths):
        uid = self._run_stage(
            "identity_allocated",
            lambda: self.operations.ensure_tenant_identity(tenant.id),
        )
        self._run_stage(
            "storage_prepared",
            lambda: self.operations.prepare_tenant_storage(
                tenant_id=tenant.id,
                uid=uid,
            ),
        )
        self._run_stage(
            "schema_initialized",
            lambda: self.operations.initialize_tenant_database(
                tenant_id=tenant.id,
                uid=uid,
                canonical_host=tenant.canonical_host,
                owner_telegram_id=tenant.owner_telegram_id,
                display_name=tenant.display_name,
            ),
        )
        return self._run_stage(
            "schema_initialized",
            lambda: complete_managed_provisioning(
                self.control_database_path,
                tenant_id=tenant.id,
                actor_telegram_id=job.actor_telegram_id,
                tenant_database_path=storage.database_path,
            ),
        )

    def _deploy_runtime(
        self,
        tenant,
        storage: TenantStoragePaths,
        *,
        previous_hosts: list[str] | None,
        route_first: bool,
    ) -> DeploymentReconciliation:
        generation = tenant.runtime_generation
        material = self._run_stage(
            "credentials_materialized",
            lambda: materialize_runtime(
                self.control_database_path,
                tenant=tenant,
                entitlements=effective_tenant_entitlements(
                    self.control_database_path, tenant.id
                ),
                cipher=self.cipher,
                signer=self.signer,
                socket_path=tenant_socket(self.paths.runtime_root, tenant.id),
            ),
        )
        self._current_tenant(tenant, generation)
        withdrawn_hosts: list[str] | None = None
        if route_first and previous_hosts is not None:
            desired_hosts = set(material.allowed_hosts)
            retained_hosts = sorted(set(previous_hosts) & desired_hosts)
            if set(previous_hosts) - desired_hosts:
                self._run_stage(
                    "routing_published",
                    lambda: self._publish_route_if_current(
                        tenant, generation, retained_hosts
                    ),
                )
                withdrawn_hosts = retained_hosts
        self._run_stage(
            "credentials_materialized",
            lambda: self.operations.write_runtime_material(
                tenant_id=tenant.id,
                runtime_generation=generation,
                credentials=material.credentials,
                manifest=material.manifest,
                signature=material.signature,
                public_key=self.signer.public_key(),
            ),
        )
        self._run_stage(
            "unit_started",
            lambda: self.operations.install_tenant_unit(tenant_id=tenant.id),
        )
        self._run_stage(
            "unit_started", lambda: self.operations.start_tenant_unit(tenant.id)
        )
        self._run_stage(
            "health_checked",
            lambda: self.operations.check_tenant_health(tenant.id, generation),
        )
        self._current_tenant(tenant, generation)
        if not route_first or withdrawn_hosts != material.allowed_hosts:
            self._run_stage(
                "routing_published",
                lambda: self._publish_route_if_current(
                    tenant, generation, material.allowed_hosts
                ),
            )
        return DeploymentReconciliation(
            stage="routing_published",
            applied_generation=generation,
            applied_hosts=tuple(material.allowed_hosts),
        )
