import os
import json
import uuid
import random
import requests
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Dict
from supabase import create_client, Client
from dotenv import load_dotenv
from google import genai

# 2. SETUP & SECURE ENVIRONMENT VARIABLES
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# The new SDK automatically detects the GEMINI_API_KEY in your environment variables!
gemini_client = genai.Client(http_options={'api_version': 'v1'})
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AuthRequest(BaseModel):
    access_token: str

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
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

# =========================================================================
# 2. THE COMPILER FUNCTION
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
# 3. THE API ROUTE
# =========================================================================
@app.post("/api/generate-portfolio")
async def generate_portfolio(request: AuthRequest):
    headers = {"Authorization": f"Bearer {request.access_token}"}

    # Fetch User
    print("Fetching User Profile...")
    profile_res = requests.get("https://graph.microsoft.com/v1.0/me", headers=headers)
    if not profile_res.ok:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    user_data = profile_res.json()
    student_name = user_data.get("displayName", "Student Name")

    # The Smart Filter
    print("Running Smart Filter...")
    search_res = requests.get("https://graph.microsoft.com/v1.0/me/drive/root/search(q='capstone OR project OR lab OR thesis')?$top=10", headers=headers)
    files = search_res.json().get("value", [])

    text_corpus = ""
    for f in files:
        url = f.get("@microsoft.graph.downloadUrl")
        if url and f["name"].endswith(('.txt', '.md', '.csv', '.py', '.js', '.json')):
            text_corpus += f"\n--- {f['name']} ---\n{requests.get(url).text[:5000]}"

    # GEMINI PHASE 1: Portfolio JSON
    print("Generating Portfolio Data...")
    json_prompt = f"""
    Extract the student's coursework into this exact JSON schema. Return ONLY JSON.
    [DATA] {text_corpus}
    
    {{
      "profile": {{"name": "{student_name}", "title": "IT Professional", "headline": "Summary", "bio": "Bio", "email": "email", "linkedin": "url"}},
      "projects": [{{ "id": "p1", "title": "Proj", "short": "Sum", "tech": ["Py"], "detailHeader": "Det", "full": "Desc", "achievements": ["Ach"], "visualType": "systems" }}],
      "skills": [{{ "title": "Data", "items": ["Py"] }}]
    }}
    """
    json_response = gemini_client.models.generate_content(model='gemini-3.1-pro', contents=json_prompt)
    portfolio_data = json.loads(json_response.text.replace("```json", "").replace("```", "").strip())

    # GEMINI PHASE 2: Design Tokens
    print("Generating Design Tokens...")
    token_prompt = f"""
    Based on this data: {json_response.text}, generate 5 CSS oklch color tokens that fit the industry vibe. 
    Return ONLY JSON matching this exact schema:
    {{"background": "oklch(1 0 0)", "foreground": "oklch(0.1 0.04 265)", "primary": "oklch(0.2 0.04 265)", "primary_foreground": "oklch(0.9 0.003 247)", "border": "oklch(0.9 0.01 255)"}}
    """
    token_response = gemini_client.models.generate_content(model='gemini-3.5-flash', contents=token_prompt)
    design_tokens = json.loads(token_response.text.replace("```json", "").replace("```", "").strip())

    # =========================================================================
    # THE COMPILER IN ACTION
    # =========================================================================
    print("Running Deterministic Compiler...")
    try:
        final_db_row = compile_portfolio_row(student_name, portfolio_data, design_tokens)
    except Exception as e:
        print(f"Compiler Validation Failed: {e}")
        raise HTTPException(status_code=500, detail="Data validation failed.")

    # PUSH TO SUPABASE
    print("Pushing validated row to Supabase...")
    supabase.table("portfolios").upsert(final_db_row, on_conflict="student_id").execute()

    print(f"SUCCESS! Live at: /{final_db_row['student_id']}")
    return {"url": f"/{final_db_row['student_id']}", "template": final_db_row['template_id']}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
