# external_checks.py
import os
import time
import requests
from typing import Dict, Any, Optional

URLSCAN_SUBMIT_URL = "https://urlscan.io/api/v1/scan/"
URLSCAN_RESULT_URL = "https://urlscan.io/api/v1/result/{uuid}/"


def urlscan_check_url(
    url: str,
    *,
    api_key: Optional[str] = None,
    visibility: str = "public",   # use "private" if you have enterprise + want private scans
    timeout_s: int = 15,
    poll_interval_s: float = 1.5,
) -> Dict[str, Any]:
    """
    Minimal urlscan.io check:
    1) Submit scan
    2) Poll until the result is ready (404 means "not ready yet")
    3) Return uuid + result JSON

    Requires URLSCAN_API_KEY in environment (or passed in).
    """
    api_key = api_key or os.getenv("URLSCAN_API_KEY")
    if not api_key:
        raise RuntimeError("Missing urlscan API key (set URLSCAN_API_KEY)")

    # ---- 1) submit
    submit_headers = {
        "API-Key": api_key,
        "Content-Type": "application/json",
    }

    submit_payload = {
        "url": url,
        "visibility": visibility,  # urlscan documented field
    }

    r = requests.post(URLSCAN_SUBMIT_URL, headers=submit_headers, json=submit_payload, timeout=10)
    if r.status_code >= 400:
        raise RuntimeError(f"urlscan submit failed [{r.status_code}]: {r.text[:300]}")

    submit_json = r.json()
    uuid = submit_json.get("uuid")
    if not uuid:
        raise RuntimeError("urlscan submit did not return a uuid")

    # ---- 2) poll for result
    result_headers = {"API-Key": api_key}
    deadline = time.time() + timeout_s
    last_status = None

    while time.time() < deadline:
        rr = requests.get(URLSCAN_RESULT_URL.format(uuid=uuid), headers=result_headers, timeout=10)
        last_status = rr.status_code

        if rr.status_code == 404:
            time.sleep(poll_interval_s)
            continue

        if rr.status_code >= 400:
            raise RuntimeError(f"urlscan result failed [{rr.status_code}]: {rr.text[:300]}")

        return {
            "uuid": uuid,
            "result": rr.json(),
        }

    raise TimeoutError(f"urlscan result not ready within {timeout_s}s (last_status={last_status})")



# EXAMPLE USAGE
# from url_check import urlscan_check_url
# result = urlscan_check_url("https://example.com", api_key=os.getenv("URLSCAN_API_KEY"))
# print(result)
