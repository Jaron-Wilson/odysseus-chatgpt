"""Chat Completions in, the Responses API out, and back.

Odysseus talks to this add-on as to any OpenAI-compatible server
(``POST /v1/chat/completions``); ChatGPT plan usage only takes
``POST /v1/responses`` with ``store: false`` and ``stream: true``. So a
request is translated to a Responses payload (``build_payload``), and the
Responses events are translated back to ``chat.completion.chunk`` events
(``ChunkTranslator``) or, for a call that did not ask to stream, collected
into one ``chat.completion`` (``collect``).

The translation is the one the core used (src/chatgpt_plan.py), so chats,
tools and images behave as before.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Iterable, Iterator, List, Optional

from chatgpt_mod.plan import friendly_error

# OpenAI caps function tools at 128 per request. Odysseus already keeps a
# request under 120 for this endpoint (the manifest's llm_provider.tool_cap);
# this is the last guard.
MAX_TOOLS = 128

# Reasoning efforts the Responses API takes in ``reasoning.effort``. A value
# outside this set is dropped; whether a given model takes it is left to the
# server.
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _user_parts(content: Any) -> List[Dict[str, Any]]:
    if not isinstance(content, list):
        return [{"type": "input_text", "text": _text_of(content)}]
    parts: List[Dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "input_text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "input_text") and isinstance(part.get("text"), str):
            parts.append({"type": "input_text", "text": part["text"]})
        elif kind == "image_url":
            img = part.get("image_url")
            url = img.get("url") if isinstance(img, dict) else img
            if isinstance(url, str) and url:
                item: Dict[str, Any] = {"type": "input_image", "image_url": url}
                if isinstance(img, dict) and img.get("detail"):
                    item["detail"] = img["detail"]
                parts.append(item)
        elif kind == "input_image":
            parts.append(part)
    return parts or [{"type": "input_text", "text": ""}]


def messages_to_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Chat Completions messages as Responses input items."""
    items: List[Dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")
        if role in ("system", "developer"):
            text = _text_of(content)
            if text:
                items.append({"role": "system", "content": [{"type": "input_text", "text": text}]})
        elif role == "assistant":
            text = _text_of(content)
            if text:
                items.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args or {})
                items.append({"type": "function_call", "call_id": tc.get("id") or f"call_{len(items)}",
                              "name": fn.get("name") or tc.get("name") or "", "arguments": args})
        elif role == "tool":
            call_id = msg.get("tool_call_id")
            if call_id:
                items.append({"type": "function_call_output", "call_id": call_id, "output": _text_of(content)})
            else:
                items.append({"role": "user", "content": [{"type": "input_text", "text": _text_of(content)}]})
        else:
            items.append({"role": "user", "content": _user_parts(content)})
    return items


def tools_to_responses(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else None
        if isinstance(fn, dict) and fn.get("name"):
            # The Responses API treats function tools as strict unless told
            # otherwise, and strict mode rejects schemas with optional
            # properties (most of Odysseus's). Chat Completions defaults to
            # non-strict, which is what these schemas were written for.
            item = {"type": "function", "name": fn["name"],
                    "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                    "strict": bool(fn.get("strict", False))}
            if fn.get("description"):
                item["description"] = fn["description"]
            out.append(item)
        elif tool.get("type") == "function" and tool.get("name"):
            out.append(tool)
    return out


def reasoning_effort_of(body: Dict[str, Any]) -> Optional[str]:
    """The request's reasoning effort, from ``reasoning_effort`` (Chat
    Completions) or ``reasoning: {"effort": ...}`` (Responses style), if it is
    a known one."""
    candidates = [body.get("reasoning_effort")]
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        candidates.append(reasoning.get("effort"))
    for value in candidates:
        if isinstance(value, str) and value in REASONING_EFFORTS:
            return value
    return None


def build_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    """A Responses payload for a Chat Completions request body."""
    payload: Dict[str, Any] = {"model": body.get("model") or "", "input": messages_to_input(body.get("messages") or [])}
    mapped = tools_to_responses(body.get("tools"))[:MAX_TOOLS]
    if mapped:
        payload["tools"] = mapped
    effort = reasoning_effort_of(body)
    if effort:
        payload["reasoning"] = {"effort": effort}
    # store/stream are required by ChatGPT plan usage; set last so nothing
    # above can override them. (Temperature and max tokens are not sent: the
    # plan's models take their own.)
    payload["store"] = False
    payload["stream"] = True
    return payload


def _sse(data: Any) -> str:
    return f"data: {json.dumps(data)}\n\n"


class ChunkTranslator:
    """Responses SSE events -> chat.completion.chunk events, ending in
    ``data: [DONE]``. A failure after the stream started becomes a short note
    in the reply, since the HTTP status was already sent."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.id = "chatcmpl-" + uuid.uuid4().hex[:24]
        self.created = int(time.time())
        self.done = False
        self.emitted_text = False
        self.text: List[str] = []
        self.usage: Dict[str, int] = {}
        self.error: Optional[Dict[str, Any]] = None
        self._calls: Dict[str, Dict[str, str]] = {}
        self._order: List[str] = []

    def _chunk(self, delta: Dict[str, Any], finish: Optional[str] = None) -> str:
        return _sse({"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

    def _call(self, key: str) -> Dict[str, str]:
        if key not in self._calls:
            self._calls[key] = {"id": "", "name": "", "arguments": ""}
            self._order.append(key)
        return self._calls[key]

    @property
    def calls(self) -> List[Dict[str, str]]:
        return [self._calls[k] for k in self._order if self._calls[k]["name"]]

    def _end(self, finish: str) -> List[str]:
        out = []
        calls = self.calls
        if calls:
            out.append(self._chunk({"tool_calls": [
                {"index": i, "id": c["id"] or f"call_{i}", "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                for i, c in enumerate(calls)]}))
            finish = "tool_calls" if finish == "stop" else finish
        out.append(self._chunk({}, finish))
        if self.usage:
            out.append(_sse({"id": self.id, "object": "chat.completion.chunk", "created": self.created,
                             "model": self.model, "choices": [], "usage": {
                                 "prompt_tokens": self.usage.get("input_tokens", 0),
                                 "completion_tokens": self.usage.get("output_tokens", 0),
                                 "total_tokens": self.usage.get("input_tokens", 0) + self.usage.get("output_tokens", 0)}}))
        out.append("data: [DONE]\n\n")
        self.done = True
        return out

    def _note(self, text: str) -> List[str]:
        self.text.append(text)
        return [self._chunk({"content": text})]

    def feed(self, event: Dict[str, Any]) -> List[str]:
        if self.done or not isinstance(event, dict):
            return []
        kind = event.get("type") or ""
        out: List[str] = []
        if kind == "response.output_text.delta":
            delta = event.get("delta") or ""
            if delta:
                if not self.emitted_text:
                    out.append(self._chunk({"role": "assistant", "content": ""}))
                self.emitted_text = True
                self.text.append(delta)
                out.append(self._chunk({"content": delta}))
        elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            delta = event.get("delta") or ""
            if delta:
                out.append(self._chunk({"reasoning_content": delta}))
        elif kind in ("response.output_item.added", "response.output_item.done"):
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call = self._call(str(item.get("id") or item.get("call_id") or event.get("output_index")))
                call["id"] = item.get("call_id") or call["id"] or str(item.get("id") or "")
                call["name"] = item.get("name") or call["name"]
                if kind == "response.output_item.done" and isinstance(item.get("arguments"), str):
                    call["arguments"] = item["arguments"]
        elif kind == "response.function_call_arguments.delta":
            call = self._call(str(event.get("item_id") or event.get("output_index")))
            call["arguments"] += event.get("delta") or ""
        elif kind == "response.function_call_arguments.done":
            call = self._call(str(event.get("item_id") or event.get("output_index")))
            if isinstance(event.get("arguments"), str):
                call["arguments"] = event["arguments"]
        elif kind == "response.completed":
            usage = (event.get("response") or {}).get("usage") or {}
            if usage.get("input_tokens") or usage.get("output_tokens"):
                self.usage = {"input_tokens": usage.get("input_tokens") or 0,
                              "output_tokens": usage.get("output_tokens") or 0}
            out.extend(self._end("stop"))
        elif kind in ("response.failed", "error"):
            err = (event.get("response") or {}).get("error") or event.get("error") or {}
            if not isinstance(err, dict):
                err = {"message": str(err)}
            code = str(err.get("code") or event.get("code") or "")
            message = friendly_error(code, err.get("message") or event.get("message") or "")
            self.error = {"message": message, "code": code,
                          "status": 429 if code == "subscription_sharing_usage_limit_exceeded" else 502}
            out.extend(self._note(("\n\n" if self.emitted_text else "") + f"[{message}]"))
            out.extend(self._end("stop"))
        elif kind == "response.incomplete":
            details = (event.get("response") or {}).get("incomplete_details") or {}
            reason = details.get("reason") or "unknown"
            out.extend(self._note(("\n\n" if self.emitted_text else "")
                                  + f"[ChatGPT stopped before finishing the response (reason: {reason}).]"))
            out.extend(self._end("length"))
        return out

    def finish(self) -> List[str]:
        """Call when the upstream stream ends; notes it if it never completed."""
        if self.done:
            return []
        self.error = {"message": "ChatGPT ended the response before it finished. Try again.",
                      "code": "stream_ended", "status": 502}
        return self._note(("\n\n" if self.emitted_text else "") + f"[{self.error['message']}]") + self._end("stop")


def parse_data_line(line: str, event_name: str = "") -> Optional[Dict[str, Any]]:
    """One SSE ``data:`` line as an event dict, or None."""
    if not line or not line.startswith("data:"):
        return None
    raw = line[5:].strip()
    if not raw or raw == "[DONE]":
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("type") and event_name:
        data["type"] = event_name
    return data


def iter_sse_events(lines: Iterable[str]) -> Iterator[Dict[str, Any]]:
    event_name = ""
    for line in lines:
        if not line:
            event_name = ""
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        event = parse_data_line(line, event_name)
        if event is not None:
            yield event


def completion(t: ChunkTranslator) -> Dict[str, Any]:
    """The collected stream as one chat.completion (a non-streamed answer)."""
    msg: Dict[str, Any] = {"role": "assistant", "content": "".join(t.text)}
    calls = t.calls
    if calls:
        msg["tool_calls"] = [{"id": c["id"] or f"call_{i}", "type": "function",
                              "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                             for i, c in enumerate(calls)]
    out = {"id": t.id, "object": "chat.completion", "created": t.created, "model": t.model,
           "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if calls else "stop"}]}
    if t.usage:
        out["usage"] = {"prompt_tokens": t.usage["input_tokens"], "completion_tokens": t.usage["output_tokens"],
                        "total_tokens": t.usage["input_tokens"] + t.usage["output_tokens"]}
    return out
