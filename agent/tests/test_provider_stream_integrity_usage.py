"""A transport EOF must not turn partial generation into executable output."""

from __future__ import annotations

from langchain_core.messages import AIMessageChunk
import pytest

from src.providers.chat import ChatLLM, ProviderStreamError


class Stream:
    _llm_type = "openai-chat"

    def __init__(self, chunks, failure=None):
        self.chunks = chunks
        self.failure = failure
        self.invoked = False

    def stream(self, *_args, **_kwargs):
        yield from self.chunks
        if self.failure:
            raise self.failure

    def invoke(self, *_args, **_kwargs):
        self.invoked = True
        raise AssertionError("Partial streams must not start an unaccounted fallback")


def client(stream):
    llm = ChatLLM.__new__(ChatLLM)
    llm._llm = stream
    llm.model_name = "mock-model"
    return llm


def usage(count):
    return {"input_tokens": count - 1, "output_tokens": 1, "total_tokens": count}


@pytest.mark.parametrize("partial", [
    AIMessageChunk(content="unfinished report"),
    AIMessageChunk(content="", tool_call_chunks=[{"name": "write_file", "args": '{"path":"out', "id": "call-a", "index": 0}]),
])
def test_clean_eof_rejects_partial_text_and_tool_calls(partial):
    stream = Stream([partial])
    with pytest.raises(ProviderStreamError, match="terminal finish reason") as caught:
        client(stream).stream_chat([{"role": "user", "content": "go"}])
    assert caught.value.retryable
    assert not stream.invoked


@pytest.mark.parametrize("metadata", [
    {"finish_reason": "stop"}, {"finish_reason": "tool_calls"},
    {"finish_reason": "length"}, {"status": "completed"},
])
def test_terminal_metadata_accepts_completed_protocol_response(metadata):
    result = client(Stream([AIMessageChunk(content="done", response_metadata=metadata)])).stream_chat([])
    assert result.content == "done"


@pytest.mark.parametrize("failure", [ConnectionResetError("reset"), ValueError("No generation chunks were returned")])
def test_failed_stream_keeps_received_usage_without_nonstream_fallback(failure):
    stream = Stream([AIMessageChunk(content="partial", usage_metadata=usage(10))], failure)
    with pytest.raises(ProviderStreamError) as caught:
        client(stream).stream_chat([])
    assert caught.value.usage_metadata["total_tokens"] == 10
    assert not stream.invoked


def test_clean_eof_keeps_reported_usage_on_the_error():
    with pytest.raises(ProviderStreamError) as caught:
        client(Stream([AIMessageChunk(content="partial", usage_metadata=usage(10))])).stream_chat([])
    assert caught.value.usage_metadata["total_tokens"] == 10


def test_cancel_keeps_usage_on_arriving_chunk_but_no_tools_or_new_text():
    chunks = [
        AIMessageChunk(content="partial", usage_metadata=usage(10)),
        AIMessageChunk(content="dropped", usage_metadata=usage(20), tool_call_chunks=[
            {"name": "write_file", "args": '{"path":"never"}', "id": "call-a", "index": 0},
        ]),
    ]
    checks = iter([False, True])
    seen = []
    result = client(Stream(chunks)).stream_chat([], should_cancel=lambda: next(checks), on_text_chunk=seen.append)
    assert result.usage_metadata["total_tokens"] == 30
    assert result.content == "partial"
    assert not result.tool_calls
    assert seen == ["partial"]


def test_cancel_on_first_chunk_keeps_usage_and_never_falls_back():
    stream = Stream([AIMessageChunk(content="dropped", usage_metadata=usage(10))])
    result = client(stream).stream_chat([], should_cancel=lambda: True)
    assert result.content == ""
    assert not result.tool_calls
    assert result.usage_metadata["total_tokens"] == 10
    assert not stream.invoked


def test_cancel_does_not_flush_buffered_tool_markup_as_user_text():
    stream = Stream([AIMessageChunk(content="<||DSML||"), AIMessageChunk(content="dropped")])
    checks = iter([False, True])
    seen = []
    result = client(stream).stream_chat([], should_cancel=lambda: next(checks), on_text_chunk=seen.append)
    assert not result.tool_calls
    assert seen == []


@pytest.mark.parametrize("metadata", [
    {"finish_reason": "length"}, {"stop_reason": "max_tokens"},
    {"status": "incomplete"}, {"status": "failed"},
])
def test_truncated_tool_arguments_are_not_released_after_langchain_repairs_json(metadata):
    chunk = AIMessageChunk(content="", tool_call_chunks=[
        {"name": "write_file", "args": '{"path":"truncated', "id": "call-a", "index": 0},
    ], response_metadata=metadata, usage_metadata=usage(17))
    assert chunk.tool_calls[0]["args"] == {"path": "truncated"}
    with pytest.raises(ProviderStreamError, match="completing its tool calls") as caught:
        client(Stream([chunk])).stream_chat([])
    assert caught.value.usage_metadata["total_tokens"] == 17
