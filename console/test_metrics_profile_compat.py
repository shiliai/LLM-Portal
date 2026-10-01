from pathlib import Path

from testutil import load_service


def _load_console(tmp_path: Path):
    return load_service(Path(__file__).parent / "console.py", {
        "CONSOLE_DATA": str(tmp_path / "console"),
        "LITELLM_MASTER_KEY": "unit-master-profile-compat",
        "ONBOARD_ADMIN_TOKEN": "onboard-profile-compat",
        "LITELLM_BASE": "http://litellm-profile-compat.invalid",
        "MCP_VISION_CONF": str(tmp_path / "vision.json"),
        "MCP_VISION_MODEL": "",
        "SESSION_COOKIE_SECURE": "false",
    })


def test_finish_metrics_accepts_colon_tensorfold_profile_metrics(tmp_path):
    console = _load_console(tmp_path)
    out = console._finish_metrics({
        "tensorfold:requests_running": 2,
        "tensorfold:requests_waiting": 1,
        "tensorfold:kv_cache_usage_ratio": 0.25,
        "tensorfold:mtp_drafted_total": 100,
        "tensorfold:mtp_accepted_total": 75,
    })
    assert out["requests_running"] == 2
    assert out["requests_waiting"] == 1
    assert out["kv_cache_pct"] == 25.0
    assert out["spec_accept_pct"] == 75.0
    assert out["runtime"] == "tensorfold"


def test_finish_metrics_deduplicates_raw_tensorfold_activity_with_adapter_contract(tmp_path):
    console = _load_console(tmp_path)
    out = console._finish_metrics({
        "vllm:num_requests_running": 2,
        "vllm:num_requests_waiting": 1,
        "tensorfold:requests_running": 2,
        "tensorfold:requests_waiting": 1,
    })
    assert out["requests_running"] == 2
    assert out["requests_waiting"] == 1


def test_vm_matcher_includes_raw_tensorfold_namespace(tmp_path):
    console = _load_console(tmp_path)
    matcher = console.vm_site_matcher("gb10")
    assert "tensorfold(_health)?" in matcher
