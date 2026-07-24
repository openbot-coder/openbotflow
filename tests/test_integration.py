"""Integration tests using real mimo-v2.5 API calls."""
import httpx
import json
import time
import asyncio
import pytest

pytestmark = pytest.mark.integration

BASE = "http://127.0.0.1:4000"


def test_health():
    print("=" * 60)
    print("TEST 1: /health")
    print("=" * 60)
    client = httpx.Client(timeout=30.0)
    r = client.get(f"{BASE}/health")
    print(f"Status: {r.status_code}")
    print(f"Body: {r.json()}")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    print("PASS\n")


def test_models_openai():
    print("=" * 60)
    print("TEST 2: /v1/models (OpenAI format)")
    print("=" * 60)
    client = httpx.Client(timeout=30.0)
    r = client.get(f"{BASE}/v1/models")
    print(f"Status: {r.status_code}")
    data = r.json()
    assert r.status_code == 200
    assert "data" in data
    for m in data["data"]:
        print(f"  - {m.get('id')} ({m.get('object')})")
    assert any(m.get("id") == "mimo-v2.5" for m in data["data"])
    print("PASS\n")


def test_models_anthropic():
    print("=" * 60)
    print("TEST 3: /v1/models (Anthropic format)")
    print("=" * 60)
    client = httpx.Client(timeout=30.0)
    r = client.get(
        f"{BASE}/v1/models",
        headers={"accept": "application/vnd.anthropic+json"},
    )
    print(f"Status: {r.status_code}")
    data = r.json()
    assert r.status_code == 200
    assert "data" in data
    for m in data["data"]:
        print(f"  - {m.get('id')}")
    print("PASS\n")


def test_chat_completions_non_stream():
    print("=" * 60)
    print("TEST 4: /v1/chat/completions (non-streaming)")
    print("=" * 60)
    client = httpx.Client(timeout=60.0)
    payload = {
        "model": "mimo-v2.5",
        "messages": [{"role": "user", "content": "Say hello in exactly 5 words."}],
        "stream": False,
    }
    r = client.post(f"{BASE}/v1/chat/completions", json=payload)
    print(f"Status: {r.status_code}")
    data = r.json()
    if r.status_code == 200 and "choices" in data:
        print(f"Response: {data['choices'][0]['message']['content']}")
        print(f"Usage: {data.get('usage', {})}")
    else:
        print(f"Body: {json.dumps(data, indent=2)}")
    assert r.status_code == 200
    assert "choices" in data
    print("PASS\n")


def test_chat_completions_stream():
    print("=" * 60)
    print("TEST 5: /v1/chat/completions (streaming)")
    print("=" * 60)
    client = httpx.Client(timeout=60.0)
    payload = {
        "model": "mimo-v2.5",
        "messages": [{"role": "user", "content": "Count from 1 to 5."}],
        "stream": True,
    }
    collected = []
    with client.stream("POST", f"{BASE}/v1/chat/completions", json=payload) as r:
        print(f"Status: {r.status_code}")
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data: "):
                chunk = line[6:]
                if chunk == "[DONE]":
                    print(f"[DONE] received")
                    break
                d = json.loads(chunk)
                delta = d.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    collected.append(content)
                    print(f'  chunk: "{content}"')
    full = "".join(collected)
    print(f"Full response: {full}")
    assert len(full) > 0
    print("PASS\n")


def test_completions_prompt():
    print("=" * 60)
    print("TEST 6: /v1/completions (prompt-based)")
    print("=" * 60)
    client = httpx.Client(timeout=60.0)
    payload = {
        "model": "mimo-v2.5",
        "prompt": "What is 2 + 2? Answer in one word.",
    }
    r = client.post(f"{BASE}/v1/completions", json=payload)
    print(f"Status: {r.status_code}")
    data = r.json()
    if r.status_code == 200 and "choices" in data:
        print(f"Response: {data['choices'][0]['message']['content']}")
    else:
        print(f"Body: {json.dumps(data, indent=2)}")
    assert r.status_code == 200
    assert "choices" in data
    print("PASS\n")


def test_anthropic_messages():
    print("=" * 60)
    print("TEST 7: /v1/messages (Anthropic format)")
    print("=" * 60)
    client = httpx.Client(timeout=60.0)
    payload = {
        "model": "mimo-v2.5",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Say good morning in 3 words."}],
    }
    r = client.post(f"{BASE}/v1/messages", json=payload)
    print(f"Status: {r.status_code}")
    data = r.json()
    if r.status_code == 200 and "content" in data:
        for block in data.get("content", []):
            print(f"Response: {block.get('text')}")
        print(f"Usage: {data.get('usage', {})}")
    else:
        print(f"Body: {json.dumps(data, indent=2)}")
    assert r.status_code == 200
    assert "content" in data
    print("PASS\n")


def test_embeddings_stub():
    print("=" * 60)
    print("TEST 8: /v1/embeddings (stub)")
    print("=" * 60)
    client = httpx.Client(timeout=30.0)
    payload = {"model": "mimo-v2.5", "input": "hello"}
    r = client.post(f"{BASE}/v1/embeddings", json=payload)
    print(f"Status: {r.status_code} (expected 501)")
    print(f"Body: {r.json()}")
    assert r.status_code == 501
    print("PASS\n")


def test_anthropic_messages_stream():
    print("=" * 60)
    print("TEST 9: /v1/messages (Anthropic format, streaming)")
    print("=" * 60)
    client = httpx.Client(timeout=60.0)
    payload = {
        "model": "mimo-v2.5",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Count 1 to 5, one per line."}],
        "stream": True,
    }
    collected = []
    event_types = set()
    with client.stream("POST", f"{BASE}/v1/messages", json=payload) as r:
        print(f"Status: {r.status_code}")
        assert r.status_code == 200
        for line in r.iter_lines():
            line = line.strip()
            if not line or not line.startswith("data: "):
                continue
            payload_str = line[6:]
            if not payload_str or payload_str == "[DONE]":
                continue
            try:
                chunk = json.loads(payload_str)
            except json.JSONDecodeError:
                continue
            t = chunk.get("type", "")
            event_types.add(t)
            if t == "content_block_delta":
                text = chunk.get("delta", {}).get("text", "")
                if text:
                    collected.append(text)
                    print(f'  chunk: "{text}"')
            elif t == "message_stop":
                print("  [message_stop] received")
    full = "".join(collected)
    print(f"Full Anthropic stream: {full}")
    # Verify stream completed with proper event types
    assert "message_start" in event_types, f"Missing message_start, got: {event_types}"
    assert r.status_code == 200
    print("PASS\n")


if __name__ == "__main__":
    # When run directly, bypass pytest and execute tests manually
    tests = [
        test_health,
        test_models_openai,
        test_models_anthropic,
        test_chat_completions_non_stream,
        test_chat_completions_stream,
        test_completions_prompt,
        test_anthropic_messages,
        test_embeddings_stub,
        test_anthropic_messages_stream,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"FAILED: {e}\n")
            failed += 1

    print("=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed out of {len(tests)}")
    print("=" * 60)
