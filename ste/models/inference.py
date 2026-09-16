import json
import re
import time

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

JUDGE_SYSTEM = (
    "You score technical writing against ASD-STE100 Simplified Technical English. "
    "You see one passage at a time with no information about its origin. "
    "Return ONLY a JSON object, no prose and no code fences, with keys "
    "approved_vocabulary, one_idea_per_sentence, sentence_length, active_voice, "
    "present_tense, noun_cluster_limit, overall, each scored 0.0 to 1.0."
)


def call_model(
    model,
    system_text,
    messages,
    api_key,
    max_tokens=600,
    temperature=0.7,
    timeout=120,
    max_retries=3,
    retry_backoff=4,
):
    """Calls the OpenRouter API with the given model, messages, and API key."""
    if not api_key:
        raise ValueError("API key is required")

    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system_text}] + messages,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/ste-retention",  # Standard for OpenRouter
        "X-Title": "STE Retention Experiment",
    }
    last = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            data = r.json()
            usage = data.get("usage", {})
            return (
                data["choices"][0]["message"]["content"],
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
            )
        except Exception as exc:
            last = exc
            if attempt < max_retries:
                time.sleep(retry_backoff * attempt)
    raise RuntimeError(f"{model} failed: {last}")


def judge_reply(reply, api_key, judge_model="anthropic/claude-sonnet-4.5"):
    """Scores a reply using a judge model."""
    raw, pt, ct = call_model(
        judge_model,
        JUDGE_SYSTEM,
        [{"role": "user", "content": f"Passage:\n\n{reply}"}],
        api_key=api_key,
        max_tokens=200,
    )
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(cleaned).get("overall"), pt, ct
    except json.JSONDecodeError:
        return None, pt, ct
