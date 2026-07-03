"""Integration tests for group routing with mimo-v2.5 + step-3.7-flash."""
import httpx
import json
import time
import pytest

pytestmark = pytest.mark.integration

BASE = "http://127.0.0.1:8765"


def test_group_routing():
    """Send multiple requests and verify both models get routed to."""
    client = httpx.Client(timeout=60.0)
    models_seen = set()

    print("=" * 60)
    print("GROUP ROUTING TEST: 10 requests to verify both models are hit")
    print("=" * 60)

    for i in range(10):
        payload = {
            "model": "mimo-v2.5",
            "messages": [{"role": "user", "content": f"Say the number {i+1} in words."}],
            "stream": False,
        }
        r = client.post(f"{BASE}/v1/chat/completions", json=payload)
        print(f"\nRequest {i+1}: status={r.status_code}")
        if r.status_code == 200:
            data = r.json()
            model_used = data.get("model", "unknown")
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            print(f"  Model used: {model_used}")
            print(f"  Response: {content[:80]}...")
            print(f"  Usage: prompt={usage.get('prompt_tokens')}, "
                  f"completion={usage.get('completion_tokens')}, "
                  f"cache={usage.get('cache_tokens')}")
            models_seen.add(model_used)
        else:
            print(f"  Error: {r.text[:200]}")

    print(f"\n{'=' * 60}")
    print(f"MODELS HIT: {models_seen}")
    print(f"UNIQUE MODELS: {len(models_seen)} / 2 expected")
    if len(models_seen) >= 2:
        print("SUCCESS: Both mimo-v2.5 and step-3.7-flash were routed to!")
    elif len(models_seen) == 1:
        print("PARTIAL: Only one model hit (may need more requests or weight adjustment)")
    else:
        print("FAIL: No models hit")
    print("=" * 60)

    return models_seen


def test_group_streaming():
    """Test streaming with group routing."""
    client = httpx.Client(timeout=60.0)

    print("\n" + "=" * 60)
    print("GROUP STREAMING TEST")
    print("=" * 60)

    payload = {
        "model": "mimo-v2.5",
        "messages": [{"role": "user", "content": "Count from 1 to 3."}],
        "stream": True,
    }

    model_used = None
    collected = []
    with client.stream("POST", f"{BASE}/v1/chat/completions", json=payload) as r:
        print(f"Status: {r.status_code}")
        for line in r.iter_lines():
            if line.startswith("data: "):
                chunk = line[6:]
                if chunk == "[DONE]":
                    print("[DONE]")
                    break
                d = json.loads(chunk)
                model_used = d.get("model", model_used)
                delta = d.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    collected.append(content)

    print(f"Model used: {model_used}")
    print(f"Full response: {''.join(collected)}")
    print("=" * 60)


def test_group_anthropic_format():
    """Test Anthropic messages format with group routing."""
    client = httpx.Client(timeout=60.0)

    print("\n" + "=" * 60)
    print("GROUP ANTHROPIC FORMAT TEST")
    print("=" * 60)

    payload = {
        "model": "mimo-v2.5",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Say hi in 2 words."}],
    }
    r = client.post(f"{BASE}/v1/messages", json=payload)
    print(f"Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"Model: {data.get('model')}")
        for block in data.get("content", []):
            print(f"Response: {block.get('text')}")
        print(f"Usage: {data.get('usage', {})}")
    else:
        print(f"Error: {r.text[:200]}")
    print("=" * 60)


if __name__ == "__main__":
    test_group_routing()
    test_group_streaming()
    test_group_anthropic_format()
