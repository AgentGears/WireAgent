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
    stable local identifier). The DACL grants pipe read/write ONLY to
    the creating user's SID — remote identities and other users are
    denied by the kernel (frozen §59: "explicitly rejects remote clients
    and enforces the qualified local-identity ACL"). Named pipes in the
    ``\\\\.\\pipe\\`` namespace are local-machine scoped; remote access
    requires the DACL to permit it, which this DACL does not.

    The current-user restriction is enforced by creating the pipe with a
    SECURITY_ATTRIBUTES whose DACL contains exactly one grant: the
    current user's SID. Implementation via ``ctypes`` on the Win32 API;
    full Windows Server 2025 qualification is Layer 8.
    """

    PIPE_NAME_PREFIX = r"\\.\pipe\webwire-authority-"

    def __init__(self, authority_domain: Path) -> None:
        domain_hash = hashlib.sha256(str(authority_domain).encode("utf-8")).hexdigest()[:16]
        self._pipe_name = f"{self.PIPE_NAME_PREFIX}{domain_hash}"
        self._handle: Optional[int] = None

    @property
    def path(self) -> str:
        return self._pipe_name

    def bind(self) -> None:
        """Create the named pipe with a current-user-only DACL."""
        import ctypes

        dacl_bytes = self._build_current_user_dacl()
        handle = self._create_pipe(dacl_bytes)
        if handle in (0, -1):
            err = ctypes.windll.kernel32.GetLastError()  # type: ignore[attr-defined]
            raise RuntimeError(f"CreateNamedPipeW failed for {self._pipe_name}: error {err}")
        self._handle = handle

    def _build_current_user_dacl(self) -> Any:
        """Build a self-relative SECURITY_DESCRIPTOR whose DACL grants
        pipe access ONLY to the current user's SID."""
        import ctypes
        import ctypes.wintypes as wt

        advapi32 = ctypes.windll.advapi32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        # Get the current process token.
        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise RuntimeError("OpenProcessToken failed")
        try:
            # Get the user SID from the token.
            needed = wt.DWORD(0)
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            buf = ctypes.create_string_buffer(needed.value or 512)
            if not advapi32.GetTokenInformation(token, 1, buf, needed, ctypes.byref(needed)):
                raise RuntimeError("GetTokenInformation failed")
            # TOKEN_USER starts with SID_AND_ATTRIBUTES {pSid, attrs}.
            sid_ptr = ctypes.cast(ctypes.byref(buf, 0), ctypes.POINTER(ctypes.c_void_p)).contents
        finally:
            kernel32.CloseHandle(token)

        # Build the EXPLICIT_ACCESS array granting the SID full pipe access.
        # TRUSTEE layout on 64-bit: pMultipleObjects(8) + MultipleTrustee(8)
        # + MultipleTrusteeOperation(4) + TrusteeForm(4) + TrusteeType(4) +
        # ptstrName(8) = 40 bytes, but with alignment this needs care.
        # Simpler: use BuildExplicitAccessWithNameW.
        class TRUSTEE_W(ctypes.Structure):
            _fields_ = [
                ("pMultipleObjects", ctypes.c_void_p),
                ("MultipleTrustee", ctypes.c_void_p),
                ("MultipleTrusteeOperation", ctypes.c_uint),
                ("TrusteeForm", ctypes.c_uint),
                ("TrusteeType", ctypes.c_uint),
                ("ptstrName", ctypes.c_wchar_p),
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
        # The name field carries the SID bytes when TrusteeForm is IS_SID.
        trustee.ptstrName = ctypes.cast(sid_ptr, ctypes.c_wchar_p)

        ea = EXPLICIT_ACCESS_W()
        ea.grfAccessPermissions = 0x1 | 0x2  # FILE_READ_DATA | FILE_WRITE_DATA
        ea.grfAccessMode = 0  # GRANT_ACCESS
        ea.grfInheritance = 0  # NO_INHERITANCE
        ea.Trustee = trustee

        acl = ctypes.c_void_p()
        result = advapi32.SetEntriesInAclW(1, ctypes.byref(ea), None, ctypes.byref(acl))
        if result != 0:
            raise RuntimeError(f"SetEntriesInAclW failed: error {result}")

        # Convert to a self-relative security descriptor the pipe accepts.
        sd = ctypes.c_void_p()
        if not advapi32.BuildSecurityDescriptorW(
            None, None, 0, None, 1, ctypes.byref(ea), None, ctypes.byref(sd)
        ):
            raise RuntimeError("BuildSecurityDescriptorW failed")

        # SECURITY_ATTRIBUTES wrapping the descriptor.
        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", wt.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wt.BOOL),
            ]

        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = sd
        sa.bInheritHandle = False
        self._sa = sa  # keep alive
        return sa

    _sa: Any = None  # SECURITY_ATTRIBUTES kept alive for the pipe lifetime

    def _create_pipe(self, sa: Any) -> int:
        """Call CreateNamedPipeW with the DACL-backed security attributes."""
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PIPE_ACCESS_DUPLEX = 0x3
        FILE_FLAG_FIRST_PIPE_INSTANCE = 0x80000
        PIPE_TYPE_BYTE = 0x1
        PIPE_READMODE_BYTE = 0x2
        PIPE_WAIT = 0x0
        return kernel32.CreateNamedPipeW(
            self._pipe_name,
            PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            1,  # single instance
            65536,  # out buffer
            65536,  # in buffer
            0,  # default timeout
            ctypes.byref(self._sa) if self._sa is not None else None,
        )

    def accept(self) -> Any:
        """Wait for a client connection on the named pipe."""
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        assert self._handle is not None
        connected = kernel32.ConnectNamedPipe(self._handle, None)
        if not connected:
            err = kernel32.GetLastError()
            if err != 535:  # ERROR_PIPE_CONNECTED is fine (already connected)
                raise RuntimeError(f"ConnectNamedPipe failed: error {err}")
        return self._handle

    def close(self) -> None:
        if self._handle is not None:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._handle)  # type: ignore[attr-defined]
            self._handle = None
        # Named pipes disappear when the last handle closes — no unlink.
