"""F-64 diagnostic: classic SD assembly + pipe creation + SDDL readback.

History: the documented 9-arg BuildSecurityDescriptorW returns
ERROR_SUCCESS but writes invalid out-params on this platform (verified
via sentinel probes + IsValidSecurityDescriptor == False; downstream
CreateNamedPipeW fails with 998). SetEntriesInAclW + classic
InitializeSecurityDescriptor/SetSecurityDescriptorDacl is the stable path.
"""

import ctypes
import ctypes.wintypes as wt

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
adv = ctypes.WinDLL("advapi32", use_last_error=True)

adv.OpenProcessToken.restype = ctypes.c_int
adv.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
adv.GetTokenInformation.restype = ctypes.c_int
adv.GetTokenInformation.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)
]
adv.IsValidSid.restype = ctypes.c_int
adv.IsValidSid.argtypes = [ctypes.c_void_p]
adv.ConvertSidToStringSidW.restype = ctypes.c_int
adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
adv.SetEntriesInAclW.restype = ctypes.c_uint32
adv.SetEntriesInAclW.argtypes = [
    ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
]
adv.InitializeSecurityDescriptor.restype = ctypes.c_int
adv.InitializeSecurityDescriptor.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
adv.SetSecurityDescriptorDacl.restype = ctypes.c_int
adv.SetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
adv.IsValidSecurityDescriptor.restype = ctypes.c_int
adv.IsValidSecurityDescriptor.argtypes = [ctypes.c_void_p]
adv.GetSecurityInfo.restype = ctypes.c_uint32
adv.GetSecurityInfo.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
]
adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = ctypes.c_int
adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(ctypes.c_uint32),
]
k32.GetCurrentProcess.restype = ctypes.c_void_p
k32.GetCurrentProcess.argtypes = []
k32.LocalFree.restype = ctypes.c_void_p
k32.LocalFree.argtypes = [ctypes.c_void_p]
k32.CreateNamedPipeW.restype = ctypes.c_void_p
k32.CreateNamedPipeW.argtypes = [
    ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
]
k32.CloseHandle.restype = ctypes.c_int
k32.CloseHandle.argtypes = [ctypes.c_void_p]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


class TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", SID_AND_ATTRIBUTES)]


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


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wt.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wt.BOOL),
    ]


# 1. Current user SID from the process token.
token = ctypes.c_void_p()
assert adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(token))
needed = ctypes.c_uint32(0)
adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
buf = ctypes.create_string_buffer(needed.value)
assert adv.GetTokenInformation(token, 1, buf, needed.value, ctypes.byref(needed))
user = ctypes.cast(buf, ctypes.POINTER(TOKEN_USER)).contents
sid = user.User.Sid
assert adv.IsValidSid(sid)
sid_str = ctypes.c_wchar_p()
adv.ConvertSidToStringSidW(sid, ctypes.byref(sid_str))
print("SID string:", sid_str.value)

# 2. One grant: current user, r/w data + create-instance.
trustee = TRUSTEE_W()
trustee.TrusteeForm = 0  # TRUSTEE_IS_SID
trustee.TrusteeType = 1  # TRUSTEE_IS_USER
trustee.ptstrName = sid
ea = EXPLICIT_ACCESS_W()
ea.grfAccessPermissions = 0x1F01FF  # FILE_ALL_ACCESS — user-only, everything
ea.grfAccessMode = 1  # GRANT_ACCESS (ACCESS_MODE starts at NOT_USED_ACCESS=0)
ea.grfInheritance = 0  # NO_INHERITANCE
ea.Trustee = trustee

acl = ctypes.c_void_p()
ret = adv.SetEntriesInAclW(1, ctypes.byref(ea), None, ctypes.byref(acl))
print("SetEntriesInAclW ret:", ret)

# 3. Classic absolute-format SD in our own buffer. On x64 the absolute
# SECURITY_DESCRIPTOR is 40 bytes (pointer fields at offsets 8/16/24/32);
# the legacy 20-byte "minimum length" under-allocates and corrupts the heap.
sd_buf = ctypes.create_string_buffer(64)
print("InitSD:", bool(adv.InitializeSecurityDescriptor(sd_buf, 1)))
print("SetDACL:", bool(adv.SetSecurityDescriptorDacl(sd_buf, True, acl, False)))
print("IsValidSD:", bool(adv.IsValidSecurityDescriptor(sd_buf)))

# 4. Create the pipe; read its real DACL back as SDDL.
sa = SECURITY_ATTRIBUTES()
sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
sa.lpSecurityDescriptor = ctypes.cast(sd_buf, ctypes.c_void_p)
sa.bInheritHandle = False

PIPE_ARGS = dict(
    dwOpenMode=0x3 | 0x80000,  # DUPLEX | FIRST_PIPE_INSTANCE
    dwPipeMode=0x0 | 0x0 | 0x0 | 0x8,  # BYTE | BYTE | WAIT | REJECT_REMOTE
    nMaxInstances=255,
)
name = r"\\.\pipe\webwire-diag-dacl"
h = k32.CreateNamedPipeW(
    name, PIPE_ARGS["dwOpenMode"], PIPE_ARGS["dwPipeMode"], PIPE_ARGS["nMaxInstances"],
    65536, 65536, 0, ctypes.byref(sa),
)
print("CreateNamedPipeW:", hex(h or 0), "err:", ctypes.get_last_error())

if h:
    sd_out = ctypes.c_void_p()
    ret3 = adv.GetSecurityInfo(h, 6, 7, None, None, ctypes.byref(sd_out), None, None)
    print("GetSecurityInfo ret:", ret3, "sd_out:", hex(sd_out.value or 0))
    if ret3 == 0 and sd_out:
        sddl = ctypes.c_wchar_p()
        ok = adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            sd_out, 1, 7, ctypes.byref(sddl), None
        )
        print("ConvertSDToString ok:", bool(ok), "err:", ctypes.get_last_error())
        if ok:
            print("Pipe DACL SDDL:", sddl.value)
    # The real acceptance probe: connect as the local client (same user).
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    client = k32.CreateFileW(name, 0x80000000 | 0x40000000, 0, None, 3, 0, None)
    print("Client CreateFileW:", hex(client or 0), "err:", ctypes.get_last_error())
    if client and client != 0xFFFFFFFFFFFFFFFF:
        k32.CloseHandle(client)
    # Second instance WITH the FIRST_PIPE_INSTANCE flag restored (full grant).
    h2a = k32.CreateNamedPipeW(
        name, PIPE_ARGS["dwOpenMode"], PIPE_ARGS["dwPipeMode"], PIPE_ARGS["nMaxInstances"],
        65536, 65536, 0, ctypes.byref(sa),
    )
    print("Second instance (with flag):", hex(h2a or 0), "err:", ctypes.get_last_error())
    if h2a and h2a != 0xFFFFFFFFFFFFFFFF:
        k32.CloseHandle(h2a)
    k32.CloseHandle(h)

if acl:
    k32.LocalFree(acl)
