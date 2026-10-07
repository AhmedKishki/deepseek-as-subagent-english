"""Expose verified intent identity without enumerating or acknowledging recovery."""
from __future__ import annotations

from . import transaction_journal as journal


def intent_identity(config, transaction_id: str, tool: str, digest: bytes | None) -> dict:
    """Read exactly one private intent through the journal's hardened backend."""
    if digest is None:
        return {}
    try:
        identity = journal._workspace_identity(config)
        identifier = journal._transaction_id(transaction_id)
        with journal._store(identity) as store:
            saved = store.read(journal._name(identifier))
        if saved is None:
            return {}
        record = journal._decode(saved[0])
        if (record.workspace_identity != identity or record.transaction_id != identifier
                or record.tool != tool or record.sha256 != digest.hex()):
            return {}
        return {"path": record.path, "sha256": record.sha256}
    except (journal.TransactionJournalError, OSError, RuntimeError, ValueError):
        # Missing identity stays unknown; the explicit recovery protocol still applies.
        return {}
