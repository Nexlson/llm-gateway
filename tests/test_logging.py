import json
import logging

from app.core.logging import configure_logging, log_request


def test_log_request_emits_single_json_line(capsys):
    configure_logging()
    log_request({
        "request_id": "r1", "pool": "cheap", "model": "deepseek-chat",
        "route_stage": "passthrough", "input_tokens": 10, "output_tokens": 5,
        "cost_usd": 0.0, "latency_ms": 42, "status": 200, "fallback_hops": 0,
    })
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    payload = json.loads(out[0])
    assert payload["request_id"] == "r1"
    assert payload["event"] == "request"
    assert payload["status"] == 200
    # No bodies leaked
    assert "messages" not in payload


def test_formatter_serializes_standard_log(capsys):
    configure_logging()
    logging.getLogger("gateway").info("hello")
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["message"] == "hello"
