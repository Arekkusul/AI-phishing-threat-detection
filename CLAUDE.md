# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI-powered phishing detection platform with two components:
- **outlook-phishing-dashboard**: Outlook Add-in (Office.js taskpane) that extracts email data and displays scan results
- **phishing-ai**: Flask backend that runs AI/ML phishing detection and threat intelligence checks in parallel

## Build & Run Commands

### Outlook Add-in (outlook-phishing-dashboard/)
```bash
npm install                    # Install dependencies
npm run dev-server             # Start dev server on https://localhost:3000
npm run build                  # Production build
npm run lint                   # Run ESLint
npm run validate               # Validate manifest.json
npm start                      # Start debugging session in Outlook
```

### Backend (phishing-ai/) - Local Development
```bash
cd phishing-ai
cp .env.example .env           # Create config file
# Edit .env with your API keys
pip install -r requirements.txt
python app.py                  # Start Flask server on port 5000
```

### Docker Deployment (Recommended)
```bash
cp .env.example .env           # Create config at repo root
# Edit .env with your API keys and POSTGRES_PASSWORD

# Build and start all services
docker compose up -d --build

# View logs
docker compose logs -f phishing-api

# Stop services
docker compose down

# Rebuild after code changes
docker compose up -d --build phishing-api
```

## Architecture

```
┌─────────────────────────────┐         HTTPS          ┌─────────────────────────────────┐
│  Outlook Add-in (Taskpane)  │ ──────────────────────►│         Flask Backend           │
│                             │                        │                                 │
│  - Extract email (EML)      │    POST /check         │  ┌─────────────────────────┐   │
│  - Display verdict/scores   │    {eml: "..."}        │  │   Detection Pipeline    │   │
│  - Report phishing button   │◄──────────────────────┐│  │                         │   │
└─────────────────────────────┘   {verdict, scores,   ││  │  Parallel Checks:       │   │
                                   reasons, indicators}││  │  ├─ BERT Model          │   │
                                                       ││  │  ├─ Header Mismatch     │   │
                                                       ││  │  ├─ Urgency Keywords    │   │
                                                       ││  │  ├─ URL Shorteners      │   │
                                                       ││  │  ├─ Suspicious TLDs     │   │
                                                       ││  │  ├─ DNS/SPF Records     │   │
                                                       ││  │  ├─ Domain Age (WHOIS)  │   │
                                                       ││  │  ├─ Sublime Security    │   │
                                                       ││  │  ├─ URLScan.io          │   │
                                                       ││  │  └─ VirusTotal          │   │
                                                       ││  │                         │   │
                                                       ││  │  + Gemini AI Reasoning  │   │
                                                       ││  └─────────────────────────┘   │
                                                       │└─────────────────────────────────┘
                                                       │
                                 POST /report          │  ──► Teams Webhook
                                 {eml, reporter}       │  ──► Telegram Bot
                                                       └──► WhatsApp (Twilio)
```

### Detection Pipeline

Located in `phishing-ai/detection_pipeline.py`. Runs all checks in parallel using `concurrent.futures`:

| Check | Weight | Description |
|-------|--------|-------------|
| BERT Model | 32% | HuggingFace `ElSlay/BERT-Phishing-Email-Model` |
| Sublime Security | 22% | External API attack score |
| Header Mismatch | 8% | From vs Reply-To address comparison |
| Urgency Keywords | 8% | Detects pressure language ("urgent", "verify now", etc.) |
| URLScan.io | 5% | Scans URLs for malicious content (optional) |
| Shortened URLs | 5% | Flags bit.ly, t.co, tinyurl, etc. |
| Suspicious TLDs | 5% | Flags .xyz, .top, .club, etc. |
| DNS Records | 5% | Verifies sender domain has MX/A records |
| SPF Record | 4% | Checks for SPF TXT record |
| VirusTotal | 3% | Hash lookup for known malicious content (optional) |
| Domain Age | 3% | WHOIS check for newly registered domains |

After checks complete, Gemini API generates human-readable reasoning.

### API Endpoints

| Endpoint | Method | Request | Response |
|----------|--------|---------|----------|
| `/check` | POST | `{eml}` | `{verdict, confidence, ai_score, sublime_score, reasons, indicators}` |
| `/api/check` | POST | `{email_text}` | Legacy format: `{classification, score, header_mismatch, urgency, domains}` |
| `/report` | POST | `{eml, reporter?}` | `{success, verdict, notifications: {teams, telegram, whatsapp}}` |
| `/health` | GET | - | `{status, timestamp, version}` |
| `/` | GET/POST | Form | Web UI for manual analysis |

### Verdict Thresholds

Weighted score aggregation determines verdict:
- **SAFE**: < 40% confidence
- **SUSPICIOUS**: 40-70% confidence
- **PHISHING**: ≥ 70% confidence

## Key Files

| File | Purpose |
|------|---------|
| `phishing-ai/app.py` | Flask app, endpoints, webhook notifications |
| `phishing-ai/detection_pipeline.py` | Parallel detection orchestration, result aggregation |
| `phishing-ai/gemini_check.py` | Gemini API for AI reasoning |
| `phishing-ai/sublime_check.py` | Sublime Security attack score API |
| `phishing-ai/virustotal_check.py` | VirusTotal hash lookup |
| `phishing-ai/url_check.py` | URLScan.io URL analysis |
| `phishing-ai/Dockerfile` | Multi-stage Docker build |
| `phishing-ai/init.sql` | PostgreSQL schema (scans, reports, audit) |
| `docker-compose.yml` | Service orchestration (API + PostgreSQL) |
| `.env.example` | Docker environment template |
| `outlook-phishing-dashboard/src/taskpane/taskpane.js` | Email extraction, API calls, UI |
| `outlook-phishing-dashboard/manifest.json` | Office Add-in manifest |

## Configuration

### Environment Variables (phishing-ai/.env)

```bash
# Required for full functionality
GEMINI_API_KEY=           # For AI reasoning
VIRUSTOTAL_API_KEY=       # For hash lookups (optional)
URLSCAN_API_KEY=          # For URL scanning (optional)

# Feature flags for optional checks
ENABLE_URLSCAN=true       # Set to false to disable URLScan.io checks
ENABLE_VIRUSTOTAL=true    # Set to false to disable VirusTotal checks

# Optional API authentication
API_KEY=                  # Set to require Bearer token

# Webhook notifications
TEAMS_WEBHOOK_URL=        # Microsoft Teams incoming webhook
TELEGRAM_BOT_TOKEN=       # Telegram bot token
TELEGRAM_CHAT_ID=         # Telegram chat ID
```

### Taskpane API URL

Set in `outlook-phishing-dashboard/src/taskpane/taskpane.js:2`:
```javascript
const API_BASE = "http://127.0.0.1:5000";
```

For Cloudflare Tunnel, update to your tunnel URL (e.g., `https://phishing-api.example.com`).

### Cloudflare Tunnel Setup

1. Install cloudflared: `brew install cloudflared` or download from cloudflare.com
2. Login: `cloudflared tunnel login`
3. Create tunnel: `cloudflared tunnel create phishing-api`
4. Copy the tunnel token from the Cloudflare dashboard
5. Add to `.env`: `CLOUDFLARE_TUNNEL_TOKEN=your_token`
6. Uncomment the `cloudflared` service in `docker-compose.yml`
7. Configure DNS in Cloudflare dashboard to point to your tunnel

### Database Schema

PostgreSQL tables (auto-created via `init.sql`):
- `scans` - Email scan results with verdict, scores, and check details
- `reports` - User-reported phishing emails with notification status
- `api_keys` - Multi-tenant API key management (future)
- `audit_log` - Request/event logging

## Adding New Detection Checks

1. Add check method in `DetectionPipeline` class (`detection_pipeline.py`)
2. Return `CheckResult` with name, score (0-100), passed boolean, and details
3. Add to `checks` list in `run()` method
4. Add weight in `_aggregate_results()` weights dict
5. Optionally add indicator text in `_build_indicators()`
