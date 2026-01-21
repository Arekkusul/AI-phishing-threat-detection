"""
Phishing Detection API Backend

Provides endpoints for:
- POST /check - Analyze email for phishing (taskpane endpoint)
- POST /api/check - Legacy API endpoint
- POST /report - Report phishing with webhook notifications
- GET / - Web UI for manual analysis
- GET /admin - Admin dashboard with scan history and stats
"""

import os
import json
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional

import requests
from flask import Flask, request, jsonify, render_template, g, abort
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

from detection_pipeline import get_pipeline, PipelineResult
from db import get_db, init_db

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Initialize database
db = init_db(app)

# CORS configuration for taskpane
CORS(app, origins=[
    "https://localhost:3000",
    "https://127.0.0.1:3000",
    os.getenv("CLOUDFLARE_TUNNEL_URL", ""),
    os.getenv("ALLOWED_ORIGIN", "*")
])

# Rate limiting configuration
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri=os.getenv("REDIS_URL", "memory://"),
    strategy="fixed-window"
)


# --- Configuration ---

class Config:
    API_KEY = os.getenv("API_KEY", "")  # Optional API key for authentication
    ADMIN_KEY = os.getenv("ADMIN_KEY", "")  # Optional admin key for dashboard

    # Input validation limits
    MAX_EML_SIZE = int(os.getenv("MAX_EML_SIZE", 25 * 1024 * 1024))  # 25MB default
    MAX_SUBJECT_LENGTH = 500
    MAX_EMAIL_LENGTH = 255

    # Caching
    CACHE_ENABLED = os.getenv("CACHE_ENABLED", "true").lower() == "true"
    CACHE_TTL_HOURS = int(os.getenv("CACHE_TTL_HOURS", 24))

    # Webhook URLs for reporting
    TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    WHATSAPP_TWILIO_SID = os.getenv("WHATSAPP_TWILIO_SID", "")
    WHATSAPP_TWILIO_TOKEN = os.getenv("WHATSAPP_TWILIO_TOKEN", "")
    WHATSAPP_FROM = os.getenv("WHATSAPP_FROM", "")
    WHATSAPP_TO = os.getenv("WHATSAPP_TO", "")


# --- Input Validation ---

class ValidationError(Exception):
    """Raised when input validation fails."""
    pass


def validate_eml_input(eml: str) -> str:
    """
    Validate and sanitize EML input.

    Args:
        eml: Raw email content

    Returns:
        Validated EML string

    Raises:
        ValidationError: If input is invalid
    """
    if not eml:
        raise ValidationError("Missing 'eml' field")

    if not isinstance(eml, str):
        raise ValidationError("'eml' must be a string")

    # Check size limit
    eml_size = len(eml.encode('utf-8'))
    if eml_size > Config.MAX_EML_SIZE:
        raise ValidationError(f"Email too large: {eml_size} bytes exceeds {Config.MAX_EML_SIZE} byte limit")

    # Basic sanity check - should contain some email-like content
    stripped = eml.strip()
    if len(stripped) < 10:
        raise ValidationError("Email content too short")

    return stripped


def compute_email_hash(eml: str) -> str:
    """Compute SHA256 hash of email content for deduplication."""
    return hashlib.sha256(eml.encode('utf-8')).hexdigest()


# --- Middleware ---

def require_api_key(f):
    """Optional API key authentication decorator."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if Config.API_KEY:
            auth_header = request.headers.get("Authorization", "")
            api_key = request.headers.get("X-API-Key", "")

            # Check Bearer token or X-API-Key header
            if auth_header.startswith("Bearer "):
                provided_key = auth_header[7:]
            else:
                provided_key = api_key

            if provided_key != Config.API_KEY:
                return jsonify({"error": "Unauthorized"}), 401

        return f(*args, **kwargs)
    return decorated


def require_admin_key(f):
    """Admin authentication decorator for dashboard."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if Config.ADMIN_KEY:
            auth_header = request.headers.get("Authorization", "")
            api_key = request.headers.get("X-Admin-Key", "")
            query_key = request.args.get("admin_key", "")

            # Check various auth methods
            if auth_header.startswith("Bearer "):
                provided_key = auth_header[7:]
            else:
                provided_key = api_key or query_key

            if provided_key != Config.ADMIN_KEY:
                return jsonify({"error": "Admin access required"}), 403

        return f(*args, **kwargs)
    return decorated


def log_request(f):
    """Log incoming requests for audit."""
    @wraps(f)
    def decorated(*args, **kwargs):
        g.request_start = datetime.utcnow()
        logger.info(f"Request: {request.method} {request.path} from {request.remote_addr}")

        response = f(*args, **kwargs)

        duration = (datetime.utcnow() - g.request_start).total_seconds()
        logger.info(f"Response: {request.path} completed in {duration:.2f}s")

        return response
    return decorated


def audit_log(event_type: str):
    """Decorator to log events to audit table."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            response = f(*args, **kwargs)

            # Log to database in background
            try:
                db.log_event(
                    event_type=event_type,
                    event_data={
                        "path": request.path,
                        "method": request.method,
                        "status": response[1] if isinstance(response, tuple) else 200
                    },
                    ip_address=request.remote_addr,
                    user_agent=request.headers.get("User-Agent", "")[:500]
                )
            except Exception as e:
                logger.warning(f"Failed to log audit event: {e}")

            return response
        return decorated
    return decorator


# --- Notification Functions ---

def send_teams_notification(subject: str, verdict: str, confidence: float,
                           reporter: Optional[str] = None) -> bool:
    """Send notification to Microsoft Teams webhook."""
    if not Config.TEAMS_WEBHOOK_URL:
        logger.warning("Teams webhook not configured")
        return False

    try:
        card = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": "FF0000" if verdict == "PHISHING" else "FFA500",
            "summary": f"Phishing Report: {subject[:50]}",
            "sections": [{
                "activityTitle": "Phishing Email Reported",
                "facts": [
                    {"name": "Subject", "value": subject[:100]},
                    {"name": "Verdict", "value": verdict},
                    {"name": "Confidence", "value": f"{confidence:.1f}%"},
                    {"name": "Reported By", "value": reporter or "Unknown"},
                    {"name": "Timestamp", "value": datetime.utcnow().isoformat()}
                ],
                "markdown": True
            }]
        }

        resp = requests.post(Config.TEAMS_WEBHOOK_URL, json=card, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Teams notification failed: {e}")
        return False


def send_telegram_notification(subject: str, verdict: str, confidence: float,
                               reporter: Optional[str] = None) -> bool:
    """Send notification to Telegram bot."""
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured")
        return False

    try:
        message = (
            f"*Phishing Email Reported*\n\n"
            f"*Subject:* {subject[:100]}\n"
            f"*Verdict:* {verdict}\n"
            f"*Confidence:* {confidence:.1f}%\n"
            f"*Reporter:* {reporter or 'Unknown'}\n"
            f"*Time:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )

        url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": Config.TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)

        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Telegram notification failed: {e}")
        return False


def send_whatsapp_notification(subject: str, verdict: str, confidence: float,
                               reporter: Optional[str] = None) -> bool:
    """Send notification via Twilio WhatsApp."""
    if not all([Config.WHATSAPP_TWILIO_SID, Config.WHATSAPP_TWILIO_TOKEN,
                Config.WHATSAPP_FROM, Config.WHATSAPP_TO]):
        logger.warning("WhatsApp not configured")
        return False

    try:
        message = (
            f"Phishing Alert\n\n"
            f"Subject: {subject[:80]}\n"
            f"Verdict: {verdict}\n"
            f"Confidence: {confidence:.1f}%\n"
            f"Reporter: {reporter or 'Unknown'}"
        )

        url = f"https://api.twilio.com/2010-04-01/Accounts/{Config.WHATSAPP_TWILIO_SID}/Messages.json"
        resp = requests.post(url, data={
            "From": f"whatsapp:{Config.WHATSAPP_FROM}",
            "To": f"whatsapp:{Config.WHATSAPP_TO}",
            "Body": message
        }, auth=(Config.WHATSAPP_TWILIO_SID, Config.WHATSAPP_TWILIO_TOKEN), timeout=10)

        return resp.status_code in [200, 201]
    except Exception as e:
        logger.error(f"WhatsApp notification failed: {e}")
        return False


# --- API Endpoints ---

@app.route("/check", methods=["POST"])
@log_request
@limiter.limit("30 per minute")
@require_api_key
@audit_log("email_scan")
def check_email():
    """
    Primary endpoint for taskpane.

    Request:
        POST /check
        Content-Type: application/json
        {
            "eml": "<raw email content or base64-prefixed EML>"
        }

    Response:
        {
            "verdict": "SAFE" | "SUSPICIOUS" | "PHISHING",
            "confidence": 75.5,
            "ai_score": 82.3,
            "sublime_score": 68.0,
            "reasons": ["Urgency language detected", ...],
            "indicators": ["From/Reply-To mismatch", ...],
            "email_hash": "abc123...",
            "cached": false
        }
    """
    try:
        data = request.get_json(force=True)
        eml = data.get("eml", "").strip()

        # Validate input
        try:
            eml = validate_eml_input(eml)
        except ValidationError as e:
            return jsonify({"error": str(e)}), 400

        # Compute hash for caching/deduplication
        email_hash = compute_email_hash(eml)

        # Check cache if enabled
        if Config.CACHE_ENABLED:
            cached = db.get_scan_by_hash(email_hash)
            if cached:
                # Check if cache is still valid
                scanned_at = cached.get("scanned_at")
                if scanned_at:
                    # Handle timezone-aware vs naive datetime comparison
                    now = datetime.now(timezone.utc)
                    if scanned_at.tzinfo is None:
                        scanned_at = scanned_at.replace(tzinfo=timezone.utc)
                    cache_age = now - scanned_at
                else:
                    cache_age = timedelta(hours=0)
                if cache_age < timedelta(hours=Config.CACHE_TTL_HOURS):
                    logger.info(f"Returning cached result for hash {email_hash[:8]}")
                    return jsonify({
                        "verdict": cached["verdict"],
                        "confidence": float(cached["confidence"]),
                        "ai_score": float(cached["ai_score"]) if cached.get("ai_score") else None,
                        "sublime_score": float(cached["sublime_score"]) if cached.get("sublime_score") else None,
                        "reasons": cached.get("reasons", []),
                        "indicators": cached.get("indicators", []),
                        "email_hash": email_hash[:16],
                        "cached": True
                    })

        # Run detection pipeline
        pipeline = get_pipeline()
        result: PipelineResult = pipeline.run(eml)

        # Parse email for metadata
        email_data = pipeline.parse_email(eml)

        # Save to database
        scan_id = db.save_scan(
            email_hash=email_hash,
            verdict=result.verdict,
            confidence=result.confidence,
            ai_score=result.ai_score,
            sublime_score=result.sublime_score,
            reasons=result.reasons,
            indicators=result.indicators,
            check_results=result.check_results,
            email_subject=email_data.get("subject"),
            email_from=email_data.get("from_addr"),
            email_to=email_data.get("to"),
            email_body=eml,
            ip_address=request.remote_addr
        )

        return jsonify({
            "verdict": result.verdict,
            "confidence": result.confidence,
            "ai_score": result.ai_score,
            "sublime_score": result.sublime_score,
            "reasons": result.reasons,
            "indicators": result.indicators,
            "email_hash": email_hash[:16],
            "cached": False,
            "scan_id": scan_id
        })

    except Exception as e:
        logger.exception("Error in /check endpoint")
        return jsonify({"error": str(e)}), 500


@app.route("/api/check", methods=["POST"])
@log_request
@limiter.limit("30 per minute")
@require_api_key
def api_check_legacy():
    """
    Legacy API endpoint - maintains backward compatibility.

    Request:
        POST /api/check
        Content-Type: application/json
        {
            "email_text": "<raw email content>"
        }

    Response:
        {
            "classification": "Legit" | "Phishing",
            "score": 85.5,
            "header_mismatch": false,
            "urgency": true,
            "domains": ["example.com"]
        }
    """
    try:
        data = request.get_json(force=True)
        email_text = data.get("email_text", "").strip()

        if not email_text:
            return jsonify({"error": "Missing 'email_text' field"}), 400

        # Validate size
        if len(email_text.encode('utf-8')) > Config.MAX_EML_SIZE:
            return jsonify({"error": "Email content too large"}), 400

        # Run detection pipeline
        pipeline = get_pipeline()
        result: PipelineResult = pipeline.run(email_text)

        # Map to legacy response format
        classification = "Phishing" if result.verdict == "PHISHING" else "Legit"
        if result.verdict == "SUSPICIOUS":
            classification = "Suspicious"

        # Extract domain info from check results
        shortened = result.check_results.get("shortened_urls")
        domains = shortened.details.get("all_domains", []) if shortened else []

        header_check = result.check_results.get("header_mismatch")
        urgency_check = result.check_results.get("urgency_keywords")

        return jsonify({
            "classification": classification,
            "score": result.confidence,
            "header_mismatch": not header_check.passed if header_check else False,
            "urgency": not urgency_check.passed if urgency_check else False,
            "domains": domains
        })

    except Exception as e:
        logger.exception("Error in /api/check endpoint")
        return jsonify({"error": str(e)}), 500


@app.route("/report", methods=["POST"])
@log_request
@limiter.limit("10 per minute")
@require_api_key
@audit_log("phishing_report")
def report_phishing():
    """
    Report phishing email - triggers webhook notifications.

    Request:
        POST /report
        Content-Type: application/json
        {
            "eml": "<raw email content>",
            "reporter": "user@example.com" (optional)
        }

    Response:
        {
            "success": true,
            "notifications": {
                "teams": true,
                "telegram": false,
                "whatsapp": false
            }
        }
    """
    try:
        data = request.get_json(force=True)
        eml = data.get("eml", "").strip()
        reporter = data.get("reporter", None)

        # Validate input
        try:
            eml = validate_eml_input(eml)
        except ValidationError as e:
            return jsonify({"error": str(e)}), 400

        # Run detection to get verdict info
        pipeline = get_pipeline()
        result: PipelineResult = pipeline.run(eml)

        # Parse email to get subject
        email_data = pipeline.parse_email(eml)
        subject = email_data.get("subject", "No Subject")
        email_hash = compute_email_hash(eml)

        # Send notifications
        notifications = {
            "teams": send_teams_notification(subject, result.verdict, result.confidence, reporter),
            "telegram": send_telegram_notification(subject, result.verdict, result.confidence, reporter),
            "whatsapp": send_whatsapp_notification(subject, result.verdict, result.confidence, reporter)
        }

        any_sent = any(notifications.values())

        # Save report to database
        report_id = db.save_report(
            email_hash=email_hash,
            verdict=result.verdict,
            confidence=result.confidence,
            reporter_email=reporter,
            teams_notified=notifications["teams"],
            telegram_notified=notifications["telegram"],
            whatsapp_notified=notifications["whatsapp"]
        )

        logger.info(f"Report submitted: subject='{subject[:50]}', verdict={result.verdict}, "
                   f"notifications={notifications}")

        return jsonify({
            "success": True,
            "verdict": result.verdict,
            "confidence": result.confidence,
            "notifications": notifications,
            "any_notification_sent": any_sent,
            "report_id": report_id
        })

    except Exception as e:
        logger.exception("Error in /report endpoint")
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
@limiter.exempt
def health_check():
    """Health check endpoint for monitoring."""
    db_status = "connected" if db.is_available else "disconnected"

    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "2.0.0",
        "database": db_status,
        "cache_enabled": Config.CACHE_ENABLED
    })


@app.route("/", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def index():
    """Web UI for manual email analysis."""
    result = None
    error_msg = None
    indicators = []
    reasons = []

    check_results = {}

    if request.method == "POST":
        email_text = request.form.get("email_text", "").strip()

        if not email_text:
            error_msg = "Please paste email content"
        elif len(email_text.encode('utf-8')) > Config.MAX_EML_SIZE:
            error_msg = "Email content too large (max 25MB)"
        else:
            try:
                pipeline = get_pipeline()
                pipeline_result = pipeline.run(email_text)

                result = {
                    "verdict": pipeline_result.verdict,
                    "confidence": pipeline_result.confidence,
                    "ai_score": pipeline_result.ai_score,
                    "sublime_score": pipeline_result.sublime_score
                }
                indicators = pipeline_result.indicators
                reasons = pipeline_result.reasons

                # Convert check_results to serializable format for template
                for name, check in pipeline_result.check_results.items():
                    check_results[name] = {
                        "name": check.name,
                        "score": check.score,
                        "passed": check.passed,
                        "details": check.details,
                        "error": check.error
                    }

                # Save to database
                email_data = pipeline.parse_email(email_text)
                db.save_scan(
                    email_hash=compute_email_hash(email_text),
                    verdict=pipeline_result.verdict,
                    confidence=pipeline_result.confidence,
                    ai_score=pipeline_result.ai_score,
                    sublime_score=pipeline_result.sublime_score,
                    reasons=reasons,
                    indicators=indicators,
                    check_results=pipeline_result.check_results,
                    email_subject=email_data.get("subject"),
                    email_from=email_data.get("from_addr"),
                    email_body=email_text,
                    ip_address=request.remote_addr
                )

            except Exception as e:
                logger.exception("Error in web UI analysis")
                error_msg = f"Analysis error: {str(e)}"

    return render_template(
        "index.html",
        result=result,
        error_msg=error_msg,
        indicators=indicators,
        reasons=reasons,
        check_results=check_results
    )


# --- Admin Dashboard ---

@app.route("/admin")
@require_admin_key
def admin_dashboard():
    """Admin dashboard with scan statistics and history."""
    # Get statistics
    scan_stats = db.get_scan_stats(days=30)
    report_stats = db.get_report_stats(days=30)
    daily_stats = db.get_daily_stats(days=14)
    top_senders = db.get_top_senders(days=30, limit=10)

    return render_template(
        "admin/dashboard.html",
        scan_stats=scan_stats,
        report_stats=report_stats,
        daily_stats=daily_stats,
        top_senders=top_senders,
        db_available=db.is_available
    )


@app.route("/admin/scans")
@require_admin_key
def admin_scans():
    """View recent scans."""
    page = request.args.get("page", 1, type=int)
    per_page = 25
    verdict_filter = request.args.get("verdict")

    scans = db.get_recent_scans(
        limit=per_page,
        offset=(page - 1) * per_page,
        verdict_filter=verdict_filter
    )

    return render_template(
        "admin/scans.html",
        scans=scans,
        page=page,
        per_page=per_page,
        verdict_filter=verdict_filter
    )


@app.route("/admin/scans/<scan_id>")
@require_admin_key
def admin_scan_detail(scan_id: str):
    """View scan details."""
    scan = db.get_scan_by_id(scan_id)
    if not scan:
        abort(404)

    return render_template(
        "admin/scan_detail.html",
        scan=scan
    )


@app.route("/admin/reports")
@require_admin_key
def admin_reports():
    """View phishing reports."""
    page = request.args.get("page", 1, type=int)
    per_page = 25

    reports = db.get_recent_reports(
        limit=per_page,
        offset=(page - 1) * per_page
    )

    return render_template(
        "admin/reports.html",
        reports=reports,
        page=page,
        per_page=per_page
    )


@app.route("/admin/audit")
@require_admin_key
def admin_audit():
    """View audit log."""
    event_type = request.args.get("event_type")

    logs = db.get_audit_log(limit=100, event_type=event_type)

    return render_template(
        "admin/audit.html",
        logs=logs,
        event_type=event_type
    )


# --- API Endpoints for Admin ---

@app.route("/api/admin/stats")
@require_admin_key
def api_admin_stats():
    """Get admin statistics as JSON."""
    return jsonify({
        "scans": db.get_scan_stats(days=30),
        "reports": db.get_report_stats(days=30),
        "daily": db.get_daily_stats(days=14),
        "top_senders": db.get_top_senders(days=30, limit=10)
    })


# --- Error Handlers ---

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api") or request.path.startswith("/admin/api"):
        return jsonify({"error": "Endpoint not found"}), 404
    return render_template("errors/404.html"), 404


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "error": "Rate limit exceeded",
        "message": str(e.description)
    }), 429


@app.errorhandler(500)
def server_error(e):
    logger.exception("Internal server error")
    if request.path.startswith("/api"):
        return jsonify({"error": "Internal server error"}), 500
    return render_template("errors/500.html"), 500


# --- Main ---

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

    logger.info(f"Starting Phishing Detection API on port {port}")
    logger.info(f"API Key auth: {'enabled' if Config.API_KEY else 'disabled'}")
    logger.info(f"Admin Key: {'configured' if Config.ADMIN_KEY else 'not configured'}")
    logger.info(f"Database: {'connected' if db.is_available else 'not available'}")
    logger.info(f"Cache: {'enabled' if Config.CACHE_ENABLED else 'disabled'}")
    logger.info(f"Teams webhook: {'configured' if Config.TEAMS_WEBHOOK_URL else 'not configured'}")
    logger.info(f"Telegram: {'configured' if Config.TELEGRAM_BOT_TOKEN else 'not configured'}")

    app.run(host="0.0.0.0", port=port, debug=debug)
