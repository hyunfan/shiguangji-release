"""Keep a Windows worker awake only for the lifetime of its active task."""
from contextlib import contextmanager
import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def execution_state_api():
    import ctypes
    function = ctypes.WinDLL('kernel32', use_last_error=True).SetThreadExecutionState
    function.argtypes = [ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    return function


@contextmanager
def prevent_idle_sleep(on_state=lambda state: None):
    """Enter and leave on the same worker thread; never change the power plan.

    ES_DISPLAY_REQUIRED and away mode are deliberately not requested. Explicit
    sleep, lid actions and shutdown retain their normal Windows behavior.
    """
    setter = None
    active = False
    state = 'unsupported'
    if sys.platform == 'win32':
        try:
            setter = execution_state_api()
            active = bool(setter(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
            state = 'active' if active else 'failed'
        except (OSError, AttributeError):
            state = 'failed'
    try:
        # This callback is inside the try so a reporting error cannot leak a
        # successful request. A failed OS request must not abort collection.
        on_state(state)
        yield active
    finally:
        released = True
        if active:
            try:
                released = bool(setter(ES_CONTINUOUS))
            except (OSError, AttributeError):
                released = False
        on_state('off' if released else 'failed')
