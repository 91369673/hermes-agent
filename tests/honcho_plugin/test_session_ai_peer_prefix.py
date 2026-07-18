"""Regression tests for AI-peer-scoped Honcho sessions."""

import json

from plugins.memory.honcho.client import HonchoClientConfig


def _config(tmp_path, payload, *, host="hermes"):
    path = tmp_path / "honcho.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return HonchoClientConfig.from_global_config(host=host, config_path=path)


def test_root_session_ai_peer_prefix_applies_to_gateway_key(tmp_path):
    cfg = _config(
        tmp_path,
        {
            "baseUrl": "http://127.0.0.1:18000",
            "aiPeer": "carmen",
            "sessionAiPeerPrefix": True,
        },
    )

    assert cfg.session_ai_peer_prefix is True
    assert (
        cfg.resolve_session_name(gateway_session_key="agent:main:telegram:dm:8640859403")
        == "carmen-agent-main-telegram-dm-8640859403"
    )


def test_host_false_overrides_root_true(tmp_path):
    cfg = _config(
        tmp_path,
        {
            "baseUrl": "http://127.0.0.1:18000",
            "aiPeer": "root-ai",
            "sessionAiPeerPrefix": True,
            "hosts": {
                "hermes.jeff": {
                    "aiPeer": "jeff",
                    "sessionAiPeerPrefix": False,
                }
            },
        },
        host="hermes.jeff",
    )

    assert cfg.session_ai_peer_prefix is False
    assert cfg.resolve_session_name(gateway_session_key="agent:main:telegram:dm:1") == "agent-main-telegram-dm-1"


def test_ai_peer_prefix_is_sanitized_and_applies_to_non_gateway_paths(tmp_path):
    cfg = _config(
        tmp_path,
        {
            "baseUrl": "http://127.0.0.1:18000",
            "aiPeer": "Jeff / Planner",
            "sessionAiPeerPrefix": True,
            "sessionStrategy": "global",
            "workspace": "shared-workspace",
        },
    )

    assert cfg.resolve_session_name() == "Jeff-Planner-shared-workspace"


def test_ai_peer_prefix_respects_honcho_session_id_limit(tmp_path):
    cfg = _config(
        tmp_path,
        {
            "baseUrl": "http://127.0.0.1:18000",
            "aiPeer": "carmen",
            "sessionAiPeerPrefix": True,
        },
    )
    key = "telegram:" + "x" * 180

    first = cfg.resolve_session_name(gateway_session_key=key)
    second = cfg.resolve_session_name(gateway_session_key=key)

    assert first is not None
    assert first == second
    assert first.startswith("carmen-")
    assert len(first) <= 100


def test_gateway_cache_signature_changes_for_ai_prefix_and_context_budget(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    path = tmp_path / "honcho.json"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path.write_text(
        json.dumps(
            {
                "baseUrl": "http://127.0.0.1:18000",
                "aiPeer": "carmen",
                "sessionAiPeerPrefix": False,
                "contextTokens": 2000,
            }
        ),
        encoding="utf-8",
    )
    before = GatewayRunner._extract_cache_busting_config({"memory": {"provider": "honcho"}})

    path.write_text(
        json.dumps(
            {
                "baseUrl": "http://127.0.0.1:18000",
                "aiPeer": "carmen",
                "sessionAiPeerPrefix": True,
                "contextTokens": 4000,
            }
        ),
        encoding="utf-8",
    )
    after = GatewayRunner._extract_cache_busting_config({"memory": {"provider": "honcho"}})

    assert before["honcho.session_ai_peer_prefix"] is False
    assert after["honcho.session_ai_peer_prefix"] is True
    assert before["honcho.context_tokens"] == 2000
    assert after["honcho.context_tokens"] == 4000
