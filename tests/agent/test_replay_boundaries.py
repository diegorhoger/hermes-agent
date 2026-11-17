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
