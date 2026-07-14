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
        # Fallback default
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
            
            # Remove markdown fence wrapper if present
            if reply.startswith("```json"):
                reply = reply[7:-3].strip()
            elif reply.startswith("```"):
                reply = reply[3:-3].strip()
                
            return json.loads(reply)
    except Exception as e:
        print(f"Error parsing sentiment with Gemini: {e}")
        
    # Return default fallback if parse failed or timed out
    return {
        "sentiment": "Neutral",
        "city": "Birmingham",
        "isUpcoming": False,
        "event": "Community Event"
    }

async def run_scraper():
    username = os.getenv("TWITTER_USERNAME")
    email = os.getenv("TWITTER_EMAIL")
    password = os.getenv("TWITTER_PASSWORD")
    
    if not username or username == "your_username":
        print("[WARNING] X credentials missing or set to placeholder in .env. Skipping X scraping.")
        return
        
    print(f"Authenticating with X account: {username}...")
    
    try:
        # Load or cache cookies to avoid login flags
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
        print(f"\n--- Fetching category: {category} ---")
        try:
            # Fetch latest chronological search results
            tweets = await client.search_tweet(query_string, product='Latest')
            
            for tweet in tweets:
                tweet_id = f"twitter_{tweet.id}"
                
                # Check if already exists in SQLite database
                cursor.execute("SELECT id FROM feedback_items WHERE id = ?", (tweet_id,))
                exists = cursor.fetchone()
                
                if exists:
                    print(f"Tweet {tweet.id} already exists in database. Skipping.")
                    continue
                    
                print(f"Found new tweet by: {tweet.user.name} (@{tweet.user.screen_name})")
                
                # Run Gemini classification on tweet
                analysis = analyze_tweet_with_gemini(tweet.text)
                
                # Format tweet timestamp
                try:
                    # Created_at is usually like "Mon Jul 14 10:20:00 +0000 2026"
                    # If it's datetime or string, handle it
                    if isinstance(tweet.created_at, str):
                        dt = datetime.strptime(tweet.created_at, "%a %b %d %H:%M:%S %z %Y")
                        date_str = dt.strftime("%Y-%m-%d")
                    else:
                        date_str = tweet.created_at.strftime("%Y-%m-%d")
                except:
                    date_str = datetime.now().strftime("%Y-%m-%d")
                
                # SQLite insertion
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
                
                # Index in vector search (ChromaDB)
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
            
        # Rate-limiting cushion
        await asyncio.sleep(5)
        
    conn.close()
    print(f"\nScraping complete! Added {total_added} new items to SQLite and ChromaDB.")

if __name__ == "__main__":
    asyncio.run(run_scraper())
