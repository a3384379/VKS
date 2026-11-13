"""A material's limits apply across every input, before unbounded expansion."""

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from vks_api import material_parser
from vks_api.service import DomainError


def archive(entries):
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as source:
        for name, content in entries:
            source.writestr(name, content)
    return stream.getvalue()


@pytest.mark.parametrize("loose", [False, True])
def test_multiple_inputs_share_expanded_byte_limit_before_reading_rejected_member(monkeypatch, loose):
    monkeypatch.setattr(material_parser, "LIMIT", 1024)
    first = b"# First\n" + b"a" * 692
    second = b"# Second\n" + b"b" * 691
    zipped = archive([("second.md", second)])
    initial = ("first.md", first) if loose else ("first.zip", archive([("first.md", first)]))
    assert sum(len(raw) for _, raw in [initial, ("second.zip", zipped)]) <= material_parser.LIMIT
    assert material_parser.parse("documents", [initial])["documents"]
    reads = []
    original = ZipFile.read

    def read(self, member, *args, **kwargs):
        reads.append(member.filename if hasattr(member, "filename") else member)
        return original(self, member, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "read", read)
    with pytest.raises(DomainError) as error:
        material_parser.parse("documents", [initial, ("second.zip", zipped)])
    assert error.value.code == "VKS_INVALID_ARCHIVE"
    assert "second.md" not in reads


def test_all_archive_entries_and_loose_inputs_share_file_limit(monkeypatch):
    monkeypatch.setattr(material_parser, "MAX_FILES", 3)
    first = archive([("folder/", ""), ("folder/a.md", "# A")])
    second = archive([("other/", ""), ("other/b.md", "# B")])
    with pytest.raises(DomainError):
        material_parser.parse("documents", [("first.zip", first), ("second.zip", second)])
    with pytest.raises(DomainError):
        material_parser.parse("documents", [("first.zip", first), ("b.md", b"# B"), ("c.md", b"# C")])
    good = material_parser.parse("documents", [("first.zip", first), ("b.md", b"# B")])
    assert [name for name, _ in good["documents"]] == ["folder/a.md", "b.md"]
