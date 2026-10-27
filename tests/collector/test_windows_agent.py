"""Native Windows filesystem/GUI checks, deliberately skipped on other hosts."""

import os
from pathlib import Path
import subprocess
import pytest
from packages.collector.snapshots import collect_directory, CollectionError

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Requires actual Windows Win32 handles and Tcl/Tk")


def junction(link, target):
    run = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    assert run.returncode == 0, "Synthetic NTFS directory junction could not be created"


def test_native_windows_reader_preserves_unicode_bytes_and_excludes_junctions_and_secrets(tmp_path):
    root = tmp_path / "已授权"
    root.mkdir()
    outside = tmp_path / "未授权"
    outside.mkdir()
    raw = b"\xef\xbb\xbf# Original\r\nWindows raw bytes\r\n"
    (root / "规则.md").write_bytes(raw)
    (root / ".ENV.local").write_text("not-uploaded")
    (outside / "private.md").write_text("outside-only")
    junction(root / "jump", outside)
    try:
        row = collect_directory(root)
        assert row.files == {"规则.md": raw}
        with pytest.raises(CollectionError):
            collect_directory(root / "jump")
    finally:
        os.rmdir(root / "jump")


def test_windows_swap_to_junction_between_stat_and_open_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("outside")
    original = os.scandir
    swapped = []

    class Entry:
        name = "child"
        path = str(child)

        def stat(self, follow_symlinks=False):
            info = child.stat(follow_symlinks=False)
            if not swapped:
                child.rmdir()
                junction(child, outside)
                swapped.append(True)
            return info

    def entries(path):
        return [Entry()] if Path(path) == root else original(path)

    monkeypatch.setattr(os, "scandir", entries)
    try:
        with pytest.raises(CollectionError):
            collect_directory(root)
        assert swapped
    finally:
        if swapped:
            os.rmdir(child)


def test_native_tk_window_starts_without_scheduling_and_scope_edits_revoke_consent(tmp_path):
    import tkinter as tk
    from packages.collector.agent_gui import CollectorWindow

    root = tk.Tk()
    root.withdraw()
    app = CollectorWindow(root, tmp_path / ".vks/collector")
    try:
        root.update()
        assert not app.consent.get() and not app.schedule.get() and app.future is None
        app.consent.set(True)
        app.schedule.set(True)
        app.fields["root"].set(str(tmp_path / "new-scope"))
        assert not app.consent.get() and not app.schedule.get()
        assert app.fields["token"].get() == ""
    finally:
        app.close()
