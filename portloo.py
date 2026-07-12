"""
Portfolio generation backend.

CHANGES IN THIS REVISION
-------------------------
- Data source:  OneDrive (Microsoft Graph token + "Smart Filter" search)
                 -> Resume upload (PDF / DOCX / plain text, capped at 10MB).
- LLM:           Gemini -> Claude (Anthropic API). Resume analysis now runs
                 with Claude's web_search tool enabled so it can look up a
                 student's real, publicly-published capstone/project work
                 (a lot of schools publish these) using their name + school,
                 and falls back to generating representative coursework
                 projects for that school/program when nothing specific
                 turns up.
- UNCHANGED:     The deterministic compiler (DesignTokens / FinalSupabaseRow /
                 compile_portfolio_row) and the Supabase write. Both Claude
                 calls still just hand a plain dict to compile_portfolio_row,
                 exactly like the Gemini calls used to.

The original OneDrive + Gemini code is left in place below, commented out,
so it can be restored quickly if needed.

NEW DEPENDENCIES FOR THIS REVISION:
    pip install anthropic python-docx python-multipart
(python-multipart is required by FastAPI for UploadFile/Form parsing.)
"""

import os
import io
import json
import uuid
import base64
import random
import requests
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Dict, Optional
from supabase import create_client, Client
from dotenv import load_dotenv
import anthropic  # NEW: replaces Gemini for both analysis + design tokens
from docx import Document  # NEW: pip install python-docx - extracts text from .docx resumes

# --- LEGACY (disabled): Gemini SDK ------------------------------------------
# from google import genai
# -----------------------------------------------------------------------------

# SETUP & SECURE ENVIRONMENT VARIABLES
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# --- LEGACY (disabled): Gemini client ---------------------------------------
# The new SDK automatically detects the GEMINI_API_KEY in your environment variables!
# gemini_client = genai.Client()
# -----------------------------------------------------------------------------

# Claude client - reads ANTHROPIC_API_KEY from the environment automatically
claude_client = anthropic.Anthropic()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://thediyblogger.com",],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- LEGACY (disabled): OneDrive OAuth request body -------------------------
# class AuthRequest(BaseModel):
#     access_token: str
# -----------------------------------------------------------------------------

# Model routing. Pin exact IDs so behavior doesn't shift under you when
# Anthropic ships a new default model. Sonnet 5 drives both calls below;
# swap CLAUDE_MODEL_DESIGN for something like "claude-haiku-4-5-20251001"
# if you want to shave cost off the token step - it's a small, tool-free call.
CLAUDE_MODEL_ANALYSIS = "claude-sonnet-5"   # resume analysis + project discovery (web search)
CLAUDE_MODEL_DESIGN = "claude-sonnet-5"     # design token generation

MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10MB resume size cap

TEMPLATES = [
    "ClassicPortfolioTemplate", "ArchitectureStudioTemplate", "MedicalClinicTemplate",
    "SaaSOperationsTemplate", "IndieGameStudioTemplate", "ClimateConsultancyTemplate",
    "RestaurantTemplate", "NonprofitImpactTemplate", "EditorialMagazineTemplate",
    "WeddingPlannerTemplate", "FitnessCoachTemplate", "RealEstateAdvisorTemplate",
    "LegalPracticeTemplate", "ResearchLabTemplate", "MusicProducerTemplate"
]

# =========================================================================
# 1. DETERMINISTIC SCHEMAS (The Gatekeepers) -- UNCHANGED
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
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

# =========================================================================
# 2. THE COMPILER FUNCTION -- UNCHANGED
# =========================================================================
def compile_portfolio_row(student_name: str, raw_portfolio_json: dict, raw_tokens_json: dict) -> dict:
    """Takes AI outputs, enforces schema, compiles CSS, and returns a perfect DB row."""

    # 1. Assign deterministic student ID
    student_id = student_name.lower().replace(" ", "-")

    # 2. Validate Design Tokens
    tokens = DesignTokens(**raw_tokens_json)

    # 3. Compile Tokens into valid Astro CSS string
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

    # 4. Compile the Final Row
    final_row = FinalSupabaseRow(
        student_id=student_id,
        template_id=random.choice(TEMPLATES),
        portfolio_data=raw_portfolio_json,
        theme_css=compiled_css
    )

    # Returns a validated, pure dictionary ready for Supabase
    return final_row.model_dump()


# =========================================================================
# 3. RESUME INTAKE + CLAUDE HELPERS (NEW - replaces OneDrive + Gemini)
# =========================================================================
def extract_resume_content(filename: str, content_type: str, file_bytes: bytes) -> list:
    """
    Turns an uploaded resume into Claude message content blocks.
      - PDF    -> sent natively as a `document` block (Claude reads layout/tables directly).
      - DOCX   -> Claude's document blocks don't accept .docx, so extract text first.
      - TXT/MD -> UTF-8 decode as plain text.
    Anything else is rejected with a clear error rather than guessed at.
    """
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
    """
    Pulls a JSON object out of a Claude response, tolerating stray markdown
    fences or commentary Claude may add (common once web search is enabled,
    since Claude sometimes narrates its search before answering).
    """
    cleaned = raw_text.replace("```json", "").replace("```", "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in Claude's response.")
    return json.loads(cleaned[start:end + 1])


def run_claude(messages: list, model: str, max_tokens: int = 4096, tools: Optional[list] = None) -> str:
    """
    Calls Claude and concatenates the text blocks of the final response.
    Long tool-use chains (e.g. several web searches) can pause a turn; if
    that happens we just resume with the same tools until Claude actually
    stops on its own.
    """
    request_kwargs = {"model": model, "max_tokens": max_tokens}
    if tools:
        request_kwargs["tools"] = tools

    response = claude_client.messages.create(messages=messages, **request_kwargs)

    while response.stop_reason == "pause_turn":
        messages = messages + [{"role": "assistant", "content": response.content}]
        response = claude_client.messages.create(messages=messages, **request_kwargs)

    return "".join(block.text for block in response.content if block.type == "text")


# =========================================================================
# 4. THE API ROUTE
# =========================================================================
@app.post("/api/generate-portfolio")
async def generate_portfolio(
    resume: UploadFile = File(...),
    student_name: Optional[str] = Form(None),
    school: Optional[str] = Form(None),
):
    # --- LEGACY (disabled): OneDrive token exchange + Smart Filter + Gemini calls ---
    # headers = {"Authorization": f"Bearer {request.access_token}"}
    #
    # # Fetch User
    # print("Fetching User Profile...")
    # profile_res = requests.get("https://graph.microsoft.com/v1.0/me", headers=headers)
    # if not profile_res.ok:
    #     raise HTTPException(status_code=401, detail="Invalid token")
    #
    # user_data = profile_res.json()
    # student_name = user_data.get("displayName", "Student Name")
    #
    # # The Smart Filter
    # print("Running Smart Filter...")
    # search_res = requests.get("https://graph.microsoft.com/v1.0/me/drive/root/search(q='capstone OR project OR lab OR thesis')?$top=10", headers=headers)
    # files = search_res.json().get("value", [])
    #
    # text_corpus = ""
    # for f in files:
    #     url = f.get("@microsoft.graph.downloadUrl")
    #     if url and f["name"].endswith(('.txt', '.md', '.csv', '.py', '.js', '.json')):
    #         text_corpus += f"\n--- {f['name']} ---\n{requests.get(url).text[:5000]}"
    #
    # # GEMINI PHASE 1: Portfolio JSON
    # print("Generating Portfolio Data...")
    # json_prompt = f"""
    # Extract the student's coursework into this exact JSON schema. Return ONLY JSON.
    # [DATA] {text_corpus}
    #
    # {{
    #   "profile": {{"name": "{student_name}", "title": "IT Professional", "headline": "Summary", "bio": "Bio", "email": "email", "linkedin": "url"}},
    #   "projects": [{{ "id": "p1", "title": "Proj", "short": "Sum", "tech": ["Py"], "detailHeader": "Det", "full": "Desc", "achievements": ["Ach"], "visualType": "systems" }}],
    #   "skills": [{{ "title": "Data", "items": ["Py"] }}]
    # }}
    # """
    # json_response = gemini_client.models.generate_content(model='gemini-3.5-flash', contents=json_prompt)
    # portfolio_data = json.loads(json_response.text.replace("```json", "").replace("```", "").strip())
    #
    # # GEMINI PHASE 2: Design Tokens
    # print("Generating Design Tokens...")
    # token_prompt = f"""
    # Based on this data: {json_response.text}, generate 5 CSS oklch color tokens that fit the industry vibe.
    # Return ONLY JSON matching this exact schema:
    # {{"background": "oklch(1 0 0)", "foreground": "oklch(0.1 0.04 265)", "primary": "oklch(0.2 0.04 265)", "primary_foreground": "oklch(0.9 0.003 247)", "border": "oklch(0.9 0.01 255)"}}
    # """
    # token_response = gemini_client.models.generate_content(model='gemini-3.5-flash', contents=token_prompt)
    # design_tokens = json.loads(token_response.text.replace("```json", "").replace("```", "").strip())
    # ----------------------------------------------------------------------------------

    # Resume intake
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

    # CLAUDE PHASE 1: Resume Analysis + Project Discovery (web search enabled)
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

    # CLAUDE PHASE 2: Design Tokens
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

    # =========================================================================
    # THE COMPILER IN ACTION -- UNCHANGED
    # =========================================================================
    print("Running Deterministic Compiler...")
    try:
        final_db_row = compile_portfolio_row(resolved_name, portfolio_data, design_tokens)
    except Exception as e:
        print(f"Compiler Validation Failed: {e}")
        raise HTTPException(status_code=500, detail="Data validation failed.")

    # PUSH TO SUPABASE -- UNCHANGED
    print("Pushing validated row to Supabase...")
    supabase.table("portfolios").upsert(final_db_row, on_conflict="student_id").execute()

    print(f"SUCCESS! Live at: /{final_db_row['student_id']}")
    return {"url": f"/{final_db_row['student_id']}", "template": final_db_row['template_id']}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
