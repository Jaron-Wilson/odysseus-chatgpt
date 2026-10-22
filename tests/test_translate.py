"""Chat Completions <-> Responses (chatgpt_mod/translate.py). No network."""
import json

from chatgpt_mod import translate as tr


def test_chat_messages_translate_to_responses_input():
    items = tr.messages_to_input([
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "low"}},
        ]},
        {"role": "assistant", "content": "A cat.", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{\"q\":\"cat\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "results"},
        {"role": "user", "content": "Thanks"},
    ])
    assert items[0] == {"role": "system", "content": [{"type": "input_text", "text": "Be brief."}]}
    assert items[1] == {"role": "user", "content": [
        {"type": "input_text", "text": "What is this?"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "low"},
    ]}
    assert items[2] == {"role": "assistant", "content": [{"type": "output_text", "text": "A cat."}]}
    assert items[3] == {"type": "function_call", "call_id": "call_1", "name": "search", "arguments": "{\"q\":\"cat\"}"}
    assert items[4] == {"type": "function_call_output", "call_id": "call_1", "output": "results"}
    assert items[5] == {"role": "user", "content": [{"type": "input_text", "text": "Thanks"}]}


def test_payload_always_sends_store_false_and_stream_true():
    tools = [{"type": "function", "function": {"name": "t", "description": "d",
                                               "parameters": {"type": "object", "properties": {}}}}]
    p = tr.build_payload({"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}], "tools": tools,
                          "stream": False, "store": True, "temperature": 0.2, "max_tokens": 9})
    assert p["store"] is False and p["stream"] is True and p["model"] == "gpt-5.5"
    assert p["tools"] == [{"type": "function", "name": "t", "description": "d",
                           "parameters": {"type": "object", "properties": {}}, "strict": False}]
    assert "messages" not in p and "temperature" not in p and "max_output_tokens" not in p
    p2 = tr.build_payload({"model": "gpt-5.5", "messages": []})
    assert p2["store"] is False and p2["stream"] is True and "tools" not in p2


def test_tools_never_exceed_openais_limit():
    tools = [{"type": "function", "function": {"name": f"t{i}"}} for i in range(200)]
    assert len(tr.build_payload({"model": "m", "messages": [], "tools": tools})["tools"]) == tr.MAX_TOOLS


def _run(events):
    t = tr.ChunkTranslator("gpt-5.5")
    out = []
    for e in events:
        out.extend(t.feed(e))
    out.extend(t.finish())
    return t, [c[6:].strip() for c in out]


def _deltas(lines):
    return [json.loads(x)["choices"][0]["delta"] for x in lines if x != "[DONE]" and json.loads(x)["choices"]]


def test_text_deltas_stream_as_chat_chunks_with_usage():
    t, lines = _run([
        {"type": "response.created"},
        {"type": "response.output_text.delta", "delta": "Hel"},
        {"type": "response.reasoning_summary_text.delta", "delta": "hmm"},
        {"type": "response.output_text.delta", "delta": "lo"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 5, "output_tokens": 2}}},
    ])
    assert lines[-1] == "[DONE]"
    d = _deltas(lines)
    assert d[0] == {"role": "assistant", "content": ""}
    assert d[1:4] == [{"content": "Hel"}, {"reasoning_content": "hmm"}, {"content": "lo"}]
    first = json.loads(lines[1])
    assert first["object"] == "chat.completion.chunk" and first["model"] == "gpt-5.5"
    assert json.loads(lines[-3])["choices"][0]["finish_reason"] == "stop"
    assert json.loads(lines[-2])["usage"] == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    assert tr.completion(t)["choices"][0]["message"]["content"] == "Hello"


def test_function_calls_become_tool_call_deltas():
    t, lines = _run([
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "search"}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "{\"q\":"},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "\"cats\"}"},
        {"type": "response.completed", "response": {}},
    ])
    d = _deltas(lines)
    assert d[0] == {"tool_calls": [{"index": 0, "id": "call_9", "type": "function",
                                    "function": {"name": "search", "arguments": "{\"q\":\"cats\"}"}}]}
    assert json.loads(lines[-2])["choices"][0]["finish_reason"] == "tool_calls"
    msg = tr.completion(t)["choices"][0]
    assert msg["finish_reason"] == "tool_calls" and msg["message"]["tool_calls"][0]["function"]["name"] == "search"


def test_failures_become_a_plain_note_and_end_the_stream():
    t, lines = _run([{"type": "response.failed", "response": {"error": {
        "code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}}}])
    note = _deltas(lines)[0]["content"]
    assert note.startswith("[Your ChatGPT plan's usage limit was reached") and lines[-1] == "[DONE]"
    assert t.error["status"] == 429
    _, lines = _run([{"type": "response.output_text.delta", "delta": "partial"},
                     {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "content_filter"}}}])
    assert "content_filter" in _deltas(lines)[-2]["content"]
    # No response.completed: said so, never a silent success.
    t, lines = _run([{"type": "response.output_text.delta", "delta": "cut"}])
    assert "ended the response" in "".join(d.get("content", "") for d in _deltas(lines))
    assert t.error["code"] == "stream_ended"


def test_sse_parsing_keeps_event_names():
    events = list(tr.iter_sse_events(["event: response.output_text.delta", 'data: {"delta":"x"}', "",
                                      "data: [DONE]"]))
    assert events == [{"delta": "x", "type": "response.output_text.delta"}]
