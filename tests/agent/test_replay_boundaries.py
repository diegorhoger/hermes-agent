"""Retry-boundary safety: nothing replays past a commitment boundary.

The API-error handler may retry only while NOTHING has committed this turn —
no visible streamed text (possibly none of the user's own words, a broken
progress line, nothing). A committed partial answer always ends the turn as
failed-partial, marked accurately, before any retry decision is made. These
tests pin the boundary from both sides, including with committed tool work in
the transcript and with an ambiguous-commitment (timeout) failure.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from agent.turn_api_error import handle_api_error
from agent.turn_truncation import recover_from_truncation

from tests.agent.test_no_replay_after_commit import _Agent, _Http, _PARTIAL, _messages

_TOOLCALL_MESSAGES = [
    {"role": "user", "content": "inspect the repo and fix it"},
    {"role": "assistant", "content": None,
     "tool_calls": [{"id": "call_1", "type": "function",
                     "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}]},
    {"role": "tool", "content": "file contents {marker: zinnia}", "tool_call_id": "call_1"},
]


def _retry(**overrides):
    base = dict(provider_stale_refresh_attempted=False, primary_recovery_attempted=True,
                auth_failover_attempted=False, context_overflow_count=0,
                restart_with_redirected_messages=False, restart_with_rebuilt_messages=False,
                restart_with_compressed_messages=False, auto_recovery_cycles_used=0)
    base.update(overrides)
    return SimpleNamespace(**base)


def _verdict(agent, error, *, messages, retry_count=0, max_retries=3):
    return handle_api_error(
        agent, api_error=error, _retry=_retry(has_retried_429=False), thinking_spinner=None,
        messages=messages, api_messages=[], api_kwargs=None, system_message=None,
        active_system_prompt="", conversation_history=None, approx_tokens=10,
        retry_count=retry_count, max_retries=max_retries, compression_attempts=0,
        max_compression_attempts=2, api_call_count=2, api_request_id=None,
        api_start_time=time.time(), effective_task_id="t", turn_id="turn", current_turn_user_idx=0)


def test_tool_committed_and_text_committed_ends_failed_partial_no_replay():
    """Tool work in the transcript + visible text committed: the no-replay contract
    fires exactly as for a plain answer. Tools are never re-run by the error path."""
    messages = _messages() + [
        {"role": "user", "content": "continue where you left off", "_length_continuation_nudge": True},
    ]
    agent = _Agent()
    agent._current_streamed_assistant_text = _PARTIAL
    verdict = _verdict(agent, _Http(503, "provider unavailable"), messages=messages)
    assert verdict.action == "return"
    assert verdict.result["failure_reason"] == "stream_interrupted"
    assert verdict.result["partial"] is True
    assert _PARTIAL in verdict.result["final_response"]


def test_tool_committed_without_text_does_not_use_the_no_replay_path():
    """Tool rows committed, zero streamed text: nothing user-visible committed, so the
    ordinary recovery machinery owns the failure (in-flight inference may be re-asked;
    committed tool effects are never re-executed)."""
    class _NoTextAgent(_Agent):
        _current_streamed_assistant_text = ""

    verdict = _verdict(_NoTextAgent(), _Http(429, "rate limited"), messages=_TOOLCALL_MESSAGES)
    assert not (verdict.action == "return" and verdict.result
                and verdict.result.get("failure_reason") == "stream_interrupted")


def test_timeout_with_committed_text_never_retries():
    """Ambiguous commitment (timeout) + committed visible text: no automatic retry."""

    class _Timeout(Exception):
        pass

    agent = _Agent()
    agent._current_streamed_assistant_text = _PARTIAL
    verdict = _verdict(agent, _Timeout("read timed out"), messages=_messages())
    assert verdict.action == "return"
    assert verdict.result["failure_reason"] == "stream_interrupted"
    assert verdict.result["failure_retryable"] is False
    assert _PARTIAL in verdict.result["final_response"]


def test_timeout_without_committed_text_does_not_use_the_no_replay_path():
    """Ambiguous commitment + zero committed output: the failure stays retryable;
    nothing user-visible is fabricated or lost."""

    class _Timeout(Exception):
        pass

    class _NoTextAgent(_Agent):
        _current_streamed_assistant_text = ""

    verdict = _verdict(_NoTextAgent(), _Timeout("read timed out"), messages=[{"role": "user", "content": "hi"}])
    assert not (verdict.action == "return" and verdict.result
                and verdict.result.get("failure_reason") == "stream_interrupted")


def test_truncation_no_replay_still_ends_turn_after_route_switch():
    """The truncation no-replay guard is provider-agnostic: whichever physical route
    the (switched) agent is on, a committed partial is never continued."""
    from hermes_constants import FINISH_REASON_LENGTH, PARTIAL_STREAM_STUB_ID

    class _SwitchedAgent:
        provider = "otherprovider"  # the fallback route the turn switched to
        model = "fallback-model"
        base_url = "https://other.invalid/v1"
        log_prefix = ""
        _current_streamed_assistant_text = _PARTIAL
        response_model = None

        @staticmethod
        def _strip_think_blocks(text):
            return text

        @staticmethod
        def _has_content_after_think_block(text):
            return bool(str(text or "").strip())

        @staticmethod
        def _vprint(*_a, **_kw):
            return None

        @staticmethod
        def _flush_status_buffer():
            return None

        @staticmethod
        def _cleanup_task_resources(_task_id):
            return None

        @staticmethod
        def _persist_session(_messages, _history):
            return None

    response = SimpleNamespace(
        id=PARTIAL_STREAM_STUB_ID,
        _dropped_tool_names=None,
        choices=[SimpleNamespace(
            index=0,
            message=SimpleNamespace(role="assistant", content=_PARTIAL,
                                    tool_calls=None, reasoning_content=None),
            finish_reason=FINISH_REASON_LENGTH,
        )],
    )
    messages = [{"role": "user", "content": "write it"}]
    verdict = recover_from_truncation(
        _SwitchedAgent(), response, FINISH_REASON_LENGTH, SimpleNamespace(),
        messages=messages, conversation_history=None,
        api_kwargs={}, api_call_count=2, effective_task_id=None, current_turn_user_idx=0,
        length_continue_retries=0, truncated_response_parts=[],
        truncated_tool_call_retries=0, retry_count=0, compression_attempts=0,
    )
    result = verdict.result or {}
    assert verdict.action == "return"
    assert result.get("failed") is True
    assert result.get("failure_reason") == "stream_interrupted"
    assert _PARTIAL in (result.get("final_response") or "")


# ---------------------------------------------------------------------------
# Runtime evidence: a side-effecting tool counter, through the REAL recovery
# path (run_conversation -> retry loop -> handle_api_error / recover_from_truncation).
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile

from run_agent import AIAgent

_MARK_TOOL = {
    "type": "function",
    "function": {
        "name": "mark_side_effect",
        "description": "increment the side-effect counter",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _tool_response(call_id="c1", arguments="{}", name="mark_side_effect"):
    msg = SimpleNamespace(
        content="",
        tool_calls=[SimpleNamespace(
            id=call_id, type="function",
            function=SimpleNamespace(name=name, arguments=arguments),
        )],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="tool_calls", index=0)],
        model="test/model", usage=None)


def _text_response(text="done", finish_reason="stop"):
    msg = SimpleNamespace(content=text, tool_calls=None, reasoning_content=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason=finish_reason, index=0)],
        model="test/model", usage=None)


def _length_text_response(text="partial answer"):
    return _text_response(text, finish_reason="length")


def _length_tool_response(call_id="c1", arguments='{"path": "a'):
    # A truncated tool call: the arguments are NOT valid JSON and can never dispatch.
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(
                role="assistant", content="",
                tool_calls=[SimpleNamespace(
                    id=call_id, type="function",
                    function=SimpleNamespace(name="mark_side_effect", arguments=arguments),
                )],
                reasoning_content=None),
            finish_reason="length", index=0)],
        model="test/model", usage=None)


def _make_side_effecting_agent(tmp_path, counter=None, fallback_model=None):
    """A real AIAgent whose tool ``mark_side_effect`` counts every dispatch
    through the REAL sequential dispatch path. Backoff is zeroed by
    tests/agent/conftest.py. The counter patch stays active for the whole
    turn (see ``_run``)."""
    if counter is None:
        counter = {"calls": 0, "args": []}

    with (
        patch("model_tools.get_tool_definitions", return_value=[_MARK_TOOL]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("run_agent._hermes_home", tmp_path),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._flush_messages_to_session_db = MagicMock(return_value=True)
    agent._save_trajectory = MagicMock()
    agent._persist_session = MagicMock()
    agent._cleanup_task_resources = MagicMock()
    agent._append_guardrail_observation = MagicMock(
        side_effect=lambda _name, _args, result, **_kw: result)
    agent._record_file_mutation_result = MagicMock()
    agent._subdirectory_hints.check_tool_call = MagicMock(return_value="")
    agent._tool_result_content_for_active_model = MagicMock(
        side_effect=lambda _name, result: result)
    agent.valid_tool_names = {"mark_side_effect"}
    agent._api_max_retries = 2
    return agent, counter


def _run(agent, script, counter):
    """Play ``script`` through the real client mock; keep the tool patch live
    for the whole turn. Each script entry is an exception to raise or a
    response to return, consumed one per API call."""
    def _fake_handle_function_call(function_name, arguments, *_a, **_kw):
        if function_name == "mark_side_effect":
            counter["calls"] += 1
            counter["args"].append(arguments)
            return f"side effect {counter['calls']} committed"
        return "unknown tool"

    calls = {"n": 0}

    def _create(*_a, **_kw):
        idx = calls["n"]
        calls["n"] += 1
        if idx < len(script):
            step = script[idx]
            if isinstance(step, Exception):
                raise step
            return step
        return _text_response("done")

    with patch("model_tools.handle_function_call", side_effect=_fake_handle_function_call):
        agent.client.chat.completions.create.side_effect = _create
        with patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"):
            return agent.run_conversation("do the thing"), calls["n"]


def _tool_rows(messages):
    return [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]


def test_failure_before_any_tool_execution_retries_and_commits_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        agent, counter = _make_side_effecting_agent(Path(tmp))

        class _Http(Exception):
            def __init__(self, status_code, message):
                super().__init__(message)
                self.status_code = status_code

        result, api_calls = _run(agent, [_Http(429, "rate limited"), _text_response("fine")], counter)
    assert counter["calls"] == 0
    assert result["final_response"] == "fine"
    assert result.get("failed") is not True
    assert api_calls == 2  # refused once, retried, committed


def test_failure_after_committed_tool_never_re_executes_the_tool():
    with tempfile.TemporaryDirectory() as tmp:
        agent, counter = _make_side_effecting_agent(Path(tmp))

        class _Http(Exception):
            def __init__(self, status_code, message):
                super().__init__(message)
                self.status_code = status_code

        # Tool commits (counter=1); the very next provider call returns a
        # permanent auth failure (no fallback configured). The turn terminates
        # as failed — the tool must NOT run again: retry/recovery paths re-arm
        # only the inference request (its payload carries the already-committed
        # tool result); they cannot re-dispatch the tool.
        result, _api_calls = _run(
            agent,
            [_tool_response("c1"), _Http(401, "invalid api key")],
            counter)
    assert counter["calls"] == 1, "the committed tool action ran more than once"
    tool_rows = _tool_rows(result.get("messages") or [])
    assert len(tool_rows) == 1
    assert result.get("failed") is True


def test_failure_after_committed_tool_then_provider_switch_keeps_single_effect():
    with tempfile.TemporaryDirectory() as tmp:
        counter = {"calls": 0, "args": []}
        agent, counter = _make_side_effecting_agent(
            Path(tmp), counter=counter,
            fallback_model=[{"provider": "openai", "model": "gpt-4o"}])
        agent._api_max_retries = 1

        class _Http(Exception):
            def __init__(self, status_code, message):
                super().__init__(message)
                self.status_code = status_code

        fallback_client = MagicMock()
        fallback_client.chat.completions.create.return_value = _text_response("done from fallback")

        def _bind(agent_, fb, fb_provider, fb_model):
            agent_.client = fallback_client
            agent_.api_mode = "chat_completions"
            agent_._provider_fallback_active = True
            return ("old-model", "old-provider")

        with patch("agent.route_binding.bind_route_entry", side_effect=_bind):
            result, _api_calls = _run(
                agent,
                [_tool_response("c1"), _Http(401, "invalid api key")],
                counter)
    assert counter["calls"] == 1, "the committed tool action ran again after the provider switch"
    assert len(_tool_rows(result.get("messages") or [])) == 1


def test_failure_during_partial_tool_arguments_never_executes_the_tool():
    with tempfile.TemporaryDirectory() as tmp:
        agent, counter = _make_side_effecting_agent(Path(tmp))
        truncated = _length_tool_response()
        result, _api_calls = _run(agent, [truncated, truncated, truncated, truncated, truncated], counter)
    assert counter["calls"] == 0, "an incomplete tool call was executed"
    assert _tool_rows(result.get("messages") or []) == []


def test_truncation_continuation_after_committed_tool_keeps_single_effect():
    with tempfile.TemporaryDirectory() as tmp:
        agent, counter = _make_side_effecting_agent(Path(tmp))
        # Turn 1: the tool commits. Turn 2's response is truncated mid-text; the
        # truncation machinery appends a continuation nudge and re-asks — without
        # re-issuing the tool.
        result, _api_calls = _run(
            agent,
            [_tool_response("c1"),
             _length_text_response("The answer so far"),
             _text_response("final answer")],
            counter)
    assert counter["calls"] == 1
    assert len(_tool_rows(result.get("messages") or [])) == 1
    # The truncation continuation stitches the fragment and the completion.
    assert "The answer so far" in result["final_response"]
    assert "final answer" in result["final_response"]
