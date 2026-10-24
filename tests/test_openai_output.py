import pytest

from senpilot.openai_output import final_output


def test_structured_answer_ignores_intermediate_commentary() -> None:
    assert (
        final_output(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "Take another action"}],
                    },
                    {"type": "reasoning", "summary": []},
                    {
                        "type": "message",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": '{"action":"stop"}'}],
                    },
                ],
            }
        )
        == '{"action":"stop"}'
    )


@pytest.mark.parametrize(
    "output",
    [
        [],
        [
            {
                "type": "message",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "Only commentary"}],
            }
        ],
        [{"type": "message", "content": [{"type": "refusal", "refusal": "Cannot comply"}]}],
        [{"type": "message", "content": None}],
        [{"type": "message", "content": [{"type": "output_text", "text": 123}]}],
        [
            {
                "type": "message",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "{}"}],
            },
            {
                "type": "message",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "[]"}],
            },
        ],
    ],
)
def test_refusal_malformed_or_ambiguous_outputs_are_not_actions(output: object) -> None:
    with pytest.raises(ValueError):
        final_output({"status": "completed", "output": output})


def test_incomplete_output_cannot_be_executed() -> None:
    with pytest.raises(ValueError):
        final_output(
            {
                "status": "incomplete",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}],
            }
        )


def test_identical_final_messages_describe_one_action() -> None:
    message = {
        "type": "message",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": '{"action":"stop"}'}],
    }
    assert (
        final_output({"status": "completed", "output": [message, message]}) == '{"action":"stop"}'
    )
