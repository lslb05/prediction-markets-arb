"""Raw zone: exactly what the venue sent, immutable, with a trail of how it arrived."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Manifest:
    source: str
    dataset: str
    url: str
    collected_at: str
    contract_version: str
    sha256: str
    size_bytes: int
    rows: int


def write(
    root: Path,
    *,
    source: str,
    dataset: str,
    body: bytes,
    url: str,
    contract_version: str,
    rows: int,
    collected_at: datetime,
) -> Manifest | None:
    """Writes one collection run. Returns None if the content matches what's stored.

    Idempotency is by hash of the body: rerunning ingestion when the venue
    hasn't changed doesn't create a new partition.
    """
    digest = hashlib.sha256(body).hexdigest()
    dataset_dir = root / source / dataset
    if _already_stored(dataset_dir, digest):
        return None

    # Colons are illegal in a Windows filename, so the timestamp is compact
    # instead of ISO 8601.
    partition = dataset_dir / f"collected={collected_at.strftime('%Y%m%dT%H%M%SZ')}"
    partition.mkdir(parents=True, exist_ok=True)
    (partition / "data.json").write_bytes(body)

    manifest = Manifest(
        source=source,
        dataset=dataset,
        url=url,
        collected_at=collected_at.isoformat(),
        contract_version=contract_version,
        sha256=digest,
        size_bytes=len(body),
        rows=rows,
    )
    (partition / "manifest.json").write_text(
        json.dumps(asdict(manifest), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


def _already_stored(dataset_dir: Path, digest: str) -> bool:
    for file in dataset_dir.glob("collected=*/manifest.json"):
        if json.loads(file.read_text(encoding="utf-8"))["sha256"] == digest:
            return True
    return False
