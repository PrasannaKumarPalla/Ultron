from pathlib import Path

from ultron.store import BLOB_SPILL_THRESHOLD, BlobStore


def test_put_is_content_addressed_and_idempotent(tmp_path: Path):
    blobs = BlobStore(tmp_path / "blobs")

    first = blobs.put(b"hello ultron")
    second = blobs.put(b"hello ultron")

    assert first == second
    assert len(first) == 64
    assert first[:2] in {p.name for p in (tmp_path / "blobs").iterdir()}
    assert blobs.get(first) == b"hello ultron"
    assert blobs.has(first)


def test_distinct_content_yields_distinct_addresses_and_sharding(tmp_path: Path):
    blobs = BlobStore(tmp_path / "blobs")

    a = blobs.put(b"a")
    b = blobs.put(b"b")

    assert a != b
    assert (tmp_path / "blobs" / a[:2] / a).exists()
    assert (tmp_path / "blobs" / b[:2] / b).exists()


def test_get_rejects_bad_digests_and_missing_blobs(tmp_path: Path):
    blobs = BlobStore(tmp_path / "blobs")

    assert blobs.get("../escape") is None
    assert blobs.get("z" * 64) is None
    assert blobs.get("a" * 64) is None
    assert blobs.has("") is False


def test_resolve_inlines_blob_reference(tmp_path: Path):
    blobs = BlobStore(tmp_path / "blobs")
    big = {"files": [{"path": "x.py", "content": "y" * 5000}]}
    digest = blobs.put_text(__import__("json").dumps(big))

    resolved = blobs.resolve({"blob": digest, "size": 999})

    assert resolved == big


def test_resolve_reports_missing_and_passthrough(tmp_path: Path):
    blobs = BlobStore(tmp_path / "blobs")

    assert blobs.resolve({"blob": "f" * 64}) == {"blob_missing": "f" * 64}
    inline = {"kind": "node.started"}
    assert blobs.resolve(inline) is inline


def test_spill_threshold_is_64kib():
    assert BLOB_SPILL_THRESHOLD == 65_536
