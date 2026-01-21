import os
import json
import hashlib
import requests
from typing import List, Dict, Any, Literal, Optional

Verdict = Literal["SAFE", "PHISHING", "SUSPICIOUS"]

# Use 2.5-flash for best quality (has separate quota from 2.0)
# Note: 2.5-flash uses "thinking tokens" so we need higher maxOutputTokens
DEFAULT_MODEL = "gemini-2.5-flash"

# Simple in-memory cache for reasoning results
_reasoning_cache: Dict[str, List[str]] = {}
_CACHE_MAX_SIZE = 100


def _get_cache_key(verdict: str, confidence: float, findings_hash: str) -> str:
    """Generate cache key from verdict, confidence bucket, and findings."""
    # Bucket confidence to improve cache hits (within 5% = same bucket)
    bucket = int(confidence / 5) * 5
    return f"{verdict}:{bucket}:{findings_hash}"


def gemini_explain_reasons(
    *,
    verdict: Verdict,
    confidence_pct: float,
    email_text: str,
    check_findings: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    timeout_s: int = 15,
) -> List[str]:
    """
    Returns 3 concise reasons explaining the phishing verdict.

    OPTIMIZED for token efficiency:
    - Uses check findings instead of full email analysis
    - Minimal prompt with structured output
    - Caches results by findings hash

    Args:
        verdict: SAFE, SUSPICIOUS, or PHISHING
        confidence_pct: 0-100 confidence score
        email_text: Email subject/excerpt for context (kept minimal)
        check_findings: Dict of detection check results (reduces token usage significantly)
        api_key: Gemini API key (or uses GEMINI_API_KEY env var)
        model: Model to use (default: gemini-2.0-flash-lite)
        timeout_s: Request timeout
    """
    api_key = api_key or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing Gemini API key (set GEMINI_API_KEY)")

    # Build compact findings summary for cache key and prompt
    findings_summary = _build_findings_summary(check_findings) if check_findings else ""
    findings_hash = hashlib.md5(findings_summary.encode()).hexdigest()[:8]

    # Check cache
    cache_key = _get_cache_key(verdict, confidence_pct, findings_hash)
    if cache_key in _reasoning_cache:
        return _reasoning_cache[cache_key]

    # Extract only subject line (most relevant for context, minimal tokens)
    subject = ""
    for line in email_text.split("\n")[:10]:
        if line.lower().startswith("subject:"):
            subject = line[8:].strip()[:100]
            break
    if not subject:
        subject = email_text[:100].replace("\n", " ")

    # Optimized prompt: provide findings so Gemini explains them, not re-analyzes
    if findings_summary:
        prompt = f"""Verdict: {verdict} ({confidence_pct:.0f}% confidence)
Subject: {subject}

Detection findings:
{findings_summary}

Write 3 short reasons (1 sentence each) explaining why this email is {verdict.lower()}. Base reasons on the findings above. Return JSON: {{"reasons":["...","...","..."]}}"""
    else:
        # Fallback if no findings provided
        prompt = f"""Verdict: {verdict} ({confidence_pct:.0f}%)
Email: {email_text[:500]}

Write 3 short reasons explaining why. JSON only: {{"reasons":["...","...","..."]}}"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            # 2.5-flash uses ~500-600 "thinking tokens" internally, so we need
            # higher limit even though actual output is only ~80 tokens
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
    }

    resp = requests.post(
        url,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        json=body,
        timeout=timeout_s
    )

    if resp.status_code >= 400:
        raise RuntimeError(f"Gemini request failed [{resp.status_code}]: {resp.text[:200]}")

    data = resp.json()

    # Extract response
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        reasons = parsed.get("reasons", [])
        reasons = [str(r).strip() for r in reasons if str(r).strip()][:3]
    except Exception:
        # Fallback parsing
        text = str(data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", ""))
        reasons = [ln.strip("-•* \t") for ln in text.splitlines() if ln.strip() and not ln.startswith("{")][:3]

    # Pad to 3 reasons if needed
    while len(reasons) < 3:
        reasons.append("")

    # Cache result (with size limit)
    if len(_reasoning_cache) >= _CACHE_MAX_SIZE:
        # Remove oldest entries (simple FIFO)
        keys_to_remove = list(_reasoning_cache.keys())[:_CACHE_MAX_SIZE // 2]
        for k in keys_to_remove:
            del _reasoning_cache[k]
    _reasoning_cache[cache_key] = reasons

    return reasons


def _build_findings_summary(check_findings: Dict[str, Any]) -> str:
    """
    Build a compact summary of detection findings for the prompt.
    This is much more token-efficient than sending the full email.
    """
    lines = []

    for name, result in check_findings.items():
        if result is None:
            continue

        # Handle CheckResult dataclass or dict
        if hasattr(result, 'score'):
            score = result.score
            passed = result.passed
            details = result.details or {}
            error = result.error
        else:
            score = result.get('score')
            passed = result.get('passed')
            details = result.get('details', {})
            error = result.get('error')

        if error:
            continue  # Skip errored checks

        if score is None:
            continue

        # Only include failed checks or high-score detections (>30)
        if passed and score < 30:
            continue

        # Build compact finding line
        finding = f"- {name}: score={score:.0f}"

        # Add relevant details compactly
        if name == "bert_model" and details.get("label"):
            finding += f" ({details['label']})"
        elif name == "sublime" and details.get("verdict"):
            finding += f" ({details['verdict']})"
            if details.get("top_signals"):
                signals = [s.get("category", s) if isinstance(s, dict) else s
                          for s in details["top_signals"][:2]]
                finding += f" [{', '.join(signals)}]"
        elif name == "header_mismatch" and details.get("mismatch"):
            finding += f" (from≠reply-to)"
        elif name == "urgency_keywords" and details.get("found_phrases"):
            phrases = details["found_phrases"][:3]
            finding += f" [{', '.join(phrases)}]"
        elif name == "shortened_urls" and details.get("shortened_domains"):
            finding += f" [{', '.join(details['shortened_domains'][:2])}]"
        elif name == "suspicious_tlds" and details.get("suspicious_domains"):
            finding += f" [{', '.join(details['suspicious_domains'][:2])}]"
        elif name == "domain_age" and details.get("new_domains"):
            finding += f" [new: {', '.join(details['new_domains'][:2])}]"
        elif name == "dns_records" and not passed:
            finding += " (no valid DNS)"
        elif name == "spf_record" and not passed:
            finding += " (no SPF record)"
        elif name == "urlscan" and details.get("malicious"):
            finding += " (malicious URL detected)"
        elif name == "virustotal":
            if details.get("malicious_count", 0) > 0:
                finding += f" ({details['malicious_count']} VT detections)"
            if details.get("dangerous_extensions"):
                exts = details["dangerous_extensions"][:2]
                finding += f" [dangerous: {', '.join(exts)}]"
        elif name == "html_threats":
            threats = details.get("threats_found", [])
            if threats:
                finding += f" [{', '.join(threats[:2])}]"

        lines.append(finding)

    return "\n".join(lines) if lines else "No significant findings"


def clear_reasoning_cache():
    """Clear the reasoning cache (useful for testing)."""
    global _reasoning_cache
    _reasoning_cache = {}