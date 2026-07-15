"""
Quora scraper using Serper.dev + Playwright.

Strategy (best FREE approach available in 2026):
  1. Use Serper.dev (2,500 free searches/month, no credit card needed) to
     search `site:quora.com` and get exact, relevant Quora URLs.
     Sign up at: https://serper.dev  →  copy your API key to .env
  2. Fetch those specific Quora URLs with Playwright and extract answer text.
  3. Always also scrapes a list of curated fallback Quora URLs, with no key needed.

Environment variables:
  SERPER_API_KEY   – From https://serper.dev (free, 2,500 queries/month, no card)

Why Serper.dev over Google CSE?
  • Google Custom Search API is CLOSED to new customers (retired Jan 2027).
  • Serper.dev gives 2,500 free searches/month vs Google's 100/day (old limit).
  • No credit card required.  Sign-up takes ~30 seconds.
"""
import os
import re
import sys
import json
import hashlib
import sqlite3
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(dotenv_path="../.env")
load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "diaspora.db")

# ---------------------------------------------------------------------------
# Serper.dev search queries  (all target site:quora.com)
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

# ---------------------------------------------------------------------------
# Curated fallback Quora URLs  (always scraped even without Serper key)
# ---------------------------------------------------------------------------
FALLBACK_URLS = [
    {"url": "https://www.quora.com/search?q=Indian+community+Birmingham+UK",    "city": "Birmingham", "label": "Indian Community Birmingham"},
    {"url": "https://www.quora.com/search?q=Diwali+Leicester+UK",               "city": "Leicester",  "label": "Diwali Leicester"},
    {"url": "https://www.quora.com/search?q=British+Indian+Midlands+events",    "city": "Birmingham", "label": "British Indian Midlands"},
    {"url": "https://www.quora.com/search?q=Navratri+Birmingham+UK",            "city": "Birmingham", "label": "Navratri Birmingham"},
    {"url": "https://www.quora.com/search?q=Indian+diaspora+UK+Midlands",       "city": "Birmingham", "label": "Indian Diaspora UK Midlands"},
    {"url": "https://www.quora.com/q/Indian-Diaspora-3",                        "city": "Birmingham", "label": "Indian Diaspora Space"},
    {"url": "https://www.quora.com/q/British-Asians",                           "city": "Birmingham", "label": "British Asians Space"},
    {"url": "https://www.quora.com/q/South-Asian-Culture",                      "city": "Birmingham", "label": "South Asian Culture Space"},
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
    "related questions", "more questions", "continue reading",
    "all related", "view more", "load more",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_uk_relevant(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in UK_KEYWORDS)


def is_noisy(text: str) -> bool:
    lower = text.lower()
    return any(n in lower for n in NOISE_SUBSTRINGS)


def is_gibberish(text: str) -> bool:
    if not text:
        return True
    tokens = re.split(r"\s+", text.strip())
    real_words = [w for w in tokens if len(w) > 1]
    if len(real_words) < 4:
        return True
    single_char_ratio = sum(1 for w in tokens if len(w) == 1) / max(len(tokens), 1)
    return single_char_ratio > 0.35


def analyze_with_gemini(text: str) -> dict:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key in ["MY_GEMINI_API_KEY", "YOUR_GEMINI_API_KEY"]:
        return {"sentiment": "Neutral", "city": "Birmingham", "isUpcoming": False, "event": "Community Event"}

    import httpx

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.1-flash-lite:generateContent?key={api_key}"
    )
    prompt = f"""
    Analyze the following Quora post regarding Indian diaspora community events in the UK Midlands.
    Extract the following information in strict JSON format:
    1. "sentiment": must be one of "Positive", "Neutral", "Negative".
    2. "city": must be the UK Midlands city mentioned (e.g., "Birmingham", "Leicester", "Coventry", "Nottingham", "Wolverhampton"). If none is mentioned, default to "Birmingham".
    3. "isUpcoming": boolean indicating if this refers to a future planned event or upcoming activity.
    4. "event": a short title for the event or topic (max 80 chars).

    Post text: "{text[:500]}"

    Respond with ONLY the JSON object, no markdown formatting.
    """
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.3},
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            reply = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            match = re.search(r"\{.*\}", reply, re.DOTALL)
            if match:
                return json.loads(match.group(0))
    except Exception as exc:
        print(f"[WARN] Gemini error: {exc}")

    return {"sentiment": "Neutral", "city": "Birmingham", "isUpcoming": False, "event": "Community Event"}


def upsert_local(item: dict):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_items (
            id TEXT PRIMARY KEY, platform TEXT, author TEXT, date TEXT,
            event TEXT, text TEXT, sentiment TEXT, city TEXT, isUpcoming INTEGER
        )
        """
    )
    cursor.execute(
        """
        INSERT OR REPLACE INTO feedback_items
            (id, platform, author, date, event, text, sentiment, city, isUpcoming)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item["id"], item["platform"], item["author"], item["date"],
            item["event"], item["text"], item["sentiment"], item["city"],
            1 if item["isUpcoming"] else 0,
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Step 1: Serper.dev  →  discover relevant Quora URLs
# ---------------------------------------------------------------------------

def fetch_quora_urls_via_serper(queries: list[str]) -> list[dict]:
    """
    Call Serper.dev with site:quora.com queries to find targeted Quora URLs.

    Free tier: 2,500 queries/month — no credit card required.
    Sign up at: https://serper.dev → Dashboard → copy API key → add to .env

    Returns list of {url, city, label, snippet} dicts.
    """
    api_key = os.getenv("SERPER_API_KEY", "")
    if not api_key or api_key in ("your_serper_api_key", ""):
        print("[INFO] SERPER_API_KEY not set. Skipping Serper step.")
        print("       Get a free key (2,500/mo, no card) at: https://serper.dev")
        return []

    import httpx

    results = []
    endpoint = "https://google.serper.dev/search"
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

    for query in queries:
        payload = {
            "q": query,
            "num": 10,
            "gl": "uk",      # UK results
            "hl": "en",
        }
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(endpoint, json=payload, headers=headers)

            if resp.status_code == 200:
                data = resp.json()
                organic = data.get("organic", [])
                for item in organic:
                    link = item.get("link", "")
                    if "quora.com" not in link:
                        continue
                    snippet = item.get("snippet", "")
                    title = item.get("title", "")
                    # Rough city detection from snippet/title
                    city = "Birmingham"
                    for c in ["Leicester", "Coventry", "Nottingham", "Wolverhampton", "Derby"]:
                        if c.lower() in (snippet + title).lower():
                            city = c
                            break
                    results.append({
                        "url": link,
                        "city": city,
                        "label": query.replace("site:quora.com ", "")[:60],
                        "snippet": snippet,
                    })
                print(f"[Serper] '{query[:55]}...' → {len(organic)} results")

            elif resp.status_code == 401:
                print("[ERROR] Serper.dev: Invalid API key. Check SERPER_API_KEY in .env")
                break
            elif resp.status_code == 429:
                print("[WARN] Serper.dev rate limit hit — stopping Serper queries.")
                break
            else:
                print(f"[WARN] Serper error {resp.status_code}: {resp.text[:200]}")

        except Exception as exc:
            print(f"[WARN] Serper request failed: {exc}")

    # Deduplicate URLs
    seen: set = set()
    unique = [r for r in results if r["url"] not in seen and not seen.add(r["url"])]
    print(f"[Serper] {len(unique)} unique Quora URLs discovered.")
    return unique


# ---------------------------------------------------------------------------
# Step 2: Playwright  →  extract text from Quora URLs
# ---------------------------------------------------------------------------

def scrape_quora_page(page, url: str, scroll_times: int = 4) -> list[str]:
    """Navigate to a Quora URL and extract visible answer/post text blocks."""
    try:
        page.goto(url, timeout=60_000, wait_until="domcontentloaded")
    except Exception as exc:
        print(f"[WARN] Failed to load {url}: {exc}")
        return []

    for selector in [
        "div.q-text",
        "span.q-text",
        "[class*='q-text']",
        "div[class*='qu-']",
        "article",
        "main",
    ]:
        try:
            page.wait_for_selector(selector, timeout=6_000)
            break
        except Exception:
            continue

    for _ in range(scroll_times):
        page.evaluate("window.scrollBy(0, 900)")
        page.wait_for_timeout(1_200)

    texts: list[str] = page.evaluate(
        """() => {
            const results = [];
            const seen = new Set();
            const selectors = document.querySelectorAll(
                'div.q-text, span.q-text, [class*="q-text"], p, article, ' +
                'div[data-testid="answer-content"]'
            );
            for (const el of selectors) {
                const style = window.getComputedStyle(el);
                if (
                    style.display === 'none' ||
                    style.visibility === 'hidden' ||
                    style.opacity === '0'
                ) continue;
                const text = (el.innerText || '').trim();
                if (text.length < 50 || seen.has(text)) continue;
                seen.add(text);
                results.push(text);
            }
            return results;
        }"""
    )
    return texts or []


def process_texts(texts: list[str], source: dict, seen_hashes: set) -> list[dict]:
    """Filter, deduplicate and build DB item dicts from raw text blocks."""
    items = []
    ui_noise_words = {
        "Upvote", "Downvote", "Share", "Comment", "Follow", "More",
        "Answer", "View", "Report", "Collapse", "Continue Reading",
        "Profile photo", "Promoted", "Sponsored",
    }

    for raw in texts:
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
        content_lines = [ln for ln in lines if ln not in ui_noise_words]
        text = " ".join(content_lines).strip()

        if len(text) < 50:
            continue
        if is_noisy(text) or is_gibberish(text):
            continue
        if not is_uk_relevant(text):
            continue

        text_hash = hashlib.md5(text.lower()[:300].encode()).hexdigest()[:12]
        if text_hash in seen_hashes:
            continue
        seen_hashes.add(text_hash)

        analysis = analyze_with_gemini(text[:500])
        event = analysis.get("event") or "General Community Feedback 2026"
        if event.lower() in ("unknown", ""):
            event = "General Community Feedback 2026"

        city = source.get("city") or analysis.get("city", "Birmingham")
        label = source.get("label", "Quora")

        item = {
            "id": f"quora_{label.replace(' ', '_')[:30]}_{text_hash}",
            "platform": "Quora",
            "author": label,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "event": event,
            "text": text[:500],
            "sentiment": analysis.get("sentiment", "Neutral"),
            "city": city,
            "isUpcoming": bool(analysis.get("isUpcoming")),
        }
        items.append(item)

    return items


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[ERROR] Playwright not installed. Run: pip install playwright && playwright install chromium")
        return

    total_added = 0
    seen_hashes: set = set()

    # Step 1: Serper.dev to discover targeted Quora URLs
    serper_sources = fetch_quora_urls_via_serper(SERPER_QUERIES)

    # Step 2: Merge with fallback URLs (always scraped)
    all_sources = list(FALLBACK_URLS)
    seen_urls = {s["url"] for s in all_sources}
    for r in serper_sources:
        if r["url"] not in seen_urls:
            all_sources.append(r)
            seen_urls.add(r["url"])

    print(f"\n[INFO] Total Quora URLs to scrape: {len(all_sources)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-GB",
            extra_http_headers={
                "Accept-Language": "en-GB,en;q=0.9",
            },
        )

        page = context.new_page()

        # Block heavy resources to speed up and reduce bot signals
        def block_resources(route):
            if route.request.resource_type in ("image", "font", "media"):
                route.abort()
            else:
                route.continue_()

        page.route("**/*", block_resources)

        for source in all_sources:
            url = source["url"]
            label = source.get("label", "Quora")
            print(f"\n--- Quora | {label}\n    {url}")

            texts = scrape_quora_page(page, url)

            # If Serper gave us a snippet, include it as candidate text
            if source.get("snippet"):
                texts.append(source["snippet"])

            if not texts:
                print(f"[INFO] No content extracted.")
                continue

            items = process_texts(texts, source, seen_hashes)
            for item in items:
                try:
                    upsert_local(item)
                    total_added += 1
                    print(f"[SUCCESS] Added Quora post: {item['id']}")
                except Exception as exc:
                    print(f"[WARN] DB error: {str(exc)[:120]}")

            print(f"[INFO] Processed {len(items)} posts from {label}")
            page.wait_for_timeout(2_500)

        browser.close()

    print(f"\nQuora ingestion complete! Processed {total_added} items total.")


if __name__ == "__main__":
    main()
