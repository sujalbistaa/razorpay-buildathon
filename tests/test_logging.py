import json

from vasool.logging import configure_logging, get_logger


def test_configure_logging_emits_json(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging()
    get_logger().info("attempt_scheduled", invoice_id="inv_1", attempt_index=0)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["event"] == "attempt_scheduled"
    assert payload["invoice_id"] == "inv_1"
    assert payload["attempt_index"] == 0
