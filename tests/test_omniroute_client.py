from __future__ import annotations

import pytest

from omniroute_client import OmniRouteClient, OmniRouteError


@pytest.mark.asyncio
async def test_list_models_real_socket(fake_omniroute):
    client = OmniRouteClient(base_url=fake_omniroute.base_url, api_key="k")
    models = await client.list_models()
    assert "gpt-4o-mini" in models
    await client.aclose()


@pytest.mark.asyncio
async def test_complete_happy_path(omniroute_client):
    result = await omniroute_client.complete(prompt="hello there", model="gpt-4o-mini")
    assert "hello there" in result.text
    assert result.model == "gpt-4o-mini"
    assert result.tokens_used > 0
    assert result.cost_estimate >= 0
    await omniroute_client.aclose()


@pytest.mark.asyncio
async def test_complete_retries_then_succeeds_on_flaky_model(omniroute_client):
    # fake server fails the first 2 calls to model="flaky", succeeds on the 3rd.
    # max_retries=2 means up to 3 total attempts, so this should succeed.
    result = await omniroute_client.complete(prompt="retry me", model="flaky")
    assert "retry me" in result.text
    await omniroute_client.aclose()


@pytest.mark.asyncio
async def test_complete_raises_after_exhausting_retries(omniroute_client):
    with pytest.raises(OmniRouteError) as excinfo:
        await omniroute_client.complete(prompt="doomed", model="always-fails")
    assert excinfo.value.error_type == "upstream_unavailable"
    await omniroute_client.aclose()


@pytest.mark.asyncio
async def test_falls_back_to_configured_model(fake_omniroute):
    client = OmniRouteClient(
        base_url=fake_omniroute.base_url,
        api_key="k",
        default_model="always-fails",
        fallback_model="gpt-4o-mini",
        max_retries=1,
        backoff_base_seconds=0.01,
    )
    result = await client.complete(prompt="need a fallback")
    assert result.model == "gpt-4o-mini"
    assert "need a fallback" in result.text
    await client.aclose()


@pytest.mark.asyncio
async def test_no_fallback_configured_raises():
    # Transport-level failure with no real server at all, no fallback set.
    client = OmniRouteClient(
        base_url="http://127.0.0.1:1",  # nothing listening
        api_key="k",
        max_retries=1,
        backoff_base_seconds=0.01,
        timeout_seconds=1.0,
    )
    with pytest.raises(OmniRouteError):
        await client.complete(prompt="no one home")
    await client.aclose()
