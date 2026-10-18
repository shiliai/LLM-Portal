from pathlib import Path


WORKER_CONFIG = Path(__file__).with_name("prometheus-gb10-worker.yml")


def test_gb10_worker_scrapes_local_dcgm_only():
    source = WORKER_CONFIG.read_text()
    assert 'job_name: dcgm' in source
    assert '127.0.0.1:9400' in source
    assert 'job_name: llm' not in source
    assert 'LLM_METRICS_PORT' not in source
