from botflow.common.context import estimate_tokens, truncate_to_context_window


def test_estimate_tokens_with_plain_text():
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
    ]
    assert estimate_tokens(messages) > 0


def test_estimate_tokens_with_multimodal_content():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }
    ]
    assert estimate_tokens(messages) > 0


def test_truncate_to_context_window_with_multimodal_content():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }
    ]
    result = truncate_to_context_window(messages, context_window=1024, max_tokens=256)
    assert len(result) == 1
