"""Cross-client transaction coordination."""

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from codelux.adapters.base import ClientAdapter
from codelux.errors import CodeluxError, RecoveryRequiredError
from codelux.models import FileState, Manifest, OperationState, PreparedChange
from codelux.snapshots import SnapshotStore


@dataclass(frozen=True)
class TransactionResult:
    committed_clients: Tuple[str, ...]
    rolled_back_clients: Tuple[str, ...]


class TransactionCoordinator:
    """Coordinate already-prepared changes without claiming cross-file atomicity."""

    def __init__(self, adapters: Mapping[str, ClientAdapter]) -> None:
        self.adapters = dict(adapters)

    def commit_all(
        self,
        changes: Tuple[PreparedChange, ...],
        manifest: Optional[Manifest] = None,
        snapshots: Optional[SnapshotStore] = None,
    ) -> TransactionResult:
        committed = []
        manifest_state = manifest
        if manifest_state is not None and snapshots is not None:
            manifest_state = snapshots.set_operation_state(
                manifest_state, OperationState.COMMITTING
            )
        try:
            for change in changes:
                adapter = self._adapter(change.client)
                adapter.commit(change)
                committed.append(change)
                if manifest_state is not None and snapshots is not None:
                    manifest_state = snapshots.update_client_state(
                        manifest_state,
                        change.client,
                        FileState.MODIFIED,
                        OperationState.COMMITTING,
                    )
        except Exception as exc:
            rolled_back = []
            rollback_errors = []
            failed_clients = []
            internal_recovery_required = isinstance(exc, RecoveryRequiredError)
            if internal_recovery_required:
                failed_clients.append(change.client)
            for change in reversed(committed):
                try:
                    self._adapter(change.client).rollback(change)
                    rolled_back.append(change.client)
                    if manifest_state is not None and snapshots is not None:
                        manifest_state = snapshots.update_client_state(
                            manifest_state,
                            change.client,
                            FileState.ROLLED_BACK,
                            OperationState.ROLLING_BACK,
                        )
                except Exception as rollback_exc:
                    rollback_errors.append(rollback_exc)
                    failed_clients.append(change.client)
            if rollback_errors or internal_recovery_required:
                if manifest_state is not None and snapshots is not None:
                    for client in failed_clients:
                        manifest_state = snapshots.update_client_state(
                            manifest_state,
                            client,
                            FileState.RECOVERY_REQUIRED,
                            OperationState.RECOVERY_REQUIRED,
                        )
                    snapshots.write_recovery(manifest_state)
                if internal_recovery_required and not rollback_errors:
                    raise exc
                raise RecoveryRequiredError(
                    "transaction rollback failed; recovery is required"
                ) from rollback_errors[0]
            if manifest_state is not None and snapshots is not None:
                manifest_state = snapshots.set_operation_state(
                    manifest_state, OperationState.ROLLED_BACK
                )
            if isinstance(exc, CodeluxError):
                raise
            raise CodeluxError("client transaction failed and was rolled back") from exc
        if manifest_state is not None and snapshots is not None:
            snapshots.set_operation_state(manifest_state, OperationState.COMMITTED)
        return TransactionResult(tuple(c.client for c in committed), ())

    def _adapter(self, client: str) -> ClientAdapter:
        try:
            return self.adapters[client]
        except KeyError as exc:
            raise CodeluxError(f"no adapter registered for {client}") from exc
