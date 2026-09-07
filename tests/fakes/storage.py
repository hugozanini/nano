"""In-memory stand-in for google.cloud.storage.Client.

Only what receipt archival uses: bucket -> blob -> upload_from_string. Uploads
are recorded so tests can assert on the object path, which is the part that has
to stay stable for a human browsing the bucket.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UploadedObject:
    bucket: str
    path: str
    content: bytes
    content_type: str


class FakeBlob:
    def __init__(self, client: FakeStorageClient, bucket: str, path: str):
        self._client = client
        self._bucket = bucket
        self.name = path

    def upload_from_string(self, data: bytes, content_type: str = "application/octet-stream"):
        if self._client.fail_with is not None:
            raise self._client.fail_with
        self._client.uploads.append(UploadedObject(self._bucket, self.name, data, content_type))


class FakeBucket:
    def __init__(self, client: FakeStorageClient, name: str):
        self._client = client
        self.name = name

    def blob(self, path: str) -> FakeBlob:
        return FakeBlob(self._client, self.name, path)

    def exists(self) -> bool:
        return True


class FakeStorageClient:
    def __init__(self) -> None:
        self.uploads: list[UploadedObject] = field(default_factory=list)  # type: ignore[assignment]
        self.uploads = []
        self.fail_with: Exception | None = None

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self, name)

    def fails(self, error: Exception) -> FakeStorageClient:
        self.fail_with = error
        return self

    @property
    def paths(self) -> list[str]:
        return [u.path for u in self.uploads]
