"""Unicode shell links through IShellLinkW/IPersistFile; no script or target runs.

Interface contracts: https://learn.microsoft.com/windows/win32/shell/links
The vtable includes inherited IUnknown/IPersist slots. Keep signatures explicit.
"""
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import uuid


def _guid(value):
    return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le)


def _call(pointer, slot, result_type, arguments, *values):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *arguments)(table[slot])
    return function(pointer, *values)


def _checked(code):
    if code < 0:
        raise OSError(f'Windows shortcut operation failed (HRESULT 0x{code & 0xffffffff:08X}).')


@contextmanager
def _interfaces():
    ole = ctypes.WinDLL('ole32')
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    ole.CoCreateInstance.restype = ctypes.c_long
    initialized = ole.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    # An already initialized MTA remains owned by its caller.
    if initialized != -2147417850:  # RPC_E_CHANGED_MODE
        _checked(initialized)
    link, persist = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        clsid = _guid('00021401-0000-0000-c000-000000000046')
        iid_link = _guid('000214f9-0000-0000-c000-000000000046')
        iid_file = _guid('0000010b-0000-0000-c000-000000000046')
        _checked(ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid_link), ctypes.byref(link)))
        _checked(_call(link, 0, ctypes.c_long, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                       ctypes.byref(iid_file), ctypes.byref(persist)))  # QueryInterface
        yield link, persist
    finally:
        for pointer in (persist, link):
            if pointer:
                _call(pointer, 2, ctypes.c_ulong, [])  # IUnknown.Release
        if initialized in (0, 1):
            ole.CoUninitialize()


def shortcut(operation, path, values=None):
    if os.name != 'nt':
        raise RuntimeError('Windows shell links require Windows.')
    if operation not in ('save', 'read'):
        raise ValueError('Unknown shortcut operation.')
    path = str(Path(path).resolve())
    with _interfaces() as (link, persist):
        if operation == 'save':
            # IShellLinkW setters take UTF-16 strings, independent of ANSI code pages.
            for slot, field in ((20, 'target'), (11, 'arguments'), (9, 'cwd'), (7, 'description')):
                _checked(_call(link, slot, ctypes.c_long, [ctypes.c_wchar_p], values[field]))
            _checked(_call(link, 15, ctypes.c_long, [ctypes.c_int], 7))  # SetShowCmd(SW_SHOWMINNOACTIVE)
            _checked(_call(persist, 6, ctypes.c_long, [ctypes.c_wchar_p, ctypes.c_int], path, 1))  # Save
        # Reload the actual file. Do not Resolve or execute the shortcut target.
        _checked(_call(persist, 5, ctypes.c_long, [ctypes.c_wchar_p, ctypes.c_uint32], path, 0))  # Load(STGM_READ)
        result = {}
        for slot, field in ((3, 'target'), (10, 'arguments'), (8, 'cwd'), (6, 'description')):
            buffer = ctypes.create_unicode_buffer(32768)
            types, arguments = [ctypes.c_wchar_p, ctypes.c_int], [buffer, len(buffer)]
            if field == 'target':
                types += [ctypes.c_void_p, ctypes.c_uint32]
                arguments += [None, 4]  # GetPath(SLGP_RAWPATH)
            _checked(_call(link, slot, ctypes.c_long, types, *arguments))
            result[field] = buffer.value
        return result
