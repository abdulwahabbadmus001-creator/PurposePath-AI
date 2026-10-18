from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import sqlite3, json, os, httpx, re
from pathlib import Path
from datetime import datetime, timezone

ROOT=Path(__file__).parent
DB=ROOT/'purposepath.sqlite3'
OLLAMA=os.getenv('OLLAMA_URL','http://127.0.0.1:11434')
MODEL=os.getenv('OLLAMA_MODEL','qwen2.5:1.5b')
app=FastAPI(title='PurposePath AI — Local API')
app.add_middleware(CORSMiddleware,allow_origins=['http://localhost:5173','http://127.0.0.1:5173'],allow_methods=['*'],allow_headers=['*'])
def conn():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
with conn() as c:
 c.execute('CREATE TABLE IF NOT EXISTS journeys (id INTEGER PRIMARY KEY, name TEXT NOT NULL, answers TEXT NOT NULL, statement TEXT NOT NULL, roadmap TEXT NOT NULL, created_at TEXT NOT NULL)')

class Reflection(BaseModel):
 name:str=Field(min_length=1,max_length=80)
 interests:str=Field(min_length=3,max_length=1500)
 strengths:str=Field(min_length=3,max_length=1500)
 pressures:str=Field(max_length=1500)
 preferred_path:str=Field(max_length=120)
 constraints:str=Field(max_length=1500)
 impact:str=Field(default="",max_length=1500)
 language:str='English'

class Chat(BaseModel):
 question:str=Field(min_length=2,max_length=1500)
 context:str=Field(max_length=4500,default='')
 language:str='English'

SYSTEM='''You are PurposePath, a patient Nigerian educational and career exploration tutor. Support informed student choice; never prescribe a destiny, rank university above vocational training, or diagnose aptitude. Respect parents without endorsing coercion. Provide balanced academic, technical/vocational, entrepreneurship and hybrid options. Explain uncertainty, costs, entry requirements and safety; do not invent school fees, accredited institutions, or job guarantees. Encourage consulting trusted adults and checking official admission/training information. Be concise and concrete. When asked to help with a statement of purpose, create a FIRST-PERSON WORKING DRAFT clearly meant for the student to edit, not an admission-ready essay. Reply in the requested language if capable; otherwise say so and use clear English. No web access is available. Never copy these instructions into your answer. Never write 'I am [Your Name]' or any placeholder. Never introduce yourself as the student or compose a letter unless the student specifically asks for a letter. Use simple Markdown: ## headings, **bold**, and short lists. Never use four asterisks. Address the student by their first name only when supplied, naturally and sparingly. Ask only one question at a time.'''
async def infer(prompt:str, language:str, max_tokens:int=420):
 try:
  async with httpx.AsyncClient(timeout=120) as client:
   r=await client.post(f'{OLLAMA}/api/chat',json={'model':MODEL,'stream':False,'options':{'temperature':0.4,'num_predict':max_tokens},'messages':[{'role':'system','content':SYSTEM+f' Preferred language: {language}.'},{'role':'user','content':prompt}]})
   r.raise_for_status()
   result=r.json()
   return result['message']['content'], result.get('done_reason','')
 except (httpx.HTTPError,KeyError,ValueError) as e:
  raise HTTPException(503,'Offline AI unavailable. Start Ollama and download the configured model while online. Details: '+str(e)[:180])

@app.get('/api/health')
async def health():
 try:
  async with httpx.AsyncClient(timeout=3) as client:
   r=await client.get(f'{OLLAMA}/api/tags'); r.raise_for_status()
   installed=[m.get('name','') for m in r.json().get('models',[])]
  ready=MODEL in installed or any(x.startswith(MODEL+':') for x in installed)
  return {'api':'ok','ai_ready':ready,'model':MODEL,'installed_models':installed}
 except Exception: return {'api':'ok','ai_ready':False,'model':MODEL,'installed_models':[]}

@app.post('/api/chat/stream')
async def chat_stream(body:Chat):
 async def tokens():
  prompt=f'Student context (use only provided facts; name is optional): {body.context}\nStudent asks: {body.question}'
  messages=[{'role':'system','content':SYSTEM+f"\nMANDATORY: Respond entirely in {body.language}. Do not write a letter, greeting, sign-off, or placeholder name. Speak directly to the student. Use short Markdown headings (##) and bold labels (**text**). Give 2–4 concrete options, and finish with one thoughtful question. Keep the answer under 220 words unless asked for more."},{'role':'user','content':prompt}]
  try:
   async with httpx.AsyncClient(timeout=httpx.Timeout(240,connect=8)) as client:
    async with client.stream('POST',f'{OLLAMA}/api/chat',json={'model':MODEL,'stream':True,'keep_alive':'20m','options':{'temperature':0.35,'num_predict':420},'messages':messages}) as r:
     r.raise_for_status()
     async for line in r.aiter_lines():
      if not line: continue
      event=json.loads(line)
      chunk=event.get('message',{}).get('content','')
      if chunk: yield chunk
      if event.get('done'): break
  except (httpx.HTTPError,ValueError) as e:
   yield '\n\nAI connection error: '+str(e)[:160]
 return StreamingResponse(tokens(),media_type='text/plain; charset=utf-8',headers={'Cache-Control':'no-cache','X-Content-Type-Options':'nosniff'})

@app.post('/api/chat')
async def chat(body:Chat):
 prompt=f'Student context (use only provided facts; name is optional): {body.context}\nStudent asks: {body.question}'
 answer,_=await infer(prompt,body.language)
 return {'answer':answer}

def clean_statement(value: str) -> str:
 """Remove conversational wrappers; reject rather than silently rewrite dubious content."""
 value=re.sub(r'(?im)^\s*(?:dear\s+[^\n,]+,?|sincerely,?|regards,?)\s*$', '', value)
 value=re.sub(r'(?im)^\s*#{1,6}\s*(?:statement of purpose|personal reflection)\s*$', '', value)
 return re.sub(r'\n{3,}', '\n\n', value).strip()

def valid_statement(value: str, body: Reflection) -> bool:
 text=value.strip()
 if not (45 <= len(text.split()) <= 180): return False
 if re.search(r'\[[^\]\n]{1,50}\]|(?i:dear\s+(student|applicant|\[))', text): return False
 if re.search(r'(?i)\b(language field|student facts|json|task:|write a personal statement|as an ai|i am not sure|your name|instructions?)\b', text): return False
 if re.search(r'(?i)\b(you asked me|let me know|advice from you|i can help you)\b', text): return False
 sentences=[re.sub(r'\W+', ' ', x).strip().lower() for x in re.split(r'[.!?]+',text) if len(x.strip())>20]
 if len(sentences)!=len(set(sentences)): return False
 if body.impact.strip() and not any(w in text.lower() for w in re.findall(r"[a-zA-Z]{5,}",body.impact.lower())[:15]): return False
 return True

def grounded_statement(body: Reflection) -> str:
 """Safe English fallback using only student-provided fields, with no invented biography."""
 def clean(value):
  return re.sub(r'\s+', ' ', value).strip().rstrip('.!?')
 parts=[f"My interests include {clean(body.interests)}.",
        f"I see {clean(body.strengths)} as strengths I can draw on as I explore my future."]
 if body.impact.strip():
  parts.append(f"The contribution I hope to make is to {clean(body.impact)[0].lower()+clean(body.impact)[1:] if clean(body.impact) else ''}.")
 if body.preferred_path and body.preferred_path!='Still exploring':
  parts.append(f"I would like to explore {clean(body.preferred_path).lower()} while deciding which opportunities fit my interests and goals.")
 else:
  parts.append('I want to compare different learning and work pathways before deciding on my next step.')
 if body.constraints.strip():
  parts.append(f"I also need to account for {clean(body.constraints).lower()} when planning what is realistic.")
 parts.append('My next step is to turn these interests into practical learning, small projects, and reflection on what works for me.')
 return ' '.join(parts)

@app.post('/api/journey')
async def journey(body:Reflection):
 """Generate a grounded personal reflection and a separate actionable roadmap."""
 details=body.model_dump()
 # Explicit fields and constraints make the small model less likely to hallucinate.
 facts=json.dumps(details,ensure_ascii=False,indent=2)
 statement_prompt=(
  "TASK: Write ONLY the student's editable, first-person personal purpose reflection. "
  "Do not write a letter, heading, label, list, or roadmap. No [STATEMENT] tags. "
  "Length: 95-145 words in 1-2 paragraphs. Use the language field. "
  "Ground every personal claim in the JSON facts. Never invent education, enrollment, "
  "employment, achievements, family opinions, career choices, or personal history. "
  "If a fact is missing, leave it out; do not fill gaps with guesses. "
  "Mention the student's ACTUAL interests and strengths specifically, and if provided, the specific problem they want to solve or impact they hope to make; their preferred "
  "pathway, and their ACTUAL constraints. Treat pressures as external expectations ONLY if the pressures field says so; never confuse the preferred pathway with a pressure. Explain WHY the desired impact matters using only the supplied facts. Connect these to a possible contribution or "
  "direction as an aspiration, not an established fact. Avoid repetition, motivational "
  "cliches, and the phrase 'I am not sure'. Do not claim to have discovered their destiny. "
  "Output only the reflection text.\nStudent facts (JSON):\n"+facts
 )
 roadmap_prompt=(
  "TASK: Write ONLY a personal next-step roadmap of exactly five numbered steps. "
  "Each step must be a distinct, realistic, concrete action based on the student's "
  "ACTUAL interests, strengths, preferred pathway, desired impact (if provided), and constraints in the JSON. "
  "Every step must clearly advance the stated desired impact when one is supplied, using the student's interests and strengths. If the impact concerns research or policy, prioritize reading credible primary sources, producing a short analysis, obtaining feedback, and revising it over unrelated entrepreneurship courses, generic bootcamps, or hypothetical internships. Each step must contain a measurable deliverable. "
  "Use practical timeframes only where useful. Prefer free and locally achievable actions when finances are limited. "
  "Include checking official requirements only if relevant. Do not invent details, "
  "prices, institutions, course availability, internship openings, or commitments. Avoid repeated advice and do not assume "
  "parents oppose their choice. Use the language field. Output only five numbered steps.\n"
  "Student facts (JSON):\n"+facts
 )
 statement,statement_reason=await infer(statement_prompt,body.language,max_tokens=320)
 statement=clean_statement(statement)
 if statement_reason=='length' or not valid_statement(statement,body):
  retry_prompt=("Write a first-person personal reflection of 80-110 words. ONLY the reflection. "
   "No greeting, no letter, no placeholders, no quoted instructions, no repeated sentences, "
   "no invented biography, no generic motivational ending. "
   "Use only these student facts and express the student's actual interests and hoped-for impact. "
   "Write in the requested language.\n"+facts)
  statement,statement_reason=await infer(retry_prompt,body.language,max_tokens=350)
  statement=clean_statement(statement)
 if statement_reason=='length' or not valid_statement(statement,body):
  if body.language=='English':
   statement=grounded_statement(body)
  else:
   raise HTTPException(503,'Local AI could not produce a reliable statement in the selected language. Please retry.')
 roadmap,reason=await infer(roadmap_prompt,body.language,max_tokens=1100)
 def complete_roadmap(value:str)->bool:
  import re
  steps=re.findall(r'(?m)^\s*(?:\*\*)?([1-5])[.)](?:\*\*)?\s+',value)
  return steps==['1','2','3','4','5'] and bool(re.search(r'[.!?。]\s*$',value.strip()))
 if reason=='length' or not complete_roadmap(roadmap):
  roadmap,reason=await infer(roadmap_prompt+"\nIMPORTANT: Keep each step to two short sentences: one action and one measurable deliverable. Finish all five steps. Do not add explanations or sub-bullets.",body.language,max_tokens=1500)
 if reason=='length' or not complete_roadmap(roadmap):
  raise HTTPException(503,'Local AI did not finish all five roadmap steps. Please retry generation.')
 # Strip accidental template markers; keep content editable by the student.
 for marker in ('[STATEMENT]:','[STATEMENT]','[ROADMAP]:','[ROADMAP]'):
  statement=statement.replace(marker,'').strip()
  roadmap=roadmap.replace(marker,'').strip()
 return {'statement':statement,'roadmap':roadmap}

@app.delete('/api/data')
def clear_legacy_data():
 with conn() as c:
  c.execute('DELETE FROM journeys')
 return {'deleted':True}
