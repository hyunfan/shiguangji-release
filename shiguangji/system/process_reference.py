"""Identify one OS process without trusting a possibly reused PID; stdlib only."""
import ctypes
from ctypes import wintypes
import os


class ProcessReference:
    """A Windows process handle remains bound to the original process, not a reused PID."""
    def __init__(self, pid):
        self.pid, self.handle = pid, None
        if os.name == 'nt':
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.handle = self.kernel.OpenProcess(0x100000, False, pid)
            if not self.handle and ctypes.get_last_error() not in {87,1168}:
                raise OSError('无法确认原后台进程，守护不会尝试重启。')

    def alive(self):
        if os.name == 'nt':
            if not self.handle:return False
            result = self.kernel.WaitForSingleObject(self.handle, 0)
            if result not in {0,258}:raise OSError('无法读取后台进程状态。')
            return result == 258
        try:os.kill(self.pid, 0);return True
        except ProcessLookupError:return False

    def close(self):
        if self.handle:self.kernel.CloseHandle(self.handle);self.handle=None
