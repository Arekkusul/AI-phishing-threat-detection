# AI-Powered Phishing Detection Platform

A production-ready phishing detection system that integrates with Microsoft Outlook via an Add-in taskpane. The platform uses AI/ML models and threat intelligence APIs to analyze emails and provide real-time phishing verdicts.

## Features

- **Outlook Add-in Integration**: Seamless taskpane that scans emails directly in Outlook
- **Quarantine Functionality**: One-click quarantine for suspicious emails
- **Multi-Signal Detection**: 12 parallel detection checks with weighted scoring
- **AI-Powered Analysis**: BERT model for phishing classification + Gemini for reasoning
- **Threat Intelligence**: Integration with Sublime Security, VirusTotal, and URLScan.io
- **Real-time Notifications**: Report phishing via Teams, Telegram, or WhatsApp
- **Admin Dashboard**: Interactive charts, scan history, and full email review
- **Result Caching**: Deduplication and caching for faster repeat scans
- **Rate Limiting**: Protection against abuse with configurable limits
- **Docker Deployment**: One-command deployment with PostgreSQL persistence
- **Cloudflare Tunnel Ready**: Secure HTTPS exposure without port forwarding

## Architecture

```
┌─────────────────────────────────┐         HTTPS          ┌─────────────────────────────────────┐
│   Outlook Add-in (Taskpane)     │ ──────────────────────►│          Flask Backend              │
│                                 │    Cloudflare Tunnel   │                                     │
│   - Scan Email button           │                        │  ┌─────────────────────────────┐   │
│   - Auto-scan mode              │    POST /check         │  │   Parallel Detection        │   │
│   - Quarantine button           │◄──────────────────────┐│  │                             │   │
│   - Report Phishing button      │   {verdict, scores,   ││  │  1. BERT Model (30%)        │   │
│   - Verdict display             │    reasons, indicators}││  │  2. Sublime Security (20%)  │   │
│                                 │                        ││  │  3. HTML Threats (8%)       │   │
└─────────────────────────────────┘                        ││  │  4. Header Mismatch (7%)    │   │
                                                           ││  │  5. Urgency Keywords (7%)   │   │
                                                           ││  │  6. URLScan.io (5%)         │   │
                                                           ││  │  7. URL Shorteners (5%)     │   │
                                                           ││  │  8. Suspicious TLDs (4%)    │   │
                                                           ││  │  9. DNS Records (4%)        │   │
                                                           ││  │  10. VirusTotal (4%)        │   │
                                                           ││  │  11. SPF Record (3%)        │   │
                                                           ││  │  12. Domain Age (3%)        │   │
                                                           ││  │                             │   │
                                                           ││  │  + Gemini AI Reasoning      │   │
                                                           ││  └─────────────────────────────┘   │
                                                           │└─────────────────────────────────────┘
                                                           │
                                 POST /report              │  ──► Teams Webhook
                                 {eml, reporter}           │  ──► Telegram Bot
                                                           └──► WhatsApp (Twilio)
```

## Quick Start

### 1. Clone and Configure

```bash
git clone <repository-url>
cd ai-phishing

# Copy environment template
cp .env.example .env

# Edit .env with your configuration
# At minimum, set POSTGRES_PASSWORD and GEMINI_API_KEY
```

### 2. Deploy with Docker

```bash
# Build and start all services
docker compose up -d --build

# Check logs
docker compose logs -f phishing-api

# Verify health
curl http://localhost:5000/health
```

### 3. Access the Platform

- **Web Analyzer**: http://localhost:5000/
- **Admin Dashboard**: http://localhost:5000/admin
- **Health Check**: http://localhost:5000/health

## Detection Pipeline

The platform runs 12 detection checks in parallel and aggregates results using weighted scoring:

| Check | Weight | Description |
|-------|--------|-------------|
| BERT Model | 30% | HuggingFace phishing classifier |
| Sublime Security | 20% | Commercial attack score API |
| HTML Threats | 8% | Hidden elements, suspicious scripts |
| Header Mismatch | 7% | From vs Reply-To comparison |
| Urgency Keywords | 7% | Pressure language detection |
| URLScan.io | 5% | URL maliciousness scanning |
| URL Shorteners | 5% | Detects bit.ly, t.co, etc. |
| Suspicious TLDs | 4% | Flags .xyz, .top, .club, etc. |
| DNS Records | 4% | Validates sender domain |
| VirusTotal | 4% | Hash reputation lookup |
| SPF Record | 3% | Email authentication check |
| Domain Age | 3% | WHOIS creation date check |

### Verdict Thresholds

- **PHISHING**: Score >= 70%
- **SUSPICIOUS**: Score 40-70%
- **SAFE**: Score < 40%

## API Reference

### POST /check - Analyze Email

Primary endpoint for email analysis.

**Request:**
```json
{
  "eml": "<raw RFC822 email or base64-encoded EML>"
}
```

**Response:**
```json
{
  "verdict": "PHISHING",
  "confidence": 85.5,
  "ai_score": 92.3,
  "sublime_score": 78.0,
  "reasons": [
    "Email contains urgency language typical of phishing",
    "Sender domain was registered 5 days ago",
    "URL redirects to suspicious domain"
  ],
  "indicators": [
    "Urgency language: urgent, verify immediately",
    "Recently registered domain: fake-bank.xyz",
    "URL shortener detected: bit.ly"
  ],
  "email_hash": "abc123def456",
  "cached": false
}
```

### POST /report - Report Phishing

Report a phishing email and trigger webhook notifications.

**Request:**
```json
{
  "eml": "<raw email content>",
  "reporter": "user@company.com"
}
```

**Response:**
```json
{
  "success": true,
  "verdict": "PHISHING",
  "confidence": 85.5,
  "notifications": {
    "teams": true,
    "telegram": true,
    "whatsapp": false
  },
  "report_id": "uuid-here"
}
```

### GET /health - Health Check

```json
{
  "status": "healthy",
  "timestamp": "2024-01-14T10:30:00.000000",
  "version": "2.0.0",
  "database": "connected",
  "cache_enabled": true
}
```

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `POSTGRES_PASSWORD` | Yes | Database password |
| `GEMINI_API_KEY` | Recommended | Google Gemini for AI reasoning |
| `API_KEY` | No | Enable API authentication |
| `ADMIN_KEY` | No | Protect admin dashboard |
| `VIRUSTOTAL_API_KEY` | No | Enable VirusTotal checks |
| `URLSCAN_API_KEY` | No | Enable URLScan.io checks |
| `TEAMS_WEBHOOK_URL` | No | Microsoft Teams notifications |
| `TELEGRAM_BOT_TOKEN` | No | Telegram notifications |
| `TELEGRAM_CHAT_ID` | No | Telegram chat destination |
| `WHATSAPP_TWILIO_SID` | No | Twilio Account SID |
| `WHATSAPP_TWILIO_TOKEN` | No | Twilio Auth Token |
| `WHATSAPP_FROM` | No | Twilio WhatsApp number |
| `WHATSAPP_TO` | No | Destination phone number |
| `CLOUDFLARE_TUNNEL_URL` | No | Your tunnel URL for CORS |

### Feature Flags

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_URLSCAN` | true | Enable URLScan.io integration |
| `ENABLE_VIRUSTOTAL` | true | Enable VirusTotal integration |
| `CACHE_ENABLED` | true | Enable result caching |
| `CACHE_TTL_HOURS` | 24 | Cache time-to-live |

## Notification Setup

### Telegram Bot

1. **Create a bot** via [@BotFather](https://t.me/botfather):
   - Send `/newbot` and follow prompts
   - Copy the **bot token**

2. **Get your Chat ID**:
   - Start a chat with your bot or add it to a group
   - Send a message
   - Visit: `https://api.telegram.org/bot<TOKEN>/getUpdates`
   - Find `"chat":{"id":123456789}`

3. **Add to `.env`**:
   ```bash
   TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
   TELEGRAM_CHAT_ID=-1001234567890
   ```

### WhatsApp (Twilio)

1. **Create Twilio account** at [twilio.com](https://www.twilio.com/)

2. **Enable WhatsApp Sandbox**:
   - Go to Console > Messaging > Try it out > Send a WhatsApp message
   - Follow instructions to join sandbox

3. **Get credentials** from Twilio Console

4. **Add to `.env`**:
   ```bash
   WHATSAPP_TWILIO_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   WHATSAPP_TWILIO_TOKEN=your_auth_token
   WHATSAPP_FROM=+14155238886
   WHATSAPP_TO=+1234567890
   ```

### Microsoft Teams

1. In Teams, go to Channel > Connectors > Incoming Webhook
2. Create webhook and copy URL
3. Add to `.env`:
   ```bash
   TEAMS_WEBHOOK_URL=https://outlook.office.com/webhook/...
   ```

## Outlook Add-in Setup

### Prerequisites

- Node.js 18+
- Microsoft 365 account
- Office Add-in sideloading enabled

### Installation

```bash
cd outlook-phishing-dashboard

# Install dependencies
npm install

# Start development server
npm run dev-server

# Sideload in Outlook
npm start
```

### Configuration

Update the API endpoint in `src/taskpane/taskpane.js`:

```javascript
const API_BASE = "https://your-tunnel-url.trycloudflare.com";
```

### Taskpane Features

- **Scan Email**: Analyze current email for phishing indicators
- **Auto Mode**: Automatically scan when switching emails
- **Quarantine**: Move suspicious emails to Junk folder (requires permissions)
- **Report**: Send phishing reports to security team via webhooks

## Admin Dashboard

Access the admin dashboard at `/admin` to view:

- **Dashboard**: Interactive pie charts and scan statistics
- **Scans**: Browse all email analyses with full details
- **Email Viewer**: View original email content (raw + rendered HTML)
- **Reports**: View user-reported phishing
- **Audit Log**: System activity log

Protect with `ADMIN_KEY` environment variable.

### Dashboard Charts

- **Verdict Distribution**: Doughnut chart showing Safe/Suspicious/Phishing breakdown
- **Daily Trend**: Stacked bar chart showing scan activity over time

## Cloudflare Tunnel Setup

### 1. Install cloudflared

```bash
# macOS
brew install cloudflared

# Linux
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
chmod +x cloudflared
```

### 2. Create Tunnel

```bash
cloudflared tunnel login
cloudflared tunnel create phishing-api
```

### 3. Configure Docker

Copy the tunnel token from the Cloudflare dashboard, then:

```bash
# Add to .env
CLOUDFLARE_TUNNEL_TOKEN=your_token_here
CLOUDFLARE_TUNNEL_URL=https://phishing-api.your-domain.com
```

Uncomment the `cloudflared` service in `docker-compose.yml`.

### 4. Configure DNS

In Cloudflare dashboard, add a CNAME record pointing to your tunnel.

## Database Schema

PostgreSQL tables:

- `scans` - Email scan results with full check details and email body
- `reports` - User-reported phishing emails
- `api_keys` - Multi-tenant API key management
- `audit_log` - Request/event logging

Views for admin:
- `recent_scans_summary` - Daily scan counts
- `top_reported_domains` - Most reported sender domains

### Database Migration

If upgrading from a previous version, run:

```bash
docker compose exec db psql -U phishing -d phishing -c \
  "ALTER TABLE scans ADD COLUMN IF NOT EXISTS email_body TEXT;"
```

## Development

### Local Backend Setup

```bash
cd phishing-ai

# Create virtual environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install dependencies
pip install -r requirements.txt

# Copy config
cp .env.example .env

# Start server
python app.py
```

### Project Structure

```
ai-phishing/
├── docker-compose.yml          # Service orchestration
├── .env.example                # Environment template
├── README.md                   # This file
├── CLAUDE.md                   # AI assistant context
│
├── phishing-ai/                # Backend API
│   ├── app.py                  # Flask application
│   ├── db.py                   # Database integration
│   ├── detection_pipeline.py   # Detection orchestration
│   ├── gemini_check.py         # Gemini AI reasoning
│   ├── sublime_check.py        # Sublime Security API
│   ├── virustotal_check.py     # VirusTotal integration
│   ├── url_check.py            # URLScan.io integration
│   ├── requirements.txt        # Python dependencies
│   ├── Dockerfile              # Container build
│   ├── init.sql                # Database schema
│   ├── static/                 # CSS stylesheets
│   └── templates/              # HTML templates
│       ├── index.html          # Web analyzer (shows all checks)
│       ├── admin/              # Admin dashboard
│       │   ├── base.html       # Admin layout
│       │   ├── dashboard.html  # Stats + charts
│       │   ├── scans.html      # Scan history
│       │   ├── scan_detail.html # Full scan + email viewer
│       │   ├── reports.html    # Phishing reports
│       │   └── audit.html      # Audit log
│       └── errors/             # Error pages
│
└── outlook-phishing-dashboard/ # Outlook Add-in
    ├── manifest.json           # Office Add-in manifest
    ├── package.json            # Node dependencies
    ├── webpack.config.js       # Build configuration
    └── src/
        └── taskpane/
            ├── taskpane.html   # Add-in UI
            ├── taskpane.js     # Email extraction, API calls, quarantine
            └── taskpane.css    # Dark theme styling
```

## Security Considerations

- **API Authentication**: Set `API_KEY` for production use
- **Admin Protection**: Set `ADMIN_KEY` to protect dashboard
- **Rate Limiting**: Default 30 requests/minute per IP
- **Input Validation**: 25MB email size limit
- **Non-root Container**: Runs as unprivileged user
- **Cloudflare Protection**: TLS and DDoS mitigation
- **Sandboxed Email Preview**: HTML rendered in sandboxed iframe
- **Email Storage**: Full email content stored for admin review

## Troubleshooting

### Container won't start

```bash
# Check logs
docker compose logs phishing-api

# Verify PostgreSQL is healthy
docker compose ps
```

### Database connection failed

```bash
# Ensure PostgreSQL is running
docker compose logs postgres

# Check connection string
docker compose exec phishing-api env | grep DATABASE_URL
```

### BERT model errors

If you see token length errors:
```
Token indices sequence length is longer than the specified maximum sequence length
```

The model automatically truncates to 512 tokens. Rebuild if issues persist:
```bash
docker compose build --no-cache phishing-api
```

### CORS errors in Outlook

Ensure `CLOUDFLARE_TUNNEL_URL` or `ALLOWED_ORIGIN` is set correctly.

### Quarantine not working

- Ensure manifest has `ReadWriteMailbox` permission
- Re-sideload the add-in after manifest changes
- Check Outlook version supports EWS/REST API

## License

MIT License - See LICENSE file for details.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## Support

For issues and feature requests, please open a GitHub issue.
