"""
Unified detection pipeline that runs all phishing checks in parallel
and aggregates results into a final verdict.
"""

import os
import re
import base64
import hashlib
import concurrent.futures
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Literal
from email import message_from_string
import email.utils

import dns.resolver
import whois
from urllib.parse import urlparse

# Import detection modules
from gemini_check import gemini_explain_reasons
from sublime_check import sublime_attack_score
from virustotal_check import virustotal_lookup_file_hash
from url_check import urlscan_check_url


Verdict = Literal["SAFE", "SUSPICIOUS", "PHISHING"]


@dataclass
class CheckResult:
    """Result from a single check module."""
    name: str
    score: Optional[float] = None  # 0-100 scale
    passed: Optional[bool] = None
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class PipelineResult:
    """Aggregated result from all detection checks."""
    verdict: Verdict
    confidence: float  # 0-100
    ai_score: Optional[float] = None
    sublime_score: Optional[float] = None
    reasons: List[str] = field(default_factory=list)
    indicators: List[str] = field(default_factory=list)
    check_results: Dict[str, CheckResult] = field(default_factory=dict)
    email_hash: Optional[str] = None


class DetectionPipeline:
    """
    Orchestrates phishing detection checks in parallel and aggregates results.
    """

    # Urgency phrases that indicate social engineering
    URGENT_PHRASES = [
        "urgent", "immediately", "account locked", "verify now",
        "reset your password", "security alert", "suspended",
        "unauthorized", "confirm your identity", "within 24 hours",
        "action required", "verify your account", "click here now"
    ]

    # Known URL shorteners
    SHORTENERS = ['bit.ly', 't.co', 'tinyurl.com', 'goo.gl', 'ow.ly',
                  'is.gd', 'buff.ly', 'adf.ly', 'cutt.ly', 'rb.gy']

    # Suspicious TLDs often used in phishing
    SUSPICIOUS_TLDS = ['.xyz', '.top', '.club', '.work', '.click',
                       '.loan', '.win', '.gq', '.ml', '.ga', '.cf']

    def __init__(self, timeout_seconds: int = 30, max_workers: int = 8):
        self.timeout = timeout_seconds
        self.max_workers = max_workers
        self._executor = None

        # Load HuggingFace model lazily
        self._phish_pipeline = None
        self._model_loaded = False

    def _get_phish_model(self):
        """Lazy load the HuggingFace phishing model."""
        if not self._model_loaded:
            from transformers import AutoTokenizer, AutoModelForSequenceClassification, pipeline
            tokenizer = AutoTokenizer.from_pretrained("ElSlay/BERT-Phishing-Email-Model")
            model = AutoModelForSequenceClassification.from_pretrained("ElSlay/BERT-Phishing-Email-Model")
            self._phish_pipeline = pipeline(
                "text-classification",
                model=model,
                tokenizer=tokenizer,
                truncation=True,
                max_length=512
            )
            self._model_loaded = True
        return self._phish_pipeline

    # Dangerous file extensions that are commonly used in phishing
    DANGEROUS_EXTENSIONS = {
        '.exe', '.scr', '.bat', '.cmd', '.com', '.pif', '.msi', '.msp',
        '.dll', '.vbs', '.vbe', '.js', '.jse', '.wsf', '.wsh', '.ps1',
        '.jar', '.hta', '.cpl', '.reg', '.lnk', '.iso', '.img',
        '.docm', '.xlsm', '.pptm', '.dotm', '.xltm', '.potm',  # Macro-enabled Office
        '.zip', '.rar', '.7z', '.tar', '.gz',  # Archives (can hide malware)
    }

    def parse_email(self, raw_email: str) -> Dict[str, Any]:
        """Parse raw email or EML content into structured data including attachments."""
        # Handle base64-encoded EML from taskpane
        if raw_email.startswith("__BASE64_EML__:"):
            try:
                b64_data = raw_email.split(":", 1)[1]
                raw_email = base64.b64decode(b64_data).decode("utf-8", errors="replace")
            except Exception:
                pass  # Fall through to parse as-is

        msg = message_from_string(raw_email)

        # Extract headers
        from_header = msg.get("From", "")
        to_header = msg.get("To", "")
        subject = msg.get("Subject", "")
        reply_to = msg.get("Reply-To", "")
        return_path = msg.get("Return-Path", "")
        message_id = msg.get("Message-ID", "")

        # Extract sender email
        from_addr = email.utils.parseaddr(from_header)[1]
        reply_addr = email.utils.parseaddr(reply_to)[1] if reply_to else ""

        # Get body and attachments
        body_text = ""
        body_html = ""
        attachments = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))

                # Check if this is an attachment
                if "attachment" in content_disposition or part.get_filename():
                    attachment = self._extract_attachment(part)
                    if attachment:
                        attachments.append(attachment)
                elif content_type == "text/plain" and not body_text:
                    try:
                        body_text = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    except:
                        body_text = str(part.get_payload())
                elif content_type == "text/html" and not body_html:
                    try:
                        body_html = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    except:
                        body_html = str(part.get_payload())
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body_text = payload.decode("utf-8", errors="replace")
            else:
                body_text = str(msg.get_payload())

        # If no plain text, strip HTML
        if not body_text and body_html:
            body_text = re.sub(r'<[^>]+>', ' ', body_html)
            body_text = re.sub(r'\s+', ' ', body_text).strip()

        return {
            "raw": raw_email,
            "from": from_header,
            "from_addr": from_addr,
            "to": to_header,
            "subject": subject,
            "reply_to": reply_addr,
            "return_path": return_path,
            "message_id": message_id,
            "body_text": body_text,
            "body_html": body_html,
            "full_text": f"Subject: {subject}\n\n{body_text}",
            "attachments": attachments
        }

    def _extract_attachment(self, part) -> Optional[Dict[str, Any]]:
        """Extract attachment metadata and compute hashes."""
        try:
            filename = part.get_filename() or "unknown"
            content_type = part.get_content_type()
            payload = part.get_payload(decode=True)

            if not payload:
                return None

            # Compute hashes
            sha256_hash = hashlib.sha256(payload).hexdigest()
            md5_hash = hashlib.md5(payload).hexdigest()

            # Get file extension
            ext = ""
            if "." in filename:
                ext = "." + filename.rsplit(".", 1)[1].lower()

            # Check if extension is dangerous
            is_dangerous = ext in self.DANGEROUS_EXTENSIONS

            return {
                "filename": filename,
                "content_type": content_type,
                "size": len(payload),
                "sha256": sha256_hash,
                "md5": md5_hash,
                "extension": ext,
                "is_dangerous_extension": is_dangerous
            }
        except Exception:
            return None

    def extract_urls(self, text: str) -> List[str]:
        """Extract all URLs from text."""
        return re.findall(r'https?://[^\s<>"\')]+', text)

    def extract_domains(self, urls: List[str]) -> List[str]:
        """Extract unique domains from URLs."""
        domains = []
        for url in urls:
            try:
                parsed = urlparse(url).netloc.lower().strip()
                parsed = parsed.rstrip("=,.;")
                if parsed.startswith("www."):
                    parsed = parsed[4:]
                if parsed and parsed not in domains:
                    domains.append(parsed)
            except:
                pass
        return domains

    # --- Individual Check Methods ---

    def check_bert_model(self, email_data: Dict) -> CheckResult:
        """Run HuggingFace BERT phishing model."""
        try:
            model = self._get_phish_model()
            text = email_data["full_text"][:2048]  # Truncate for model

            result = model(text)[0]
            # LABEL_0 = legit, LABEL_1 = phishing
            is_phishing = result["label"] == "LABEL_1"
            score = result["score"] * 100

            if not is_phishing:
                score = 100 - score  # Invert for legit classification

            return CheckResult(
                name="bert_model",
                score=score,
                passed=not is_phishing,
                details={"label": result["label"], "raw_score": result["score"]}
            )
        except Exception as e:
            return CheckResult(name="bert_model", error=str(e))

    def check_header_mismatch(self, email_data: Dict) -> CheckResult:
        """Check for From/Reply-To header mismatch."""
        from_addr = email_data.get("from_addr", "").lower()
        reply_addr = email_data.get("reply_to", "").lower()

        mismatch = bool(from_addr and reply_addr and from_addr != reply_addr)

        return CheckResult(
            name="header_mismatch",
            passed=not mismatch,
            score=80 if mismatch else 0,
            details={"from": from_addr, "reply_to": reply_addr, "mismatch": mismatch}
        )

    def check_urgency_keywords(self, email_data: Dict) -> CheckResult:
        """Detect urgency/pressure language."""
        text = email_data["full_text"].lower()
        found = [phrase for phrase in self.URGENT_PHRASES if phrase in text]

        score = min(len(found) * 15, 70)  # Cap at 70

        return CheckResult(
            name="urgency_keywords",
            passed=len(found) == 0,
            score=score,
            details={"found_phrases": found, "count": len(found)}
        )

    def check_shortened_urls(self, email_data: Dict) -> CheckResult:
        """Detect URL shorteners in email."""
        urls = self.extract_urls(email_data["body_text"] + " " + email_data.get("body_html", ""))
        domains = self.extract_domains(urls)

        shortened = [d for d in domains if any(s in d for s in self.SHORTENERS)]

        return CheckResult(
            name="shortened_urls",
            passed=len(shortened) == 0,
            score=60 if shortened else 0,
            details={"shortened_domains": shortened, "all_domains": domains}
        )

    def check_suspicious_tlds(self, email_data: Dict) -> CheckResult:
        """Check for suspicious TLDs."""
        urls = self.extract_urls(email_data["body_text"] + " " + email_data.get("body_html", ""))
        domains = self.extract_domains(urls)

        suspicious = [d for d in domains if any(d.endswith(tld) for tld in self.SUSPICIOUS_TLDS)]

        return CheckResult(
            name="suspicious_tlds",
            passed=len(suspicious) == 0,
            score=50 if suspicious else 0,
            details={"suspicious_domains": suspicious}
        )

    def check_html_threats(self, email_data: Dict) -> CheckResult:
        """Analyze HTML body for phishing threats.

        Checks for:
        - Hidden/invisible elements (display:none, tiny fonts, etc.)
        - Mismatched link text vs href (shows paypal.com, links to malicious.xyz)
        - JavaScript/event handlers
        - Form submissions to external URLs
        - Data URIs (can hide malicious content)
        - IP address URLs
        - Excessive URL encoding
        """
        html = email_data.get("body_html", "")
        if not html or len(html) < 50:
            return CheckResult(
                name="html_threats",
                passed=True,
                score=0,
                details={"skipped": True, "reason": "No HTML body"}
            )

        threats = []
        threat_score = 0

        # 1. Check for hidden/invisible elements
        hidden_patterns = [
            (r'display\s*:\s*none', "Hidden element (display:none)"),
            (r'visibility\s*:\s*hidden', "Hidden element (visibility:hidden)"),
            (r'font-size\s*:\s*[01]px', "Tiny font (0-1px)"),
            (r'opacity\s*:\s*0[^.]', "Invisible element (opacity:0)"),
            (r'height\s*:\s*[01]px', "Tiny element (1px height)"),
            (r'width\s*:\s*[01]px', "Tiny element (1px width)"),
            (r'position\s*:\s*absolute[^>]*left\s*:\s*-\d{3,}', "Off-screen element"),
        ]
        for pattern, desc in hidden_patterns:
            if re.search(pattern, html, re.IGNORECASE):
                threats.append(desc)
                threat_score += 15

        # 2. Check for JavaScript/event handlers
        js_patterns = [
            (r'<script[^>]*>', "Embedded JavaScript"),
            (r'javascript:', "JavaScript URL"),
            (r'on(click|load|error|mouseover|mouseout|focus|blur)\s*=', "Event handler"),
            (r'eval\s*\(', "eval() usage"),
            (r'document\.(write|cookie|location)', "DOM manipulation"),
        ]
        for pattern, desc in js_patterns:
            if re.search(pattern, html, re.IGNORECASE):
                threats.append(desc)
                threat_score += 25

        # 3. Check for form submissions
        form_match = re.search(r'<form[^>]*action\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
        if form_match:
            action_url = form_match.group(1)
            if action_url.startswith('http'):
                threats.append(f"Form submits to external URL")
                threat_score += 20

        # 4. Check for data URIs (can hide malicious content)
        if re.search(r'data:[^;]+;base64,', html, re.IGNORECASE):
            threats.append("Data URI detected (can hide malicious content)")
            threat_score += 15

        # 5. Check for mismatched link text vs href
        link_pattern = r'<a[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>([^<]+)</a>'
        for match in re.finditer(link_pattern, html, re.IGNORECASE):
            href = match.group(1).lower()
            text = match.group(2).lower().strip()

            # Skip if text is not a domain-like string
            if not re.match(r'^[a-z0-9][-a-z0-9.]*\.[a-z]{2,}', text):
                continue

            # Extract domain from href
            href_domain = ""
            href_match = re.search(r'https?://([^/]+)', href)
            if href_match:
                href_domain = href_match.group(1).lower()
                if href_domain.startswith('www.'):
                    href_domain = href_domain[4:]

            # Check if link text looks like a domain but doesn't match href
            text_domain = text.replace('www.', '')
            if href_domain and text_domain != href_domain:
                # Check if it's a subdomain match
                if not href_domain.endswith('.' + text_domain) and not text_domain.endswith('.' + href_domain):
                    threats.append(f"Link text mismatch: '{text}' links to '{href_domain}'")
                    threat_score += 30

        # 6. Check for IP address URLs
        if re.search(r'https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', html):
            threats.append("URL with IP address instead of domain")
            threat_score += 20

        # 7. Check for excessive URL encoding
        encoded_count = len(re.findall(r'%[0-9a-fA-F]{2}', html))
        if encoded_count > 20:
            threats.append(f"Excessive URL encoding ({encoded_count} encoded chars)")
            threat_score += 10

        # 8. Check for tracking pixels (1x1 images)
        if re.search(r'<img[^>]*(width|height)\s*=\s*["\']?1["\']?[^>]*(width|height)\s*=\s*["\']?1', html, re.IGNORECASE):
            threats.append("Tracking pixel detected (1x1 image)")
            threat_score += 5

        # Cap score at 100
        threat_score = min(threat_score, 100)

        return CheckResult(
            name="html_threats",
            passed=len(threats) == 0,
            score=threat_score,
            details={
                "threats_found": threats[:10],  # Limit to 10 for readability
                "threat_count": len(threats)
            }
        )

    def check_dns_records(self, email_data: Dict) -> CheckResult:
        """Verify sender domain has valid DNS records."""
        from_addr = email_data.get("from_addr", "")
        if "@" not in from_addr:
            return CheckResult(name="dns_records", passed=True, score=0, details={"skipped": True})

        domain = from_addr.split("@")[1].lower()

        try:
            resolver = dns.resolver.Resolver()
            resolver.nameservers = ['8.8.8.8', '8.8.4.4']
            resolver.timeout = 5
            resolver.lifetime = 5

            has_mx = False
            has_a = False

            try:
                resolver.resolve(domain, 'MX')
                has_mx = True
            except:
                pass

            try:
                resolver.resolve(domain, 'A')
                has_a = True
            except:
                pass

            valid = has_mx or has_a
            return CheckResult(
                name="dns_records",
                passed=valid,
                score=0 if valid else 40,
                details={"domain": domain, "has_mx": has_mx, "has_a": has_a}
            )
        except Exception as e:
            return CheckResult(name="dns_records", error=str(e))

    def check_spf_record(self, email_data: Dict) -> CheckResult:
        """Check if sender domain has SPF record."""
        from_addr = email_data.get("from_addr", "")
        if "@" not in from_addr:
            return CheckResult(name="spf_record", passed=True, score=0, details={"skipped": True})

        domain = from_addr.split("@")[1].lower()

        try:
            resolver = dns.resolver.Resolver()
            resolver.nameservers = ['8.8.8.8', '8.8.4.4']
            resolver.timeout = 5

            records = resolver.resolve(domain, 'TXT')
            has_spf = any('v=spf1' in str(r) for r in records)

            return CheckResult(
                name="spf_record",
                passed=has_spf,
                score=0 if has_spf else 25,
                details={"domain": domain, "has_spf": has_spf}
            )
        except Exception as e:
            return CheckResult(name="spf_record", error=str(e), passed=True, score=0)

    def check_domain_age(self, email_data: Dict) -> CheckResult:
        """Check domain age via WHOIS - new domains are suspicious."""
        urls = self.extract_urls(email_data["body_text"])
        domains = self.extract_domains(urls)[:2]  # Limit to 2 domains to reduce timeout risk

        if not domains:
            return CheckResult(name="domain_age", passed=True, score=0, details={"skipped": True})

        new_domains = []
        checked_domains = []

        for domain in domains:
            try:
                # Use a thread with timeout for WHOIS lookup
                import threading
                result_container = {"whois": None, "error": None}

                def whois_lookup():
                    try:
                        result_container["whois"] = whois.whois(domain)
                    except Exception as e:
                        result_container["error"] = str(e)

                thread = threading.Thread(target=whois_lookup)
                thread.daemon = True
                thread.start()
                thread.join(timeout=8)  # 8 second timeout per domain

                if thread.is_alive():
                    # WHOIS lookup timed out
                    continue

                w = result_container["whois"]
                if w and w.creation_date:
                    creation = w.creation_date
                    if isinstance(creation, list):
                        creation = creation[0]
                    from datetime import datetime, timedelta
                    checked_domains.append(domain)
                    if datetime.now() - creation < timedelta(days=30):
                        new_domains.append(domain)
            except:
                pass

        return CheckResult(
            name="domain_age",
            passed=len(new_domains) == 0,
            score=70 if new_domains else 0,
            details={"new_domains": new_domains, "checked": checked_domains}
        )

    def check_sublime_security(self, email_data: Dict) -> CheckResult:
        """Run Sublime Security attack score API."""
        try:
            # Sublime API requires base64-encoded RFC822 message
            encoded_email = base64.b64encode(email_data["raw"].encode()).decode()
            result = sublime_attack_score(
                encoded_email,
                timeout_s=15,
                raise_for_http_errors=False
            )

            if "error" in result:
                return CheckResult(name="sublime", error=result.get("error"))

            # Sublime returns score 0-100 and verdict (malicious/benign)
            score = result.get("score", 0)
            verdict = result.get("verdict", "unknown")
            top_signals = result.get("top_signals", [])

            return CheckResult(
                name="sublime",
                score=score,
                passed=verdict != "malicious" and score < 50,
                details={
                    "score": score,
                    "verdict": verdict,
                    "top_signals": top_signals[:3],
                    "graymail_score": result.get("graymail_score", 0)
                }
            )
        except Exception as e:
            return CheckResult(name="sublime", error=str(e))

    def check_urlscan(self, email_data: Dict) -> CheckResult:
        """Run URLScan.io on URLs in email."""
        urls = self.extract_urls(email_data["body_text"])
        if not urls:
            return CheckResult(name="urlscan", passed=True, score=0, details={"skipped": True})

        api_key = os.getenv("URLSCAN_API_KEY")
        if not api_key:
            return CheckResult(name="urlscan", error="URLSCAN_API_KEY not configured")

        # Only scan first URL to avoid rate limits
        url = urls[0]
        try:
            result = urlscan_check_url(url, api_key=api_key, timeout_s=20)

            verdicts = result.get("result", {}).get("verdicts", {})
            overall = verdicts.get("overall", {})
            malicious = overall.get("malicious", False)
            score_val = overall.get("score", 0)

            return CheckResult(
                name="urlscan",
                score=score_val * 10 if malicious else 0,
                passed=not malicious,
                details={"url": url, "malicious": malicious, "verdicts": verdicts}
            )
        except TimeoutError:
            return CheckResult(name="urlscan", error="Scan timeout", passed=True, score=0)
        except Exception as e:
            return CheckResult(name="urlscan", error=str(e))

    def check_virustotal(self, email_data: Dict) -> CheckResult:
        """Check attachment hashes against VirusTotal.

        Scans actual attachment SHA256 hashes, not email body.
        Also flags dangerous file extensions even if VT has no data.
        """
        attachments = email_data.get("attachments", [])

        if not attachments:
            return CheckResult(
                name="virustotal",
                passed=True,
                score=0,
                details={"skipped": True, "reason": "No attachments"}
            )

        api_key = os.getenv("VIRUSTOTAL_API_KEY")
        if not api_key:
            # Still check for dangerous extensions even without API key
            dangerous_attachments = [a for a in attachments if a.get("is_dangerous_extension")]
            if dangerous_attachments:
                return CheckResult(
                    name="virustotal",
                    passed=False,
                    score=60,
                    details={
                        "skipped_vt": True,
                        "reason": "No API key",
                        "dangerous_extensions": [a["filename"] for a in dangerous_attachments]
                    }
                )
            return CheckResult(
                name="virustotal",
                passed=True,
                score=0,
                details={"skipped": True, "reason": "No API key"}
            )

        # Scan each attachment (limit to 5 to avoid rate limits)
        scan_results = []
        total_malicious = 0
        total_suspicious = 0
        dangerous_extensions = []

        for attachment in attachments[:5]:
            sha256 = attachment.get("sha256")
            filename = attachment.get("filename", "unknown")
            is_dangerous = attachment.get("is_dangerous_extension", False)

            if is_dangerous:
                dangerous_extensions.append(filename)

            if not sha256:
                continue

            try:
                result = virustotal_lookup_file_hash(sha256, api_key=api_key)

                if result["found"]:
                    stats = result.get("raw", {}).get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
                    malicious = stats.get("malicious", 0)
                    suspicious = stats.get("suspicious", 0)

                    total_malicious += malicious
                    total_suspicious += suspicious

                    scan_results.append({
                        "filename": filename,
                        "sha256": sha256[:16] + "...",
                        "found": True,
                        "malicious": malicious,
                        "suspicious": suspicious
                    })
                else:
                    scan_results.append({
                        "filename": filename,
                        "sha256": sha256[:16] + "...",
                        "found": False
                    })
            except Exception as e:
                scan_results.append({
                    "filename": filename,
                    "error": str(e)[:50]
                })

        # Calculate score
        # High score if malicious files found, medium if dangerous extensions
        score = 0
        if total_malicious > 0:
            score = min(100, 70 + (total_malicious * 5))
        elif total_suspicious > 0:
            score = min(60, 30 + (total_suspicious * 5))
        elif dangerous_extensions:
            score = 40  # Dangerous extension but no VT data

        return CheckResult(
            name="virustotal",
            score=score,
            passed=total_malicious == 0 and len(dangerous_extensions) == 0,
            details={
                "attachments_scanned": len(scan_results),
                "total_malicious": total_malicious,
                "total_suspicious": total_suspicious,
                "dangerous_extensions": dangerous_extensions,
                "scan_results": scan_results,
                "malicious_count": total_malicious  # For Gemini summary
            }
        )

    def get_gemini_reasoning(
        self,
        verdict: Verdict,
        confidence: float,
        email_data: Dict,
        check_results: Optional[Dict[str, CheckResult]] = None
    ) -> List[str]:
        """Get AI reasoning from Gemini for the verdict.

        Optimized to pass check findings to Gemini instead of raw email,
        reducing token usage by ~80%.
        """
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return ["AI reasoning unavailable - Gemini API key not configured"]

        try:
            reasons = gemini_explain_reasons(
                verdict=verdict if verdict != "SUSPICIOUS" else "PHISHING",
                confidence_pct=confidence,
                email_text=email_data["full_text"][:500],  # Only need subject/brief context
                check_findings=check_results,  # Pass findings for efficient reasoning
                api_key=api_key
            )
            return [r for r in reasons if r]
        except Exception as e:
            return [f"AI reasoning error: {str(e)}"]

    # --- Main Pipeline ---

    def run(self, raw_email: str) -> PipelineResult:
        """
        Run the full detection pipeline on an email.
        Returns aggregated verdict and all check results.
        """
        # Parse email
        email_data = self.parse_email(raw_email)
        email_hash = hashlib.sha256(raw_email.encode()).hexdigest()[:16]

        # Define checks to run in parallel
        checks = [
            ("bert_model", lambda: self.check_bert_model(email_data)),
            ("header_mismatch", lambda: self.check_header_mismatch(email_data)),
            ("urgency_keywords", lambda: self.check_urgency_keywords(email_data)),
            ("shortened_urls", lambda: self.check_shortened_urls(email_data)),
            ("suspicious_tlds", lambda: self.check_suspicious_tlds(email_data)),
            ("html_threats", lambda: self.check_html_threats(email_data)),
            ("dns_records", lambda: self.check_dns_records(email_data)),
            ("spf_record", lambda: self.check_spf_record(email_data)),
            ("domain_age", lambda: self.check_domain_age(email_data)),
            ("sublime", lambda: self.check_sublime_security(email_data)),
        ]

        # Optional external API checks (controlled via environment variables)
        if os.getenv("ENABLE_URLSCAN", "true").lower() not in ("false", "0", "no"):
            checks.append(("urlscan", lambda: self.check_urlscan(email_data)))

        if os.getenv("ENABLE_VIRUSTOTAL", "true").lower() not in ("false", "0", "no"):
            checks.append(("virustotal", lambda: self.check_virustotal(email_data)))

        # Run checks in parallel
        results: Dict[str, CheckResult] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_name = {
                executor.submit(check_fn): name
                for name, check_fn in checks
            }

            try:
                for future in concurrent.futures.as_completed(future_to_name, timeout=self.timeout):
                    name = future_to_name[future]
                    try:
                        results[name] = future.result()
                    except Exception as e:
                        results[name] = CheckResult(name=name, error=str(e))
            except concurrent.futures.TimeoutError:
                # Some checks timed out - collect results from completed futures
                for future, name in future_to_name.items():
                    if name not in results:
                        if future.done():
                            try:
                                results[name] = future.result(timeout=0)
                            except Exception as e:
                                results[name] = CheckResult(name=name, error=str(e))
                        else:
                            results[name] = CheckResult(name=name, error="Check timed out")

        # Aggregate results
        verdict, confidence = self._aggregate_results(results)

        # Get AI reasoning (pass check results for efficient token usage)
        reasons = self.get_gemini_reasoning(verdict, confidence, email_data, results)

        # Build indicators list
        indicators = self._build_indicators(results)

        # Extract scores for response
        ai_score = results.get("bert_model", CheckResult(name="")).score
        sublime_score = results.get("sublime", CheckResult(name="")).score

        return PipelineResult(
            verdict=verdict,
            confidence=round(confidence, 1),
            ai_score=round(ai_score, 1) if ai_score is not None else None,
            sublime_score=round(sublime_score, 1) if sublime_score is not None else None,
            reasons=reasons,
            indicators=indicators,
            check_results=results,
            email_hash=email_hash
        )

    def _aggregate_results(self, results: Dict[str, CheckResult]) -> tuple[Verdict, float]:
        """
        Aggregate check results into final verdict and confidence.
        Uses weighted scoring with the BERT model as primary signal.
        """
        weights = {
            "bert_model": 0.50,
            "sublime": 0.20,
            "urlscan": 0.05,
            "virustotal": 0.04,
            "header_mismatch": 0.01,
            "urgency_keywords": 0.07,
            "html_threats": 0.01,  # HTML threat analysis
            "shortened_urls": 0.05,
            "suspicious_tlds": 0.02,
            "dns_records": 0.01,
            "spf_record": 0.01,
            "domain_age": 0.03,
        }

        total_weight = 0
        weighted_score = 0

        for name, result in results.items():
            if result.score is not None and name in weights:
                weight = weights[name]
                weighted_score += result.score * weight
                total_weight += weight

        # Normalize
        if total_weight > 0:
            confidence = weighted_score / total_weight
        else:
            confidence = 50  # Uncertain

        # Determine verdict
        if confidence >= 70:
            verdict = "PHISHING"
        elif confidence >= 40:
            verdict = "SUSPICIOUS"
        else:
            verdict = "SAFE"

        return verdict, confidence

    def _build_indicators(self, results: Dict[str, CheckResult]) -> List[str]:
        """Build human-readable list of indicators from check results."""
        indicators = []

        if results.get("header_mismatch") and not results["header_mismatch"].passed:
            indicators.append("From/Reply-To header mismatch detected")

        urgency = results.get("urgency_keywords")
        if urgency and urgency.details.get("found_phrases"):
            phrases = urgency.details["found_phrases"][:3]
            indicators.append(f"Urgency language: {', '.join(phrases)}")

        if results.get("shortened_urls") and not results["shortened_urls"].passed:
            domains = results["shortened_urls"].details.get("shortened_domains", [])
            indicators.append(f"URL shortener detected: {', '.join(domains[:2])}")

        if results.get("suspicious_tlds") and not results["suspicious_tlds"].passed:
            domains = results["suspicious_tlds"].details.get("suspicious_domains", [])
            indicators.append(f"Suspicious TLD: {', '.join(domains[:2])}")

        if results.get("dns_records") and not results["dns_records"].passed:
            indicators.append("Sender domain has no valid DNS records")

        if results.get("spf_record") and not results["spf_record"].passed:
            indicators.append("Sender domain missing SPF record")

        if results.get("domain_age") and not results["domain_age"].passed:
            domains = results["domain_age"].details.get("new_domains", [])
            indicators.append(f"Recently registered domain: {', '.join(domains[:2])}")

        html_result = results.get("html_threats")
        if html_result and not html_result.passed:
            threats = html_result.details.get("threats_found", [])
            if threats:
                indicators.append(f"HTML threats: {', '.join(threats[:2])}")

        urlscan_result = results.get("urlscan")
        if urlscan_result and not urlscan_result.passed:
            url = urlscan_result.details.get("url", "unknown")
            indicators.append(f"URLScan flagged malicious URL: {url[:50]}")

        vt_result = results.get("virustotal")
        if vt_result and not vt_result.passed:
            malicious = vt_result.details.get("total_malicious", 0)
            dangerous_exts = vt_result.details.get("dangerous_extensions", [])

            if malicious > 0:
                indicators.append(f"VirusTotal: {malicious} vendors flagged attachment as malicious")
            if dangerous_exts:
                indicators.append(f"Dangerous attachment type: {', '.join(dangerous_exts[:2])}")

        return indicators


# Singleton instance for reuse
_pipeline_instance: Optional[DetectionPipeline] = None

def get_pipeline() -> DetectionPipeline:
    """Get or create the detection pipeline singleton."""
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = DetectionPipeline()
    return _pipeline_instance
