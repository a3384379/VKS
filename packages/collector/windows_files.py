"""Windows no-reparse snapshot reader. All handles are read-only and held in scope.

Uses CreateFileW(OPEN_REPARSE_POINT/BACKUP_SEMANTICS), handle attributes and
GetFinalPathNameByHandleW. Parent handles deny deletion while children are read.
"""

from contextlib import contextmanager, ExitStack
from hashlib import sha256
import os
import ntpath
from pathlib import Path
import stat


def collect_windows(root):
    import ctypes as c
    from ctypes import wintypes as w
    from .snapshots import CollectionError, Snapshot, included, IGNORED, SECRET_NAMES, _limits
    from packages.analyzers.vks_ingest import MAX_FILE_BYTES, safe_path, source_digest

    class Info(c.Structure):
        _fields_ = [
            ("attributes", w.DWORD),
            ("created", w.FILETIME),
            ("accessed", w.FILETIME),
            ("written", w.FILETIME),
            ("volume", w.DWORD),
            ("size_high", w.DWORD),
            ("size_low", w.DWORD),
            ("links", w.DWORD),
            ("index_high", w.DWORD),
            ("index_low", w.DWORD),
        ]

    api = c.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    api.CreateFileW.restype = w.HANDLE
    api.GetFileInformationByHandle.argtypes = [w.HANDLE, c.POINTER(Info)]
    api.GetFileInformationByHandle.restype = w.BOOL
    api.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
    api.GetFinalPathNameByHandleW.restype = w.DWORD
    api.GetFileType.argtypes = [w.HANDLE]
    api.GetFileType.restype = w.DWORD
    api.CloseHandle.argtypes = [w.HANDLE]
    api.CloseHandle.restype = w.BOOL
    api.ReadFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
    api.ReadFile.restype = w.BOOL

    @contextmanager
    def opened(path, directory, ancestor=False):
        # Read-only attributes on directories; read-only contents on regular files.
        handle = api.CreateFileW(
            str(path), 0x80 if directory else 0x80000000, 3 if ancestor else 1, None, 3, 0x00200000 | 0x02000000, None
        )
        if handle == c.c_void_p(-1).value:
            raise CollectionError("VKS_SOURCE_READ_FAILED", "Windows 来源正在变化、被占用或无权读取；未采集越界资料")
        try:
            info = Info()
            if (
                not api.GetFileInformationByHandle(handle, c.byref(info))
                or info.attributes & 0x400
                or bool(info.attributes & 0x10) != directory
                or api.GetFileType(handle) != 1
            ):
                raise CollectionError("VKS_SOURCE_READ_FAILED", "Windows 来源含重解析点/特殊文件或读取期间发生变化")
            pathbuf = c.create_unicode_buffer(32768)
            size = api.GetFinalPathNameByHandleW(handle, pathbuf, len(pathbuf), 0)
            if not 0 < size < len(pathbuf):
                raise CollectionError("VKS_SOURCE_READ_FAILED", "无法核实 Windows 来源实际范围")
            final = ntpath.normcase(ntpath.normpath(pathbuf.value))
            yield handle, info, final
        finally:
            api.CloseHandle(handle)

    root = Path(root).absolute()
    files = {}
    issues = []
    visited = 0
    with ExitStack() as ancestors:
        # Hold every real ancestor against rename/junction replacement.
        for parent in reversed(root.parents):
            ancestors.enter_context(opened(parent, True, ancestor=True))
        with opened(root, True) as (_, _, allowed):

            def visit(directory, prefix="", depth=0):
                nonlocal visited
                visited += 1
                if depth > 64 or visited > 10000:
                    raise CollectionError("VKS_LIMIT_EXCEEDED", "来源目录层级或数量超限")
                try:
                    entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
                    for entry in entries:
                        relative = (prefix + "/" + entry.name).lstrip("/")
                        info = entry.stat(follow_symlinks=False)
                        if info.st_file_attributes & 0x400:
                            if included(relative):
                                issues.append(dict(relative_path=relative, reason_code="POLICY_EXCLUDED"))
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if (
                                entry.name.casefold() in IGNORED
                                or entry.name.casefold().startswith(".env")
                                or entry.name.casefold() in SECRET_NAMES
                            ):
                                continue
                            with opened(entry.path, True) as (_, _, final):
                                if ntpath.commonpath([allowed, final]) != allowed:
                                    raise CollectionError("VKS_FORBIDDEN", "Windows 目录越出授权范围")
                                visit(Path(entry.path), relative, depth + 1)
                        elif included(relative):
                            with opened(entry.path, False) as (handle, actual, final):
                                if ntpath.commonpath([allowed, final]) != allowed:
                                    raise CollectionError("VKS_FORBIDDEN", "Windows 文件越出授权范围")
                                size = (actual.size_high << 32) | actual.size_low
                                if size > MAX_FILE_BYTES:
                                    raise CollectionError("VKS_LIMIT_EXCEEDED", "来源文件超过配额")
                                chunks = []
                                total = 0
                                buffer = c.create_string_buffer(65536)
                                count = w.DWORD()
                                while True:
                                    if not api.ReadFile(handle, buffer, len(buffer), c.byref(count), None):
                                        raise CollectionError("VKS_SOURCE_READ_FAILED", "Windows 文件读取期间发生变化")
                                    if not count.value:
                                        break
                                    total += count.value
                                    if total > MAX_FILE_BYTES:
                                        raise CollectionError("VKS_LIMIT_EXCEEDED", "来源文件超过配额")
                                    chunks.append(buffer.raw[: count.value])
                                raw = b"".join(chunks)
                                if len(raw) != size:
                                    raise CollectionError("VKS_SOURCE_READ_FAILED", "Windows 来源文件大小发生变化")
                                files[safe_path(relative)] = raw
                                _limits(files)
                except CollectionError:
                    raise
                except (OSError, ValueError):
                    raise CollectionError("VKS_SOURCE_READ_FAILED", "Windows 来源读取失败或范围发生变化") from None

            visit(root)
    records = [dict(path=p, sha256=sha256(raw).hexdigest(), size_bytes=len(raw)) for p, raw in files.items()]
    return Snapshot("local", source_digest(records), files, issues)
