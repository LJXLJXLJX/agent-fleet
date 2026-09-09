"""Best-effort text filtering inspired by AvaCore's web tool policies."""

import re
import unicodedata
from urllib.parse import unquote

from config import BLOCKED_TERMS, BLOCKED_URLS, LEAK_PROFILES

BLOCKED = "Content unavailable under the research policy. Try a different source or query."
GUIDANCE = (
    " Never search for benchmark names, datasets, answer keys or the question verbatim."
    " A blocked result is not evidence of relevance; use a different source or query."
)


def _normalize(value):
    return unicodedata.normalize("NFKC", unquote(value)).casefold().replace("\x00", "")


def _ngrams(value, size):
    words = re.findall(r"\w+", _normalize(value))
    return {tuple(words[i : i + size]) for i in range(len(words) - size + 1)}


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _text([*value.keys(), *value.values()])
    return "\n".join(map(_text, value)) if isinstance(value, list) else ""


class LeakPolicy:
    def __init__(self, profile, instruction):
        self.question_size, self.query_size = LEAK_PROFILES[profile]
        question = instruction.partition("\n\nQuestion:\n")[2] or instruction
        if not question.strip():
            raise ValueError("Leak filtering requires the public task instruction")
        self.question = _ngrams(question, self.question_size)

    def blocked_request(self, arguments):
        value = _normalize(_text(arguments))
        compact = re.sub(r"[\W_]+", "", value)
        return any(
            _normalize(term) in value or re.sub(r"[\W_]+", "", _normalize(term)) in compact
            for term in (*BLOCKED_TERMS, *BLOCKED_URLS) if term
        )

    def blocked_response(self, response, query=""):
        content = response.get("result", {}).get("content", [])
        # The proxy cannot inspect binary resources. Never forward unchecked content.
        if any(item.get("type") != "text" for item in content):
            return True
        text = _text(response)
        return bool(
            self.blocked_request(response)
            or self.question & _ngrams(text, self.question_size)
            or _ngrams(query, self.query_size) & _ngrams(text, self.query_size)
        )

    @staticmethod
    def response(request):
        return {
            "jsonrpc": "2.0", "id": request.get("id"),
            "result": {"isError": True, "content": [{"type": "text", "text": BLOCKED}]},
        }
