"""Standalone Facebook scraper using Playwright. Run as a separate process to avoid asyncio conflicts."""
import os
import sys
import re
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

NOISE_SUBSTRINGS = (
    "page insights data", "privacy · terms", "ad choices · cookies",
    "information about page insights data", "advertising · ad choices",
    "centralstagecrew.contact@gmail.com",
)

PAGE_KEYWORDS = {
    "AISfestival": ["indian summer", "summer on the square", "festival", "leicester"],
    "ShivamEvents": ["shivam", "events", "leicester", "garba", "diwali"],
    "CentralStageCrew": ["central stage crew", "csc", "crew", "events", "midlands", "birmingham"],
    "SriBardai": ["sri bardai", "bardai", "brahmin", "samaj", "leicester", "katha", "patotsav"],
    "ShreePrajapatiAssociationLeicester": ["prajapati", "leicester", "samaj", "community"],
}

def is_noisy(text):
    lower = text.lower()
    if lower.startswith(NOISE_PREFIXES):
        return True
    if "posted a memory" in lower or "friend suggestion" in lower:
        return True
    if any(n in lower for n in NOISE_SUBSTRINGS):
        return True
    return False

def is_gibberish(text):
    """Reject Facebook obfuscated React text nodes and random base64-like strings."""
    if not text:
        return True
    tokens = [w for w in re.split(r'\s+', text) if w]
    # Real posts are made of readable words
    if not tokens:
        return True
    # No real word separators -> random encoded string
    if ' ' not in text and '\n' not in text and '\t' not in text and len(tokens) == 1:
        return True
    long_words = [w for w in tokens if len(w) > 1]
    if len(long_words) < 3:
        return True
    # Too many single-character tokens (vertical obfuscated text)
    single_char_ratio = sum(1 for w in tokens if len(w) == 1) / len(tokens)
    if single_char_ratio > 0.4:
        return True
    return False

def is_uk_relevant(text, page):
    """Check for Midlands / Indian diaspora keywords and page-specific terms."""
    text_lower = text.lower()
    uk_keywords = [
        "midlands", "birmingham", "leicester", "coventry", "nottingham",
        "wolverhampton", "derby", "walsall", "solihull", "uk", "united kingdom",
        "england", "indian community", "diaspora", "garba", "navratri", "diwali",
        "hindu", "sikh", "gujarati", "punjabi", "bollywood", "samaj", "mandir",
        "temple", "festival", "community event", "cultural", "katha", "patotsav",
        "satsang", "puja", "havan", "british hindu", "crew", "events", "stage",
        "central", "shivam", "bardai", "summer", "square", "leicester"
    ]
    if page in PAGE_KEYWORDS:
        uk_keywords = list(set(uk_keywords + PAGE_KEYWORDS[page]))
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
            event TEXT, text TEXT, sentiment TEXT, city TEXT, isUpcoming INTEGER, parent_id TEXT
        )
    """)
    try:
        cursor.execute("ALTER TABLE feedback_items ADD COLUMN parent_id TEXT")
    except sqlite3.OperationalError:
        pass
    cursor.execute("""
        INSERT OR REPLACE INTO feedback_items (id, platform, author, date, event, text, sentiment, city, isUpcoming, parent_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (item["id"], item["platform"], item["author"], item["date"], item["event"],
          item["text"], item["sentiment"], item["city"], 1 if item["isUpcoming"] else 0, item.get("parent_id")))
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

                # Extract visible post text and comment text from all articles
                articles_data = page.evaluate("""() => {
                    const results = [];
                    const articles = document.querySelectorAll('div[role="article"]');
                    for (const art of articles) {
                        const style = window.getComputedStyle(art);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                        
                        const dirs = art.querySelectorAll('div[dir="auto"], span[dir="auto"]');
                        const texts = [];
                        const seen = new Set();
                        for (const el of dirs) {
                            const innerStyle = window.getComputedStyle(el);
                            if (innerStyle.display === 'none' || innerStyle.visibility === 'hidden' || innerStyle.opacity === '0') continue;
                            const text = (el.innerText || '').trim();
                            if (text.length < 25 || seen.has(text)) continue;
                            if (text.match(/^(Like|Comment|Share|Follow|See more|View more|All reactions|Write a comment|Press Enter)/i)) continue;
                            seen.add(text);
                            texts.push(text);
                        }
                        
                        if (texts.length > 0) {
                            // The first block is usually the post text. Subsequent blocks are usually comments.
                            results.push({
                                post: texts[0],
                                comments: texts.slice(1)
                            });
                        }
                    }
                    return results;
                }""")
                count = 0

                # Deduplicate raw posts by content within this page run
                seen_texts = set()
                for art in articles_data:
                    post_text = art["post"]
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
                    if is_noisy(post_text) or is_gibberish(post_text):
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

                        event = analysis.get("event", "General Community Feedback 2026")
                        if not event or event.lower() == "unknown":
                            event = "General Community Feedback 2026"

                        # Trust page-city mapping, fall back to Gemini
                        city = PAGE_CITIES.get(fb_page, analysis.get("city", "Birmingham"))

                        new_item = {
                            "id": post_id,
                            "platform": "Facebook",
                            "author": fb_page,
                            "date": datetime.now().strftime("%Y-%m-%d"),
                            "event": event,
                            "text": post_text[:500],
                            "sentiment": analysis.get("sentiment", "Neutral"),
                            "city": city,
                            "isUpcoming": bool(analysis.get("isUpcoming")),
                            "parent_id": None
                        }
                        upsert_local(new_item)
                        total_added += 1
                        count += 1
                        print(f"[SUCCESS] Added Facebook post: {post_id}")

                        # Process up to 3 comments for this post
                        comments_added = 0
                        for comm_text in art["comments"]:
                            if comments_added >= 3:
                                break
                            
                            # Clean comment text
                            comm_lines = [l.strip() for l in comm_text.split("\n") if l.strip()]
                            comm_content = [l for l in comm_lines if l not in ui_words
                                             and not l.startswith("All reactions")
                                             and not l.startswith("See more")]
                            comm_clean = " ".join(comm_content).strip()
                            
                            if len(comm_clean) < 25 or is_noisy(comm_clean) or is_gibberish(comm_clean):
                                continue
                                
                            comm_hash = hashlib.md5(comm_clean.lower()[:300].encode()).hexdigest()[:12]
                            comment_id = f"facebook_comment_{fb_page}_{comm_hash}"
                            
                            print(f"  Analyzing comment: {comm_clean[:60]}...")
                            comm_analysis = analyze_with_gemini(comm_clean[:500])
                            
                            comm_item = {
                                "id": comment_id,
                                "platform": "Facebook",
                                "author": f"Commenter_{comm_hash[:6]}",
                                "date": datetime.now().strftime("%Y-%m-%d"),
                                "event": event, # inherit parent event
                                "text": comm_clean[:500],
                                "sentiment": comm_analysis.get("sentiment", "Neutral"),
                                "city": city, # inherit parent city
                                "isUpcoming": False,
                                "parent_id": post_id
                            }
                            upsert_local(comm_item)
                            total_added += 1
                            comments_added += 1
                            print(f"  [SUCCESS] Added Facebook comment: {comment_id}")

                    except Exception as e:
                        print(f"[WARN] Error processing post: {str(e).encode('ascii','ignore').decode()[:100]}")
                        continue

                print(f"[INFO] Processed {count} posts (plus comments) from {fb_page}")
            except Exception as e:
                print(f"Error fetching {fb_page}: {str(e).encode('ascii','ignore').decode()}")

        browser.close()

    print(f"\nFacebook ingestion complete! Processed {total_added} items.")

if __name__ == "__main__":
    main()
