"""Explicit legacy stores for compatibility and copy-only upgrade tests.

Normal registration creates completion-ready stores. Legacy fixtures construct the original base
schema intentionally, so compatibility checks do not depend on the current initialization default.
"""

from pathlib import Path

from exacly import execution_store as store


def initialize_legacy_ledger(path: Path) -> None:
    """Create only a new empty v1 ledger, preserving any existing store or other path."""
    deadline = store.Deadline()
    with (
        store.locked(path, write=True, deadline=deadline) as (absolute, _),
        store.database(absolute, create=True, deadline=deadline) as (connection, created),
    ):
        store.require(created, "legacy fixture requires a new ledger")
        store.configure(connection, deadline=deadline)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"PRAGMA application_id={store.APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={store.USER_VERSION}")
        for statement in store.SCHEMA.values():
            connection.execute(statement)
        connection.commit()
        store.validate(connection, deadline=deadline)
