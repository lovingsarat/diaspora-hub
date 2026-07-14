import os
import asyncio
import json
import sqlite3
from datetime import datetime
from twikit import Client
import httpx
from dotenv import load_dotenv

DB_PATH = os.path.join(os.path.dirname(__file__), "diaspora.db")

# Official accounts to ingest tweets from directly
OFFICIAL_ACCOUNTS = ["MEAIndia", "HCI_London", "CGI_Bghm"]

# Load environment
load_dotenv(dotenv_path="../.env")
load_dotenv()

# Initialize Twikit Client
client = Client('en-US')

# Try to load Twitter session from .env cookies, cookies.json, or login
def load_twitter_session():
    """Load Twitter session using env cookies, cookies.json, or login fallback.
    Returns True if session is ready, False otherwise."""
    # Method 1: Read cookies directly from .env variables
    auth_token = os.getenv("TWITTER_AUTH_TOKEN", "")
    ct0 = os.getenv("TWITTER_CT0", "")
    if auth_token and ct0 and auth_token != "your_auth_token" and ct0 != "your_ct0":
        try:
            client.set_cookies({"auth_token": auth_token, "ct0": ct0})
            print("Loaded Twitter session from .env cookies (auth_token + ct0).")
            return True
        except Exception as e:
            print(f"[WARN] Failed to set cookies from .env: {e}")

    # Method 2: Load from cookies.json file
    cookies_file = "cookies.json"
    if os.path.exists(cookies_file):
        try:
            with open(cookies_file, 'r') as f:
                cookies_data = json.load(f)
            if isinstance(cookies_data, list):
                cookies_dict = {c["name"]: c["value"] for c in cookies_data if "name" in c and "value" in c}
                client.set_cookies(cookies_dict)
                print("Loaded Twitter session from cookies.json (array format).")
                return True
            elif isinstance(cookies_data, dict):
                if "ct0" in cookies_data or "auth_token" in cookies_data:
                    client.set_cookies(cookies_data)
                    print("Loaded Twitter session from cookies.json (dict format).")
                    return True
        except Exception as e:
            print(f"[WARN] Failed to load cookies.json: {e}")

    # Method 3: Fallback to login with credentials
    username = os.getenv("TWITTER_USERNAME")
    email = os.getenv("TWITTER_EMAIL")
    password = os.getenv("TWITTER_PASSWORD")
    if username and username != "your_username":
        print(f"Attempting X login with username: {username}...")
        try:
            asyncio.get_event_loop().run_until_complete(
                client.login(auth_info_1=username, auth_info_2=email, password=password)
            )
            client.save_cookies(cookies_file)
            print("Successfully logged in and saved session.")
            return True
        except Exception as e:
            print(f"[ERROR] X login failed: {e}")
    else:
        print("[WARNING] No Twitter credentials or cookies found. Skipping X scraping.")
    return False

def make_json_valid(s: str) -> str:
    """Extract the first valid JSON object from a string, handling duplicate/trailing braces."""
    import re
    s = s.strip()
    # Use regex to greedily match from first { to last }, then walk back to find valid JSON
    match = re.search(r'\{.*\}', s, re.DOTALL)
    if not match:
        return s
    candidate = match.group(0)
    # Try progressively shorter strings by trimming trailing } until valid JSON is found
    while candidate.endswith('}'):
        try:
            json.loads(candidate)
            return candidate  # Found valid JSON
        except json.JSONDecodeError as e:
            if 'Extra data' in str(e) or 'Expecting' in str(e):
                candidate = candidate.rstrip()
                # Remove last trailing }
                last_brace = candidate.rfind('}')
                if last_brace == -1:
                    break
                candidate = candidate[:last_brace].rstrip()
            else:
                break
    return s

# Function to analyze tweet contents using Gemini API (structured extraction)
def analyze_tweet_with_gemini(text: str) -> dict:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key in ["MY_GEMINI_API_KEY", "YOUR_GEMINI_API_KEY"]:
        return {
            "sentiment": "Neutral",
            "city": "Birmingham",
            "isUpcoming": False,
            "event": "Community Event"
        }
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={api_key}"
    
    prompt = f"""
    Analyze the following social media post regarding Indian diaspora community events in the UK Midlands.
    Extract the following information in strict JSON format:
    1. "sentiment": must be one of "Positive", "Neutral", "Negative".
    2. "city": must be the UK Midlands city mentioned (e.g., "Birmingham", "Leicester", "Coventry", "Nottingham", "Wolverhampton"). If none is mentioned, default to "Birmingham".
    3. "isUpcoming": boolean indicating if this refers to a future planned event or upcoming activity (rather than a past retrospective event).
    4. "event": a concise, capitalized name for the event referenced (e.g. "Leicester Diwali Lights Switch-On 2026", "Midlands Holi Festival 2026", or "General Community Feedback 2026").
    
    Post Text:
    "{text}"
    
    Output JSON directly (no markdown blocks, no wrapping):
    """
    
    try:
        response = httpx.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1
            }
        }, timeout=20.0)
        
        if response.status_code == 200:
            res_data = response.json()
            reply = res_data["candidates"][0]["content"]["parts"][0]["text"].strip()
            
            import re
            match = re.search(r'\{.*\}', reply, re.DOTALL)
            if match:
                reply = match.group(0)
            
            reply = make_json_valid(reply)
            return json.loads(reply)
    except Exception as e:
        print(f"Error parsing sentiment with Gemini: {e}. Raw reply: {reply if 'reply' in locals() else 'None'}")
        
    return {
        "sentiment": "Neutral",
        "city": "Birmingham",
        "isUpcoming": False,
        "event": "Community Event"
    }

# Facebook pages/groups to scrape for Indian diaspora Midlands content
FACEBOOK_PAGES = [
    "AISfestival",            # An Indian Summer festival, Leicester
    "ShivamEvents",           # Shivam Events, Leicester
    "CentralStageCrew",       # Central Stage Crew (Midlands Indian events)
    "SriBardai",              # Sri Bardai Brahmin Samaj Leicester
    "ShreePrajapatiAssociationLeicester",  # SPAL Leicester
]

# Scraper method 4: Facebook page scraping using direct HTTP requests
def run_facebook_scraper():
    """Scrape Facebook pages for Indian diaspora community posts in the Midlands."""
    import requests
    from bs4 import BeautifulSoup

    # Load cookies from fb_cookies.json (exported from browser extension)
    cookies = {}
    fb_cookies_file = os.path.join(os.path.dirname(__file__), "fb_cookies.json")

    # Also check .env for individual cookies
    c_user = os.getenv("FACEBOOK_C_USER", "")
    xs = os.getenv("FACEBOOK_XS", "")
    datr = os.getenv("FACEBOOK_DATR", "")

    if c_user and xs and datr and c_user != "your_c_user":
        cookies = {"c_user": c_user, "xs": xs, "datr": datr}
        print("Loaded Facebook cookies from .env (c_user + xs + datr).")
    elif os.path.exists(fb_cookies_file):
        try:
            with open(fb_cookies_file, 'r') as f:
                cookie_data = json.load(f)
            if isinstance(cookie_data, list):
                cookies = {c["name"]: c["value"] for c in cookie_data if "name" in c and "value" in c}
            elif isinstance(cookie_data, dict):
                cookies = cookie_data
            print(f"Loaded {len(cookies)} Facebook cookies from fb_cookies.json.")
        except Exception as e:
            print(f"[WARN] Failed to load fb_cookies.json: {e}")

    if not cookies or "c_user" not in cookies:
        print("[WARNING] Facebook cookies not configured. Skipping Facebook scraping.")
        print("  Option 1: Set FACEBOOK_C_USER, FACEBOOK_XS, and FACEBOOK_DATR in .env")
        print("  Option 2: Export all cookies from browser to backend/fb_cookies.json")
        print("  (Use a browser extension like 'Cookie-Editor' to export as JSON)")
        return
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    total_added = 0

    for page in FACEBOOK_PAGES:
        print(f"\n--- Fetching Facebook posts from {page} ---")
        try:
            url = f"https://m.facebook.com/{page}/posts/"
            resp = requests.get(url, cookies=cookies, headers=headers, timeout=30)
            if resp.status_code != 200:
                print(f"[WARN] HTTP {resp.status_code} for {page}")
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            count = 0

            # Mobile Facebook posts are in <div> elements with data-sigil="m-story"
            # or in story containers with class containing "story"
            story_divs = soup.find_all("div", attrs={"data-sigil": "m-story"})
            if not story_divs:
                # Fallback: look for post content in common mobile FB structures
                story_divs = soup.find_all("div", class_=lambda c: c and "story_body" in str(c))

            if not story_divs:
                # Another fallback: look for divs containing post text patterns
                story_divs = soup.find_all("div", {"role": "article"})

            for story in story_divs:
                try:
                    # Extract text content
                    text_div = story.find("div", {"data-sigil": "story-body-text"})
                    if not text_div:
                        text_div = story.find("p")
                    if not text_div:
                        text_div = story

                    post_text = text_div.get_text(separator=" ", strip=True)
                    if not post_text or len(post_text) < 20:
                        continue

                    # Check UK relevance
                    if not is_uk_relevant(post_text):
                        continue

                    # Try to find timestamp
                    timestamp = story.find("abbr")
                    if timestamp and timestamp.get("data-utime"):
                        date_str = datetime.fromtimestamp(int(timestamp["data-utime"])).strftime("%Y-%m-%d")
                    else:
                        date_str = datetime.now().strftime("%Y-%m-%d")

                    post_id = f"facebook_{page}_{count}_{date_str.replace('-', '')}"
                    analysis = analyze_tweet_with_gemini(post_text)

                    new_item = {
                        "id": post_id,
                        "platform": "Facebook",
                        "author": page,
                        "date": date_str,
                        "event": analysis.get("event", "General Community Feedback 2026"),
                        "text": post_text,
                        "sentiment": analysis.get("sentiment", "Neutral"),
                        "city": analysis.get("city", "Birmingham"),
                        "isUpcoming": bool(analysis.get("isUpcoming")),
                    }
                    upsert_feedback_local(new_item)
                    total_added += 1
                    count += 1
                    print(f"[SUCCESS] Added Facebook post: {post_id}")
                except Exception as post_err:
                    print(f"[WARN] Skipping Facebook post: {str(post_err).encode('ascii','ignore').decode()}")
                    continue

            if count == 0:
                # Try the desktop Graph API-style endpoint as fallback
                print(f"[INFO] No posts from mobile site, trying desktop fallback for {page}...")
                url2 = f"https://www.facebook.com/{page}/posts/"
                resp2 = requests.get(url2, cookies=cookies, headers=headers, timeout=30)
                if resp2.status_code == 200:
                    soup2 = BeautifulSoup(resp2.text, "html.parser")
                    articles = soup2.find_all("div", {"role": "article"})
                    for article in articles:
                        try:
                            post_text = article.get_text(separator=" ", strip=True)
                            if not post_text or len(post_text) < 50:
                                continue
                            if not is_uk_relevant(post_text):
                                continue

                            post_id = f"facebook_{page}_{count}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
                            analysis = analyze_tweet_with_gemini(post_text[:500])

                            new_item = {
                                "id": post_id,
                                "platform": "Facebook",
                                "author": page,
                                "date": datetime.now().strftime("%Y-%m-%d"),
                                "event": analysis.get("event", "General Community Feedback 2026"),
                                "text": post_text[:500],
                                "sentiment": analysis.get("sentiment", "Neutral"),
                                "city": analysis.get("city", "Birmingham"),
                                "isUpcoming": bool(analysis.get("isUpcoming")),
                            }
                            upsert_feedback_local(new_item)
                            total_added += 1
                            count += 1
                            print(f"[SUCCESS] Added Facebook post (desktop): {post_id}")
                        except Exception:
                            continue

            print(f"[INFO] Processed {count} posts from {page}")
        except Exception as e:
            print(f"Error fetching Facebook page {page}: {str(e).encode('ascii','ignore').decode()}")
        import time
        time.sleep(3)

    print(f"\nFacebook ingestion complete! Processed {total_added} items.")

# Local SQLite upsert
def upsert_feedback_local(item):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedback_items (
            id TEXT PRIMARY KEY,
            platform TEXT,
            author TEXT,
            date TEXT,
            event TEXT,
            text TEXT,
            sentiment TEXT,
            city TEXT,
            isUpcoming INTEGER
        )
    """)
    cursor.execute("""
        INSERT OR REPLACE INTO feedback_items (id, platform, author, date, event, text, sentiment, city, isUpcoming)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        item["id"], item["platform"], item["author"], item["date"],
        item["event"], item["text"], item["sentiment"], item["city"],
        1 if item.get("isUpcoming") else 0
    ))
    conn.commit()
    conn.close()

# Fast UK relevance gate — checks tweet text for at least one UK location/context keyword.
# Runs before the Gemini API call to save quota on irrelevant international content.
UK_KEYWORDS = [
    "uk", "england", "britain", "british", "birmingham", "leicester", "coventry",
    "wolverhampton", "nottingham", "derby", "west midlands", "east midlands",
    "midlands", "manchester", "london", "bradford", "luton", "slough",
    "nhs", "council", "mp ", "parliament", "whitehall", "home office"
]

def is_uk_relevant(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in UK_KEYWORDS)

# Scraper method 1: Official Twitter API v2 Bearer Token Ingestion
async def run_official_api_scraper(bearer_token: str):
    headers = {"Authorization": f"Bearer {bearer_token}"}
    UK_CITIES = '("Birmingham" OR "Leicester" OR "Coventry" OR "Wolverhampton" OR "Nottingham" OR "Derby" OR "West Midlands" OR "UK" OR "England" OR "Britain")'
    queries = {
        "diaspora_community":   f'{UK_CITIES} ("British Indian" OR "British Asian" OR "South Asian" OR "Desi community") lang:en -is:retweet',
        "cultural_events":      f'{UK_CITIES} ("Diwali" OR "Navratri" OR "Vaisakhi" OR "Holi" OR "Eid" OR "Mela") lang:en -is:retweet',
    }
    
    total_added = 0
    
    async with httpx.AsyncClient() as http_client:
        for category, query_string in queries.items():
            print(f"\n--- Fetching category: {category} (Official API) ---")
            url = "https://api.twitter.com/2/tweets/search/recent"
            params = {
                "query": query_string,
                "tweet.fields": "created_at",
                "expansions": "author_id",
                "user.fields": "username,name",
                "max_results": 10
            }
            try:
                response = await http_client.get(url, headers=headers, params=params, timeout=15.0)
                if response.status_code == 429:
                    print("[ERROR] Official X API Rate Limited. Please wait.")
                    break
                if response.status_code != 200:
                    print(f"[ERROR] Official X API returned status {response.status_code}: {response.text}")
                    continue
                    
                resp_json = response.json()
                tweets = resp_json.get("data", [])
                users = {u["id"]: u for u in resp_json.get("includes", {}).get("users", [])}
                
                for tweet in tweets:
                    tweet_id = f"twitter_{tweet['id']}"
                    
                    # UK relevance gate
                    author_id = tweet.get("author_id")
                    user_info = users.get(author_id, {})
                    if not is_uk_relevant(tweet["text"]):
                        print(f"[SKIP] Non-UK content filtered out from @{user_info.get('username', 'XUser')}")
                        continue

                    screen_name = user_info.get("username", "XUser")
                    
                    print(f"Found new tweet by: {user_info.get('name', 'User')} (@{screen_name})")
                    
                    # Perform Gemini analysis
                    analysis = analyze_tweet_with_gemini(tweet["text"])
                    
                    # Date formatting
                    try:
                        date_str = tweet["created_at"][:10]
                    except:
                        date_str = datetime.now().strftime("%Y-%m-%d")
                        
                    new_item = {
                        "id": tweet_id,
                        "platform": "Twitter",
                        "author": f"@{screen_name}",
                        "date": date_str,
                        "event": analysis.get("event", "General Community Feedback 2026"),
                        "text": tweet["text"],
                        "sentiment": analysis.get("sentiment", "Neutral"),
                        "city": analysis.get("city", "Birmingham"),
                        "isUpcoming": bool(analysis.get("isUpcoming")),
                    }
                    upsert_feedback_local(new_item)
                    total_added += 1
                    print(f"[SUCCESS] Added tweet: {tweet['id']}")
            except Exception as e:
                print(f"Error fetching category {category} with Official API: {e}")
                
            await asyncio.sleep(2)
            
    print(f"\nIngestion complete! Processed {total_added} items in Supabase.")

# Scraper method 2: Twikit Browser Scraper (Fallback)
async def run_twikit_scraper():
    if not load_twitter_session():
        return

    # UK-targeted queries: require explicit UK city/region mention + English language
    UK_CITIES = '("Birmingham" OR "Leicester" OR "Coventry" OR "Wolverhampton" OR "Nottingham" OR "Derby" OR "West Midlands" OR "East Midlands" OR "UK" OR "England" OR "Britain")'
    queries = {
        "diaspora_community":   f'({UK_CITIES}) ("Indian diaspora" OR "South Asian" OR "Desi community" OR "British Indian" OR "British Asian") lang:en -is:retweet',
        "cultural_events":      f'({UK_CITIES}) ("Diwali" OR "Navratri" OR "Vaisakhi" OR "Holi" OR "Eid" OR "Mela" OR "Garba") lang:en -is:retweet',
        "local_issues":         f'({UK_CITIES}) ("Indian community" OR "Asian community" OR "South Asian") ("council" OR "MP" OR "police" OR "NHS" OR "mosque" OR "temple" OR "gurdwara") lang:en -is:retweet',
        "diaspora_news":        f'({UK_CITIES}) ("British Indian" OR "British Pakistani" OR "British Bangladeshi" OR "British Sikh" OR "British Hindu" OR "British Muslim") lang:en -is:retweet',
    }
    
    total_added = 0
    
    for category, query_string in queries.items():
        print(f"\n--- Fetching category: {category} (Twikit Scraper) ---")
        try:
            tweets = await client.search_tweet(query_string, product='Latest')
            
            for tweet in tweets:
                try:
                    tweet_id = f"twitter_{tweet.id}"
                    
                    # UK relevance gate: skip tweets with no UK connection
                    if not is_uk_relevant(tweet.text):
                        safe_name = tweet.user.screen_name.encode('ascii','ignore').decode()
                        print(f"[SKIP] Non-UK content filtered out from @{safe_name}")
                        continue
                        
                    safe_name = tweet.user.name.encode('ascii','ignore').decode()
                    safe_screen = tweet.user.screen_name.encode('ascii','ignore').decode()
                    print(f"Found new tweet by: {safe_name} (@{safe_screen})")
                    
                    analysis = analyze_tweet_with_gemini(tweet.text)
                    
                    try:
                        if isinstance(tweet.created_at, str):
                            dt = datetime.strptime(tweet.created_at, "%a %b %d %H:%M:%S %z %Y")
                            date_str = dt.strftime("%Y-%m-%d")
                        else:
                            date_str = tweet.created_at.strftime("%Y-%m-%d")
                    except:
                        date_str = datetime.now().strftime("%Y-%m-%d")
                    
                    new_item = {
                        "id": tweet_id,
                        "platform": "Twitter",
                        "author": f"@{tweet.user.screen_name}",
                        "date": date_str,
                        "event": analysis.get("event", "General Community Feedback 2026"),
                        "text": tweet.text,
                        "sentiment": analysis.get("sentiment", "Neutral"),
                        "city": analysis.get("city", "Birmingham"),
                        "isUpcoming": bool(analysis.get("isUpcoming")),
                    }
                    upsert_feedback_local(new_item)
                    total_added += 1
                    print(f"[SUCCESS] Added tweet: {tweet.id}")
                    
                except Exception as tweet_err:
                    print(f"[WARN] Skipping tweet due to error: {str(tweet_err).encode('ascii','ignore').decode()}")
                    continue
            
        except Exception as e:
            print(f"Error searching category {category}: {str(e).encode('ascii','ignore').decode()}")
            
        await asyncio.sleep(5)
        
    print(f"\nIngestion complete! Processed {total_added} items in local DB.")

# Scraper method 3: Fetch tweets from specific official accounts
async def run_account_scraper():
    """Fetch recent tweets from official accounts like @MEAIndia, @HCI_London, @CGI_Bghm."""
    if not load_twitter_session():
        return

    total_added = 0
    for account in OFFICIAL_ACCOUNTS:
        print(f"\n--- Fetching tweets from @{account} ---")
        try:
            user = await client.get_user_by_screen_name(account)
            if not user:
                print(f"[WARN] Could not find user @{account}")
                continue

            tweets = await client.get_user_tweets(user.id, tweet_type='Tweets')
            if not tweets:
                print(f"[INFO] No recent tweets found for @{account}")
                continue

            # Twikit returns a ResultList — iterate safely
            tweet_list = list(tweets) if tweets else []
            print(f"[INFO] Found {len(tweet_list)} tweets for @{account}")

            for tweet in tweet_list:
                try:
                    tweet_id = f"twitter_{tweet.id}"

                    # Relaxed UK filter for official accounts — they post about India-UK matters
                    # but may not always mention UK keywords explicitly
                    if not is_uk_relevant(tweet.text):
                        # Still include if the account is always UK-relevant
                        if account not in ("HCI_London", "CGI_Bghm"):
                            print(f"[SKIP] Non-UK content from @{account}")
                            continue

                    analysis = analyze_tweet_with_gemini(tweet.text)

                    try:
                        if isinstance(tweet.created_at, str):
                            dt = datetime.strptime(tweet.created_at, "%a %b %d %H:%M:%S %z %Y")
                            date_str = dt.strftime("%Y-%m-%d")
                        else:
                            date_str = tweet.created_at.strftime("%Y-%m-%d")
                    except:
                        date_str = datetime.now().strftime("%Y-%m-%d")

                    new_item = {
                        "id": tweet_id,
                        "platform": "Twitter",
                        "author": f"@{account}",
                        "date": date_str,
                        "event": analysis.get("event", "General Community Feedback 2026"),
                        "text": tweet.text,
                        "sentiment": analysis.get("sentiment", "Neutral"),
                        "city": analysis.get("city", "London"),
                        "isUpcoming": bool(analysis.get("isUpcoming")),
                    }
                    upsert_feedback_local(new_item)
                    total_added += 1
                    print(f"[SUCCESS] Added tweet from @{account}: {tweet.id}")
                except Exception as tweet_err:
                    print(f"[WARN] Skipping tweet: {str(tweet_err).encode('ascii','ignore').decode()}")
                    continue
        except Exception as e:
            import traceback
            print(f"Error fetching @{account}: {str(e).encode('ascii','ignore').decode()}")
            traceback.print_exc()
        await asyncio.sleep(3)

    print(f"\nOfficial account ingestion complete! Processed {total_added} items.")

# Deduplicate the local SQLite database in-place
def deduplicate_db():
    import re as _re
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, platform, author, date, event, text, sentiment, city, isUpcoming FROM feedback_items")
    rows = cursor.fetchall()

    def get_tokens(text):
        return set(_re.findall(r"[a-z0-9£]+", text.lower()))

    def is_near_dup(a, b):
        ta, tb = get_tokens(a), get_tokens(b)
        if not ta and not tb:
            return True
        shared = ta & tb
        union = ta | tb
        return len(union) > 0 and len(shared) / len(union) >= 0.85

    def score(item):
        return (
            (4 if item[8] else 0)
            + (2 if item[4] not in ("Community Event", "General Community Feedback 2026") else 0)
            + (1 if item[6] != "Neutral" else 0)
        )

    unique = []
    for row in rows:
        dup_idx = next(
            (
                i for i, existing in enumerate(unique)
                if existing[1].lower() == row[1].lower()
                and existing[2].lower() == row[2].lower()
                and existing[3] == row[3]
                and is_near_dup(existing[5], row[5])
            ),
            -1,
        )
        if dup_idx == -1:
            unique.append(row)
        elif score(row) > score(unique[dup_idx]):
            unique[dup_idx] = row

    before = len(rows)
    after = len(unique)

    if before != after:
        cursor.execute("DELETE FROM feedback_items")
        for row in unique:
            cursor.execute(
                "INSERT INTO feedback_items (id, platform, author, date, event, text, sentiment, city, isUpcoming) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row
            )
        conn.commit()
        print(f"\nDeduplicated DB: {before} -> {after} items (removed {before - after} duplicates)")
    else:
        print(f"\nNo duplicates found in DB ({before} items).")

    conn.close()

async def run_scraper():
    bearer_token = os.getenv("TWITTER_BEARER_TOKEN")
    if bearer_token and bearer_token != "your_bearer_token":
        await run_official_api_scraper(bearer_token)
    else:
        await run_twikit_scraper()

    # Also fetch from official accounts (@MEAIndia, @HCI_London, @CGI_Bghm)
    await run_account_scraper()

    # Fetch from Facebook pages
    run_facebook_scraper()

    # Deduplicate the local DB
    deduplicate_db()

    # Auto-export to data.json
    print("\n--- Exporting to data.json ---")
    import subprocess
    export_script = os.path.join(os.path.dirname(__file__), "export_data.py")
    subprocess.run(["python", export_script], cwd=os.path.dirname(__file__))

if __name__ == "__main__":
    asyncio.run(run_scraper())
