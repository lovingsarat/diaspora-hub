"""
Quora scraper using Serper.dev (snippet-first, NO Playwright).

Serper.dev already extracts Quora answer text into search snippets.
We use those snippets directly as the data - no browser needed,
no Quora anti-bot issues, runs in seconds.

Free tier: 2,500 searches/month, no credit card. https://serper.dev
"""
import os
import re
import sys
import json
import hashlib
import sqlite3
import time
from datetime import datetime
from dotenv import load_dotenv

# Fix Windows console encoding
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

load_dotenv(dotenv_path="../.env")
load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "diaspora.db")

# ---------------------------------------------------------------------------
# Serper.dev queries  (all target site:quora.com)
# ---------------------------------------------------------------------------
SERPER_QUERIES = [
    "site:quora.com Indian diaspora Birmingham UK community events",
    "site:quora.com British Indian Midlands Diwali Navratri Birmingham",
    "site:quora.com Indian community Leicester UK festival culture",
    "site:quora.com South Asian British Midlands events",
    "site:quora.com Diwali Leicester Birmingham UK experience",
    "site:quora.com British Sikh Hindu Gujarati Punjabi Midlands community",
    "site:quora.com Navratri Garba UK Birmingham Leicester",
    "site:quora.com Indian culture UK Birmingham Coventry Nottingham",
]

UK_KEYWORDS = [
    "midlands", "birmingham", "leicester", "coventry", "nottingham",
    "wolverhampton", "derby", "walsall", "solihull", "uk", "united kingdom",
    "england", "british", "indian community", "diaspora", "garba", "navratri",
    "diwali", "hindu", "sikh", "gujarati", "punjabi", "bollywood", "samaj",
    "mandir", "temple", "festival", "community event", "cultural", "south asian",
    "british indian", "british asian", "mela", "vaisakhi", "holi", "bhangra",
]

NOISE_SUBSTRINGS = (
    "sign up", "sign in", "log in", "create account", "join quora",
    "answer this question", "add a comment", "see all answers",
    "be the first to answer", "ask quora",
    "privacy policy", "terms of service", "cookie policy",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_uk_relevant(text: str) -> bool:
    return any(kw in text.lower() for kw in UK_KEYWORDS)


def is_noisy(text: str) -> bool:
    return any(n in text.lower() for n in NOISE_SUBSTRINGS)


def is_gibberish(text: str) -> bool:
    if not text or len(text) < 30:
        return True
    tokens = re.split(r"\s+", text.strip())
    real_words = [w for w in tokens if len(w) > 1]
    if len(real_words) < 4:
        return True
    return False


def analyze_with_gemini(text: str) -> dict:
    import httpx
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key in ["MY_GEMINI_API_KEY", "YOUR_GEMINI_API_KEY"]:
        return {"sentiment": "Neutral", "city": "Birmingham", "isUpcoming": False, "event": "Community Event"}
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.1-flash-lite:generateContent?key={api_key}"
    )
    prompt = (
        f'Analyze this Quora post about Indian diaspora events in UK Midlands. '
        f'Return JSON with: "sentiment" (Positive/Neutral/Negative), '
        f'"city" (Birmingham/Leicester/Coventry/Nottingham/Wolverhampton, default Birmingham), '
        f'"isUpcoming" (bool), "event" (short title max 80 chars). '
        f'JSON only.\n\nText: "{text[:400]}"'
    )
    try:
        with httpx.Client(timeout=25.0) as client:
            resp = client.post(url, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3},
            })
        if resp.status_code == 200:
            reply = (
                resp.json().get("candidates", [{}])[0]
                .get("content", {}).get("parts", [{}])[0].get("text", "")
            )
            m = re.search(r"\{.*?\}", reply, re.DOTALL)
            if m:
                return json.loads(m.group(0))
    except Exception as exc:
        print(f"[WARN] Gemini: {exc}")
    return {"sentiment": "Neutral", "city": "Birmingham", "isUpcoming": False, "event": "Community Event"}


def upsert_local(item: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS feedback_items (
        id TEXT PRIMARY KEY, platform TEXT, author TEXT, date TEXT,
        event TEXT, text TEXT, sentiment TEXT, city TEXT, isUpcoming INTEGER, parent_id TEXT
    )""")
    try:
        c.execute("ALTER TABLE feedback_items ADD COLUMN parent_id TEXT")
    except sqlite3.OperationalError:
        pass
    c.execute("""INSERT OR REPLACE INTO feedback_items
        (id, platform, author, date, event, text, sentiment, city, isUpcoming, parent_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (item["id"], item["platform"], item["author"], item["date"],
         item["event"], item["text"], item["sentiment"], item["city"],
         1 if item["isUpcoming"] else 0, item.get("parent_id")))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Serper.dev search  ->  snippets as data
# ---------------------------------------------------------------------------

def fetch_via_serper(queries: list) -> list:
    """Query Serper.dev and return results with snippets as usable text."""
    import httpx
    api_key = os.getenv("SERPER_API_KEY", "")
    if not api_key or api_key in ("your_serper_api_key", ""):
        print("[INFO] SERPER_API_KEY not set. Get free key at https://serper.dev")
        return []

    results = []
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

    for query in queries:
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(
                    "https://google.serper.dev/search",
                    json={"q": query, "num": 10, "gl": "uk", "hl": "en"},
                    headers=headers,
                )
            if resp.status_code == 200:
                organic = resp.json().get("organic", [])
                for item in organic:
                    if "quora.com" not in item.get("link", ""):
                        continue
                    snippet = item.get("snippet", "")
                    title = item.get("title", "")
                    city = "Birmingham"
                    for c in ["Leicester", "Coventry", "Nottingham", "Wolverhampton", "Derby"]:
                        if c.lower() in (snippet + title).lower():
                            city = c
                            break
                    results.append({
                        "url": item.get("link", ""),
                        "title": title,
                        "snippet": snippet,
                        "city": city,
                        "label": query.replace("site:quora.com ", "")[:60],
                    })
                print(f"[Serper] Query {queries.index(query)+1}/{len(queries)}: {len(organic)} results")
            elif resp.status_code == 401:
                print("[ERROR] Serper: invalid API key")
                break
            elif resp.status_code == 429:
                print("[WARN] Serper rate limit")
                break
            else:
                print(f"[WARN] Serper {resp.status_code}")
        except Exception as exc:
            print(f"[WARN] Serper error: {exc}")

    # Deduplicate by URL
    seen: set = set()
    unique = [r for r in results if r["url"] not in seen and not seen.add(r["url"])]  # type: ignore
    print(f"[Serper] Total: {len(unique)} unique Quora snippets")
    return unique


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("[Quora] Starting snippet-first scraper (no browser required)...")

    seen_hashes: set = set()
    total_added = 0

    results = fetch_via_serper(SERPER_QUERIES)
    if not results:
        print("[WARN] No results. Set SERPER_API_KEY in .env")
        return

    print(f"\n[INFO] Analyzing {len(results)} Quora snippets with Gemini...")

    for r in results:
        # Combine title + snippet for richest context
        text = f"{r['title']}. {r['snippet']}".strip(" .")
        if not text or len(text) < 35:
            continue
        if is_noisy(text) or is_gibberish(text):
            continue
        if not is_uk_relevant(text):
            print(f"[SKIP] Not UK relevant: {text[:60]}...")
            continue

        text_hash = hashlib.md5(text.lower()[:300].encode()).hexdigest()[:12]
        if text_hash in seen_hashes:
            continue
        seen_hashes.add(text_hash)

        label = r["label"]
        city = r["city"]
        print(f"\n[Analyze] {text[:80]}...")

        analysis = analyze_with_gemini(text)
        event = analysis.get("event") or "General Community Feedback 2026"
        if not event or event.lower() == "unknown":
            event = "General Community Feedback 2026"

        item = {
            "id": f"quora_{label.replace(' ', '_')[:28]}_{text_hash}",
            "platform": "Quora",
            "author": label[:80],
            "date": datetime.now().strftime("%Y-%m-%d"),
            "event": event,
            "text": text[:500],
            "sentiment": analysis.get("sentiment", "Neutral"),
            "city": city or analysis.get("city", "Birmingham"),
            "isUpcoming": bool(analysis.get("isUpcoming")),
        }
        try:
            upsert_local(item)
            total_added += 1
            print(f"[SUCCESS] {item['sentiment']} | {item['city']} | {item['event'][:50]}")
        except Exception as exc:
            print(f"[WARN] DB error: {exc}")

        time.sleep(0.2)  # gentle Gemini rate limiting

    print(f"\n[DONE] Quora scraper complete. Added {total_added} items.")


if __name__ == "__main__":
    main()
