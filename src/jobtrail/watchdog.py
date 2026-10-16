"""Stop the server when the process that started it goes away.

The macOS app launches `jobtrail serve` as a child. If the app is force-quit or crashes it cannot
clean up after itself, and the server would keep running with no window. The app passes its own
pid in JOBTRAIL_PARENT_PID, and the server exits shortly after that process disappears."""

import os
import threading
import time
from collections.abc import Callable


def process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # signal 0 only checks that the process exists
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it just belongs to someone else
    return True


def watch_parent(
    pid: int,
    on_gone: Callable[[], None],
    *,
    interval: float = 2.0,
    alive: Callable[[int], bool] = process_is_alive,
) -> threading.Thread:
    """Call `on_gone` once, from a background thread, when `pid` is no longer running."""

    def loop() -> None:
        while alive(pid):
            time.sleep(interval)
        on_gone()

    thread = threading.Thread(target=loop, name="parent-watchdog", daemon=True)
    thread.start()
    return thread


def parent_pid_from_env(environ: dict[str, str] | None = None) -> int | None:
    value = (environ if environ is not None else os.environ).get("JOBTRAIL_PARENT_PID", "").strip()
    return int(value) if value.isdigit() and int(value) > 1 else None
