"""In-memory stand-in for google.cloud.firestore.Client.

Models only the surface Nano actually uses: collection/document addressing,
get/set/update/create/delete, and transactions. `create()` raises AlreadyExists
the way the real client does, which is what the message-dedupe guard relies on.
"""

from __future__ import annotations

import copy
import threading
from typing import Any

from google.api_core import exceptions as gcp_exceptions

SERVER_TIMESTAMP = "__FAKE_SERVER_TIMESTAMP__"


class FakeDocumentSnapshot:
    def __init__(self, doc_id: str, data: dict[str, Any] | None):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data) if self._data is not None else None

    def get(self, field: str, default: Any = None) -> Any:
        return (self._data or {}).get(field, default)


class FakeDocumentReference:
    def __init__(self, client: FakeFirestoreClient, path: str):
        self._client = client
        self.path = path
        self.id = path.rsplit("/", 1)[-1]

    def get(self, transaction: Any = None) -> FakeDocumentSnapshot:
        with self._client.lock:
            self._client.reads.append(self.path)
            return FakeDocumentSnapshot(self.id, self._client.store.get(self.path))

    def set(self, data: dict[str, Any], merge: bool = False) -> None:
        with self._client.lock:
            self._client.writes.append(("set", self.path, copy.deepcopy(data)))
            if merge and self.path in self._client.store:
                self._client.store[self.path].update(copy.deepcopy(data))
            else:
                self._client.store[self.path] = copy.deepcopy(data)

    def update(self, data: dict[str, Any]) -> None:
        with self._client.lock:
            if self.path not in self._client.store:
                raise gcp_exceptions.NotFound(f"No document to update: {self.path}")
            self._client.writes.append(("update", self.path, copy.deepcopy(data)))
            self._client.store[self.path].update(copy.deepcopy(data))

    def create(self, data: dict[str, Any]) -> None:
        """Fails if the document already exists — the atomic claim primitive."""
        with self._client.lock:
            if self.path in self._client.store:
                raise gcp_exceptions.AlreadyExists(f"Document already exists: {self.path}")
            self._client.writes.append(("create", self.path, copy.deepcopy(data)))
            self._client.store[self.path] = copy.deepcopy(data)

    def delete(self) -> None:
        with self._client.lock:
            self._client.writes.append(("delete", self.path, None))
            self._client.store.pop(self.path, None)

    def collection(self, name: str) -> FakeCollectionReference:
        return FakeCollectionReference(self._client, f"{self.path}/{name}")


class FakeCollectionReference:
    def __init__(self, client: FakeFirestoreClient, path: str):
        self._client = client
        self.path = path

    def document(self, doc_id: str) -> FakeDocumentReference:
        return FakeDocumentReference(self._client, f"{self.path}/{doc_id}")

    def stream(self):
        prefix = f"{self.path}/"
        with self._client.lock:
            items = [
                (path, data)
                for path, data in self._client.store.items()
                if path.startswith(prefix) and "/" not in path[len(prefix) :]
            ]
        return [FakeDocumentSnapshot(p.rsplit("/", 1)[-1], d) for p, d in items]


class FakeTransaction:
    """Good enough for the read-modify-write patterns Nano uses.

    The real client retries on contention; the fake executes inline, which is
    what a single-threaded unit test wants. Concurrency is covered separately by
    exercising create() directly.
    """

    def __init__(self, client: FakeFirestoreClient):
        self._client = client

    def __call__(self, func, *args, **kwargs):
        return func(self, *args, **kwargs)

    def get(self, ref: FakeDocumentReference) -> FakeDocumentSnapshot:
        return ref.get()

    def set(self, ref: FakeDocumentReference, data: dict[str, Any], merge: bool = False) -> None:
        ref.set(data, merge=merge)

    def update(self, ref: FakeDocumentReference, data: dict[str, Any]) -> None:
        ref.update(data)

    def delete(self, ref: FakeDocumentReference) -> None:
        ref.delete()


class FakeFirestoreClient:
    def __init__(self, initial: dict[str, dict[str, Any]] | None = None):
        self.store: dict[str, dict[str, Any]] = copy.deepcopy(initial or {})
        self.reads: list[str] = []
        self.writes: list[tuple[str, str, dict[str, Any] | None]] = []
        self.lock = threading.Lock()

    def collection(self, name: str) -> FakeCollectionReference:
        return FakeCollectionReference(self, name)

    def document(self, path: str) -> FakeDocumentReference:
        return FakeDocumentReference(self, path)

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    # --- assertions helpers -------------------------------------------------

    def doc(self, path: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.store.get(path))

    def write_count(self, path: str) -> int:
        return sum(1 for _, p, _ in self.writes if p == path)
