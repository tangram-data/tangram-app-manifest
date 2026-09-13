"""Authenticate the native desktop parent before a worker executes a connector.

The private pipe alone does not prove who launched a publicly executable SDK.
Use the kernel's process identity through Security.framework, never ps output,
a supplied PID, an environment flag or the discovery token.
"""
import ctypes as C
import os
import sys


def authenticated_supervisor():
    if sys.platform != "darwin":
        return False
    owned = []
    try:
        cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        sec = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
        ptr = C.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ptr, C.c_char_p, C.c_uint32]
        cf.CFStringCreateWithCString.restype = ptr
        cf.CFNumberCreate.argtypes = [ptr, C.c_int, ptr]
        cf.CFNumberCreate.restype = ptr
        cf.CFDictionaryCreate.argtypes = [ptr, C.POINTER(ptr), C.POINTER(ptr), C.c_long, ptr, ptr]
        cf.CFDictionaryCreate.restype = ptr
        cf.CFRelease.argtypes = [ptr]
        sec.SecCodeCopyGuestWithAttributes.argtypes = [ptr, ptr, C.c_uint32, C.POINTER(ptr)]
        sec.SecRequirementCreateWithString.argtypes = [ptr, C.c_uint32, C.POINTER(ptr)]
        sec.SecCodeCheckValidity.argtypes = [ptr, C.c_uint32, ptr]
        parent = C.c_int(os.getppid())
        pid = cf.CFNumberCreate(None, 9, C.byref(parent))  # kCFNumberIntType
        owned.append(pid)
        key = ptr.in_dll(sec, "kSecGuestAttributePid")
        keys, values = (ptr * 1)(key.value), (ptr * 1)(pid)
        # Values remain owned until after the dictionary is released; no
        # retain/release callbacks are needed for this synchronous lookup.
        attributes = cf.CFDictionaryCreate(None, keys, values, 1, None, None)
        owned.append(attributes)
        code = ptr()
        if sec.SecCodeCopyGuestWithAttributes(None, attributes, 0, C.byref(code)) != 0:
            return False
        owned.append(code.value)
        rule = cf.CFStringCreateWithCString(None,
            b'anchor apple generic and certificate leaf[subject.OU] = "S7A9QSG6L4" and identifier "tangram-local-runtime-spike" and !entitlement["com.apple.security.get-task-allow"] exists',
            0x08000100)  # UTF-8; production Tangram signing team, not caller configuration
        owned.append(rule)
        requirement = ptr()
        if sec.SecRequirementCreateWithString(rule, 0, C.byref(requirement)) != 0:
            return False
        owned.append(requirement.value)
        return sec.SecCodeCheckValidity(code, 0, requirement) == 0
    except (OSError, ValueError, AttributeError):
        return False
    finally:
        if "cf" in locals():
            for value in reversed(owned):
                if value: cf.CFRelease(value)
