from hashlib import sha256
from pathlib import Path
import os
import pytest
from packages.collector.snapshots import CollectionError
from packages.collector.sync import collect_authorized, inventory, snapshot_delta


def test_sync_scope_bytes_moves_and_missing_file_storm(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (root / "a.md").write_bytes(b"\xef\xbb\xbf# Chinese\r\noriginal\r\n")
    (root / ".env").write_text("secret")
    try:
        (root / "escape.md").symlink_to(other / "private.md")
    except OSError:
        if os.name != "nt":
            raise
    (other / "private.md").write_text("private")
    before = collect_authorized(root, root)
    assert set(before.files) == {"a.md"}
    assert before.files["a.md"] == (root / "a.md").read_bytes()
    with pytest.raises(CollectionError):
        collect_authorized(other, root)
    (root / "a.md").rename(root / "b.md")
    after = collect_authorized(root, root)
    delta = snapshot_delta(inventory(before), inventory(after), scope_before="scope", scope_after="scope")
    assert delta["moved"] == [{"from": "a.md", "to": "b.md", "sha256": sha256(before.files["a.md"]).hexdigest()}]
    with pytest.raises(CollectionError):
        snapshot_delta({}, {}, scope_before="old", scope_after="new")
    storm = snapshot_delta({str(i): str(i) for i in range(30)}, {"0": "0"}, scope_before="scope", scope_after="scope")
    assert storm["scope_anomaly"] and storm["requires_review"]
