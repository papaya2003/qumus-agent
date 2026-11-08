import sys
import os
import threading
import time

current_dir = os.path.dirname(os.path.abspath(__file__))
repo_root = os.path.abspath(os.path.join(current_dir, os.pardir, os.pardir, os.pardir))
seg_dir = os.path.abspath(os.path.join(current_dir, os.pardir, "segmenter"))
if not os.path.isdir(seg_dir):
    # Fallback for older layouts that placed segmenter at repo root.
    seg_dir = os.path.join(repo_root, "segmenter")
if not os.path.isdir(seg_dir):
    raise ImportError(f"segmenter folder not found at {seg_dir}. Expected it under agents/processing_agent or the repo root.")
sys.path.append(seg_dir)

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from livesegment import LiveSegment

_active_observers = {}
_active_handlers = {}


class NewFileHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self._condition = threading.Condition()
        self._file_states = {}
        self._active_count = 0

    def _placeholder(fname):
        print(f"New file detected: {fname}")

    def set_run_method(self, method=_placeholder):
        self.method = method

    def on_created(self, event):
        if event.is_directory:
            return
        filename = os.path.basename(event.src_path)
        with self._condition:
            self._active_count += 1
            self._file_states[filename] = {"state": "processing", "error": None}
            self._condition.notify_all()
        try:
            time.sleep(0.1)
            self.method(filename)
        except Exception as exc:
            with self._condition:
                self._file_states[filename] = {"state": "error", "error": str(exc)}
                self._condition.notify_all()
        else:
            with self._condition:
                self._file_states[filename] = {"state": "complete", "error": None}
                self._condition.notify_all()
        finally:
            with self._condition:
                self._active_count -= 1
                self._condition.notify_all()

    def wait_for_file(self, filename: str, timeout: float = 180.0) -> None:
        deadline = time.monotonic() + float(timeout)
        with self._condition:
            while True:
                state = self._file_states.get(os.path.basename(filename))
                if state and state["state"] == "complete":
                    return
                if state and state["state"] == "error":
                    raise RuntimeError(
                        f"Segmentation failed for {filename}: {state['error']}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Timed out waiting for segmentation of {filename}")
                self._condition.wait(timeout=remaining)

    def wait_until_idle(self, timeout: float = 180.0) -> None:
        deadline = time.monotonic() + float(timeout)
        with self._condition:
            while self._active_count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for active segmentation task")
                self._condition.wait(timeout=remaining)


def _build_handler(
    mag: int,
    photo_dir: str,
    result_dir: str,
    result_txt: str,
    grow: float = 0.5,
    material_type: str = "Gr",
) -> NewFileHandler:
    if mag not in (10, 20, 100):
        raise ValueError("Invalid Magnification Given; expected 10, 20, or 100.")

    event_handler = NewFileHandler()
    event_handler.set_run_method(
        LiveSegment(
            photo_dir,
            result_dir,
            result_txt,
            mag,
            focus_disks=[],
            grow=grow,
            material_type=material_type,
        )
    )
    return event_handler


def start_observer(
    mag: int,
    photo_dir: str,
    result_dir: str,
    result_txt: str,
    grow: float = 0.5,
    material_type: str = "Gr",
    key: str = "default",
) -> None:
    global _active_observers, _active_handlers
    if key in _active_observers:
        stop_observer(key)

    event_handler = _build_handler(
        mag,
        photo_dir,
        result_dir,
        result_txt,
        grow=grow,
        material_type=material_type,
    )
    observer = Observer()
    observer.schedule(event_handler, path=photo_dir, recursive=False)
    observer.start()
    _active_handlers[key] = event_handler
    _active_observers[key] = observer


def wait_for_observer_file(key: str, filename: str, timeout: float = 180.0) -> None:
    handler = _active_handlers.get(key)
    if handler is None:
        raise RuntimeError(f"Segmentation observer {key!r} is not running")
    handler.wait_for_file(filename, timeout=timeout)


def wait_for_observer_files(
    key: str,
    filenames,
    timeout: float = 300.0,
    continue_on_error: bool = False,
):
    failures = []
    deadline = time.monotonic() + float(timeout)
    for filename in filenames:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for segmentation observer {key!r}")
        try:
            wait_for_observer_file(key, filename, timeout=remaining)
        except Exception as exc:
            if not continue_on_error:
                raise
            failures.append(
                {
                    "filename": os.path.basename(filename),
                    "error": str(exc),
                }
            )
    return failures


def stop_observer(key: str = "default", drain_timeout: float = 180.0) -> None:
    global _active_observers, _active_handlers
    keys = list(_active_observers.keys()) if key == "__all__" else [key]
    for obs_key in keys:
        observer = _active_observers.get(obs_key)
        if observer is None:
            continue
        handler = _active_handlers.get(obs_key)
        try:
            if handler is not None:
                handler.wait_until_idle(timeout=drain_timeout)
        finally:
            observer.stop()
            observer.join()
            _active_observers.pop(obs_key, None)
            _active_handlers.pop(obs_key, None)


if __name__ == "__main__":
    # Example: python agents/processing_agent/tools/observer.py 100 C:/Users/Qumus_PC/Desktop/2d_World/hardware/photo_dir/m_100 C:/Users/Qumus_PC/Desktop/2d_World/hardware/photo_dir/segmented_m_100 C:/Users/Qumus_PC/Desktop/2d_World/hardware/results_0.1_0.1_100.txt
    import sys

    mag = int(sys.argv[1])
    grow = 0.5
    # rad = int(430*grow)
    # f1 = focus_disk(np.zeros((755 * grow, 1350 * grow)), rad, invert=True)
    # f2 = focus_disk(np.zeros((755 * grow, 1350 * grow)), rad - 10, invert=True)
    photo_dir = sys.argv[2]
    result_dir = sys.argv[3]
    result_txt = sys.argv[4]
    start_observer(mag=mag, photo_dir=photo_dir, result_dir=result_dir, result_txt=result_txt, grow=grow)
    print(f"Watching {photo_dir} for new files...")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_observer()
