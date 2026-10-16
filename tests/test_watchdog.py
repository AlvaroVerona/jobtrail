import os
import subprocess
import sys
import threading
import time

from jobtrail.watchdog import parent_pid_from_env, process_is_alive, watch_parent


def test_a_running_process_is_alive_and_a_dead_one_is_not():
    assert process_is_alive(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not process_is_alive(child.pid)


def test_the_callback_fires_once_when_the_parent_goes_away():
    states = iter([True, True, False])
    fired = threading.Event()
    calls = []

    thread = watch_parent(
        1234,
        lambda: (calls.append(1), fired.set()),
        interval=0.01,
        alive=lambda pid: next(states, False),
    )
    assert fired.wait(2)
    thread.join(2)
    assert calls == [1]


def test_nothing_fires_while_the_parent_lives():
    fired = threading.Event()
    watch_parent(1234, fired.set, interval=0.01, alive=lambda pid: True)
    time.sleep(0.1)
    assert not fired.is_set()


def test_parent_pid_comes_from_the_environment():
    assert parent_pid_from_env({"JOBTRAIL_PARENT_PID": "4321"}) == 4321
    for bad in ("", "abc", "-5", "0", "1", " "):
        assert parent_pid_from_env({"JOBTRAIL_PARENT_PID": bad}) is None
    assert parent_pid_from_env({}) is None


def test_a_real_server_exits_when_its_parent_disappears(tmp_path):
    """End to end: a throwaway 'parent' starts the server and dies; the server must follow."""
    port = 8795
    parent_code = (
        "import os, subprocess, sys\n"
        "env = dict(os.environ, JOBTRAIL_PARENT_PID=str(os.getpid()),"
        f" JOBTRAIL_PORT='{port}', JOBTRAIL_DATA_DIR=r'{tmp_path}')\n"
        "child = subprocess.Popen([sys.executable, '-m', 'jobtrail.cli', 'serve'], env=env,"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(child.pid, flush=True)\n"
        "import time; time.sleep(60)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True
    )
    try:
        server_pid = int(parent.stdout.readline())
        assert process_is_alive(server_pid)
        time.sleep(1)  # let the server and its watchdog start
        parent.kill()
        parent.wait()
        deadline = time.time() + 15
        while time.time() < deadline and process_is_alive(server_pid):
            time.sleep(0.25)
        assert not process_is_alive(server_pid), "the server outlived its parent"
    finally:
        parent.kill()
        try:
            os.kill(server_pid, 9)
        except (ProcessLookupError, NameError):
            pass
