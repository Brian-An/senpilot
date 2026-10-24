"""Read a completed structured answer without interpreting assistant commentary as data."""


def final_output(result: object) -> str:
    if not isinstance(result, dict) or result.get("status") != "completed":
        raise ValueError("model response is incomplete")
    output = result.get("output")
    if not isinstance(output, list):
        raise ValueError("model response has no output")
    messages = [
        item
        for item in output
        if isinstance(item, dict)
        and item.get("type") == "message"
        and item.get("phase") != "commentary"
    ]
    explicit = [item for item in messages if item.get("phase") == "final_answer"]
    answers = explicit or messages
    if not answers:
        raise ValueError("model response has no final answer")
    texts = []
    for answer in answers:
        content = answer.get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise ValueError("model response has ambiguous final content")
        part = content[0]
        if (
            not isinstance(part, dict)
            or part.get("type") != "output_text"
            or not isinstance(part.get("text"), str)
        ):
            raise ValueError("model declined the structured answer")
        texts.append(part["text"])
    if len(set(texts)) != 1:
        raise ValueError("model response has conflicting final answers")
    return texts[0]
