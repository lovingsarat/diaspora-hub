import sys
import os
import uuid
from datetime import datetime
from dotenv import load_dotenv

from database import get_db_connection, index_item_in_chroma
from scraper import analyze_tweet_with_gemini

# Load environment
load_dotenv(dotenv_path="../.env")
load_dotenv()

def add_custom_feedback():
    if len(sys.argv) < 2:
        print("Usage: python add_feedback.py \"Your review text here\" [platform] [city] [author]")
        return
        
    text = sys.argv[1]
    platform = sys.argv[2] if len(sys.argv) > 2 else "Twitter"
    manual_city = sys.argv[3] if len(sys.argv) > 3 else None
    author = sys.argv[4] if len(sys.argv) > 4 else "@ManualUser"
    
    print("Running Gemini analysis on the custom text...")
    analysis = analyze_tweet_with_gemini(text)
    
    # Override city if manually specified
    if manual_city:
        analysis["city"] = manual_city
        
    item_id = f"manual_{uuid.uuid4().hex[:8]}"
    date_str = datetime.now().strftime("%Y-%m-%d")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO feedback_items (id, platform, author, date, event, text, sentiment, city, isUpcoming)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        item_id,
        platform,
        author,
        date_str,
        analysis.get("event", "General Community Feedback 2026"),
        text,
        analysis.get("sentiment", "Neutral"),
        analysis.get("city", "Birmingham"),
        1 if analysis.get("isUpcoming") else 0
    ))
    
    conn.commit()
    conn.close()
    
    # Index in ChromaDB Vector Search
    new_item = {
        "id": item_id,
        "platform": platform,
        "author": author,
        "date": date_str,
        "event": analysis.get("event", "General Community Feedback 2026"),
        "text": text,
        "sentiment": analysis.get("sentiment", "Neutral"),
        "city": analysis.get("city", "Birmingham"),
        "isUpcoming": 1 if analysis.get("isUpcoming") else 0
    }
    index_item_in_chroma(new_item)
    
    print("\n[SUCCESS] Feedback successfully inserted and indexed!")
    print(f"ID: {item_id}")
    print(f"Platform: {platform}")
    print(f"Event: {analysis.get('event')}")
    print(f"Sentiment: {analysis.get('sentiment')}")
    print(f"City: {analysis.get('city')}")
    print(f"Is Upcoming: {analysis.get('isUpcoming')}")

if __name__ == "__main__":
    add_custom_feedback()
