"""
Lightweight local fallback for the external ``json-repair`` package.

It covers the subset of behavior used in this repository:
- ``loads(text)`` for slightly malformed JSON / JS object literals
- ``repair_json(text, return_objects=False)`` compatibility
"""
import ast
import json
import re
from typing import Any


def _strip_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


def _replace_js_literals_for_python(text: str) -> str:
    text = re.sub(r"\btrue\b", "True", text)
    text = re.sub(r"\bfalse\b", "False", text)
    text = re.sub(r"\bnull\b", "None", text)
    text = re.sub(r"\bundefined\b", "None", text)
    return text


def _replace_js_literals_for_json(text: str) -> str:
    return re.sub(r"\bundefined\b", "null", text)


def loads(text: Any) -> Any:
    if text is None or isinstance(text, (dict, list, int, float, bool)):
        return text
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8")
    if not isinstance(text, str):
        return text
    text = text.strip()
    if not text:
        return {}
    candidates = [
        ("json", text),
        ("json", _strip_trailing_commas(_replace_js_literals_for_json(text))),
        ("python", text),
        ("python", _strip_trailing_commas(_replace_js_literals_for_python(text))),
    ]
    last_error = None
    for mode, candidate in candidates:
        try:
            if mode == "json":
                return json.loads(candidate)
            return ast.literal_eval(candidate)
        except Exception as exc:
            last_error = exc
    raise last_error


def repair_json(text: Any, return_objects: bool = False) -> Any:
    obj = loads(text)
    if return_objects:
        return obj
    return json.dumps(obj, ensure_ascii=False)
