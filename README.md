# Portloo — Backend

FastAPI service that turns an uploaded resume into a live portfolio: parses the resume, uses Claude to build profile/projects/skills JSON, generates a matching color theme, and writes it to Supabase. Also handles Stripe billing and receipt emails via Resend.

Paired with the [frontend](../compiler-main), live at otilof.com.

## Endpoints
- `POST /api/generate-portfolio` — resume in, portfolio created
- `POST /api/regenerate-theme` — auth required, regenerates the color theme
- `POST /api/stripe-webhook` — handles Premium upgrade on checkout complete

## Setup
```bash
pip install -r requirements.txt
python portloo.py     # localhost:10000
```

## Deploy
Currently on Render. 
