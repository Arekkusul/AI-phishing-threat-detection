import os
import time
import requests
from typing import Dict, Any, Optional

VT_FILE_LOOKUP_URL = "https://www.virustotal.com/api/v3/files/{sha256}"


def virustotal_lookup_file_hash(
    sha256: str,
    *,
    api_key: Optional[str] = None,
    timeout_s: int = 15,
    max_retries_429: int = 4,
    backoff_s: float = 1.5,
) -> Dict[str, Any]:
    """
    Looks up a file hash (sha256) in VirusTotal (VT v3):
      GET /api/v3/files/{sha256}

    - Retries on 429 (rate limit) with exponential backoff
    - Returns raw JSON if found
    - Returns a normalized response when not found (404)

    Env var supported: VIRUSTOTAL_API_KEY
    """
    api_key = api_key or os.getenv("VIRUSTOTAL_API_KEY")
    if not api_key:
        raise RuntimeError("Missing VirusTotal API key (set VIRUSTOTAL_API_KEY)")

    url = VT_FILE_LOOKUP_URL.format(sha256=sha256)
    headers = {"x-apikey": api_key}

    attempt = 0
    while True:
        resp = requests.get(url, headers=headers, timeout=timeout_s)

        # Rate limited: retry like your Tines config
        if resp.status_code == 429 and attempt < max_retries_429:
            sleep_for = backoff_s * (2 ** attempt)
            time.sleep(sleep_for)
            attempt += 1
            continue

        # Not found: VT hasn't seen this hash
        if resp.status_code == 404:
            return {
                "found": False,
                "sha256": sha256,
                "status_code": 404,
                "raw": None,
            }

        # Any other error
        if resp.status_code >= 400:
            raise RuntimeError(f"VT lookup failed [{resp.status_code}]: {resp.text[:300]}")

        # Success
        data = resp.json()
        return {
            "found": True,
            "sha256": sha256,
            "status_code": resp.status_code,
            "raw": data,
        }



#-- EXAMPLE USAGE

#-- from virustotal_check import virustotal_lookup_file_hash

#-- result = virustotal_lookup_file_hash("your_sha256_here")
#-- if result["found"]:
#--    vt_raw = result["raw"]

