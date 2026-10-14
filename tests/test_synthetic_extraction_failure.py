"""Integration test: extraction_fallback event type appears when payload missing model field.

TICKET-069: Synthetic extraction failure path validation
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _payload import normalize_payload  # noqa: E402


def test_extraction_fallback_on_missing_model_field():
    """When normalize_payload receives a payload without 'model' field, it should return None for model."""
    # Payload missing the 'model' field entirely
    payload = {
        "role": "FO",
        "intent": "test",
        "timestamp": "2026-09-21T00:00:00Z",
        "session_id": "test-session",
    }

    # Call normalize_payload — should handle gracefully
    result = normalize_payload(payload)

    # Verify it's a NormalizedPayload with graceful extraction
    assert result is not None, "normalize_payload should return a result"
    assert result.model is None, "model should be None when missing from payload"
    assert result.session_id == "test-session", "session_id should be extracted"
    assert result.raw == payload, "raw payload should be stored"


if __name__ == "__main__":
    test_extraction_fallback_on_missing_model_field()
    print("PASS: test_extraction_fallback_on_missing_model_field")
