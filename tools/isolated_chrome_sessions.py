"""Persistent, profile-isolated local Chrome sessions for browser_exec.

This module deliberately never attaches to the user's normal Chrome data
folder.  Each stable Hermes browser session name owns a distinct Google Chrome
``--user-data-dir`` and an ephemeral loopback CDP endpoint.  The profile stays
on disk across Chrome/Hermes restarts, so cookies and logins persist while two
session names can hold different logins to the same site.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Iterator, Mapping, Optional
from urllib.request import Request, urlopen

import psutil

from hermes_constants import get_hermes_home


_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DEFAULT_SESSION = "default"
_DEFAULT_STARTUP_TIMEOUT = 20.0


@dataclass(frozen=True)
class SessionLayout:
    session_name: str
    daemon_name: str
    profile_dir: Path
    state_dir: Path
    state_file: Path
    log_file: Path
    lock_file: Path


@dataclass(frozen=True)
class IsolatedChromeSession:
    session_name: str
    daemon_name: str
    profile_dir: Path
    cdp_url: str
    pid: int


def _local_config(browser_cfg: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    raw = (browser_cfg or {}).get("local_chrome") or {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def is_enabled(browser_cfg: Optional[Mapping[str, Any]]) -> bool:
    """Whether local Browser Use calls should get isolated Chrome profiles."""
    mode = str(_local_config(browser_cfg).get("mode") or "existing").strip().lower()
    return mode == "isolated"


def real_user_home() -> Path:
    """Return the OS account home, ignoring a Hermes profile's HOME override."""
    if os.name == "posix":
        try:
            import pwd

            return Path(pwd.getpwuid(os.getuid()).pw_dir).expanduser().resolve()
        except (KeyError, OSError):
            pass
    candidate = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if not candidate:
        raise RuntimeError("cannot resolve the real OS user home for Chrome/Keychain")
    return Path(candidate).expanduser().resolve()


def _resolve_root(value: Any, *, hermes_home: Path, default: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        return default.resolve()
    expanded = Path(os.path.expandvars(value.strip())).expanduser()
    if not expanded.is_absolute():
        expanded = hermes_home / expanded
    return expanded.resolve()


def _known_default_data_dirs(user_home: Path) -> set[Path]:
    candidates = {
        user_home / "Library" / "Application Support" / "Google" / "Chrome",
        user_home / "Library" / "Application Support" / "Google" / "Chrome Canary",
        user_home / "Library" / "Application Support" / "Microsoft Edge",
        user_home / ".config" / "google-chrome",
        user_home / ".config" / "chromium",
        user_home / ".config" / "microsoft-edge",
    }
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        base = Path(local_app)
        candidates.update(
            {
                base / "Google" / "Chrome" / "User Data",
                base / "Microsoft" / "Edge" / "User Data",
            }
        )
    return {path.expanduser().resolve() for path in candidates}


def _security_path_parts(path: Path) -> tuple[str, ...]:
    parts = path.parts
    if sys.platform == "darwin" or os.name == "nt":
        # Default APFS/NTFS volumes are case-insensitive while Path equality
        # remains case-sensitive. Reject case-only aliases conservatively;
        # this may over-reject on a case-sensitive macOS volume, but it can
        # never expose the personal Chrome data directory.
        return tuple(part.casefold() for part in parts)
    return tuple(parts)


def _same_or_nested_path(left: Path, right: Path) -> bool:
    left_parts = _security_path_parts(left)
    right_parts = _security_path_parts(right)
    return (
        left_parts == right_parts
        or left_parts[: len(right_parts)] == right_parts
        or right_parts[: len(left_parts)] == left_parts
    )


def resolve_layout(
    session_name: str = "",
    browser_cfg: Optional[Mapping[str, Any]] = None,
    *,
    hermes_home: Optional[Path] = None,
) -> SessionLayout:
    requested_name = session_name or _DEFAULT_SESSION
    if not _SESSION_RE.fullmatch(requested_name):
        raise ValueError("isolated Chrome session name must be 1-64 letters, digits, dashes, or underscores")
    # Default APFS/NTFS volumes are case-insensitive. Canonicalize the identity
    # before deriving profile/state/daemon paths so `Work` and `work` cannot
    # create two controllers over one physical profile directory.
    name = requested_name.casefold() if sys.platform == "darwin" or os.name == "nt" else requested_name

    home = Path(hermes_home or get_hermes_home()).expanduser().resolve()
    local_cfg = _local_config(browser_cfg)
    profile_root = _resolve_root(
        local_cfg.get("profile_root"),
        hermes_home=home,
        default=home / "browser_profiles" / "chrome",
    )
    state_root = home / "browser_sessions" / "chrome"
    profile_dir = (profile_root / name).resolve()
    default_dirs = _known_default_data_dirs(real_user_home())
    if any(_same_or_nested_path(profile_dir, default_dir) for default_dir in default_dirs):
        raise ValueError(
            "isolated Chrome profile resolves to the default Chrome data directory; "
            "choose a dedicated browser.local_chrome.profile_root"
        )
    try:
        profile_dir.relative_to(profile_root)
    except ValueError as exc:  # defensive; the name regex already prevents this
        raise ValueError("isolated Chrome session escapes its profile root") from exc

    state_root = state_root.resolve()
    state_dir = (state_root / name).resolve()
    try:
        state_dir.relative_to(state_root)
    except ValueError as exc:
        raise ValueError("isolated Chrome session escapes its state root") from exc
    namespace = hashlib.sha256(str(home).encode("utf-8")).hexdigest()[:10]
    suffix_budget = 64 - len("hiso--") - len(namespace)
    if len(name) <= suffix_budget:
        daemon_suffix = name
    else:
        # Browser Harness caps BU_NAME at 64 chars. Keep a readable prefix but
        # bind the truncated suffix to the complete session identity so two
        # names that differ only near their end never share one daemon.
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(name.encode("utf-8")).digest()
        ).decode("ascii").rstrip("=")
        daemon_suffix = f"{name[: suffix_budget - len(digest) - 1]}-{digest}"
    daemon_name = f"hiso-{namespace}-{daemon_suffix}"
    return SessionLayout(
        session_name=name,
        daemon_name=daemon_name,
        profile_dir=profile_dir,
        state_dir=state_dir,
        state_file=state_dir / "state.json",
        log_file=state_dir / "chrome.log",
        lock_file=state_dir / "session.lock",
    )


def build_launch_command(executable: Path, layout: SessionLayout, *, headless: bool) -> list[str]:
    command = [
        str(executable),
        f"--user-data-dir={layout.profile_dir}",
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
    ]
    if headless:
        command.append("--headless=new")
    command.append("about:blank")
    return command


def build_launch_environment(base_env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    env = dict(base_env if base_env is not None else os.environ)
    env["HOME"] = str(real_user_home())
    return env


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"isolated Chrome path is not a real directory: {path}")
    os.chmod(path, 0o700)


def _open_regular_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open one owned state/log file without following a symlink."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        try:
            before = os.lstat(path)
            if stat.S_ISLNK(before.st_mode):
                raise RuntimeError(f"refusing symlinked isolated Chrome file: {path}")
        except FileNotFoundError:
            pass
    fd = os.open(path, flags | nofollow, mode)
    try:
        opened = os.fstat(fd)
        current = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
        ):
            raise RuntimeError(f"isolated Chrome file identity changed during open: {path}")
        return fd
    except Exception:
        os.close(fd)
        raise


def _set_open_file_mode(fd: int, path: Path, mode: int = 0o600) -> None:
    if hasattr(os, "fchmod"):
        os.fchmod(fd, mode)
    else:  # pragma: no cover - Windows lacks fchmod
        os.chmod(path, mode)


def read_devtools_active_port(profile_dir: Path) -> Optional[tuple[int, str]]:
    try:
        fd = _open_regular_nofollow(profile_dir / "DevToolsActivePort", os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        if len(lines) < 2:
            return None
        port = int(lines[0].strip())
        ws_path = lines[1].strip()
        if not (1 <= port <= 65535) or not ws_path.startswith("/devtools/browser/"):
            return None
        return port, ws_path
    except (OSError, ValueError):
        return None


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_private_directory(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = _open_regular_nofollow(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def write_state(layout: SessionLayout, payload: Mapping[str, Any]) -> None:
    _atomic_write_json(layout.state_file, payload)


def _read_state(layout: SessionLayout) -> dict[str, Any]:
    try:
        fd = _open_regular_nofollow(layout.state_file, os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid isolated Chrome state file: {layout.state_file}") from exc


@contextmanager
def _session_lock(layout: SessionLayout) -> Iterator[None]:
    """Cross-process OS lock; file bytes are never treated as ownership."""
    _ensure_private_directory(layout.state_dir)
    lock_fd = _open_regular_nofollow(layout.lock_file, os.O_RDWR | os.O_CREAT)
    _set_open_file_mode(lock_fd, layout.lock_file)
    with os.fdopen(lock_fd, "a+b") as handle:
        deadline = time.monotonic() + 15.0
        if os.name == "posix":
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("timed out waiting for isolated Chrome session lock") from None
                    time.sleep(0.05)

            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return

        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("timed out waiting for isolated Chrome session lock") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        raise RuntimeError(f"isolated Chrome session locks are unsupported on {os.name}")


def _find_chrome_executable(local_cfg: Mapping[str, Any]) -> Path:
    configured = local_cfg.get("executable_path")
    if isinstance(configured, str) and configured.strip():
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"configured Google Chrome executable does not exist: {path}")
        return path

    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates.extend(
            [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                real_user_home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
            ]
        )
    elif os.name == "nt":
        for base in filter(None, (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA"))):
            candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise RuntimeError(
        "Google Chrome/Chromium was not found; set browser.local_chrome.executable_path"
    )


def _process_command(pid: int) -> Optional[str]:
    if pid <= 0:
        return None
    try:
        return " ".join(psutil.Process(pid).cmdline())
    except (psutil.Error, OSError):
        return None


def _pid_owns_profile(pid: int, executable: Path, profile_dir: Path) -> bool:
    try:
        process = psutil.Process(pid)
        cmdline = process.cmdline()
        process_exe = process.exe() or (cmdline[0] if cmdline else "")
    except (psutil.Error, OSError):
        return False
    return str(process_exe) == str(executable) and f"--user-data-dir={profile_dir}" in cmdline


def _find_owned_profile_pid(executable: Path, profile_dir: Path) -> Optional[int]:
    needle_exe = str(executable)
    needle_profile = f"--user-data-dir={profile_dir}"
    for process in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            cmdline = process.info.get("cmdline") or []
            process_exe = process.info.get("exe") or (cmdline[0] if cmdline else "")
            if str(process_exe) == needle_exe and needle_profile in cmdline:
                return int(process.info["pid"])
        except (psutil.Error, OSError, ValueError, TypeError):
            continue
    return None


def _endpoint_ready(port: int, ws_path: str, timeout: float = 0.5) -> bool:
    try:
        request = Request(f"http://127.0.0.1:{port}/json/version", headers={"Connection": "close"})
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(64 * 1024).decode("utf-8"))
        endpoint = str(payload.get("webSocketDebuggerUrl") or "")
        return endpoint.endswith(ws_path) and endpoint.startswith(("ws://127.0.0.1:", "ws://localhost:"))
    except Exception:
        return False


def _launch_chrome(
    executable: Path,
    layout: SessionLayout,
    *,
    headless: bool,
    env: Mapping[str, str],
) -> subprocess.Popen:
    _ensure_private_directory(layout.profile_dir)
    _ensure_private_directory(layout.state_dir)
    log_fd = _open_regular_nofollow(
        layout.log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND
    )
    _set_open_file_mode(log_fd, layout.log_file)
    log_handle = os.fdopen(log_fd, "ab", buffering=0)
    try:
        return subprocess.Popen(
            build_launch_command(executable, layout, headless=headless),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            env=dict(env),
            start_new_session=True,
        )
    finally:
        log_handle.close()


def _launch_chrome_with_retry(
    executable: Path,
    layout: SessionLayout,
    *,
    local_cfg: Mapping[str, Any],
    headless: bool,
    env: Mapping[str, str],
) -> tuple[subprocess.Popen, Path]:
    """Survive Chrome's updater atomically replacing the app bundle mid-launch."""
    resolved = executable
    for attempt in range(3):
        try:
            return (
                _launch_chrome(resolved, layout, headless=headless, env=env),
                resolved,
            )
        except FileNotFoundError:
            if attempt == 2:
                raise
            time.sleep(0.2)
            resolved = _find_chrome_executable(local_cfg)
    raise AssertionError("unreachable")


def _session_result(layout: SessionLayout, pid: int, port: int) -> IsolatedChromeSession:
    return IsolatedChromeSession(
        session_name=layout.session_name,
        daemon_name=layout.daemon_name,
        profile_dir=layout.profile_dir,
        cdp_url=f"http://127.0.0.1:{port}",
        pid=pid,
    )


def ensure_session(
    session_name: str = "",
    *,
    browser_cfg: Optional[Mapping[str, Any]] = None,
    hermes_home: Optional[Path] = None,
) -> IsolatedChromeSession:
    if not is_enabled(browser_cfg):
        raise RuntimeError("isolated local Chrome mode is not enabled")
    local_cfg = _local_config(browser_cfg)
    layout = resolve_layout(session_name, browser_cfg, hermes_home=hermes_home)
    executable = _find_chrome_executable(local_cfg)
    headless = bool(local_cfg.get("headless", False))
    try:
        startup_timeout = float(local_cfg.get("startup_timeout", _DEFAULT_STARTUP_TIMEOUT))
    except (TypeError, ValueError):
        startup_timeout = _DEFAULT_STARTUP_TIMEOUT
    startup_timeout = max(2.0, min(startup_timeout, 60.0))

    _ensure_private_directory(layout.profile_dir)
    _ensure_private_directory(layout.state_dir)
    with _session_lock(layout):
        state = _read_state(layout)
        port_info = read_devtools_active_port(layout.profile_dir)
        state_pid = state.get("pid")
        try:
            pid = int(str(state_pid))
        except (TypeError, ValueError):
            pid = 0

        if (
            pid
            and port_info
            and _pid_owns_profile(pid, executable, layout.profile_dir)
            and _endpoint_ready(*port_info)
        ):
            write_state(
                layout,
                {
                    **state,
                    "pid": pid,
                    "executable": str(executable),
                    "profile_dir": str(layout.profile_dir),
                    "last_used_at": time.time(),
                },
            )
            return _session_result(layout, pid, port_info[0])

        discovered_pid = _find_owned_profile_pid(executable, layout.profile_dir)
        if discovered_pid and port_info and _endpoint_ready(*port_info):
            write_state(
                layout,
                {
                    "pid": discovered_pid,
                    "executable": str(executable),
                    "profile_dir": str(layout.profile_dir),
                    "port": port_info[0],
                    "ws_path": port_info[1],
                    "started_at": state.get("started_at") or time.time(),
                    "last_used_at": time.time(),
                },
            )
            return _session_result(layout, discovered_pid, port_info[0])
        if discovered_pid:
            raise RuntimeError(
                f"owned Chrome process {discovered_pid} exists for {layout.profile_dir} "
                "but its loopback CDP endpoint is not healthy; stop that isolated session before retrying"
            )

        try:
            (layout.profile_dir / "DevToolsActivePort").unlink()
        except FileNotFoundError:
            pass
        process, executable = _launch_chrome_with_retry(
            executable,
            layout,
            local_cfg=local_cfg,
            headless=headless,
            env=build_launch_environment(),
        )
        deadline = time.monotonic() + startup_timeout
        last_port: Optional[tuple[int, str]] = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"isolated Google Chrome exited with code {process.returncode}; see {layout.log_file}"
                )
            last_port = read_devtools_active_port(layout.profile_dir)
            if last_port and _endpoint_ready(*last_port):
                write_state(
                    layout,
                    {
                        "pid": process.pid,
                        "executable": str(executable),
                        "profile_dir": str(layout.profile_dir),
                        "port": last_port[0],
                        "ws_path": last_port[1],
                        "started_at": time.time(),
                        "last_used_at": time.time(),
                    },
                )
                return _session_result(layout, process.pid, last_port[0])
            time.sleep(0.1)

        if _pid_owns_profile(process.pid, executable, layout.profile_dir):
            os.kill(process.pid, signal.SIGTERM)
        raise RuntimeError(
            f"isolated Google Chrome did not publish a healthy DevToolsActivePort within "
            f"{startup_timeout:.0f}s; see {layout.log_file}"
        )


def stop_session(
    session_name: str = "",
    *,
    browser_cfg: Optional[Mapping[str, Any]] = None,
    hermes_home: Optional[Path] = None,
    timeout: float = 10.0,
) -> bool:
    layout = resolve_layout(session_name, browser_cfg, hermes_home=hermes_home)
    with _session_lock(layout):
        state = _read_state(layout)
        if not state:
            return False
        try:
            pid = int(str(state.get("pid")))
        except (TypeError, ValueError):
            raise RuntimeError("isolated Chrome state has no valid pid") from None
        executable = Path(str(state.get("executable") or "")).expanduser()
        if not _pid_owns_profile(pid, executable, layout.profile_dir):
            raise RuntimeError("isolated Chrome process ownership could not be proven; refusing to stop it")
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + max(0.5, timeout)
        while time.monotonic() < deadline:
            if _process_command(pid) is None:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"isolated Chrome process {pid} did not exit after SIGTERM")
        try:
            layout.state_file.unlink()
        except FileNotFoundError:
            pass
        try:
            (layout.profile_dir / "DevToolsActivePort").unlink()
        except FileNotFoundError:
            pass
        return True
