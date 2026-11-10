"""The single Qumus entry point: live work and History (recordings) in one app.

From anywhere: .venv/bin/python scripts/start_qumus.py [--status | --stop | --restart]
(Windows: .venv\\Scripts\\python.exe scripts\\start_qumus.py ...)
`Start Qumus.command` / `Stop Qumus.command` (macOS, in ~/Projects/Qumus and in v2)
and `Start Qumus.bat` / `Stop Qumus.bat` (Windows) invoke this script.

It always opens the owner Hall-bar workspace and heals the states that used to
block startup (Jialei 2026-11-04, "one stable entry"):
- a WAL left by a window closed without Control+C is backed up, then
  checkpointed into the database (only while no Qumus uses the workspace);
- a workspace made by older code (bench geometry) or for another target is
  moved aside intact to runs/workspace/archive/ and a fresh one is started
  with the same target bundle. Nothing is deleted.

Robust entry (Jialei 2026-11-06): Qumus was often started from different
terminals and folders, the terminal of a running server got lost, and a new
start only said "press Control+C in its original terminal". Now:
- the running server holds an exclusive lock on runs/workspace/qumus-server.lock
  for its whole life; the file records its pid, port, code version and log.
  The kernel drops the lock when the process ends, however it ends, so the
  lock can never be stale. Healing and archiving happen only under this lock.
- starting again while the same version runs just opens the browser; an older
  version or a server that does not answer is restarted after one question;
- the server runs in the background by default (closing a terminal or VS Code
  does not stop it); `--stop` / Stop Qumus.command stops it safely (SIGINT, the
  same as Control+C). There is no terminal-bound mode and no other launcher.
- closing every Qumus page in the browser stops it after STOP_WHEN_CLOSED_S
  seconds, unless agents or a lab job are working (then once they finish);
  see qumus2/ui/presence.py.
- a server started before this lock existed is recognised by its command line
  (`qumus2 serve` on this workspace's database, this repository) and handled
  the same way. Other Qumus servers (other ports, scratch databases) are never
  touched.

Windows (2026-11-06): the same behaviour with three differences, because a
locked file handle cannot be handed to a child process there and a signal
sent from another process cannot be caught:
- the lock is a byte-range lock (msvcrt) and the server takes it itself: the
  launcher starts `start_qumus.py --serve-holding-lock`, which locks, records
  its pid and runs the server in the same process (LOCK_HANDOFF);
- `--stop` asks the server over HTTP (POST /api/server/shutdown with the token
  recorded in the lock file), which then stops itself as Control+C does. This
  is tried first on every system; signals remain the fallback (on Windows the
  fallback is `taskkill /T /F`, which also ends the lab process);
- the server starts in its own process group without a console window, so
  closing the Start Qumus.bat window does not stop it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import runpy
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

WINDOWS = os.name == 'nt'
if WINDOWS:
    import msvcrt
else:
    import fcntl

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = 'runs/workspace/hallbar-linked-owner-20261101'
DATABASE = WORKSPACE + '/qumus.db'
# Outside WORKSPACE on purpose: archive_workspace moves WORKSPACE's contents.
LOCK = 'runs/workspace/qumus-server.lock'
LOGS = 'runs/workspace/logs'
HOST = '127.0.0.1'
PORT = 8765
STARTUP_TIMEOUT_S = 180.0
STOP_TIMEOUT_S = 30.0
# Closing every Qumus page stops it after this long (Jialei 2026-11-06).
STOP_WHEN_CLOSED_S = 15
# Windows: the server takes the lock itself (see the module docstring).
LOCK_HANDOFF = WINDOWS
# Windows locks a byte far past the JSON record, so readers are not blocked.
WINDOWS_LOCK_BYTE = 1 << 30
LAUNCHER = 'Qumus.bat' if WINDOWS else 'Qumus.command'
# The same names as in qumus2/ui/presence.py (repeated: --status and --stop
# do not import the package).
SHUTDOWN_TOKEN_ENV = 'QUMUS_SHUTDOWN_TOKEN'
SHUTDOWN_TOKEN_HEADER = 'X-Qumus-Shutdown-Token'


def _stamp() -> str:
    return time.strftime('%Y%m%d-%H%M%S', time.gmtime())


def _local_time(epoch: float | None) -> str:
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(epoch)) if epoch else 'unknown time'


def heal_wal(database: Path) -> bool:
    """Back up and checkpoint a leftover WAL. Caller ensures no server runs."""
    wal = database.with_name(database.name + '-wal')
    if not database.exists() or not wal.exists() or not wal.stat().st_size:
        return False
    backup = REPO / 'runs/workspace/backups' / ('wal-' + _stamp())
    backup.mkdir(parents=True, exist_ok=True)
    for path in (database, wal, database.with_name(database.name + '-shm')):
        if path.exists():
            shutil.copy2(path, backup / path.name)
    with sqlite3.connect(database) as connection:
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    print(f'Recovered an unclean shutdown (backup: {backup.relative_to(REPO)}).', flush=True)
    return True


def workspace_mismatch(database: Path, bundle: Path) -> str | None:
    """Why the existing workspace cannot open with this code, or None.

    The brain's store no longer holds the laboratory (the simulated lab keeps its
    own store in qumus-simlab), so only a store made by the old in-process
    testbench is refused: it mixed laboratory state into the brain's records."""
    del bundle
    if not database.exists():
        return None
    with sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True) as connection:
        row = connection.execute("SELECT value FROM meta WHERE key = 'simulation_bench_geometry'").fetchone() \
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone() else None
    if row is not None or database.with_suffix('.testbench.pkl').exists():
        return 'it was made by the old in-process testbench, before the laboratory became its own program'
    return None


def archive_workspace(workspace: Path, reason: str) -> Path:
    """Move everything but the target bundles aside, intact."""
    archive = REPO / 'runs/workspace/archive' / f'{workspace.name}-{_stamp()}'
    archive.mkdir(parents=True)
    for path in workspace.iterdir():
        if path.name == 'targets':
            shutil.copytree(path, archive / 'targets')
        else:
            shutil.move(str(path), archive / path.name)
    print('The previous workspace cannot open with this version (' + reason + ').', flush=True)
    print(f'It was kept unchanged in {archive.relative_to(REPO)}; starting a fresh workspace.', flush=True)
    return archive


# --- finding, stopping and registering the one workspace server -------------

def code_version() -> str | None:
    """The commit this checkout runs, or None outside Git."""
    try:
        result = subprocess.run(['git', '-C', str(REPO), 'rev-parse', '--short=12', 'HEAD'],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def lock_file(path: Path, *, create: bool = True) -> int | None:
    """An fd holding the exclusive lock on `path`, or None while another process holds it.
    The lock ends with the process that holds the fd, however it ends."""
    if not create and not path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if WINDOWS:
            os.lseek(fd, WINDOWS_LOCK_BYTE, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def try_lock(*, create: bool = True) -> int | None:
    """An fd holding the exclusive workspace lock, or None while a server holds it."""
    return lock_file(REPO / LOCK, create=create)


def read_state(path: Path | None = None) -> dict:
    try:
        state = json.loads((path or REPO / LOCK).read_text(encoding='utf-8') or '{}')
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def write_state(fd: int, state: dict) -> None:
    data = json.dumps(state, indent=2).encode()
    os.ftruncate(fd, 0)
    if hasattr(os, 'pwrite'):
        os.pwrite(fd, data, 0)
    else:  # Windows
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, data)


def health(port: int, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(f'http://{HOST}:{port}/api/health', timeout=timeout) as response:
            value = json.loads(response.read())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        # A just-closed server may leave sockets in TIME_WAIT. Match the
        # application's reuse policy so a normal restart is not refused.
        # (On Windows SO_REUSEADDR would let the bind steal a port in use, and
        # TIME_WAIT does not block a bind there.)
        if not WINDOWS:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, port))
        except OSError:
            return False
    return True


def alive(pid: int) -> bool:
    if WINDOWS:
        # os.kill(pid, 0) would END the process there (TerminateProcess)
        from qumus2.runtime.oscompat import pid_alive

        return pid_alive(pid)
    with contextlib.suppress(ChildProcessError, OSError):
        if os.waitpid(pid, os.WNOHANG)[0] == pid:   # our own child that has exited
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _run(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def process_command(pid: int) -> str:
    return _run(['ps', '-o', 'command=', '-p', str(pid)]).strip()


def process_cwd(pid: int) -> Path | None:
    for line in _run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn']).splitlines():
        if line.startswith('n'):
            return Path(line[1:])
    proc = Path(f'/proc/{pid}/cwd')
    with contextlib.suppress(OSError):
        return proc.resolve()
    return None


def listener_pid(port: int) -> int | None:
    for word in _run(['lsof', '-nP', f'-tiTCP:{port}', '-sTCP:LISTEN']).split():
        if word.isdigit():
            return int(word)
    return None


def belongs_here(pid: int, role: str = 'serve') -> bool:
    """`qumus2 <role>` on this repository's owner workspace database."""
    command = process_command(pid)
    if f'qumus2 {role}' not in command or DATABASE not in command:
        return False
    cwd = process_cwd(pid)
    if cwd is not None:
        with contextlib.suppress(OSError):
            return cwd.resolve() == REPO.resolve()
    return str(REPO) in command


def leftover_lab_pids() -> list[int]:
    """Lab processes of this workspace whose server is gone (they hold the database)."""
    found = []
    for word in _run(['pgrep', '-f', f'qumus2 lab --db {DATABASE}']).split():
        if word.isdigit() and int(word) != os.getpid() and belongs_here(int(word), 'lab'):
            found.append(int(word))
    return found


@dataclass
class Server:
    pid: int
    port: int
    version: str | None = None
    started_at: float | None = None
    log: str | None = None
    registered: bool = True   # False: started before the lock file existed
    starting: bool = False
    token: str | None = None  # for POST /api/server/shutdown


def find_server(*, read_only: bool = False) -> Server | None:
    """The server of this workspace, if one runs. Leaves no lock held.
    read_only (--check-only) opens no socket and asks only lsof about the port."""
    fd = try_lock(create=False)
    if fd is None and (REPO / LOCK).exists():
        state = read_state()
        pid = state.get('pid')
        if isinstance(pid, int) and alive(pid):
            return Server(pid=pid, port=int(state.get('port') or PORT), version=state.get('version'),
                          started_at=state.get('started_at'), log=state.get('log'),
                          starting=state.get('phase') == 'starting', token=state.get('token'))
        # The lock is held, but its record is not readable yet: a start is under way.
        return Server(pid=-1, port=int(state.get('port') or PORT), starting=True)
    if fd is not None:
        os.close(fd)
    if not read_only and port_free(PORT):
        return None
    pid = listener_pid(PORT)
    if pid is not None and belongs_here(pid):
        return Server(pid=pid, port=PORT, registered=False)
    return None


def describe(server: Server) -> str:
    if not server.registered:
        return f'pid {server.pid}, port {server.port}, started before this launcher version'
    return (f'pid {server.pid}, port {server.port}, started {_local_time(server.started_at)}, '
            f'code {server.version or "unknown"}')


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.2)
    return not alive(pid)


def request_shutdown(port: int, token: str) -> bool:
    """Ask the server to stop itself as Control+C does (POST /api/server/shutdown)."""
    request = urllib.request.Request(f'http://{HOST}:{port}/api/server/shutdown', data=b'',
                                     method='POST', headers={SHUTDOWN_TOKEN_HEADER: token})
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def stop_pid(pid: int, timeout: float = STOP_TIMEOUT_S, *, port: int | None = None,
             token: str | None = None) -> bool:
    """The server's own clean stop over HTTP when its token is known, else SIGINT
    (what Control+C sends: agents pause at a safe point, the lab saves its
    state); SIGTERM if it does not end in time. On Windows a signal from
    outside cannot be caught, so the fallback there ends the process tree."""
    asked = bool(token and port and request_shutdown(port, token))
    if asked and _wait_gone(pid, timeout):
        return True
    if WINDOWS:
        _run(['taskkill', '/PID', str(pid), '/T', '/F'])
        return _wait_gone(pid, 10.0)
    steps = ((signal.SIGINT, timeout), (signal.SIGTERM, 10.0))
    for sig, wait in steps[1:] if asked else steps:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        if _wait_gone(pid, wait):
            return True
    return False


def stop_server(server: Server) -> bool:
    if server.pid <= 0:
        print('Qumus is still starting; try again in a minute.')
        return False
    print(f'Stopping Qumus ({describe(server)}) ...', flush=True)
    if not stop_pid(server.pid, port=server.port, token=server.token):
        force = f'taskkill /PID {server.pid} /T /F' if WINDOWS else f'kill -9 {server.pid}'
        print(f'Qumus (pid {server.pid}) did not stop. Force it with: {force}')
        return False
    for pid in leftover_lab_pids():
        stop_pid(pid, 10.0)
    deadline = time.monotonic() + 10
    while not port_free(server.port) and time.monotonic() < deadline:
        time.sleep(0.2)
    print('Qumus stopped.', flush=True)
    return True


def ask(question: str, *, default: bool, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    hint = '[Y/n]' if default else '[y/N]'
    try:
        answer = input(f'{question} {hint} ').strip().lower()
    except EOFError:
        return False
    return default if not answer else answer in ('y', 'yes')


def open_browser(url: str, no_browser: bool) -> None:
    if not no_browser:
        webbrowser.open(url)


def print_status() -> int:
    server = find_server()
    if server is None:
        print('Qumus is not running.')
        leftovers = leftover_lab_pids()
        if leftovers:
            print('A leftover lab process still holds the workspace: pid ' +
                  ', '.join(map(str, leftovers)) + '. `--stop` ends it.')
        return 1
    answer = health(server.port)
    print(f'Qumus is running: http://{HOST}:{server.port} ({describe(server)}).')
    current = code_version()
    if server.registered and server.version and current and server.version != current:
        print(f'It runs older code than this checkout ({current}); start again to restart it.')
    if answer is None:
        print('It does not answer right now.')
    elif answer.get('busy'):
        print('It is working on a task.')
    if server.log:
        print('Log:', server.log)
    return 0


def stop_command() -> int:
    server = find_server()
    if server is None:
        leftovers = leftover_lab_pids()
        for pid in leftovers:
            stop_pid(pid, 10.0)
        print('Ended a leftover lab process.' if leftovers else 'Qumus is not running.')
        return 0
    return 0 if stop_server(server) else 1


def handle_running(server: Server, args: argparse.Namespace) -> bool:
    """True when the caller should go on and start a new server."""
    url = f'http://{HOST}:{server.port}'
    if server.starting:
        print('Qumus is starting in another window; waiting for it ...', flush=True)
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline and health(server.port, 1.0) is None:
            time.sleep(1.0)
        if health(server.port) is None:
            print('It did not come up. Run `--status`, or `--stop` and start again.')
            raise SystemExit(1)
        print(f'Qumus is running. Opening {url}')
        open_browser(url, args.no_browser)
        return False
    answer = health(server.port)
    current = code_version()
    same = (answer is not None and server.registered and server.version == current
            and answer.get('pid') in (None, server.pid))
    if not args.restart and same:
        print(f'Qumus is already running ({describe(server)}). Opening {url}')
        open_browser(url, args.no_browser)
        return False
    if args.restart:
        why = 'You asked for a restart.'
    elif answer is None:
        why = 'It does not answer.'
    elif not server.registered:
        why = 'It was started by an older launcher, so its code version is unknown.'
    else:
        why = f'It runs older code ({server.version or "unknown"}; this checkout is {current or "unknown"}).'
    print(f'Qumus is already running ({describe(server)}).')
    print(why)
    busy = bool(answer and answer.get('busy'))
    if busy:
        print('It is working on a task right now. A restart pauses it at a safe point; '
              'the saved workspace resumes after the restart.')
    if not ask('Restart it now?', default=not busy, assume_yes=args.yes):
        if answer is not None:
            print(f'Kept the running Qumus. Opening {url}')
            open_browser(url, args.no_browser)
        else:
            print(f'Kept it. Stop it with Stop {LAUNCHER} (or --stop) and start again.')
        return False
    if not stop_server(server):
        raise SystemExit(1)
    return True


def acquire_lock(timeout: float = 15.0) -> int:
    deadline = time.monotonic() + timeout
    while True:
        fd = try_lock()
        if fd is not None:
            return fd
        if time.monotonic() > deadline:
            print('Another Qumus start or server holds the workspace. Run --status.')
            raise SystemExit(1)
        time.sleep(0.3)


def wait_until_up(process: subprocess.Popen, port: int, log: Path) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if health(port, 1.0) is not None:
            return True
        time.sleep(0.5)
    print('Qumus did not start. The end of its log:', flush=True)
    with contextlib.suppress(OSError):
        print(''.join(log.read_text(errors='replace', encoding="utf-8").splitlines(keepends=True)[-30:]))
    if process.poll() is None:
        stop_pid(process.pid, 10.0)
    return False


def start_background(command: list[str], fd: int, state: dict, port: int = PORT) -> Path | None:
    """Start the server detached from this terminal; it inherits the lock
    (LOCK_HANDOFF: it takes the lock itself, see serve_holding_lock).
    Returns its log once it answers, or None (it is stopped again)."""
    log = REPO / LOGS / f'qumus-{_stamp()}.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    state = state | {'token': token}
    env = {**os.environ, SHUTDOWN_TOKEN_ENV: token}
    write_state(fd, state | {'log': str(log)})
    with log.open('ab') as output:
        if LOCK_HANDOFF:
            os.close(fd)   # released here, taken again by the server itself
            env['PYTHONUTF8'] = '1'   # its log and files are UTF-8 whatever the code page
            flags = (getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
                     | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            wrapped = [sys.executable, str(Path(__file__).resolve()), '--serve-holding-lock',
                       str(REPO / LOCK), '--', *command]
            process = subprocess.Popen(wrapped, cwd=REPO, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, env=env, creationflags=flags,
                                       start_new_session=not WINDOWS)
        else:
            process = subprocess.Popen(command, cwd=REPO, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, env=env, start_new_session=True,
                                       pass_fds=(fd,))
    if not LOCK_HANDOFF:
        write_state(fd, state | {'pid': process.pid, 'log': str(log)})
    if not wait_until_up(process, port, log):
        return None
    if not LOCK_HANDOFF:
        write_state(fd, state | {'pid': process.pid, 'log': str(log), 'phase': 'running'})
        os.close(fd)   # the server's copy keeps the lock
    return log


def serve_holding_lock(argv: list[str]) -> int:
    """`--serve-holding-lock <lock> -- <python> (-m <module> | <script>) <args...>`:
    take the workspace lock, record this process as the server, then run the
    server in this same process, so the lock lives exactly as long as it."""
    if len(argv) < 4 or argv[1] != '--':
        print('usage: --serve-holding-lock <lock> -- <python> -m <module> <args...>', file=sys.stderr)
        return 2
    path, command = Path(argv[0]), argv[2:]
    deadline = time.monotonic() + 15.0
    while (fd := lock_file(path)) is None:
        if time.monotonic() > deadline:
            print('Another Qumus start or server holds the workspace lock.', file=sys.stderr)
            return 1
        time.sleep(0.2)
    state = read_state(path) | {'pid': os.getpid(), 'phase': 'starting'}
    write_state(fd, state)
    port = int(state.get('port') or PORT)

    def mark_running() -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if health(port, 1.0) is not None:
                write_state(fd, state | {'phase': 'running'})
                return
            time.sleep(0.5)

    threading.Thread(target=mark_running, daemon=True, name='lock-record').start()
    if command[1] == '-m':
        sys.argv = [command[2], *command[3:]]
        runpy.run_module(command[2], run_name='__main__', alter_sys=True)
    else:
        sys.argv = command[1:]
        runpy.run_path(command[1], run_name='__main__')
    return 0   # fd (and the lock) is released when this process ends


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--target-bundle', type=Path,
                        help='explicit operator selection of the private owner target.json')
    parser.add_argument('--status', action='store_true', help='say whether Qumus runs, and where')
    parser.add_argument('--stop', action='store_true', help='stop the running Qumus safely')
    parser.add_argument('--restart', action='store_true', help='stop the running Qumus, then start')
    parser.add_argument('--yes', action='store_true', help='answer yes to the restart question')
    if len(sys.argv) > 1 and sys.argv[1] == '--serve-holding-lock':
        raise SystemExit(serve_holding_lock(sys.argv[2:]))
    args = parser.parse_args()
    os.chdir(REPO)
    if args.status:
        raise SystemExit(print_status())
    if args.stop:
        raise SystemExit(stop_command())
    if (REPO / 'runs/desktop-openai').exists():
        raise SystemExit('The old workspace still exists. Migrate it before starting; see docs/run-data.md.')
    database = REPO / DATABASE
    bundles = ([args.target_bundle.resolve()] if args.target_bundle else
               sorted(database.parent.glob('targets/*/target.json')))
    if len(bundles) != 1 or not bundles[0].is_file():
        print('Startup blocked: select exactly one existing owner target bundle under '
              f'{WORKSPACE}/targets/<sourceSHA>/target.json.')
        raise SystemExit(2)
    from qumus2.testbench.hallbar import load_imported_hallbar_target

    try:
        load_imported_hallbar_target(bundles[0].parent)
    except (ValueError, OSError) as error:
        print('Startup blocked: invalid owner target bundle: ' + str(error))
        raise SystemExit(2) from None
    # The simulated laboratory (qumus-simlab) is started by `serve` when the
    # stations in stations.yaml are simulated and not running; the operator's
    # target goes to it.
    command = [sys.executable, '-m', 'qumus2', 'serve', '--openai',
               '--db', DATABASE, '--budget', '40', '--host', HOST, '--port', str(PORT),
               '--target-bundle', str(bundles[0]),
               '--stop-when-closed', str(STOP_WHEN_CLOSED_S)]
    command.append('--no-browser')   # the launcher opens the page once the server answers
    if args.check_only:
        print('Repository:', REPO)
        print('Read-only startup plan; services and execution are not started.')
        server = find_server(read_only=True)
        if server is not None:
            print(f'Qumus is already running ({describe(server)}); a start would open it, '
                  'or offer a restart if it is older or does not answer.')
        else:
            wal = database.with_name(database.name + '-wal')
            if wal.exists() and wal.stat().st_size:
                print('A leftover WAL will be backed up and recovered at startup.')
            else:
                reason = workspace_mismatch(database, bundles[0])
                if reason:
                    print('The workspace will be archived and started fresh at startup: ' + reason)
        print('Command:', ' '.join(command))
        return
    server = find_server()
    if server is not None and not handle_running(server, args):
        return
    fd = acquire_lock()
    if not port_free(PORT):
        pid = listener_pid(PORT)
        print(f'Port {PORT} is used by another program' + (f' (pid {pid}: {process_command(pid)})'
                                                            if pid else '') + '.')
        print('Qumus needs this port. Close that program, then start again.')
        raise SystemExit(1)
    leftovers = leftover_lab_pids()
    if leftovers:
        print('A lab process from an earlier Qumus still holds the workspace (pid '
              + ', '.join(map(str, leftovers)) + ').')
        if not ask('End it and continue?', default=True, assume_yes=args.yes) or \
                not all(stop_pid(pid, 10.0) for pid in leftovers):
            raise SystemExit(1)
    heal_wal(database)
    reason = workspace_mismatch(database, bundles[0])
    if reason:
        archive_workspace(database.parent, reason)
    url = f'http://{HOST}:{PORT}'
    state = {'pid': os.getpid(), 'port': PORT, 'version': code_version(), 'started_at': time.time(),
             'phase': 'starting', 'repository': str(REPO)}
    print('Starting Qumus in the background ...', flush=True)
    log = start_background(command, fd, state)
    if log is None:
        raise SystemExit(1)
    print(f'Qumus — software testbench. Open {url}', flush=True)
    print('Recordings: History. Watching them makes no model calls.', flush=True)
    print('New messages use model credit (estimated $40 workspace cap).', flush=True)
    print('Qumus runs in the background: this window may be closed.', flush=True)
    print(f'Closing every Qumus page in the browser stops it ({STOP_WHEN_CLOSED_S} s later; '
          f'a reload does not). Stop {LAUNCHER} stops it at once.', flush=True)
    print(f'Log: {log.relative_to(REPO)}', flush=True)
    open_browser(url, args.no_browser)


if __name__ == '__main__':
    main()
