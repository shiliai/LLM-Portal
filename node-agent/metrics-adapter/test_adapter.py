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
