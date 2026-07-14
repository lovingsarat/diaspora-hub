"""Standalone Facebook scraper using Playwright. Run as a separate process to avoid asyncio conflicts."""
import os
import sys
import json
import sqlite3
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(dotenv_path="../.env")
load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "diaspora.db")

FACEBOOK_PAGES = [
    "AISfestival",
    "ShivamEvents",
    "CentralStageCrew",
    "SriBardai",
    "ShreePrajapatiAssociationLeicester",
]

PAGE_CITIES = {
    "AISfestival": "Leicester",
    "ShivamEvents": "Leicester",
    "CentralStageCrew": "Birmingham",
    "SriBardai": "Leicester",
    "ShreePrajapatiAssociationLeicester": "Leicester",
}

NOISE_PREFIXES = ("unread", "you have a new friend suggestion", "commented on",
                  "posted a memory", "shared a new reel", "shared", "was at",
                  "posted in")

def is_noisy(text):
    lower = text.lower()
    return lower.startswith(NOISE_PREFIXES) or "posted a memory" in lower or "friend suggestion" in lower

def is_uk_relevant(text, page):
    """Posts from curated Midlands Indian community pages are assumed relevant.
       Otherwise check for UK/Midlands/Indian community keywords."""
    if page in PAGE_CITIES:
        return True
    uk_keywords = [
        "midlands", "birmingham", "leicester", "coventry", "nottingham",
        "wolverhampton", "derby", "walsall", "solihull", "uk", "united kingdom",
        "england", "indian community", "diaspora", "garba", "navratri", "diwali",
        "hindu", "sikh", "gujarati", "punjabi", "bollywood", "samaj", "mandir",
        "temple", "festival", "community event", "cultural", "katha", "patotsav",
        "satsang", "puja", "havan", "british hindu"
    ]
    text_lower = text.lower()
    return any(kw in text_lower for kw in uk_keywords)

def analyze_with_gemini(text):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key in ["MY_GEMINI_API_KEY", "YOUR_GEMINI_API_KEY"]:
        return {"sentiment": "Neutral", "city": "Birmingham", "isUpcoming": False, "event": "Community Event"}

    import httpx
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={api_key}"
    prompt = f"""
    Analyze the following social media post regarding Indian diaspora community events in the UK Midlands.
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
            resp = client.post(url, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3},
            })
        if resp.status_code == 200:
            data = resp.json()
            reply = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            import re
            match = re.search(r'\{.*\}', reply, re.DOTALL)
            if match:
                return json.loads(match.group(0))
    except Exception as e:
        print(f"Error analyzing with Gemini: {e}")

    return {"sentiment": "Neutral", "city": "Birmingham", "isUpcoming": False, "event": "Community Event"}

def upsert_local(item):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedback_items (
            id TEXT PRIMARY KEY, platform TEXT, author TEXT, date TEXT,
            event TEXT, text TEXT, sentiment TEXT, city TEXT, isUpcoming INTEGER
        )
    """)
    cursor.execute("""
        INSERT OR REPLACE INTO feedback_items (id, platform, author, date, event, text, sentiment, city, isUpcoming)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (item["id"], item["platform"], item["author"], item["date"], item["event"],
          item["text"], item["sentiment"], item["city"], 1 if item["isUpcoming"] else 0))
    conn.commit()
    conn.close()

def main():
    from playwright.sync_api import sync_playwright

    c_user = os.getenv("FACEBOOK_C_USER", "")
    xs = os.getenv("FACEBOOK_XS", "")
    datr = os.getenv("FACEBOOK_DATR", "")

    if not c_user or not xs or not datr or c_user == "your_c_user":
        print("[WARNING] Facebook cookies not configured.")
        return

    cookies = {"c_user": c_user, "xs": xs, "datr": datr}
    print("Loaded Facebook cookies from .env.")

    pw_cookies = [
        {"name": name, "value": value, "domain": ".facebook.com", "path": "/"}
        for name, value in cookies.items()
    ]

    total_added = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-GB",
        )
        context.add_cookies(pw_cookies)
        page = context.new_page()

        for fb_page in FACEBOOK_PAGES:
            print(f"\n--- Fetching Facebook posts from {fb_page} ---")
            try:
                url = f"https://www.facebook.com/{fb_page}/posts/"
                page.goto(url, timeout=60000, wait_until="domcontentloaded")

                # Wait for posts to load
                try:
                    page.wait_for_selector('div[role="article"]', timeout=15000)
                except Exception:
                    print(f"[INFO] Waiting for articles on {fb_page}, trying scroll...")
                    page.evaluate("window.scrollBy(0, 500)")
                    page.wait_for_timeout(3000)
                    try:
                        page.wait_for_selector('div[role="article"]', timeout=10000)
                    except Exception:
                        print(f"[INFO] No articles found for {fb_page}")
                        continue

                # Scroll to load more posts
                for _ in range(3):
                    page.evaluate("window.scrollBy(0, 1000)")
                    page.wait_for_timeout(2000)

                # Extract post text using JavaScript — broad approach to find post content
                raw_posts = page.evaluate("""() => {
                    const posts = [];
                    // Get all div[dir="auto"] elements with substantial text
                    const textDivs = document.querySelectorAll('div[dir="auto"]');
                    const seen = new Set();
                    for (const div of textDivs) {
                        const text = div.textContent.trim();
                        if (text.length < 30 || seen.has(text)) continue;
                        // Skip if this text is a child of another div we already captured
                        seen.add(text);
                        // Skip UI elements
                        if (text.match(/^(Like|Comment|Share|Follow|See more|View more)/)) continue;
                        posts.push(text);
                    }
                    // Also check for span elements with post text
                    const spans = document.querySelectorAll('span[dir="auto"]');
                    for (const span of spans) {
                        const text = span.textContent.trim();
                        if (text.length < 30 || seen.has(text)) continue;
                        seen.add(text);
                        if (text.match(/^(Like|Comment|Share|Follow|See more|View more)/)) continue;
                        posts.push(text);
                    }
                    return posts;
                }""")
                count = 0

                # Deduplicate raw posts by content within this page run
                seen_texts = set()
                for post_text in raw_posts:
                    if not post_text or len(post_text) < 30:
                        continue

                    # Clean UI text
                    lines = [l.strip() for l in post_text.split("\n") if l.strip()]
                    ui_words = {"Like", "Comment", "Share", "Follow", "More", "Send",
                                "Not now", "Close", "Continue", "Allow", "View more comments",
                                "Write a comment", "Press Enter to send"}
                    content_lines = [l for l in lines if l not in ui_words
                                     and not l.startswith("All reactions")
                                     and not l.startswith("See more")]
                    post_text = " ".join(content_lines)

                    if not post_text or len(post_text) < 30:
                        continue
                    if is_noisy(post_text):
                        continue

                    # Use stable content hash to avoid duplicates across runs
                    import hashlib
                    text_hash = hashlib.md5(post_text.lower()[:300].encode()).hexdigest()[:12]
                    if text_hash in seen_texts:
                        continue
                    seen_texts.add(text_hash)

                    if not is_uk_relevant(post_text, fb_page):
                        continue

                    try:
                        post_id = f"facebook_{fb_page}_{text_hash}"
                        analysis = analyze_with_gemini(post_text[:500])

                        # Trust page-city mapping, fall back to Gemini
                        city = PAGE_CITIES.get(fb_page, analysis.get("city", "Birmingham"))

                        new_item = {
                            "id": post_id,
                            "platform": "Facebook",
                            "author": fb_page,
                            "date": datetime.now().strftime("%Y-%m-%d"),
                            "event": analysis.get("event", "General Community Feedback 2026"),
                            "text": post_text[:500],
                            "sentiment": analysis.get("sentiment", "Neutral"),
                            "city": city,
                            "isUpcoming": bool(analysis.get("isUpcoming")),
                        }
                        upsert_local(new_item)
                        total_added += 1
                        count += 1
                        print(f"[SUCCESS] Added Facebook post: {post_id}")
                    except Exception as e:
                        print(f"[WARN] Error processing post: {str(e).encode('ascii','ignore').decode()[:100]}")
                        continue

                print(f"[INFO] Processed {count} posts from {fb_page}")
            except Exception as e:
                print(f"Error fetching {fb_page}: {str(e).encode('ascii','ignore').decode()}")

        browser.close()

    print(f"\nFacebook ingestion complete! Processed {total_added} items.")

if __name__ == "__main__":
    main()
