"""The built-in, no-code agent: read one event, follow the manifest's `instructions`, publish one event.

It asks the model only for the output fields the input event doesn't already carry (so ids, titles and urls are copied,
never re-typed by a small model), checks the reply is a JSON object with those fields, and retries once if not.
"""
import json
import re

import harness


def parse_json_object(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1]):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def main() -> None:
    ev = harness.event()
    payload = ev["payload"]
    caps = harness.capabilities()
    topic = caps["writes_topics"][0]
    model = caps["models"][0]
    schema = harness.output_schema()
    props = schema.get("properties", {})
    required = [k for k in schema.get("required", []) if k not in payload]
    ask = [k for k in props if k not in payload]
    def describe(k: str) -> str:
        limit = f' (max {props[k]["maxLength"]} characters)' if "maxLength" in props[k] else ""
        return f'- "{k}": {props[k].get("type", "string")}{limit}'

    spec = "\n".join(describe(k) for k in ask)
    prompt = (f"{harness.instructions()}\n\nInput:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
              f"Reply with ONLY a JSON object, no other text. Fields: {', '.join(ask)}\n{spec}")
    out = None
    for attempt in range(2):
        retry = "\n\nYour last reply was not valid JSON. Reply with ONLY the JSON object."
        text = harness.infer(model, prompt if attempt == 0 else prompt + retry, max_tokens=600)
        out = parse_json_object(text)
        if out is not None and all(k in out for k in required):
            break
        out = None
    if out is None:
        raise SystemExit("the model did not return the required JSON fields")
    result = {k: payload[k] for k in props if k in payload}
    for k in ask:
        if k in out:
            v = out[k]
            result[k] = v[: props[k]["maxLength"]] if isinstance(v, str) and "maxLength" in props[k] else v
    harness.publish(topic, result)


main()
