"""M7 Layer 4 — the platform-local IPC endpoint abstraction.

Local transport only, no TCP fallback (frozen §16/§59). Endpoint
existence is never authority. Two adapters:

- **POSIX**: a Unix-domain socket under the canonical authority domain,
  owner-restricted permissions (0o600), stale-path cleanup ONLY after
  ownership is held (frozen §16: never before acquisition).
- **Windows**: a named pipe created with an explicit DACL restricting
  access to the current user. The pipe lives in the local object
  namespace; the DACL is the access boundary (frozen §59).

Full real-platform qualification is Layers 7–8; Layer 4 implements the
security properties now (frozen contract item 1).
"""

from __future__ import annotations

import ctypes  # noqa: F401 — used by _win32() via vars(ctypes)
import hashlib
import os
import socket
import sys
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "IPCEndpoint",
    "PosixDomainSocketEndpoint",
    "WindowsNamedPipeEndpoint",
    "create_endpoint",
]


class IPCEndpoint:
    """The platform-neutral local IPC endpoint interface."""

    def bind(self) -> None:
        """Create and secure the endpoint. Raises on failure."""
        raise NotImplementedError

    def accept(self) -> Any:
        """Accept one connection (blocking). Returns a connection object."""
        raise NotImplementedError

    def close(self) -> None:
        """Close the listener and remove the rendezvous."""
        raise NotImplementedError

    @property
    def path(self) -> str:
        """The endpoint's local rendezvous identifier."""
        raise NotImplementedError


def _win32() -> Any:
    """ctypes.windll resolved at runtime (Windows-only; runtime dict
    resolution keeps mypy portable per platform - the M8 F-25 lesson)."""
    return vars(ctypes).get("windll")


# F-64: all Win32 DLLs used for handle-bearing calls are loaded with
# use_last_error=True so ``ctypes.get_last_error()`` reads a per-thread
# snapshot that no intermediate Python-level call can clobber. The
# previous failure (OpenProcessToken returning garbage) came from calling
# these APIs WITHOUT argtypes: ctypes passes Python ints as 32-bit C
# ints, truncating 64-bit HANDLEs. Every handle-bearing declaration
# below declares restype/argtypes with c_void_p.


def _win_kernel32() -> Any:
    """kernel32 with use_last_error semantics (Windows-only, lazily)."""
    if sys.platform != "win32":  # pragma: no cover - POSIX never reaches this
        raise RuntimeError("kernel32 is Windows-only")
    global _KERNEL32
    if _KERNEL32 is None:
        windll_cls = getattr(ctypes, "WinDLL", None)
        if windll_cls is None:  # pragma: no cover - defensive
            raise RuntimeError("ctypes.WinDLL unavailable")
        k32 = windll_cls("kernel32", use_last_error=True)
        k32.CreateNamedPipeW.restype = ctypes.c_void_p
        k32.CreateNamedPipeW.argtypes = [
            ctypes.c_wchar_p,  # lpName
            ctypes.c_uint32,  # dwOpenMode
            ctypes.c_uint32,  # dwPipeMode
            ctypes.c_uint32,  # nMaxInstances
            ctypes.c_uint32,  # nOutBufferSize
            ctypes.c_uint32,  # nInBufferSize
            ctypes.c_uint32,  # nDefaultTimeOutMs
            ctypes.c_void_p,  # lpSecurityAttributes
        ]
        k32.ConnectNamedPipe.restype = ctypes.c_int
        k32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.ReadFile.restype = ctypes.c_int
        k32.ReadFile.argtypes = [
            ctypes.c_void_p,  # hFile
            ctypes.c_void_p,  # lpBuffer
            ctypes.c_uint32,  # nNumberOfBytesToRead
            ctypes.POINTER(ctypes.c_uint32),  # lpNumberOfBytesRead
            ctypes.c_void_p,  # lpOverlapped (None = synchronous)
        ]
        k32.WriteFile.restype = ctypes.c_int
        k32.WriteFile.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        k32.CloseHandle.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CancelIoEx.restype = ctypes.c_int
        k32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,  # lpFileName
            ctypes.c_uint32,  # dwDesiredAccess
            ctypes.c_uint32,  # dwShareMode
            ctypes.c_void_p,  # lpSecurityAttributes
            ctypes.c_uint32,  # dwCreationDisposition
            ctypes.c_uint32,  # dwFlagsAndAttributes
            ctypes.c_void_p,  # hTemplateFile
        ]
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.GetCurrentProcess.argtypes = []
        k32.LocalFree.restype = ctypes.c_void_p
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        _KERNEL32 = k32
    return _KERNEL32


def _win_advapi32() -> Any:
    """advapi32 with use_last_error semantics (Windows-only, lazily)."""
    if sys.platform != "win32":  # pragma: no cover - POSIX never reaches this
        raise RuntimeError("advapi32 is Windows-only")
    global _ADVAPI32
    if _ADVAPI32 is None:
        windll_cls = getattr(ctypes, "WinDLL", None)
        if windll_cls is None:  # pragma: no cover - defensive
            raise RuntimeError("ctypes.WinDLL unavailable")
        adv = windll_cls("advapi32", use_last_error=True)
        adv.OpenProcessToken.restype = ctypes.c_int
        adv.OpenProcessToken.argtypes = [
            ctypes.c_void_p,  # ProcessHandle
            ctypes.c_uint32,  # DesiredAccess
            ctypes.POINTER(ctypes.c_void_p),  # TokenHandle
        ]
        adv.GetTokenInformation.restype = ctypes.c_int
        adv.GetTokenInformation.argtypes = [
            ctypes.c_void_p,  # TokenHandle
            ctypes.c_int,  # TokenInformationClass
            ctypes.c_void_p,  # TokenInformation
            ctypes.c_uint32,  # TokenInformationLength
            ctypes.POINTER(ctypes.c_uint32),  # ReturnLength
        ]
        # F-64: the classic absolute-descriptor assembly — SetEntriesInAclW
        # builds the ACL, InitializeSecurityDescriptor +
        # SetSecurityDescriptorDacl wrap it. The documented 9-argument
        # BuildSecurityDescriptorW was implemented and tested first
        # (argtypes exact); it returns ERROR_SUCCESS but writes invalid
        # out-params on this platform (sentinel probes; IsValidSecurity
        # descriptor == False; downstream CreateNamedPipeW fails 998).
        # SetEntriesInAclW + SetSecurityDescriptorDacl is the stable path
        # with identical DACL semantics.
        adv.SetEntriesInAclW.restype = ctypes.c_uint32
        adv.SetEntriesInAclW.argtypes = [
            ctypes.c_uint32,  # cCountOfExplicitEntries
            ctypes.c_void_p,  # pListOfExplicitEntries
            ctypes.c_void_p,  # OldAcl
            ctypes.POINTER(ctypes.c_void_p),  # NewAcl (out, LocalAlloc'd)
        ]
        adv.InitializeSecurityDescriptor.restype = ctypes.c_int
        adv.InitializeSecurityDescriptor.argtypes = [
            ctypes.c_void_p,  # pSecurityDescriptor
            ctypes.c_uint32,  # dwRevision
        ]
        adv.SetSecurityDescriptorDacl.restype = ctypes.c_int
        adv.SetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p,  # pSecurityDescriptor
            ctypes.c_int,  # bDaclPresent
            ctypes.c_void_p,  # pDacl
            ctypes.c_int,  # bDaclDefaulted
        ]
        _ADVAPI32 = adv
    return _ADVAPI32


_KERNEL32: Any = None
_ADVAPI32: Any = None

# Win32 error codes used by the pipe path.
_ERROR_SUCCESS = 0
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_ERROR_PIPE_CONNECTED = 535
_ERROR_PIPE_BUSY = 231
_ERROR_OPERATION_ABORTED = 995

_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _WindowsPipeConnection:
    """A connected named-pipe handle with SOCKET-COMPATIBLE recv/send/close.

    The transport's bounded framing (``_read_exact`` / ``_write_all``)
    consumes the socket interface; this adapter provides it over
    synchronous ReadFile/WriteFile so the transport code is identical on
    both platforms. Broken/closed peer states surface as
    ConnectionError, matching socket semantics."""

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    @classmethod
    def open_client(cls, pipe_name: str) -> "_WindowsPipeConnection":
        """Connect as the LOCAL client (CreateFileW on the pipe path)."""
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        k32 = _win_kernel32()
        handle = k32.CreateFileW(
            pipe_name,
            GENERIC_READ | GENERIC_WRITE,
            0,  # no sharing — one client per pipe instance
            None,
            OPEN_EXISTING,
            0,  # no overlapped; synchronous like a POSIX socket
            None,
        )
        if not handle or handle == _INVALID_HANDLE:
            err = ctypes.get_last_error()
            raise ConnectionError(f"ConnectFileW to {pipe_name} failed: error {err}")
        return cls(handle)

    def recv(self, bufsize: int) -> bytes:
        """Read up to bufsize bytes; b''-on-EOF is expressed by the
        caller's ConnectionError contract (pipes report broken peers as
        ReadFile failures, not zero-length reads)."""
        k32 = _win_kernel32()
        buf = ctypes.create_string_buffer(bufsize)
        read = ctypes.c_uint32(0)
        ok = k32.ReadFile(self._handle, buf, bufsize, ctypes.byref(read), None)
        if not ok:
            err = ctypes.get_last_error()
            if err in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_OPERATION_ABORTED):
                raise ConnectionError(f"pipe closed by peer (error {err})")
            raise OSError(f"ReadFile failed: error {err}")
        return buf.raw[: read.value]

    def send(self, data: bytes) -> int:
        k32 = _win_kernel32()
        written = ctypes.c_uint32(0)
        ok = k32.WriteFile(self._handle, data, len(data), ctypes.byref(written), None)
        if not ok:
            err = ctypes.get_last_error()
            if err in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_OPERATION_ABORTED):
                raise ConnectionError(f"pipe closed by peer (error {err})")
            raise OSError(f"WriteFile failed: error {err}")
        return written.value

    def close(self) -> None:
        # CancelIoEx first: a handler thread blocked in ReadFile on this
        # handle is released with ERROR_OPERATION_ABORTED before the
        # handle vanishes underneath it (the drain path relies on this).
        if self._handle is not None:
            k32 = _win_kernel32()
            k32.CancelIoEx(self._handle, None)
            k32.CloseHandle(self._handle)
            self._handle = None


def connect_local_stream(path: str) -> Any:
    """The platform client connector: a POSIX domain socket or a local
    named-pipe connection, both exposing recv/send/close. One request
    per connection; the caller owns the returned object."""
    if sys.platform == "win32":
        return _WindowsPipeConnection.open_client(path)
    if not hasattr(socket, "AF_UNIX"):
        raise RuntimeError("AF_UNIX sockets are POSIX-only")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    return sock


def create_endpoint(authority_domain: Path) -> IPCEndpoint:
    """The platform adapter: POSIX domain socket or Windows named pipe."""
    if sys.platform == "win32":
        return WindowsNamedPipeEndpoint(authority_domain)
    return PosixDomainSocketEndpoint(authority_domain)


class PosixDomainSocketEndpoint(IPCEndpoint):
    """Unix-domain socket under the canonical authority domain.

    Permissions are owner-restricted (0o600) at creation. Stale paths are
    unlinked ONLY inside bind() — which the server calls AFTER the owner
    lock is held — never before (frozen §16)."""

    def __init__(self, authority_domain: Path) -> None:
        self._path = authority_domain / "authority.sock"
        self._socket: Optional[socket.socket] = None

    @property
    def path(self) -> str:
        return str(self._path)

    def bind(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Stale-path cleanup — ONLY legal under held ownership (the server
        # calls bind() after acquiring the AuthorityOwnerLock).
        if self._path.exists():
            try:
                self._path.unlink()
            except OSError as exc:
                raise RuntimeError(f"could not clean stale IPC path {self._path}: {exc!r}") from exc
        if not hasattr(socket, "AF_UNIX"):  # pragma: no cover — POSIX only
            raise RuntimeError("AF_UNIX unavailable on this platform")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Restrict to owner before anyone can connect.
            old_umask = os.umask(0o177)
            try:
                sock.bind(str(self._path))
            finally:
                os.umask(old_umask)
            os.chmod(self._path, 0o600)
            sock.listen(8)
        except OSError as exc:
            sock.close()
            raise RuntimeError(f"IPC bind failed on {self._path}: {exc!r}") from exc
        self._socket = sock

    def accept(self) -> Any:
        assert self._socket is not None
        conn, _ = self._socket.accept()
        return conn

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass


class WindowsNamedPipeEndpoint(IPCEndpoint):
    """A local named pipe with an explicit current-user DACL.

    The pipe name is derived from the canonical authority domain (a
    stable local identifier). The DACL grants pipe access ONLY to the
    creating user's SID — remote identities and other users are denied
    by the kernel (frozen §59: "explicitly rejects remote clients and
    enforces the qualified local-identity ACL"). Named pipes in the
    ``\\\\.\\pipe\\`` namespace are local-machine scoped; remote access
    requires the DACL to permit it, which this DACL does not.
    """

    PIPE_NAME_PREFIX = r"\\.\pipe\webwire-authority-"

    # The single DACL grant: the creating user's SID with FILE_ALL_ACCESS.
    # FILE_ALL_ACCESS (not the minimal r/w+create-instance mask) because the
    # kernel's subsequent-instance access check denied the creator's own
    # next CreateNamedPipeW with the minimal mask. The security property is
    # unchanged: exactly ONE ACE, exactly the creating user — every other
    # identity is denied by omission and remote clients are additionally
    # rejected by PIPE_REJECT_REMOTE_CLIENTS.
    _USER_PIPE_ACCESS = 0x1F01FF  # FILE_ALL_ACCESS

    def __init__(self, authority_domain: Path) -> None:
        domain_hash = hashlib.sha256(str(authority_domain).encode("utf-8")).hexdigest()[:16]
        self._pipe_name = f"{self.PIPE_NAME_PREFIX}{domain_hash}"
        self._handle: Optional[int] = None
        self._sd_buffer: Any = None  # absolute SECURITY_DESCRIPTOR (own buffer)
        self._acl: Any = None  # SetEntriesInAclW allocation (LocalFree'd)
        self._sa: Any = None  # SECURITY_ATTRIBUTES kept alive for instance creation

    @property
    def path(self) -> str:
        return self._pipe_name

    def bind(self) -> None:
        """Create the FIRST named-pipe instance with the user-only DACL."""
        self._sa = self._build_current_user_security_attributes()
        self._handle = self._create_instance(first=True)

    def _create_instance(self, *, first: bool) -> Any:
        """Create one pipe instance. ``first`` adds FILE_FLAG_FIRST_PIPE_INSTANCE
        — foreign pre-created-pipe detection on bind; the accept loop's
        NEXT-instance creation must NOT set it (the kernel refuses a second
        instance carrying the flag even for the creating process)."""
        k32 = _win_kernel32()
        PIPE_ACCESS_DUPLEX = 0x3
        FILE_FLAG_FIRST_PIPE_INSTANCE = 0x80000
        PIPE_TYPE_BYTE = 0x0  # F-64: byte mode is 0x0 (0x1 is message type)
        PIPE_READMODE_BYTE = 0x0  # F-64: byte readmode is 0x0 (0x2 is message)
        PIPE_WAIT = 0x0
        PIPE_REJECT_REMOTE_CLIENTS = 0x8  # F-64: kernel-level remote rejection
        PIPE_UNLIMITED_INSTANCES = 255  # the accept loop creates the next instance per connection
        open_mode = PIPE_ACCESS_DUPLEX | (FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0)
        handle = k32.CreateNamedPipeW(
            self._pipe_name,
            open_mode,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            PIPE_UNLIMITED_INSTANCES,
            65536,  # out buffer
            65536,  # in buffer
            0,  # default timeout
            ctypes.byref(self._sa) if self._sa is not None else None,
        )
        if not handle or handle == _INVALID_HANDLE:
            err = ctypes.get_last_error()
            raise RuntimeError(f"CreateNamedPipeW failed for {self._pipe_name}: error {err}")
        return handle

    def _build_current_user_security_attributes(self) -> Any:
        """SECURITY_ATTRIBUTES whose self-relative descriptor (built by
        the 9-arg BuildSecurityDescriptorW ABI) grants pipe access ONLY
        to the current user's SID."""
        import ctypes.wintypes as wt

        adv = _win_advapi32()
        k32 = _win_kernel32()

        # The current process token — with argtypes declared, the
        # pseudo-handle crosses the boundary at full pointer width.
        token = ctypes.c_void_p()
        TOKEN_QUERY = 0x0008
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            err = ctypes.get_last_error()
            raise RuntimeError(f"OpenProcessToken failed: error {err}")
        try:
            # TokenUser == 1: the TOKEN_USER struct { SID_AND_ATTRIBUTES }.
            needed = ctypes.c_uint32(0)
            adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            buf = ctypes.create_string_buffer(max(needed.value, 64))
            if not adv.GetTokenInformation(token, 1, buf, needed.value, ctypes.byref(needed)):
                err = ctypes.get_last_error()
                raise RuntimeError(f"GetTokenInformation failed: error {err}")

            class SID_AND_ATTRIBUTES(ctypes.Structure):
                _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]

            class TOKEN_USER(ctypes.Structure):
                _fields_ = [("User", SID_AND_ATTRIBUTES)]

            user = ctypes.cast(buf, ctypes.POINTER(TOKEN_USER)).contents
            sid = user.User.Sid
            if not sid:
                raise RuntimeError("GetTokenInformation returned a null user SID")
        finally:
            k32.CloseHandle(token)

        # The single grant: the current user's SID by VALUE (TRUSTEE_IS_SID
        # means ptstrName points at the SID bytes — c_void_p, never a
        # wide string). TRUSTEE_W is the FIVE-field Win32 layout:
        # pMultipleObjects(8) + MultipleTrusteeOperation(enum, 4) +
        # TrusteeForm(4) + TrusteeType(4) + ptstrName(8) — there is no
        # separate MultipleTrustee pointer field.
        class TRUSTEE_W(ctypes.Structure):
            _fields_ = [
                ("pMultipleObjects", ctypes.c_void_p),
                ("MultipleTrusteeOperation", ctypes.c_uint32),
                ("TrusteeForm", ctypes.c_uint32),
                ("TrusteeType", ctypes.c_uint32),
                ("ptstrName", ctypes.c_void_p),
            ]

        class EXPLICIT_ACCESS_W(ctypes.Structure):
            _fields_ = [
                ("grfAccessPermissions", wt.DWORD),
                ("grfAccessMode", wt.DWORD),
                ("grfInheritance", wt.DWORD),
                ("Trustee", TRUSTEE_W),
            ]

        trustee = TRUSTEE_W()
        trustee.TrusteeForm = 0  # TRUSTEE_IS_SID
        trustee.TrusteeType = 1  # TRUSTEE_IS_USER
        trustee.ptstrName = sid

        ea = EXPLICIT_ACCESS_W()
        ea.grfAccessPermissions = self._USER_PIPE_ACCESS
        ea.grfAccessMode = 1  # GRANT_ACCESS (ACCESS_MODE starts at NOT_USED_ACCESS=0)
        ea.grfInheritance = 0  # NO_INHERITANCE
        ea.Trustee = trustee

        acl = ctypes.c_void_p()
        ret = adv.SetEntriesInAclW(1, ctypes.byref(ea), None, ctypes.byref(acl))
        if ret != _ERROR_SUCCESS:
            raise RuntimeError(f"SetEntriesInAclW failed: error {ret}")

        # The absolute descriptor lives in OUR buffer — no allocator to
        # free beyond the ACL. On x64 the absolute SECURITY_DESCRIPTOR is
        # 40 bytes (pointer fields at offsets 8/16/24/32); the legacy
        # 20-byte "minimum length" under-allocates and corrupts the heap
        # when SetSecurityDescriptorDacl writes the DACL pointer.
        sd_buffer = ctypes.create_string_buffer(64)
        SECURITY_DESCRIPTOR_REVISION = 1
        if not adv.InitializeSecurityDescriptor(sd_buffer, SECURITY_DESCRIPTOR_REVISION):
            err = ctypes.get_last_error()
            raise RuntimeError(f"InitializeSecurityDescriptor failed: error {err}")
        if not adv.SetSecurityDescriptorDacl(sd_buffer, True, acl, False):
            err = ctypes.get_last_error()
            raise RuntimeError(f"SetSecurityDescriptorDacl failed: error {err}")

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", wt.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wt.BOOL),
            ]

        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = ctypes.cast(sd_buffer, ctypes.c_void_p)
        sa.bInheritHandle = False
        self._sd_buffer = sd_buffer  # must outlive every CreateNamedPipeW
        self._acl = acl  # LocalAlloc'd by the API; LocalFree'd in close()
        return sa

    def accept(self) -> Any:
        """Wait for one client, then create the NEXT instance so later
        clients still find a rendezvous. Returns a socket-compatible
        connection over the connected handle."""
        k32 = _win_kernel32()
        assert self._handle is not None
        listen_handle = self._handle
        connected = k32.ConnectNamedPipe(listen_handle, None)
        if not connected:
            err = ctypes.get_last_error()
            if err != _ERROR_PIPE_CONNECTED:  # already-connected is fine
                raise RuntimeError(f"ConnectNamedPipe failed: error {err}")
        try:
            self._handle = self._create_instance(first=False)  # the next rendezvous
        except BaseException:
            # If the successor instance cannot be created, the endpoint is
            # unusable — close the connected handle and fail the accept.
            k32.CancelIoEx(listen_handle, None)
            k32.CloseHandle(listen_handle)
            raise
        return _WindowsPipeConnection(listen_handle)

    def close(self) -> None:
        if self._handle is not None:
            k32 = _win_kernel32()
            # CancelIoEx releases an accept thread blocked in
            # ConnectNamedPipe before the handle is closed underneath it.
            k32.CancelIoEx(self._handle, None)
            k32.CloseHandle(self._handle)
            self._handle = None
        if self._acl is not None:
            _win_kernel32().LocalFree(self._acl)
            self._acl = None
        self._sd_buffer = None
        self._sa = None
        # Named pipes disappear when the last handle closes — no unlink.
