# ruff: noqa: E501
"""Photos and documents for the encrypted export: each file is encrypted on its own and appended to a pack file (encrypted bytes only).

The index (which record is which attachment, where it sits in which pack, its size and SHA-256) goes INSIDE the encrypted database as
the table _vault_files, so the packs themselves show nothing: no names, no sizes (padded to buckets), no order.
"""
import hashlib
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from . import vault_crypto

log = logging.getLogger("vault_attachments")
PACK_TARGET_BYTES = 256 * 1024 * 1024


def r2_fetch(object_key: str) -> bytes:
    """The stored bytes of one photo/document from the private bucket."""
    from . import attachments
    return attachments.storage().get_object(Bucket=attachments._bucket(), Key=object_key)["Body"].read()


class PackWriter:
    """Appends encrypted records to pack files in `directory`. A record never straddles two packs."""

    def __init__(self, directory: Path, recipient_pub: bytes, fetch=r2_fetch, target: int | None = None):
        self.dir, self.pub, self.fetch, self.target = Path(directory), recipient_pub, fetch, target or PACK_TARGET_BYTES
        self.packs: list[dict] = []
        self._f: Any = None
        self._hash: Any = None
        self._size = 0
        self.counts = {"files": 0, "missing": 0, "checksum_differs": 0}

    def _open_pack(self) -> None:
        self.close_pack()
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / (secrets.token_hex(12) + ".bin")
        self._f = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
        self._hash, self._size = hashlib.sha256(), 0
        self.packs.append({"path": str(path), "size": 0, "sha256": None})

    def close_pack(self) -> None:
        if self._f:
            self._f.close()
            self.packs[-1].update(size=self._size, sha256=self._hash.hexdigest())
            self._f = None

    def add(self, object_key: str, expected_sha256: str | None) -> dict:
        """Fetch, check, encrypt and append one file. Never raises for one bad file: the outcome is in the returned entry."""
        try:
            data = self.fetch(object_key)
        except Exception:
            log.warning("attachment not readable: %s", object_key)
            self.counts["missing"] += 1
            return {"status": "missing"}
        sha = hashlib.sha256(data).hexdigest()
        status = "ok"
        if expected_sha256 and expected_sha256.strip().lower() != sha:
            status = "checksum_differs"
            self.counts["checksum_differs"] += 1
        try:
            record, file_id = vault_crypto.encrypt_file(data, self.pub)
        except ValueError:
            self.counts["missing"] += 1
            return {"status": "too_large"}
        if self._f is None or (self._size and self._size + len(record) > self.target):
            self._open_pack()
        offset = self._size
        self._f.write(record)
        self._hash.update(record)
        self._size += len(record)
        self.counts["files"] += 1
        return {"status": status, "file_id": file_id.hex(), "pack": len(self.packs) - 1, "offset": offset,
                "length": len(record), "size": len(data), "sha256": sha}

    def finish(self) -> list[dict]:
        self.close_pack()
        return self.packs
