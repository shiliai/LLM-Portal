import adapter


def setup_function(_function):
    with adapter.state["lock"]:
        adapter.state["samples"] = []
        adapter.state["drafted"] = 0.0
        adapter.state["accepted"] = 0.0
        adapter.state["kv"] = None
        adapter._gauges.update({
            "out_tps": 0.0,
            "in_tps": 0.0,
            "running_max": 0,
            "waiting_max": 0,
            "has_running": False,
            "has_waiting": False,
        })


def test_parse_and_aggregate_tensorfold_samples_with_labels():
    body = """
# HELP tensorfold_requests_inflight running
tensorfold_requests_inflight{model="a"} 3
tensorfold_requests_inflight{model="b"} 2
tensorfold_prompt_tokens_total{model="a"} 10
tensorfold_prompt_tokens_total{model="b"} 7
"""

    samples = adapter.parse_samples(body)
    assert adapter.aggregate_engine_samples(samples) == {
        "tensorfold_requests_inflight": 5.0,
        "tensorfold_prompt_tokens_total": 17.0,
    }


def test_translate_does_not_duplicate_tensorfold_prefix():
    output = adapter.translate(
        'tensorfold_prompt_tokens_total{model="glm"} 17\n'
        'tensorfold_requests_stalled{model="glm"} 2\n'
    )

    assert 'vllm:prompt_tokens_total{model="glm"} 17' in output
    assert 'vllm:cache_cached_prompt_tokens_total' not in output
    assert 'vllm:num_requests_waiting{model="glm"}' not in output
    assert 'tensorfold_tensorfold_' not in output


def test_translate_exposes_windowed_activity_and_waiting():
    with adapter.state["lock"]:
        adapter._gauges.update({
            "running_max": 7,
            "waiting_max": 2,
            "has_running": True,
            "has_waiting": True,
        })

    output = adapter.translate("# engine is idle between scrapes\n")

    assert "vllm:num_requests_running 7" in output
    assert "vllm:num_requests_waiting 2" in output


def test_translate_falls_back_to_current_scrape_before_sampler_warms_up():
    output = adapter.translate(
        'tensorfold_requests_inflight{model="glm"} 4\n'
        'tensorfold_requests_stalled{model="glm"} 1\n'
    )

    assert "vllm:num_requests_running 4" in output
    assert "vllm:num_requests_waiting 1" in output


def test_translate_normalizes_colon_profile_metrics_and_health_fallbacks():
    output = adapter.translate(
        "# current TensorFold profile\n"
        "tensorfold:requests_running 2\n"
        "tensorfold:requests_waiting 1\n"
        "tensorfold:prompt_tokens_total 1153\n"
        "tensorfold:generation_tokens_total 13900\n"
        "tensorfold:kv_cache_usage_ratio{pool=\"0\"} 0.25\n"
        "tensorfold:mtp_drafted_total 12202\n"
        "tensorfold:mtp_accepted_total 11806\n"
        "tensorfold_health:completion_tokens_total 14494\n"
        "tensorfold_health:requests_total 43\n"
    )

    assert "vllm:num_requests_running 2" in output
    assert "vllm:num_requests_waiting 1" in output
    assert "vllm:prompt_tokens_total 1153" in output
    assert "vllm:generation_tokens_total 13900" in output
    assert "vllm:kv_cache_usage_perc{pool=\"0\"} 0.25" in output
    assert "vllm:spec_decode_num_draft_tokens_total 12202" in output
    assert "vllm:spec_decode_num_accepted_tokens_total 11806" in output
    # Health counters overlap with the primary counters and must not double
    # the stable contract when both are exposed by one profile.
    assert output.count("vllm:generation_tokens_total") == 1
    assert output.count("vllm:request_success_total") == 1


def test_sample_uses_generation_counter_from_colon_profile(monkeypatch):
    bodies = iter([
        "tensorfold:generation_tokens_total 100\n"
        "tensorfold:prompt_tokens_total 40\n"
        "tensorfold:requests_running 1\n",
        "tensorfold:generation_tokens_total 160\n"
        "tensorfold:prompt_tokens_total 70\n"
        "tensorfold:requests_running 1\n",
    ])
    clock = iter([100.0, 101.0])

    class _Response:
        def read(self):
            return next(bodies).encode()

    monkeypatch.setattr(adapter.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response())
    monkeypatch.setattr(adapter.time, "time", lambda: next(clock))
    monkeypatch.setattr(adapter, "tail_log", lambda: None)

    adapter.sample()
    adapter.sample()

    assert adapter.state["samples"][-1][1] == 160
    assert adapter._gauges["out_tps"] == 60
