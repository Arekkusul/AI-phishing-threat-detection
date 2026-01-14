import requests
from typing import Dict, Any, Optional

SUBLIME_ATTACK_SCORE_URL = "https://analyzer.sublime.security/v0/messages/attack_score"


def sublime_attack_score(
    raw_message_rfc822: str,
    *,
    timeout_s: int = 20,
    raise_for_http_errors: bool = True
) -> Dict[str, Any]:
    """
    Equivalent to your Tines HTTP request:

      POST https://analyzer.sublime.security/v0/messages/attack_score
      JSON: { "raw_message": "<rfc822>" }

    Returns the raw JSON response from Sublime.
    """
    payload = {"raw_message": raw_message_rfc822}

    resp = requests.post(
        SUBLIME_ATTACK_SCORE_URL,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=timeout_s
    )

    # Minimal + predictable error handling
    if raise_for_http_errors and resp.status_code >= 400:
        raise RuntimeError(f"Sublime attack_score failed [{resp.status_code}]: {resp.text[:300]}")

    # Some endpoints might return non-json errors; handle gracefully
    try:
        data = resp.json()
    except Exception:
        data = {"error": "Non-JSON response", "status_code": resp.status_code, "text": resp.text[:500]}

    return data



#-- EXAMPLE USAGE

#-- from sublime_security_check import sublime_attack_score
#--score_result = sublime_attack_score(raw_message_rfc822=email_text)
