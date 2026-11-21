"""Open VKS instance secrets as regular files with owner-only access."""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import stat
import sys
from functools import lru_cache
from pathlib import Path


class _DarwinStatfs(ctypes.Structure):
    # The 64-bit struct statfs layout from Darwin's <sys/mount.h>.
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_reserved", ctypes.c_uint32 * 8),
    ]


@lru_cache(maxsize=1)
def _macos_file_security_api():
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    # Intel has a separate symbol for the 64-bit inode ABI.
    fstatfs = getattr(lib, "fstatfs$INODE64") if os.uname().machine == "x86_64" else lib.fstatfs
    fstatfs.argtypes = (ctypes.c_int, ctypes.POINTER(_DarwinStatfs))
    pointer = ctypes.c_void_p
    lib.acl_get_fd_np.argtypes = (ctypes.c_int, ctypes.c_int)
    lib.acl_get_fd_np.restype = pointer
    lib.acl_valid.argtypes = (pointer,)
    lib.acl_get_entry.argtypes = (pointer, ctypes.c_int, ctypes.POINTER(pointer))
    lib.acl_get_tag_type.argtypes = (pointer, ctypes.POINTER(ctypes.c_int))
    lib.acl_get_qualifier.argtypes = (pointer,)
    lib.acl_get_qualifier.restype = pointer
    lib.mbr_uid_to_uuid.argtypes = (ctypes.c_uint32, ctypes.POINTER(ctypes.c_ubyte))
    lib.acl_free.argtypes = (pointer,)
    return lib, fstatfs


def _verify_macos_permissions(fd: int, owner: int):
    unverifiable = "无法验证 VKS 私有文件的 macOS ACL"
    try:
        lib, fstatfs = _macos_file_security_api()
    except (OSError, AttributeError):
        raise PermissionError(unverifiable) from None
    filesystem = _DarwinStatfs()
    if fstatfs(fd, ctypes.byref(filesystem)):
        raise PermissionError("无法验证 VKS 私有文件所在 macOS 卷的权限")
    if filesystem.f_flags & 0x00200000:  # MNT_IGNORE_OWNERSHIP
        raise PermissionError("VKS 私有文件所在卷启用了“忽略此宗卷的所有权”")
    acl = lib.acl_get_fd_np(fd, 0x100)  # ACL_TYPE_EXTENDED
    if not acl:
        # On a valid descriptor, ENOENT signals an absent extended ACL.
        if ctypes.get_errno() == errno.ENOENT:
            return
        raise PermissionError(unverifiable)
    try:
        if lib.acl_valid(acl):
            raise PermissionError(unverifiable)
        owner_uuid = None
        entry = ctypes.c_void_p()
        tag = ctypes.c_int()
        entry_id = 0  # ACL_FIRST_ENTRY
        while lib.acl_get_entry(acl, entry_id, ctypes.byref(entry)) == 0:
            entry_id = -1  # ACL_NEXT_ENTRY
            if lib.acl_get_tag_type(entry, ctypes.byref(tag)):
                raise PermissionError(unverifiable)
            if tag.value == 2:  # ACL_EXTENDED_DENY
                continue
            if tag.value != 1:  # ACL_EXTENDED_ALLOW
                raise PermissionError(unverifiable)
            if owner_uuid is None:
                owner_uuid = (ctypes.c_ubyte * 16)()
                if lib.mbr_uid_to_uuid(owner, owner_uuid):
                    raise PermissionError(unverifiable)
            qualifier = lib.acl_get_qualifier(entry)
            if not qualifier:
                raise PermissionError(unverifiable)
            try:
                if ctypes.string_at(qualifier, len(owner_uuid)) != bytes(owner_uuid):
                    raise PermissionError("VKS 私有文件 ACL 向其他身份开放")
            finally:
                lib.acl_free(qualifier)
        # Darwin's iterator reports EINVAL after the last entry.
        if ctypes.get_errno() != errno.EINVAL:
            raise PermissionError(unverifiable)
    finally:
        lib.acl_free(acl)


def _open_private_posix(path: Path, create: bool) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL if create else os.O_RDONLY | os.O_NONBLOCK
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError("VKS 私有文件要求当前所有者和私有访问权限")
        if sys.platform == "darwin":
            _verify_macos_permissions(fd, info.st_uid)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_private_windows(path: Path, create: bool) -> int:
    import msvcrt

    import pywintypes
    import win32api
    import win32con as con
    import win32file
    import win32security as security

    path = Path(os.path.abspath(path))
    if len(path.drive) != 2 or not path.drive.endswith(":"):
        raise PermissionError("VKS 私有文件需要 Windows 本机磁盘")
    for parent in reversed(path.parents):
        if parent.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise PermissionError("VKS 私有文件不能经重解析点访问")

    token = security.OpenProcessToken(win32api.GetCurrentProcess(), con.TOKEN_QUERY)
    try:
        current_sid, _ = security.GetTokenInformation(token, security.TokenUser)
    finally:
        token.Close()
    current_user = security.ConvertSidToStringSid(current_sid)
    # SYSTEM and local administrators are already privileged on the device.
    trusted = {current_user, "S-1-5-18", "S-1-5-32-544"}
    attributes = None
    if create:
        attributes = pywintypes.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            f"O:{current_user}D:P(A;;FA;;;{current_user})(A;;FA;;;SY)", 1
        )
    access = (con.GENERIC_WRITE if create else con.GENERIC_READ) | con.READ_CONTROL
    try:
        handle = win32file.CreateFile(
            str(path),
            access,
            con.FILE_SHARE_READ | con.FILE_SHARE_DELETE,
            attributes,
            con.CREATE_NEW if create else con.OPEN_EXISTING,
            con.FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
    except pywintypes.error as exc:
        if exc.winerror in (2, 3):
            raise FileNotFoundError("VKS 私有文件不存在") from None
        if exc.winerror in (80, 183):
            raise FileExistsError("VKS 私有文件已存在") from None
        raise OSError("无法打开 VKS 私有文件") from None
    try:
        flags = win32file.GetFileInformationByHandle(handle)[0]
        if flags & (stat.FILE_ATTRIBUTE_DIRECTORY | stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise PermissionError("VKS 私有文件必须是常规文件")
        descriptor = security.GetSecurityInfo(
            handle, security.SE_FILE_OBJECT, security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION
        )
        owner = security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
        dacl = descriptor.GetSecurityDescriptorDacl()
        if owner != current_user or dacl is None:
            raise PermissionError("VKS 私有文件要求当前所有者及受限 ACL")
        for index in range(dacl.GetAceCount()):
            (kind, _), _, sid, *extra = dacl.GetAce(index)
            if kind == 1 and not extra:  # ACCESS_DENIED_ACE_TYPE
                continue
            if kind != 0 or extra or security.ConvertSidToStringSid(sid) not in trusted:
                raise PermissionError("VKS 私有文件 ACL 向其他身份开放或无法验证")
        raw = handle.Detach()
        try:
            return msvcrt.open_osfhandle(raw, os.O_BINARY | (os.O_WRONLY if create else os.O_RDONLY))
        except BaseException:
            win32api.CloseHandle(raw)
            raise
    except pywintypes.error:
        raise OSError("无法验证 VKS 私有文件的 Windows ACL") from None
    finally:
        handle.Close()


def _open_private(path: Path, *, create=False) -> int:
    if os.name == "nt":
        return _open_private_windows(path, create)
    return _open_private_posix(path, create)


def read_private_file(path: Path, max_bytes: int) -> bytes:
    with os.fdopen(_open_private(path), "rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("VKS 私有文件超过大小限制")
    return raw


def write_private_file(path: Path, value: bytes, *, create_only=False):
    temporary = path.with_name(".vks-private-" + secrets.token_hex(16))
    try:
        with os.fdopen(_open_private(temporary, create=True), "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if create_only:
            os.link(temporary, path, follow_symlinks=False)
        else:
            try:
                fd = _open_private(path)
            except FileNotFoundError:
                pass
            else:
                os.close(fd)
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
