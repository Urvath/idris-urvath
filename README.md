# BhashaBiz — Speak it. Snap it. Ask it. 🪴

> **Buildathon architecture: direct Sarvam APIs only. No Sarvam Voice Agent / Agent deployment is used or required.** The Flask app orchestrates separate API calls for STT, chat/tool-calling, TTS, and document processing.

An **AI CFO for India's small shops**. The owner talks (Malayalam by default — or
English, Manglish, Tamil, Hindi, any mix), snaps a supplier bill, or asks a question.
The app keeps the books, answers with **exact numbers**, compares this week with
last week, and checks **bank-readiness** — all explained in the owner's own language,
like a trusted family accountant.

Built entirely on **Sarvam AI**.

---

## 1. How to run it (for non-coders)

You need **Python 3.9+** installed.

**Windows:** double-click `run.bat`
**Mac / Linux:** open a terminal in this folder and run `bash run.sh`

Or manually:

```
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5000** in Chrome.

**One-time setup:** click **⚙️ Settings** (top right) and paste your Sarvam API key
(free at dashboard.sarvam.ai). Use **headphones 🎧** for the smoothest conversation.

The app starts with **two weeks of sample data for "Thomas Stores", a small grocery
in Kasaragod, Kerala**, already loaded. There are three tabs:

- **🗣️ Talk** — the voice conversation (mic, or type)
- **🧾 Scan** — upload a supplier bill photo/PDF, check it, save it
- **📊 Insights** — today's numbers, this week vs last week, who owes money, bank-readiness, the full book

---

## 2. Test script (with the exact numbers the app computes)

Reset the sample data first (Insights tab → **Reload sample data**), then try:

| Say / do | What you should get |
|---|---|
| **“നമസ്കാരം! ഇന്ന് മിനി 400 രൂപാ അരി കടം വാങ്ങി.”** (Namaskaram! Mini took 400 rupees of rice on credit) | Read-back: “Mini, rice, 400 rupees, credit. Correct?” |
| “Yes / അതെ / Aano” | “Saved. Mini now owes you **1,400 rupees** in total.” — watch the Insights tab update |
| **“എന്റെ പണം കുറവാണെങ്കിലും വിൽപ്പന കൂടുതലാണോ?”** — *“Why is my cash lower even though sales are up?”* | The agent calls **compare_weeks** and explains: sales went **up by ₹10,200** (₹40,100 vs ₹29,900), but cash is down because supplier payments went **up ₹11,000** (₹27,500 vs ₹16,500), credit given went **up ₹8,800** (₹14,300 vs ₹5,500), and only **₹500** of it came back (vs ₹2,300). Net cash: **−₹11,500 this week vs +₹600 last week**. Then 1–2 actions |
| “Who owes me money?” | Anwar ₹6,000 · Rasheed ₹5,000 · Girija ₹3,200 · Fathima ₹1,800 · Mini ₹1,400 — total ₹17,400 (after the Mini entry above; ₹17,000 on fresh sample data) |
| “How much did I sell this week?” | **₹40,100** (last 7 days including today) |
| “Did I make a profit?” | All-time: sales ₹70,000, costs ₹59,400, profit **₹10,600** — 15% of sales |
| “How is my business doing?” | The health check: profitable on paper, but money is sitting outside as credit |
| “Am I ready for a bank loan?” | **Bank-readiness check: 65/100** with what a bank looks for on each factor — plus the mandatory line that this is not a credit score and banks decide themselves |
| “Suresh paid 500 rupees” | **Refused**: Suresh owes ₹0 right now (his ₹1,500 credit from last week was fully paid). The agent asks you to double-check the amount or name — nothing is saved |
| “Any pending payments to remind?” | Girija ₹3,200 waiting 10 days, Anwar ₹2,000 waiting 8 days, Mini ₹1,000 waiting 12 days, Rasheed ₹3,500 waiting 3 days (amounts never exceed what each person still owes today) |
| “End of day summary” | Today: sales ₹4,300 (before the Mini entry; ₹4,700 after), cash change +₹2,800 |
| “That was wrong, the rent was 4,500 not 4,000” | BhashaBiz reads back old → new and saves only after your yes |
| **Scan tab:** upload any bill photo | Reads supplier, date, items, total → you check → saved to the book |
| Switch to English/Hindi mid-conversation | The assistant switches too |

The **Insights tab** shows all of this live, with this week vs last week as a table.

---

## 3. Which Sarvam models are used, and for what (for the judges)

| Sarvam API | Model | Role in BhashaBiz |
|---|---|---|
| **Speech-to-Text** | **Saaras v4** | Listens to the owner (Malayalam, Tamil, Hindi, English, code-mixed), with auto language detection. Known customer names are passed as **keyterms** so names are heard correctly |
| **Chat + tool calling** | **Sarvam-105B** | Direct Chat Completions API with tool calling: decides what to do, structures transactions, phrases every reply in the owner's language and mix |
| **Text-to-Speech** | **Bulbul v3** | The agent's voice |
| **Language Identification** | text LID | Picks the voice language for typed input |
| **Transliteration** | Mayura | Customer names are always stored in English letters, even when spoken in Malayalam/Tamil/Hindi script |
| **Document AI** | **Sarvam Vision 1.5** | Reads supplier bills for the Scan tab |

**The direct API orchestration loop:** owner speaks → Saaras v4 transcribes → Sarvam-105B replies **or calls a tool** → the tool runs in plain Python → the exact result goes back to the model → the final reply is spoken by Bulbul v3.

**Numbers are never computed by the model.** Every figure comes from a deterministic Python tool (`compare_weeks`, `get_dues`, `get_profit`, `bank_readiness`, …). The model is instructed to speak only numbers returned by deterministic tools in the same turn, and to say so if it cannot answer reliably.

**Saves are confirmed in code, not just prompted.** `propose_entry` only *stages* an entry and returns the read-back; `confirm_entry` is the only function that writes, and only when something is staged. Undo and correction work the same way (`propose_undo` / `propose_correction` → read-back → yes → `confirm_entry`). A payment larger than what that person owes is refused outright. Scanned bills go through the same propose→confirm path — the owner's "Confirm" click in the Scan tab is the yes.

**Name handling.** Spoken names are transliterated to English letters (Sarvam transliteration), then matched to existing people in the book — handling script differences, spelling variations, and suffixes like chettan, chechi, akka, anna, bhai — so one customer is always one entry. The agent can list customers with `list_customers`, and known names are fed to speech recognition as keyterms.

**Bank-readiness check (0–100).** What a bank looks for, where the shop stands, and which records are weak: profit the bank can see (25), cash coming in (20), getting credit money back (20), not depending on one customer (15), regular records (20). It is presented as guidance only — the assistant always says it is not a credit score or a lending decision, and banks decide for themselves.

### Safeguards

- No investment / tax / legal advice — the assistant points to a bank officer or accountant.
- No invented entries or numbers; unclear input gets one short clarifying question at a time.
- Data stays on the owner's machine (a local JSON file).

---

## 4. Files

| File | What it is |
|---|---|
| `app.py` | Everything server-side: agent brain, 15 exact-math tools, propose→confirm flow, name matching, bill scanning, week comparison, bank-readiness |
| `templates/index.html` | The Talk / Scan / Insights interface |
| `bhashabiz_data.json` | The book — created automatically |
| `requirements.txt`, `run.bat`, `run.sh` | Install & start |

## 5. Troubleshooting

- **Key rejected** — paste a fresh key in Settings.
- **Mic not working** — check the browser's mic permission (lock icon → Microphone → Allow), or use the typing box (same brain, same voice).
- **Agent silent** — press play once (browsers need one click), or pick another voice.
- **Busy / rate limit (429)** — wait a few seconds and speak again.
- **Bill not read** — retake the photo (flat, good light), or tell the use the Talk tab.
- **Fresh start** — Insights tab → Reload sample data / Clear book.


## 6. Important buildathon note: no Voice Agent deployment

You do **not** create or deploy a Sarvam Voice Agent in the Sarvam dashboard. The project calls the individual APIs directly from the Flask backend:

1. `speech_to_text.transcribe(...)` — Saaras speech-to-text.
2. `chat.completions(...)` — Sarvam-105B with tool definitions.
3. Local Python functions — deterministic ledger operations and calculations.
4. `text_to_speech.convert(...)` — Bulbul speech generation.
5. `doc_ai.*` — asynchronous document digitisation for invoice scans.

The only app you run/deploy is this Flask web app. For a local buildathon demo, run `python app.py` and open the local URL. For a public demo, host the Flask app on a Python-capable service and set `SARVAM_API_KEY` as a server-side environment variable. Do not commit your real API key or put it in a public repository. The current Settings field is convenient for local judging/demo use; for a public production deployment, replace browser-supplied keys with server-side environment secrets and add user authentication, per-user data isolation, and persistent database storage.
