import os
import asyncio
import json
import sqlite3
from datetime import datetime
from twikit import Client
import httpx
from dotenv import load_dotenv

from database import get_db_connection, index_item_in_chroma

# Load environment
load_dotenv(dotenv_path="../.env")
load_dotenv()

# Initialize Twikit Client
client = Client('en-US')

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
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={api_key}"
    
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
            
            if reply.startswith("```json"):
                reply = reply[7:-3].strip()
            elif reply.startswith("```"):
                reply = reply[3:-3].strip()
                
            return json.loads(reply)
    except Exception as e:
        print(f"Error parsing sentiment with Gemini: {e}")
        
    return {
        "sentiment": "Neutral",
        "city": "Birmingham",
        "isUpcoming": False,
        "event": "Community Event"
    }

# Scraper method 1: Official Twitter API v2 Bearer Token Ingestion
async def run_official_api_scraper(bearer_token: str):
    headers = {"Authorization": f"Bearer {bearer_token}"}
    queries = {
        "diaspora": '("Indian diaspora" OR "Desi") ("West Midlands" OR "Birmingham")',
        "events": '("Indian event" OR "Diwali" OR "Mela") ("West Midlands" OR "Birmingham" OR "Coventry")'
    }
    
    conn = get_db_connection()
    cursor = conn.cursor()
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
                    
                    # Verify uniqueness
                    cursor.execute("SELECT id FROM feedback_items WHERE id = ?", (tweet_id,))
                    exists = cursor.fetchone()
                    if exists:
                        print(f"Tweet {tweet['id']} already exists in SQLite. Skipping.")
                        continue
                        
                    author_id = tweet.get("author_id")
                    user_info = users.get(author_id, {})
                    screen_name = user_info.get("username", "XUser")
                    
                    print(f"Found new tweet by: {user_info.get('name', 'User')} (@{screen_name})")
                    
                    # Perform Gemini analysis
                    analysis = analyze_tweet_with_gemini(tweet["text"])
                    
                    # Date formatting
                    try:
                        date_str = tweet["created_at"][:10]
                    except:
                        date_str = datetime.now().strftime("%Y-%m-%d")
                        
                    cursor.execute("""
                        INSERT INTO feedback_items (id, platform, author, date, event, text, sentiment, city, isUpcoming)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        tweet_id,
                        "Twitter",
                        f"@{screen_name}",
                        date_str,
                        analysis.get("event", "General Community Feedback 2026"),
                        tweet["text"],
                        analysis.get("sentiment", "Neutral"),
                        analysis.get("city", "Birmingham"),
                        1 if analysis.get("isUpcoming") else 0
                    ))
                    
                    new_item = {
                        "id": tweet_id,
                        "platform": "Twitter",
                        "author": f"@{screen_name}",
                        "date": date_str,
                        "event": analysis.get("event", "General Community Feedback 2026"),
                        "text": tweet["text"],
                        "sentiment": analysis.get("sentiment", "Neutral"),
                        "city": analysis.get("city", "Birmingham"),
                        "isUpcoming": 1 if analysis.get("isUpcoming") else 0
                    }
                    index_item_in_chroma(new_item)
                    total_added += 1
                    print(f"[SUCCESS] Added & indexed tweet: {tweet['id']}")
                    
                conn.commit()
            except Exception as e:
                print(f"Error fetching category {category} with Official API: {e}")
                
            await asyncio.sleep(2)
            
    conn.close()
    print(f"\nIngestion complete! Added {total_added} new items to SQLite and ChromaDB.")

# Scraper method 2: Twikit Browser Scraper (Fallback)
async def run_twikit_scraper():
    username = os.getenv("TWITTER_USERNAME")
    email = os.getenv("TWITTER_EMAIL")
    password = os.getenv("TWITTER_PASSWORD")
    
    if not username or username == "your_username":
        print("[WARNING] X credentials missing or set to placeholder in .env. Skipping X scraping.")
        return
        
    print(f"Authenticating with X account: {username}...")
    
    try:
        cookies_file = "cookies.json"
        if os.path.exists(cookies_file):
            client.load_cookies(cookies_file)
            print("Loaded session cookies from cache.")
        else:
            await client.login(
                auth_info_1=username,
                auth_info_2=email,
                password=password
            )
            client.save_cookies(cookies_file)
            print("Successfully authenticated and cached session cookies.")
    except Exception as e:
        print(f"[ERROR] Authentication failed: {e}")
        return

    queries = {
        "diaspora": '("Indian diaspora" OR "Desi") AND ("West Midlands" OR "Birmingham")',
        "events": '("Indian event" OR "Diwali" OR "Mela") AND ("West Midlands" OR "Birmingham" OR "Coventry")'
    }
    
    conn = get_db_connection()
    cursor = conn.cursor()
    total_added = 0
    
    for category, query_string in queries.items():
        print(f"\n--- Fetching category: {category} (Twikit Scraper) ---")
        try:
            tweets = await client.search_tweet(query_string, product='Latest')
            
            for tweet in tweets:
                tweet_id = f"twitter_{tweet.id}"
                
                cursor.execute("SELECT id FROM feedback_items WHERE id = ?", (tweet_id,))
                exists = cursor.fetchone()
                
                if exists:
                    print(f"Tweet {tweet.id} already exists in database. Skipping.")
                    continue
                    
                print(f"Found new tweet by: {tweet.user.name} (@{tweet.user.screen_name})")
                
                analysis = analyze_tweet_with_gemini(tweet.text)
                
                try:
                    if isinstance(tweet.created_at, str):
                        dt = datetime.strptime(tweet.created_at, "%a %b %d %H:%M:%S %z %Y")
                        date_str = dt.strftime("%Y-%m-%d")
                    else:
                        date_str = tweet.created_at.strftime("%Y-%m-%d")
                except:
                    date_str = datetime.now().strftime("%Y-%m-%d")
                
                cursor.execute("""
                    INSERT INTO feedback_items (id, platform, author, date, event, text, sentiment, city, isUpcoming)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    tweet_id,
                    "Twitter",
                    f"@{tweet.user.screen_name}",
                    date_str,
                    analysis.get("event", "General Community Feedback 2026"),
                    tweet.text,
                    analysis.get("sentiment", "Neutral"),
                    analysis.get("city", "Birmingham"),
                    1 if analysis.get("isUpcoming") else 0
                ))
                
                new_item = {
                    "id": tweet_id,
                    "platform": "Twitter",
                    "author": f"@{tweet.user.screen_name}",
                    "date": date_str,
                    "event": analysis.get("event", "General Community Feedback 2026"),
                    "text": tweet.text,
                    "sentiment": analysis.get("sentiment", "Neutral"),
                    "city": analysis.get("city", "Birmingham"),
                    "isUpcoming": 1 if analysis.get("isUpcoming") else 0
                }
                index_item_in_chroma(new_item)
                
                total_added += 1
                print(f"[SUCCESS] Added & indexed tweet: {tweet.id}")
                
            conn.commit()
            
        except Exception as e:
            print(f"Error searching category {category}: {e}")
            
        await asyncio.sleep(5)
        
    conn.close()
    print(f"\nIngestion complete! Added {total_added} new items to SQLite and ChromaDB.")

async def run_scraper():
    bearer_token = os.getenv("TWITTER_BEARER_TOKEN")
    if bearer_token and bearer_token != "your_bearer_token":
        await run_official_api_scraper(bearer_token)
    else:
        await run_twikit_scraper()

if __name__ == "__main__":
    asyncio.run(run_scraper())
