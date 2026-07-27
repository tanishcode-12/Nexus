from __future__ import annotations

import pytest

from errors import http_status_for


@pytest.mark.parametrize(
    "error_type,expected_status",
    [
        ("unknown_tool", 404),
        ("auth_error", 401),
        ("forbidden_scope", 403),
        ("rate_limited", 429),
        ("bad_arguments", 400),
        ("upstream_unavailable", 502),
        ("internal_error", 500),
        ("some_future_error_type_we_didnt_anticipate", 500),  # safe default
    ],
)
def test_error_status_mapping(error_type, expected_status):
    result = {"error": True, "error_type": error_type, "message": "x"}
    assert http_status_for(result) == expected_status


def test_success_result_maps_to_200():
    assert http_status_for({"error": False, "text": "ok"}) == 200
