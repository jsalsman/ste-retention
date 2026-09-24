"""Thread-safe in-memory Cloud Storage test double with generation fencing."""

import threading
from dataclasses import dataclass
from pathlib import Path


class PreconditionFailed(Exception):
    """Represent a failed Cloud Storage generation match."""


class NotFound(Exception):
    """Represent an absent in-memory Cloud Storage object."""


@dataclass
class _Object:
    """Store immutable bytes and the generation assigned to one object version."""

    contents: bytes
    generation: int


class FakeBlob:
    """Expose the generation-conditioned subset of the Blob API used in production."""

    def __init__(self, bucket, name, generation=None):
        """Bind an object name and optional generation returned by a metadata read."""
        self.bucket = bucket
        self.name = name
        # API-created blobs learn their generation after a successful mutation.
        self.generation = generation

    def upload_from_string(self, contents, *, content_type, if_generation_match):
        """Atomically upload bytes when the expected generation is current."""
        del content_type
        with self.bucket.lock:
            current = self.bucket.objects.get(self.name)
            expected = 0 if current is None else current.generation
            if if_generation_match != expected:
                raise PreconditionFailed
            # Increment a bucket-wide counter exactly as object generations advance.
            self.bucket.next_generation += 1
            self.generation = self.bucket.next_generation
            value = contents.encode() if isinstance(contents, str) else bytes(contents)
            self.bucket.objects[self.name] = _Object(value, self.generation)
            if self.bucket.mirror_root is not None:
                # App tests model the mounted view used by status and leaderboard reads.
                path = self.bucket.mirror_root / self.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
            if self.bucket.ambiguous_uploads:
                # Mutation succeeds before a simulated lost response reports failure.
                self.bucket.ambiguous_uploads -= 1
                raise PreconditionFailed

    def download_as_bytes(self, *, if_generation_match):
        """Return bytes only when the metadata generation remains current."""
        with self.bucket.lock:
            current = self.bucket.objects.get(self.name)
            if current is None:
                raise NotFound
            if current.generation != if_generation_match:
                raise PreconditionFailed
            return current.contents

    def delete(self, *, if_generation_match):
        """Atomically delete only the object generation observed by the caller."""
        with self.bucket.lock:
            current = self.bucket.objects.get(self.name)
            if current is None:
                raise NotFound
            if current.generation != if_generation_match:
                raise PreconditionFailed
            del self.bucket.objects[self.name]
            if self.bucket.mirror_root is not None:
                # Mirror API deletion into the fake mounted view immediately.
                (self.bucket.mirror_root / self.name).unlink(missing_ok=True)


class FakeBucket:
    """Hold object versions atomically and optionally simulate ambiguous uploads."""

    def __init__(self, mirror_root: Path | None = None):
        """Create an empty bucket with a process-safe mutation lock and generation clock."""
        self.lock = threading.Lock()
        self.objects = {}
        self.mirror_root = mirror_root
        # Generation zero means absent, matching ``if_generation_match=0`` semantics.
        self.next_generation = 0
        self.ambiguous_uploads = 0
        self.vanish_on_get = 0

    def blob(self, name):
        """Return a mutable blob handle for an object name."""
        # Handles share the bucket lock rather than carrying cached object content.
        return FakeBlob(self, name)

    def get_blob(self, name):
        """Return current object metadata or None without exposing mutable storage."""
        with self.lock:
            if self.vanish_on_get and name in self.objects:
                # Model a release between a failed create and its fresh metadata read.
                self.vanish_on_get -= 1
                del self.objects[name]
                return None
            current = self.objects.get(name)
            if current is None:
                return None
            # The returned handle pins subsequent reads to this observed generation.
            return FakeBlob(self, name, current.generation)
