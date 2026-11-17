"""A mid-answer provider death with visible content committed must not be replayed.

When the stream ends without a real finish_reason (PARTIAL_STREAM_STUB_ID) with
visible text already delivered to the user, the truncation recovery used to
continue the answer with fresh requests — duplicating delivered output. The
turn now ends as failed with the delivered partial retained instead.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from hermes_constants import PARTIAL_STREAM_STUB_ID
from hermes_constants import FINISH_REASON_LENGTH
from agent.turn_truncation import recover_from_truncation


def _agent():
    agent = MagicMock()
    agent._current_streamed_assistant_text = "The report body already shown."
    return agent


def _response():
    return SimpleNamespace(
        id=PARTIAL_STREAM_STUB_ID,
        _dropped_tool_names=None,
        choices=[SimpleNamespace(
            index=0,
            message=SimpleNamespace(role="assistant", content="The report body already shown.",
                                    tool_calls=None, reasoning_content=None),
            finish_reason=FINISH_REASON_LENGTH,
        )],
    )


def test_partial_stub_with_visible_text_ends_turn_without_continuation():
    agent = _agent()
    messages = [
        {"role": "user", "content": "write the report"},
        {"role": "assistant", "content": "The report body already shown.",
         "_length_continuation_fragment": True},
    ]
    verdict = recover_from_truncation(
        agent, _response(), FINISH_REASON_LENGTH, MagicMock(),
        messages=messages, conversation_history=None, api_kwargs={},
        api_call_count=1, effective_task_id=None, current_turn_user_idx=0,
        length_continue_retries=0, truncated_response_parts=[],
        truncated_tool_call_retries=0, retry_count=0, compression_attempts=0,
    )
    result = verdict.result or {}
    assert verdict.action == "return"
    assert result.get("failed") is True
    assert result.get("failure_reason") == "stream_interrupted"
    # No continuation fragment or nudge was appended to the transcript.
    assert not any(m.get("_length_continuation_nudge") for m in messages)
    assert len(messages) == 2
