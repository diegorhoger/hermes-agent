"""A visible answer already committed to the user must never be replayed.

When the provider's stream breaks mid-response after text was emitted, the old
code retried the same request — duplicating user-visible output and violating
the host's no-replay-after-commit contract. The committed-output guard ends
the turn with the delivered text instead of scheduling another attempt.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from agent.turn_api_error import handle_api_error


class _Agent:
    log_prefix = ""
    verbose = False
    verbose_logging = False
    provider = "openrouter"
    model = "m"
    base_url = "https://openrouter.ai/api/v1"
    _current_streamed_assistant_text = "Committed partial answer visible to the user."
    _fallback_index = 0
    _provider_overflow_unsupported_models: set = set()
    _image_rejecting_models: set = set()

    @staticmethod
    def _strip_think_blocks(text):
        return text

    @staticmethod
    def _has_content_after_think_block(text):
        return bool(str(text or "").strip())

    def _summarize_api_error(self, error):
        return str(error)

    def _persist_session(self, messages, conversation_history):
        return None

    def _recover_with_credential_pool(self, **kwargs):
        return False, kwargs.get("has_retried_429", False)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


_PARTIAL = "Committed partial answer visible to the user."


def _messages():
    return [
        {"role": "user", "content": "write the report"},
        {"role": "assistant", "content": _PARTIAL, "_length_continuation_fragment": True},
        {"role": "user", "content": "continue where you left off", "_length_continuation_nudge": True},
    ]


class _Http(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def test_mid_stream_error_ends_turn_no_retry():
    error = _Http(503, "provider unavailable")
    retry = SimpleNamespace(provider_stale_refresh_attempted=False, primary_recovery_attempted=True,
                            auth_failover_attempted=False, context_overflow_count=0,
                            restart_with_redirected_messages=False, restart_with_rebuilt_messages=False,
                            restart_with_compressed_messages=False, auto_recovery_cycles_used=0)
    messages = _messages()
    verdict = handle_api_error(
        _Agent(), api_error=error, _retry=retry, thinking_spinner=None,
        messages=messages, api_messages=[], api_kwargs=None, system_message=None,
        active_system_prompt="", conversation_history=None, approx_tokens=10,
        retry_count=0, max_retries=3, compression_attempts=0, max_compression_attempts=2,
        api_call_count=1, api_request_id=None, api_start_time=time.time(),
        effective_task_id="t", turn_id="turn", current_turn_user_idx=0)
    assert verdict.action == "return"
    result = verdict.result
    assert result["failed"] is True
    assert result["failure_reason"] == "stream_interrupted"
    assert result["failure_retryable"] is False
    assert result["partial"] is True
    # The delivered partial is retained for the user; nothing is replayed.
    assert _PARTIAL in result["final_response"]


def test_pre_commit_error_still_retries_normally():
    """A 429 that never started content remains retryable (no committed output)."""
    error = _Http(429, "rate limited")

    class _FreshAgent(_Agent):
        _current_streamed_assistant_text = ""

    agent = _FreshAgent()
    retry = SimpleNamespace(provider_stale_refresh_attempted=False, primary_recovery_attempted=True,
                            auth_failover_attempted=False, context_overflow_count=0,
                            restart_with_redirected_messages=False, restart_with_rebuilt_messages=False,
                            restart_with_compressed_messages=False, auto_recovery_cycles_used=0,
                            has_retried_429=False)
    messages = [{"role": "user", "content": "hello"}]
    verdict = handle_api_error(
        agent, api_error=error, _retry=retry, thinking_spinner=None,
        messages=messages, api_messages=[], api_kwargs=None, system_message=None,
        active_system_prompt="", conversation_history=None, approx_tokens=10,
        retry_count=1, max_retries=3, compression_attempts=0, max_compression_attempts=2,
        api_call_count=2, api_request_id=None, api_start_time=time.time(),
        effective_task_id="t", turn_id="turn", current_turn_user_idx=0)
    # The committed-output guard must not fire pre-commit; the normal 429
    # recovery machinery owns the failure from here.
    assert not (verdict.action == "return" and verdict.result
                and verdict.result.get("failure_reason") == "stream_interrupted")
