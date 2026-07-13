"""
Portfolio generation backend.

CHANGES IN THIS REVISION
-------------------------
- Data source:  OneDrive (Microsoft Graph token + "Smart Filter" search)
                 -> Resume upload (PDF / DOCX / plain text, capped at 10MB).
- LLM:           Gemini -> Claude (Anthropic API). 
- UNCHANGED:     The deterministic compiler and the Supabase write.
- PAYMENTS:      Stripe Webhooks & Resend Emails integrated via FastAPI Background Tasks.

NEW DEPENDENCIES FOR THIS REVISION:
    pip install anthropic python-docx python-multipart stripe resend
"""

import os
import io
import json
import uuid
import base64
import random
import requests
from io import BytesIO
from datetime import datetime, timezone
import sentry_sdk
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Dict, Optional
from supabase import create_client, Client
from dotenv import load_dotenv
import anthropic  
from docx import Document  
import stripe
import resend

# =========================================================================
# SETUP & SECURE ENVIRONMENT VARIABLES
# =========================================================================
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

STRIPE_API_KEY = os.getenv("STRIPE_API_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")

stripe.api_key = STRIPE_API_KEY
resend.api_key = RESEND_API_KEY
claude_client = anthropic.Anthropic()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================================================================
# SENTRY (error tracking)
# =========================================================================
sentry_sdk.init(
    dsn=os.getenv(
        "SENTRY_DSN",
        "https://656ee7113846d7e01ecb6a2aa7d5d193@o4511700341555200.ingest.us.sentry.io/4511729236901888",
    ),
    # Add data like request headers and IP for users,
    # see https://docs.sentry.io/platforms/python/data-management/data-collected/ for more info
    send_default_pii=True,
    # Percentage of requests to trace for performance monitoring (0.0 - 1.0).
    traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.2")),
)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://thediyblogger.com", "http://localhost:4321"],
    allow_origin_regex=r"https:\/\/.*\.pages\.dev",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CLAUDE_MODEL_ANALYSIS = "claude-sonnet-5"
CLAUDE_MODEL_DESIGN = "claude-sonnet-5"
MAX_RESUME_BYTES = 10 * 1024 * 1024  

TEMPLATES = [
    "ClassicPortfolioTemplate", "ArchitectureStudioTemplate", "MedicalClinicTemplate",
    "SaaSOperationsTemplate", "IndieGameStudioTemplate", "ClimateConsultancyTemplate",
    "RestaurantTemplate", "NonprofitImpactTemplate", "EditorialMagazineTemplate",
    "WeddingPlannerTemplate", "FitnessCoachTemplate", "RealEstateAdvisorTemplate",
    "LegalPracticeTemplate", "ResearchLabTemplate", "MusicProducerTemplate"
]

# =========================================================================
# 1. DETERMINISTIC SCHEMAS (The Gatekeepers) 
# =========================================================================
class DesignTokens(BaseModel):
    background: str
    foreground: str
    primary: str
    primary_foreground: str
    border: str

class FinalSupabaseRow(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    student_id: str
    template_id: str
    portfolio_data: dict
    theme_css: str
    is_premium: bool = False
    tokens_remaining: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

# =========================================================================
# 2. THE COMPILER FUNCTION 
# =========================================================================
def compile_portfolio_row(student_name: str, raw_portfolio_json: dict, raw_tokens_json: dict) -> dict:
    student_id = student_name.lower().replace(" ", "-")
    tokens = DesignTokens(**raw_tokens_json)

    compiled_css = f"""
    :root {{
        --radius: 0.625rem;
        --background: {tokens.background};
        --foreground: {tokens.foreground};
        --primary: {tokens.primary};
        --primary-foreground: {tokens.primary_foreground};
        --border: {tokens.border};
    }}
    @layer base {{
        * {{ border-color: var(--border); }}
        body {{ background-color: var(--background); color: var(--foreground); }}
    }}
    """

    final_row = FinalSupabaseRow(
        student_id=student_id,
        template_id=random.choice(TEMPLATES),
        portfolio_data=raw_portfolio_json,
        theme_css=compiled_css
    )
    return final_row.model_dump()


# =========================================================================
# 3. HELPER FUNCTIONS (Claude & Resume Extraction)
# =========================================================================
def extract_resume_content(filename: str, content_type: str, file_bytes: bytes) -> list:
    name = (filename or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    content_type = content_type or ""

    if ext == "pdf" or content_type == "application/pdf":
        return [{
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(file_bytes).decode("utf-8"),
            },
        }]

    if ext == "docx" or content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        document = Document(io.BytesIO(file_bytes))
        text = "\n".join(p.text for p in document.paragraphs if p.text.strip())
        return [{"type": "text", "text": f"[RESUME TEXT]\n{text}"}]

    if ext in ("txt", "md") or content_type.startswith("text/"):
        text = file_bytes.decode("utf-8", errors="ignore")
        return [{"type": "text", "text": f"[RESUME TEXT]\n{text}"}]

    raise ValueError(f"Unsupported resume file type: '{filename}'. Please upload a PDF, DOCX, or plain text file.")


def extract_json_block(raw_text: str) -> dict:
    cleaned = raw_text.replace("```json", "").replace("```", "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in Claude's response.")
    return json.loads(cleaned[start:end + 1])


def run_claude(messages: list, model: str, max_tokens: int = 4096, tools: Optional[list] = None) -> str:
    request_kwargs = {"model": model, "max_tokens": max_tokens}
    if tools:
        request_kwargs["tools"] = tools

    response = claude_client.messages.create(messages=messages, **request_kwargs)

    while response.stop_reason == "pause_turn":
        messages = messages + [{"role": "assistant", "content": response.content}]
        response = claude_client.messages.create(messages=messages, **request_kwargs)

    return "".join(block.text for block in response.content if block.type == "text")


# =========================================================================
# 4. BACKGROUND WORKERS & EMAILS (Payments)
# =========================================================================
def send_receipt_email(user_email: str, pdf_bytes: bytes, session_id: str):
    """Fires an email via Resend with the generated PDF attached."""
    try:
        resend.Emails.send({
            "from": "Portloo Billing <receipts@thediyblogger.com>",
            "to": [user_email],
            "subject": "Your Portloo Premium Receipt",
            "html": "<h2>Welcome to Premium!</h2><p>Your account has been upgraded and 5 theme regeneration tokens have been added to your dashboard. Your receipt is attached below.</p>",
            "attachments": [
                {
                    "filename": f"Portloo_Invoice_{session_id}.pdf",
                    "content": list(pdf_bytes)  # Resend requires the raw bytes as a list
                }
            ]
        })
        print(f"✉️ Receipt successfully sent to {user_email}")
    except Exception as e:
        print(f"⚠️ Email Error: {e}")

def process_premium_upgrade(session: dict):
    """Executes asynchronously to update DB, generate PDF, and send email."""
    student_id = getattr(session, "client_reference_id", None)
    stripe_session_id = getattr(session, "id", None)
    amount_total = getattr(session, "amount_total", 0) / 100
    
    if not student_id:
       print("❌ No student_id found in Stripe session.")
       return

    print(f"🔄 Background Task Started: Upgrading {student_id}...")

    # 1. Update Portfolio Premium Status & Reset Tokens
    db_response = supabase.table("portfolios").update({
        "is_premium": True,
        "tokens_remaining": 5
    }).eq("student_id", student_id).execute()
    
    if not db_response.data:
        print(f"❌ Failed to find portfolio row for {student_id}")
        return
        
    user_id = db_response.data[0].get("user_id")

    # 2. Generate Invoice PDF programmatically (Placeholder for actual PDF layout generation)
    pdf_buffer = BytesIO()
    pdf_buffer.write(b"%PDF-1.4 Mock Invoice Data...") 
    pdf_buffer.seek(0)
    raw_pdf_bytes = pdf_buffer.getvalue()
    
    # 3. Upload PDF to Supabase Storage Bucket
    file_name = f"inv_{stripe_session_id}.pdf"
    bucket_path = f"{user_id}/{file_name}" if user_id else f"anonymous/{file_name}"
    
    try:
        supabase.storage.from_("invoices").upload(
            path=bucket_path,
            file=raw_pdf_bytes,
            file_options={"content-type": "application/pdf"}
        )
        print(f"📁 Invoice PDF saved securely to storage path: invoices/{bucket_path}")
    except Exception as e:
        print(f"⚠️ Storage Upload Warning: {e}")

    # 4. Log Row to Invoice History Table
    if user_id:
        supabase.table("invoices").insert({
            "user_id": user_id,
            "stripe_session_id": stripe_session_id,
            "amount": amount_total,
            "pdf_path": bucket_path
        }).execute()

    # 5. Invoke transactional Email API to dispatch receipt + PDF copy
    customer_details = getattr(session, "customer_details", None)
    user_email = getattr(customer_details, "email", None) if customer_details else None
    
    if user_email:
        send_receipt_email(user_email, raw_pdf_bytes, stripe_session_id)
    else:
        print("⚠️ No email found in Stripe session to send receipt.")

# =========================================================================
# 5. API ROUTES
# =========================================================================

@app.post("/api/stripe-webhook")
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        # Enqueue execution to background runner threads immediately
        background_tasks.add_task(process_premium_upgrade, session)
        print("⚡ Webhook verified. Task offloaded to background processor.")

    # Return immediate 200 OK acknowledgment to Stripe in under 200ms
    return {"status": "received"}


class RegenerateRequest(BaseModel):
    student_id: str

@app.post("/api/regenerate-theme")
async def regenerate_theme(req: RegenerateRequest):
    print(f"Attempting to regenerate theme for {req.student_id}...")
    
    response = supabase.table("portfolios").select("*").eq("student_id", req.student_id).execute()
    data = response.data
    
    if not data:
        raise HTTPException(status_code=404, detail="Portfolio not found.")
        
    portfolio = data[0]
    
    tokens = portfolio.get("tokens_remaining", 0)
    if tokens <= 0:
        raise HTTPException(status_code=403, detail="Out of regeneration tokens. Please upgrade to Premium.")
        
    print("Generating fresh Design Tokens...")
    token_prompt = f"""
    Based on this student data: {json.dumps(portfolio['portfolio_data'])}, generate 5 CSS oklch color tokens that fit the student's field/profession.
    Ensure this color palette is distinctly DIFFERENT from their previous design.
    Return ONLY JSON matching this exact schema:
    {{"background": "oklch(1 0 0)", "foreground": "oklch(0.1 0.04 265)", "primary": "oklch(0.2 0.04 265)", "primary_foreground": "oklch(0.9 0.003 247)", "border": "oklch(0.9 0.01 255)"}}
    """
    
    token_text = run_claude(
        [{"role": "user", "content": token_prompt}],
        model=CLAUDE_MODEL_DESIGN,
        max_tokens=512,
    )

    try:
        new_design_tokens = extract_json_block(token_text)
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=502, detail="Could not parse new design tokens from Claude.")

    tokens_obj = DesignTokens(**new_design_tokens)
    compiled_css = f"""
    :root {{
        --radius: 0.625rem;
        --background: {tokens_obj.background};
        --foreground: {tokens_obj.foreground};
        --primary: {tokens_obj.primary};
        --primary-foreground: {tokens_obj.primary_foreground};
        --border: {tokens_obj.border};
    }}
    @layer base {{
        * {{ border-color: var(--border); }}
        body {{ background-color: var(--background); color: var(--foreground); }}
    }}
    """
    
    new_template = random.choice(TEMPLATES)
    
    update_data = {
        "theme_css": compiled_css,
        "template_id": new_template,
        "tokens_remaining": tokens - 1
    }
    
    supabase.table("portfolios").update(update_data).eq("student_id", req.student_id).execute()
    return {"message": "Theme regenerated successfully", "tokens_remaining": tokens - 1}


@app.post("/api/generate-portfolio")
async def generate_portfolio(
    resume: UploadFile = File(...),
    student_name: Optional[str] = Form(None),
    school: Optional[str] = Form(None),
):
    print("Reading uploaded resume...")
    resume_bytes = await resume.read()
    if not resume_bytes:
        raise HTTPException(status_code=400, detail="Resume file is empty.")
    if len(resume_bytes) > MAX_RESUME_BYTES:
        raise HTTPException(status_code=413, detail="Resume file exceeds the 10MB limit.")

    try:
        resume_content_blocks = extract_resume_content(resume.filename, resume.content_type, resume_bytes)
    except Exception as e:
        print(f"Resume parsing failed: {e}")
        raise HTTPException(status_code=400, detail=f"Could not read the resume file: {e}")

    known_facts = []
    if student_name:
        known_facts.append(f"The student's name is: {student_name}.")
    if school:
        known_facts.append(f"The student's school is: {school}.")

    print("Analyzing resume + discovering projects with Claude...")
    analysis_instructions = (
        "You are building a public student portfolio site from the attached resume.\n"
        + "\n".join(known_facts) + "\n\n" +
        """Return ONLY a valid JSON object (no markdown fences, no commentary before or
after) matching exactly this schema:

{
  "profile": {
    "name": "Full name as it appears on the resume",
    "school": "Their university/college",
    "title": "Their professional field/major (e.g. 'Mechanical Engineering Student') - infer this, never default to 'IT Professional'",
    "headline": "One-sentence professional headline",
    "bio": "2-3 sentence third-person bio",
    "email": "Email from the resume, or empty string",
    "linkedin": "LinkedIn URL from the resume, or empty string"
  },
  "projects": [
    {
      "id": "p1",
      "title": "Project title",
      "short": "One-sentence summary",
      "tech": ["Tech 1", "Tech 2"],
      "detailHeader": "Short header for the detail view",
      "full": "2-4 sentence description",
      "achievements": ["Achievement 1", "Achievement 2"],
      "visualType": "systems",
      "source": "resume | web | generated"
    }
  ],
  "skills": [
    {"title": "Category", "items": ["Skill 1", "Skill 2"]}
  ]
}

How to build the "projects" array:
1. Pull in any capstone/project/thesis work the resume itself mentions. Mark these "source": "resume".
2. Use web search to look for this student's own publicly published capstone or project
   work - search using their name together with their school (school capstone showcases,
   GitHub, personal sites, published papers, LinkedIn posts). Many schools publish capstone
   work publicly, so real hits are common. If you find genuine, publicly available work,
   describe it accurately in your own words and mark it "source": "web".
3. If you can't find anything specific to this student, fill in 2-4 representative
   projects a student in their program at their school would plausibly build, based on
   that program's typical coursework/capstone curriculum. Mark these "source": "generated"
   and keep them concrete and field-appropriate, not generic software boilerplate.
4. Every project must fit the student's actual field of study - do not default to
   generic IT/software projects unless that IS their field.
"""
    )

    analysis_messages = [{
        "role": "user",
        "content": resume_content_blocks + [{"type": "text", "text": analysis_instructions}],
    }]

    analysis_text = run_claude(
        analysis_messages,
        model=CLAUDE_MODEL_ANALYSIS,
        max_tokens=4096,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
    )

    try:
        portfolio_data = extract_json_block(analysis_text)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Resume analysis parsing failed: {e}")
        raise HTTPException(status_code=502, detail="Could not parse resume analysis from Claude.")

    resolved_name = student_name or portfolio_data.get("profile", {}).get("name", "Student Name")

    print("Generating Design Tokens...")
    token_prompt = f"""
    Based on this data: {json.dumps(portfolio_data)}, generate 5 CSS oklch color tokens that fit the student's field/profession.
    Return ONLY JSON matching this exact schema:
    {{"background": "oklch(1 0 0)", "foreground": "oklch(0.1 0.04 265)", "primary": "oklch(0.2 0.04 265)", "primary_foreground": "oklch(0.9 0.003 247)", "border": "oklch(0.9 0.01 255)"}}
    """
    token_text = run_claude(
        [{"role": "user", "content": token_prompt}],
        model=CLAUDE_MODEL_DESIGN,
        max_tokens=512,
    )

    try:
        design_tokens = extract_json_block(token_text)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Design token parsing failed: {e}")
        raise HTTPException(status_code=502, detail="Could not parse design tokens from Claude.")

    print("Running Deterministic Compiler...")
    try:
        final_db_row = compile_portfolio_row(resolved_name, portfolio_data, design_tokens)
    except Exception as e:
        print(f"Compiler Validation Failed: {e}")
        raise HTTPException(status_code=500, detail="Data validation failed.")

    print("Pushing validated row to Supabase...")
    supabase.table("portfolios").upsert(final_db_row, on_conflict="student_id").execute()

    print(f"SUCCESS! Live at: /{final_db_row['student_id']}")
    return {"url": f"/{final_db_row['student_id']}", "template": final_db_row['template_id'], "student_id": final_db_row['student_id']}

@app.get("/sentry-debug")
async def trigger_error():
    """Temporary route to verify Sentry is receiving events. Safe to remove once confirmed."""
    division_by_zero = 1 / 0


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
