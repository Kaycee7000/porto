import os
import json
import random
import requests
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

# 1. IMPORT THE NEW GOOGLE GENAI SDK
from google import genai
from google.genai import types

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

@app.post("/api/generate-portfolio")
async def generate_portfolio(request: AuthRequest):
    token = request.access_token
    headers = {"Authorization": f"Bearer {token}"}

    print("Fetching User Profile...")
    profile_res = requests.get("https://graph.microsoft.com/v1.0/me", headers=headers)
    if not profile_res.ok:
        raise HTTPException(status_code=401, detail="Invalid OneDrive Token")
    
    user_data = profile_res.json()
    student_id = user_data.get("displayName", "student").lower().replace(" ", "-")

    print("Running Smart Filter to find Capstones and Projects...")
    search_url = "https://graph.microsoft.com/v1.0/me/drive/root/search(q='capstone OR project OR lab OR thesis')?$top=10"
    search_res = requests.get(search_url, headers=headers)
    files = search_res.json().get("value", [])

    text_corpus = ""
    
    print(f"Found {len(files)} relevant documents. Extracting text...")
    for f in files:
        download_url = f.get("@microsoft.graph.downloadUrl")
        if download_url and f["name"].endswith(('.txt', '.md', '.csv', '.py', '.js', '.json')):
            doc_res = requests.get(download_url)
            text_corpus += f"\n--- FILE: {f['name']} ---\n{doc_res.text[:5000]}"

    # ==========================================================
    # UPDATED GEMINI PHASE 1: Generate JSON Data
    # ==========================================================
    print("Asking Gemini to generate JSON Portfolio Data...")
    json_prompt = f"""
    Analyze these college documents and extract the student's coursework into this exact JSON schema.
    [RAW DATA]
    {text_corpus}
    
    {{
      "profile": {{
        "name": "{user_data.get('displayName', 'Student Name')}",
        "title": "IT Professional",
        "headline": "A punchy summary",
        "bio": "A professional paragraph.",
        "email": "{user_data.get('mail', 'student@email.com')}",
        "linkedin": "linkedin.com/in/username"
      }},
      "projects": [ {{ "id": "p1", "title": "Project", "short": "Summary", "tech": ["Python"], "detailHeader": "Details", "full": "Full desc", "achievements": ["Ach 1"], "visualType": "systems" }} ],
      "skills": [ {{ "title": "Data", "items": ["Python", "SQL"] }} ]
    }}
    """
    
    json_response = gemini_client.models.generate_content(
        model='gemini-2.5-pro-latest',
        contents=json_prompt,
        config=types.GenerateContentConfig(
            system_instruction="You are a strict data parser. You must return ONLY raw, valid JSON. Do not use markdown blocks. Do not include any text before or after the JSON."
        )
    )
    portfolio_data = json.loads(json_response.text)

    # ==========================================================
    # UPDATED GEMINI PHASE 2: Generate CSS
    # ==========================================================
    print("Asking Gemini to generate CSS Theme...")
    css_prompt = f"""
    Generate ONLY standard Tailwind CSS variables based on this portfolio: {json_response.text}.
    DO NOT include `@import "tailwindcss";`. Only output the :root variables and @layer base.
    
    Format exactly like this:
    :root {{ --background: oklch(1 0 0); --primary: oklch(0.2 0.04 265); }}
    .dark {{ --background: oklch(0.1 0.04 265); }}
    @layer base {{ body {{ background-color: var(--background); color: var(--foreground); }} }}
    """
    
    css_response = gemini_client.models.generate_content(
        model='gemini-2.5-flash-latest',
        contents=css_prompt
    )
    theme_css = css_response.text.replace("```css", "").replace("```", "").strip()

    print("Pushing to Supabase Database...")
    chosen_template = random.choice(TEMPLATES)

    supabase.table("portfolios").upsert({
        "student_id": student_id,
        "template_id": chosen_template,
        "portfolio_data": portfolio_data,
        "theme_css": theme_css,
        "is_premium": False 
    }, on_conflict="student_id").execute()

    print(f"SUCCESS! Portfolio live at: /{student_id}")
    return {"url": f"/{student_id}", "template": chosen_template}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
