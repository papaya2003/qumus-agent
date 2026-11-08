"""The single Qumus entry point: live work and History (recordings) in one app.

From anywhere: .venv/bin/python scripts/start_qumus.py [--status | --stop | --restart]
`Start Qumus.command` / `Stop Qumus.command` (in ~/Projects/Qumus and in v2)
invoke this script.

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
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

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
    """Why the existing workspace cannot open with this code and target, or None."""
    if not database.exists():
        return None
    from qumus2.cli import _serve_config
    from qumus2.testbench.bench_geometry import PROFILE, WORKSPACE_META

    with sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True) as connection:
        row = connection.execute('SELECT value FROM meta WHERE key = ?', (WORKSPACE_META,)).fetchone()
        occupied = any(connection.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
                       for table in ('sample', 'run', 'project'))
    # The same rule as bench_geometry.admit_workspace_geometry: an occupied
    # workspace opens only as an exact checkpoint of this geometry.
    if row is not None and row[0] != PROFILE:
        return f'it was made for bench geometry {row[0]}, this code uses {PROFILE}'
    if occupied and row is None:
        return 'it predates the recorded bench geometry'
    if occupied and not database.with_suffix('.testbench.pkl').exists():
        return 'its saved laboratory state is missing'
    try:
        _serve_config(argparse.Namespace(db=str(database), profile='hallbar-linked', reactor=False,
                                         target_bundle=str(bundle)), prepare=False)
    except ValueError as error:
        return str(error)
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


def try_lock(*, create: bool = True) -> int | None:
    """An fd holding the exclusive workspace lock, or None while a server holds it."""
    path = REPO / LOCK
    if not create and not path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def read_state() -> dict:
    try:
        state = json.loads((REPO / LOCK).read_text() or '{}')
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def write_state(fd: int, state: dict) -> None:
    data = json.dumps(state, indent=2).encode()
    os.ftruncate(fd, 0)
    os.pwrite(fd, data, 0)


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
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, port))
        except OSError:
            return False
    return True


def alive(pid: int) -> bool:
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
                          starting=state.get('phase') == 'starting')
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


def stop_pid(pid: int, timeout: float = STOP_TIMEOUT_S) -> bool:
    """SIGINT (what Control+C sends: agents pause at a safe point, the lab
    saves its state), then SIGTERM if it does not end in time."""
    for sig, wait in ((signal.SIGINT, timeout), (signal.SIGTERM, 10.0)):
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
    if not stop_pid(server.pid):
        print(f'Qumus (pid {server.pid}) did not stop. Force it with: kill -9 {server.pid}')
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
            print('Kept it. Stop it with Stop Qumus.command (or --stop) and start again.')
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
        print(''.join(log.read_text(errors='replace').splitlines(keepends=True)[-30:]))
    if process.poll() is None:
        stop_pid(process.pid, 10.0)
    return False


def start_background(command: list[str], fd: int, state: dict, port: int = PORT) -> Path | None:
    """Start the server detached from this terminal; it inherits the lock.
    Returns its log once it answers, or None (it is stopped again)."""
    log = REPO / LOGS / f'qumus-{_stamp()}.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    write_state(fd, state | {'log': str(log)})
    with log.open('ab') as output:
        process = subprocess.Popen(command, cwd=REPO, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(fd,))
    write_state(fd, state | {'pid': process.pid, 'log': str(log)})
    if not wait_until_up(process, port, log):
        return None
    write_state(fd, state | {'pid': process.pid, 'log': str(log), 'phase': 'running'})
    os.close(fd)   # the server's copy keeps the lock
    return log


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
    command = [sys.executable, '-m', 'qumus2', 'serve', '--openai', '--lab-process',
               '--db', DATABASE, '--budget', '40', '--host', HOST, '--port', str(PORT),
               '--profile', 'hallbar-linked', '--target-bundle', str(bundles[0]),
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
          'a reload does not). Stop Qumus.command stops it at once.', flush=True)
    print(f'Log: {log.relative_to(REPO)}', flush=True)
    open_browser(url, args.no_browser)


if __name__ == '__main__':
    main()
