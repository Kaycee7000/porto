import os
import json
import random
import requests
import warnings
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import google.generativeai as genai
from supabase import create_client, Client
from dotenv import load_dotenv

# 1. SETUP & SECURE ENVIRONMENT VARIABLES
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") # Use the service role key for backend inserts

genai.configure(api_key=GEMINI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()

# Allow Astro frontend to talk to this Python API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Change to your Cloudflare URL in production
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

    # 1. GET USER INFO (To create their custom URL /student_id)
    print("Fetching User Profile...")
    profile_res = requests.get("https://graph.microsoft.com/v1.0/me", headers=headers)
    if not profile_res.ok:
        raise HTTPException(status_code=401, detail="Invalid OneDrive Token")
    
    user_data = profile_res.json()
    # Create a URL-friendly student ID (e.g., "John Doe" -> "john-doe")
    student_id = user_data.get("displayName", "student").lower().replace(" ", "-")

    # 2. THE SMART FILTER: Ask Microsoft Graph for the 10 most relevant portfolio docs
    print("Running Smart Filter to find Capstones and Projects...")
    search_url = "https://graph.microsoft.com/v1.0/me/drive/root/search(q='capstone OR project OR lab OR thesis')?$top=10"
    search_res = requests.get(search_url, headers=headers)
    files = search_res.json().get("value", [])

    text_corpus = ""
    
    # 3. DOWNLOAD & EXTRACT TEXT
    print(f"Found {len(files)} relevant documents. Extracting text...")
    for f in files:
        download_url = f.get("@microsoft.graph.downloadUrl")
        if download_url and f["name"].endswith(('.txt', '.md', '.csv', '.py', '.js', '.json')):
            doc_res = requests.get(download_url)
            text_corpus += f"\n--- FILE: {f['name']} ---\n{doc_res.text[:5000]}" # Limit to first 5000 chars per doc to save context

    # 4. GEMINI PHASE 1: Generate JSON Data
    print("Asking Gemini to generate JSON Portfolio Data...")
    model_pro = genai.GenerativeModel('gemini-1.5-pro')
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
    json_response = model_pro.generate_content(json_prompt, generation_config={"response_mime_type": "application/json"})
    portfolio_data = json.loads(json_response.text)

    # 5. GEMINI PHASE 2: Generate CSS (Without @import tailwindcss!)
    print("Asking Gemini to generate CSS Theme...")
    css_prompt = f"""
    Generate ONLY standard Tailwind CSS variables based on this portfolio: {json_response.text}.
    DO NOT include `@import "tailwindcss";`. Only output the :root variables and @layer base.
    
    Format exactly like this:
    :root {{ --background: oklch(1 0 0); --primary: oklch(0.2 0.04 265); }}
    .dark {{ --background: oklch(0.1 0.04 265); }}
    @layer base {{ body {{ background-color: var(--background); color: var(--foreground); }} }}
    """
    model_flash = genai.GenerativeModel('gemini-1.5-flash')
    css_response = model_flash.generate_content(css_prompt)
    theme_css = css_response.text.replace("```css", "").replace("```", "").strip()

    # 6. RANDOMIZE LAYOUT & PUSH TO SUPABASE
    print("Pushing to Supabase Database...")
    chosen_template = random.choice(TEMPLATES)

    # Upsert (Insert or Update) the user in the database
    supabase.table("portfolios").upsert({
        "student_id": student_id,
        "template_id": chosen_template,
        "portfolio_data": portfolio_data,
        "theme_css": theme_css,
        "is_premium": False # Watermark is ON by default
    }, on_conflict="student_id").execute()

    print(f"SUCCESS! Portfolio live at: /{student_id}")
    return {"url": f"/{student_id}", "template": chosen_template}

if __name__ == "__main__":
    import uvicorn
    # Run the API server on port 8000
    uvicorn.run(app, host="0.0.0.0", port=8000)
