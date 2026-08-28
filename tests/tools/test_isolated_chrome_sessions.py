"""Tests for persistent, profile-isolated local Google Chrome sessions."""

from pathlib import Path
import os
import threading

import pytest

from tools import isolated_chrome_sessions as iso


def _cfg(**overrides):
    local = {"mode": "isolated"}
    local.update(overrides)
    return {"local_chrome": local}


def test_isolated_mode_is_explicit_opt_in():
    assert iso.is_enabled({}) is False
    assert iso.is_enabled({"local_chrome": {"mode": "existing"}}) is False
    assert iso.is_enabled(_cfg()) is True


def test_config_defaults_preserve_existing_chrome_attach_mode():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    local = DEFAULT_CONFIG["browser"]["local_chrome"]
    assert local == {
        "mode": "existing",
        "headless": False,
        "profile_root": "",
        "executable_path": "",
        "startup_timeout": 20,
        "idle_timeout": 1800,
    }


def test_layout_is_stable_profile_scoped_and_private(tmp_path):
    home_a = tmp_path / "hermes-a"
    home_b = tmp_path / "hermes-b"
    first = iso.resolve_layout("seller-us", _cfg(), hermes_home=home_a)
    repeat = iso.resolve_layout("seller-us", _cfg(), hermes_home=home_a)
    other_session = iso.resolve_layout("seller-jp", _cfg(), hermes_home=home_a)
    other_profile = iso.resolve_layout("seller-us", _cfg(), hermes_home=home_b)

    assert first == repeat
    assert first.profile_dir == home_a / "browser_profiles" / "chrome" / "seller-us"
    assert first.state_dir == home_a / "browser_sessions" / "chrome" / "seller-us"
    assert first.daemon_name != other_session.daemon_name
    assert first.daemon_name != other_profile.daemon_name
    assert len(first.daemon_name) <= 64


def test_long_session_names_keep_distinct_daemon_identities(tmp_path):
    shared_prefix = "a" * 63
    first = iso.resolve_layout(shared_prefix + "x", _cfg(), hermes_home=tmp_path)
    second = iso.resolve_layout(shared_prefix + "y", _cfg(), hermes_home=tmp_path)
    assert first.profile_dir != second.profile_dir
    assert first.daemon_name != second.daemon_name
    assert len(first.daemon_name) <= 64
    assert len(second.daemon_name) <= 64

    # These two names collide under the old 32-bit (8-hex) digest. Keep the
    # exact regression so truncation cannot silently return to a collision-
    # prone identity.
    collision_a = "a" * 48 + "0000000000001013"
    collision_b = "a" * 48 + "000000000000b183"
    prior_a = iso.resolve_layout(collision_a, _cfg(), hermes_home=tmp_path)
    prior_b = iso.resolve_layout(collision_b, _cfg(), hermes_home=tmp_path)
    assert prior_a.daemon_name != prior_b.daemon_name


def test_macos_daemon_name_fits_browser_harness_socket_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(iso.sys, "platform", "darwin")
    first = iso.resolve_layout("a" * 64, _cfg(), hermes_home=tmp_path)
    second = iso.resolve_layout("a" * 63 + "b", _cfg(), hermes_home=tmp_path)

    assert len(first.daemon_name.encode("utf-8")) <= 38
    assert len(second.daemon_name.encode("utf-8")) <= 38
    assert first.daemon_name != second.daemon_name


def test_session_identity_is_case_canonical_on_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(iso.sys, "platform", "darwin")
    upper = iso.resolve_layout("Seller-US", _cfg(), hermes_home=tmp_path)
    lower = iso.resolve_layout("seller-us", _cfg(), hermes_home=tmp_path)
    assert upper == lower
    assert upper.session_name == "seller-us"


def test_layout_rejects_unsafe_session_name(tmp_path):
    with pytest.raises(ValueError, match="session"):
        iso.resolve_layout("../Default", _cfg(), hermes_home=tmp_path)


def test_layout_rejects_symlink_escape_from_state_root(tmp_path):
    home = tmp_path / "hermes"
    state_root = home / "browser_sessions" / "chrome"
    state_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (state_root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="state root"):
        iso.resolve_layout("escape", _cfg(), hermes_home=home)


def test_custom_profile_cannot_be_chrome_default(tmp_path, monkeypatch):
    real_home = tmp_path / "user"
    default_dir = real_home / "Library" / "Application Support" / "Google" / "Chrome"
    monkeypatch.setattr(iso, "real_user_home", lambda: real_home)
    cfg = _cfg(profile_root=str(default_dir.parent))
    with pytest.raises(ValueError, match="default Chrome data directory"):
        iso.resolve_layout("Chrome", cfg, hermes_home=tmp_path / "hermes")
    nested_cfg = _cfg(profile_root=str(default_dir))
    with pytest.raises(ValueError, match="default Chrome data directory"):
        iso.resolve_layout("automation", nested_cfg, hermes_home=tmp_path / "hermes")


def test_default_profile_guard_rejects_case_only_alias_on_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(iso.sys, "platform", "darwin")
    monkeypatch.setattr(iso, "real_user_home", lambda: Path("/Users/example"))
    cfg = _cfg(profile_root="/users/example/Library/Application Support/Google/Chrome")
    with pytest.raises(ValueError, match="default Chrome data directory"):
        iso.resolve_layout("automation", cfg, hermes_home=tmp_path / "hermes")


def test_launch_command_uses_ephemeral_loopback_cdp_and_custom_profile(tmp_path):
    layout = iso.resolve_layout("work", _cfg(), hermes_home=tmp_path / "hermes")
    command = iso.build_launch_command(
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        layout,
        headless=False,
    )
    assert f"--user-data-dir={layout.profile_dir}" in command
    assert "--remote-debugging-port=0" in command
    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--no-first-run" in command
    assert "--no-default-browser-check" in command
    assert not any(arg.startswith("--profile-directory=") for arg in command)


def test_launch_environment_uses_real_home_for_keychain(monkeypatch, tmp_path):
    real_home = tmp_path / "real-user"
    monkeypatch.setattr(iso, "real_user_home", lambda: real_home)
    env = iso.build_launch_environment({"HOME": "/tmp/hermes-profile", "KEEP": "yes"})
    assert env["HOME"] == str(real_home)
    assert env["KEEP"] == "yes"


def test_launch_retries_transient_chrome_bundle_replacement(monkeypatch, tmp_path):
    layout = iso.resolve_layout("retry", _cfg(), hermes_home=tmp_path / "hermes")
    executable = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    calls = []
    marker = object()

    def fake_launch(path, *args, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise FileNotFoundError(str(path))
        return marker

    monkeypatch.setattr(iso, "_launch_chrome", fake_launch)
    monkeypatch.setattr(iso, "_find_chrome_executable", lambda cfg: executable)
    monkeypatch.setattr(iso.time, "sleep", lambda seconds: None)
    process, resolved = iso._launch_chrome_with_retry(
        executable,
        layout,
        local_cfg={},
        headless=True,
        env={},
    )
    assert process is marker
    assert resolved == executable
    assert calls == [executable, executable]


def test_read_devtools_active_port_requires_loopback_port_file(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    port_file = profile / "DevToolsActivePort"
    port_file.write_text("40123\n/devtools/browser/abc\n")
    assert iso.read_devtools_active_port(profile) == (40123, "/devtools/browser/abc")
    port_file.write_text("not-a-port\n/devtools/browser/abc\n")
    assert iso.read_devtools_active_port(profile) is None


def test_session_lock_uses_os_lock_not_stale_pid_payload(tmp_path, monkeypatch):
    layout = iso.resolve_layout("lock", _cfg(), hermes_home=tmp_path / "hermes")
    layout.state_dir.mkdir(parents=True)
    layout.lock_file.write_text(f"{iso.os.getpid()}-stale-payload\n")
    # The old PID-file lock treats this as held forever. An OS advisory lock
    # must ignore stale bytes and acquire the currently unlocked inode.
    monotonic = iter((0.0, 16.0))
    monkeypatch.setattr(iso.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(iso.time, "sleep", lambda seconds: None)
    with iso._session_lock(layout):
        assert layout.lock_file.exists()
    assert layout.lock_file.stat().st_mode & 0o777 == 0o600


def test_session_lock_serializes_threads(tmp_path):
    layout = iso.resolve_layout("serialize", _cfg(), hermes_home=tmp_path / "hermes")
    first_acquired = threading.Event()
    release_first = threading.Event()
    second_acquired = threading.Event()

    def first():
        with iso._session_lock(layout):
            first_acquired.set()
            assert release_first.wait(2)

    def second():
        assert first_acquired.wait(2)
        with iso._session_lock(layout):
            second_acquired.set()

    one = threading.Thread(target=first)
    two = threading.Thread(target=second)
    one.start()
    two.start()
    assert first_acquired.wait(2)
    assert not second_acquired.wait(0.1)
    release_first.set()
    one.join(2)
    two.join(2)
    assert not one.is_alive() and not two.is_alive()
    assert second_acquired.is_set()


def test_activity_lease_nonblocking_probe_skips_busy_session(tmp_path):
    layout = iso.resolve_layout("active", _cfg(), hermes_home=tmp_path / "hermes")
    with iso.activity_lease(layout) as lease_fd:
        assert isinstance(lease_fd, int)
        with iso.activity_lease(layout, blocking=False) as competing_fd:
            assert competing_fd is None


def test_activity_lease_is_shared_by_canonical_profile_across_hermes_homes(tmp_path):
    profile_root = tmp_path / "shared-profiles"
    cfg = _cfg(profile_root=str(profile_root))
    first_home = tmp_path / "home-a"
    second_home = tmp_path / "home-b"
    first = iso.resolve_layout("shared", cfg, hermes_home=first_home)
    second = iso.resolve_layout("shared", cfg, hermes_home=second_home)
    second.state_dir.mkdir(parents=True)
    iso.write_state(second, {"pid": 1, "last_used_at": 0.0})
    callbacks = []

    assert first.profile_dir == second.profile_dir
    assert first.activity_lock_file == second.activity_lock_file
    with iso.activity_lease(first):
        assert iso.cleanup_idle_session(
            "shared",
            browser_cfg=cfg,
            hermes_home=second_home,
            idle_timeout=1800,
            now=5000.0,
            before_chrome_stop=lambda candidate: callbacks.append(candidate) or True,
        ) is False
    assert callbacks == []


def test_idle_cleanup_stops_only_after_timeout(tmp_path, monkeypatch):
    cfg = _cfg(idle_timeout=1800)
    home = tmp_path / "hermes"
    layout = iso.resolve_layout("idle", cfg, hermes_home=home)
    layout.state_dir.mkdir(parents=True)
    iso.write_state(
        layout,
        {
            "pid": 4321,
            "executable": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "profile_dir": str(layout.profile_dir),
            "last_used_at": 100.0,
        },
    )
    events = []
    commands = iter(("chrome", None))
    monkeypatch.setattr(iso, "_pid_owns_profile", lambda *args: True)
    monkeypatch.setattr(iso, "_process_command", lambda pid: next(commands))
    monkeypatch.setattr(iso.os, "kill", lambda pid, sig: events.append(("chrome", pid)))

    assert iso.cleanup_idle_session(
        "idle",
        browser_cfg=cfg,
        hermes_home=home,
        idle_timeout=1800,
        now=1000.0,
        before_chrome_stop=lambda candidate: events.append(("daemon", candidate.daemon_name)) or True,
    ) is False
    assert events == []

    assert iso.cleanup_idle_session(
        "idle",
        browser_cfg=cfg,
        hermes_home=home,
        idle_timeout=1800,
        now=2000.1,
        before_chrome_stop=lambda candidate: events.append(("daemon", candidate.daemon_name)) or True,
    ) is True
    assert events[0][0] == "daemon"
    assert events[1] == ("chrome", 4321)
    assert not layout.state_file.exists()


def test_idle_cleanup_never_stops_session_with_active_lease(tmp_path):
    cfg = _cfg(idle_timeout=1800)
    home = tmp_path / "hermes"
    layout = iso.resolve_layout("busy", cfg, hermes_home=home)
    layout.state_dir.mkdir(parents=True)
    iso.write_state(layout, {"pid": 1, "last_used_at": 0.0})
    callbacks = []

    with iso.activity_lease(layout):
        assert iso.cleanup_idle_session(
            "busy",
            browser_cfg=cfg,
            hermes_home=home,
            idle_timeout=1800,
            now=5000.0,
            before_chrome_stop=lambda candidate: callbacks.append(candidate) or True,
        ) is False
    assert callbacks == []


def test_session_lock_rejects_symlink_without_touching_target(tmp_path):
    layout = iso.resolve_layout("symlink-lock", _cfg(), hermes_home=tmp_path / "hermes")
    layout.state_dir.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("sentinel")
    os.chmod(victim, 0o644)
    layout.lock_file.symlink_to(victim)
    with pytest.raises((OSError, RuntimeError)):
        with iso._session_lock(layout):
            pass
    assert victim.read_text() == "sentinel"
    assert victim.stat().st_mode & 0o777 == 0o644


def test_ensure_session_reuses_healthy_owned_process(tmp_path, monkeypatch):
    cfg = _cfg()
    layout = iso.resolve_layout("reuse", cfg, hermes_home=tmp_path / "hermes")
    layout.profile_dir.mkdir(parents=True)
    layout.state_dir.mkdir(parents=True)
    (layout.profile_dir / "DevToolsActivePort").write_text("40123\n/devtools/browser/abc\n")
    iso.write_state(layout, {"pid": 1234, "executable": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"})

    monkeypatch.setattr(iso, "_pid_owns_profile", lambda pid, executable, profile: True)
    monkeypatch.setattr(iso, "_endpoint_ready", lambda port, ws_path, timeout=0.5: True)
    monkeypatch.setattr(iso, "_launch_chrome", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must reuse")))

    session = iso.ensure_session("reuse", browser_cfg=cfg, hermes_home=tmp_path / "hermes")
    assert session.pid == 1234
    assert session.cdp_url == "http://127.0.0.1:40123"
    assert session.profile_dir == layout.profile_dir


def test_stop_refuses_pid_without_exact_profile_ownership(tmp_path, monkeypatch):
    cfg = _cfg()
    layout = iso.resolve_layout("owned", cfg, hermes_home=tmp_path / "hermes")
    layout.state_dir.mkdir(parents=True)
    iso.write_state(layout, {"pid": 4321, "executable": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"})
    monkeypatch.setattr(iso, "_pid_owns_profile", lambda *args: False)
    with pytest.raises(RuntimeError, match="ownership"):
        iso.stop_session("owned", browser_cfg=cfg, hermes_home=tmp_path / "hermes")
