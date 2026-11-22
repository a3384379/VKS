"""Native credential stores. Token material stays in-process and out of argv."""

from __future__ import annotations

import ctypes as C
import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path


class CredentialError(ValueError):
    pass


def token_reference(path: Path, target: str, endpoint: str) -> str:
    return hashlib.sha256(f"{os.path.normcase(path.resolve())}\n{target}\n{endpoint}".encode()).hexdigest()


def validate_token(value: str) -> str:
    if not value or len(value) > 2048 or any(not 33 <= ord(c) <= 126 for c in value):
        raise CredentialError("凭据必须是非空 ASCII bearer token，最长 2048 字符")
    return value


def session_variable(target: str) -> str:
    return {"mcp": "VKS_CONNECT_MCP_TOKEN", "embedding": "VKS_CONNECT_EMBEDDING_KEY"}.get(target, "VKS_CONNECT_TOKEN")


def load_token(path: Path, target: str, endpoint: str) -> str:
    session = os.getenv(session_variable(target))
    if session is not None:
        return validate_token(session)
    value = credential_store().get(token_reference(path, target, endpoint))
    if value is None:
        raise CredentialError("未找到连接凭据。请使用 auth set 或为当前会话提供 token")
    return validate_token(value)


def credential_store():
    if sys.platform == "darwin":
        return MacKeychain()
    if sys.platform == "win32":
        return WindowsCredentialManager()
    raise CredentialError("当前平台没有连接器系统密钥库后端，请使用会话环境变量")


class LocalPlanCredentials:
    """Keep ChatGPT refresh credentials private to the current macOS user."""

    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, reference: str, *, create=False) -> Path:
        if create:
            self.directory.mkdir(mode=0o700, exist_ok=True)
        info = self.directory.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CredentialError("本机 ChatGPT 授权目录必须仅允许当前用户访问。")
        return self.directory / reference

    def get(self, reference: str) -> str | None:
        try:
            path = self._path(reference)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise CredentialError("本机 ChatGPT 授权文件必须仅允许当前用户访问。")
                return validate_token(handle.read(2049).decode("ascii"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise CredentialError("无法读取本机 ChatGPT 授权。") from exc

    def set(self, reference: str, value: str) -> None:
        raw = validate_token(value).encode("ascii")
        try:
            path = self._path(reference, create=True)
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as handle:
                pending = Path(handle.name)
                try:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                    os.replace(pending, path)
                finally:
                    pending.unlink(missing_ok=True)
        except OSError as exc:
            raise CredentialError("无法保存本机 ChatGPT 授权。") from exc

    def remove(self, reference: str) -> None:
        try:
            self._path(reference).unlink(missing_ok=True)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise CredentialError("无法移除本机 ChatGPT 授权。") from exc


class MacKeychain:
    service = b"VKS Connector"

    def __init__(self):
        self.security = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.foundation = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.security.SecKeychainFindGenericPassword.argtypes = (
            C.c_void_p,
            C.c_uint32,
            C.c_char_p,
            C.c_uint32,
            C.c_char_p,
            C.POINTER(C.c_uint32),
            C.POINTER(C.c_void_p),
            C.POINTER(C.c_void_p),
        )
        self.security.SecKeychainAddGenericPassword.argtypes = (
            C.c_void_p,
            C.c_uint32,
            C.c_char_p,
            C.c_uint32,
            C.c_char_p,
            C.c_uint32,
            C.c_char_p,
            C.c_void_p,
        )
        self.security.SecKeychainItemModifyAttributesAndData.argtypes = (
            C.c_void_p,
            C.c_void_p,
            C.c_uint32,
            C.c_char_p,
        )
        self.security.SecKeychainItemFreeContent.argtypes = (C.c_void_p, C.c_void_p)
        self.security.SecKeychainItemDelete.argtypes = (C.c_void_p,)
        self.foundation.CFRelease.argtypes = (C.c_void_p,)
        self.foundation.CFRelease.restype = None

    def _check(self, status):
        if status:
            raise CredentialError(f"Keychain 操作未完成（系统状态 {status}），可改用当前会话方式")

    def _lookup(self, account, length=None, data=None, item=None):
        return self.security.SecKeychainFindGenericPassword(
            None,
            len(self.service),
            self.service,
            len(account),
            account,
            length,
            data,
            item,
        )

    def get(self, reference):
        length, data = C.c_uint32(), C.c_void_p()
        status = self._lookup(reference.encode(), C.byref(length), C.byref(data))
        if status == -25300:  # errSecItemNotFound
            return None
        self._check(status)
        try:
            return C.string_at(data, length.value).decode("ascii")
        finally:
            self.security.SecKeychainItemFreeContent(None, data)

    def set(self, reference, value):
        account, raw = reference.encode(), validate_token(value).encode()
        status = self.security.SecKeychainAddGenericPassword(
            None,
            len(self.service),
            self.service,
            len(account),
            account,
            len(raw),
            raw,
            None,
        )
        if status != -25299:  # errSecDuplicateItem
            self._check(status)
            return
        item = C.c_void_p()
        self._check(self._lookup(account, item=C.byref(item)))
        try:
            self._check(self.security.SecKeychainItemModifyAttributesAndData(item, None, len(raw), raw))
        finally:
            self.foundation.CFRelease(item)

    def remove(self, reference):
        item = C.c_void_p()
        status = self._lookup(reference.encode(), item=C.byref(item))
        if status != -25300:
            self._check(status)
            try:
                self._check(self.security.SecKeychainItemDelete(item))
            finally:
                self.foundation.CFRelease(item)


class WindowsCredentialManager:
    def __init__(self):
        from ctypes import wintypes as W

        class Credential(C.Structure):
            _fields_ = [
                ("Flags", W.DWORD),
                ("Type", W.DWORD),
                ("TargetName", W.LPWSTR),
                ("Comment", W.LPWSTR),
                ("LastWritten", W.FILETIME),
                ("CredentialBlobSize", W.DWORD),
                ("CredentialBlob", C.POINTER(C.c_ubyte)),
                ("Persist", W.DWORD),
                ("AttributeCount", W.DWORD),
                ("Attributes", C.c_void_p),
                ("TargetAlias", W.LPWSTR),
                ("UserName", W.LPWSTR),
            ]

        self.Credential = Credential
        self.lib = C.WinDLL("Advapi32", use_last_error=True)
        self.lib.CredReadW.argtypes = (W.LPCWSTR, W.DWORD, W.DWORD, C.POINTER(C.POINTER(Credential)))
        self.lib.CredWriteW.argtypes = (C.POINTER(Credential), W.DWORD)
        self.lib.CredDeleteW.argtypes = (W.LPCWSTR, W.DWORD, W.DWORD)
        self.lib.CredFree.argtypes = (C.c_void_p,)
        self.lib.CredFree.restype = None

    def _target(self, reference):
        return "VKS Connector/" + reference

    def _error(self):
        return CredentialError(f"Credential Manager 操作未完成（系统状态 {C.get_last_error()}），可改用当前会话方式")

    def get(self, reference):
        item = C.POINTER(self.Credential)()
        if not self.lib.CredReadW(self._target(reference), 1, 0, C.byref(item)):
            if C.get_last_error() == 1168:  # ERROR_NOT_FOUND
                return None
            raise self._error()
        try:
            record = item.contents
            return C.string_at(record.CredentialBlob, record.CredentialBlobSize).decode("ascii")
        finally:
            self.lib.CredFree(item)

    def set(self, reference, value):
        raw = validate_token(value).encode()
        blob = (C.c_ubyte * len(raw)).from_buffer_copy(raw)
        record = self.Credential(
            Type=1,
            TargetName=self._target(reference),
            CredentialBlobSize=len(raw),
            CredentialBlob=blob,
            Persist=2,
            UserName="vks-connect",  # CRED_PERSIST_LOCAL_MACHINE, scoped to the current user
        )
        if not self.lib.CredWriteW(C.byref(record), 0):
            raise self._error()

    def remove(self, reference):
        if not self.lib.CredDeleteW(self._target(reference), 1, 0) and C.get_last_error() != 1168:
            raise self._error()
