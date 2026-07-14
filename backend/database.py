import os
import sqlite3
from typing import List, Dict, Any, Optional
import httpx
import chromadb
from dotenv import load_dotenv

# Load env variables
load_dotenv(dotenv_path="../.env")
load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "diaspora.db")
CHROMA_PATH = os.path.join(os.path.dirname(__file__), "chroma_db")

# Initialize SQLite Connection
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# Helper to generate Gemini embedding (768 dimensions)
def get_gemini_embedding(text: str) -> List[float]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key in ["MY_GEMINI_API_KEY", "YOUR_GEMINI_API_KEY"]:
        # Fallback dummy embedding (768 dimensions)
        return [0.0] * 768
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={api_key}"
    try:
        response = httpx.post(url, json={
            "model": "models/text-embedding-004",
            "content": {
                "parts": [{"text": text}]
            }
        }, timeout=10.0)
        
        if response.status_code == 200:
            return response.json()["embedding"]["values"]
    except Exception as e:
        print(f"Error generating embedding: {e}")
        
    return [0.0] * 768

# Initialize ChromaDB Client
def get_chroma_collection():
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    # Collection will store embeddings and metadata
    return client.get_or_create_collection("diaspora_feedback")

# Index single item in ChromaDB
def index_item_in_chroma(item: Dict[str, Any]):
    try:
        collection = get_chroma_collection()
        embedding = get_gemini_embedding(item["text"])
        
        # Prepare metadata
        metadata = {
            "platform": item["platform"],
            "author": item["author"],
            "date": item["date"],
            "event": item["event"],
            "sentiment": item["sentiment"],
            "city": item["city"],
            "isUpcoming": int(item.get("isUpcoming", 0))
        }
        
        collection.upsert(
            ids=[str(item["id"])],
            embeddings=[embedding],
            documents=[item["text"]],
            metadatas=[metadata]
        )
    except Exception as e:
        print(f"Failed to index item in ChromaDB: {e}")

# Query ChromaDB for top-k similar items to generate RAG context
def query_chroma_context(query_text: str, k: int = 5) -> str:
    try:
        # Check database size first. For small datasets (<= 30 items),
        # passing the complete context guarantees the LLM will see all positive/negative/neutral reviews.
        # This also serves as a perfect zero-config fallback if the user's API Key is invalid.
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM feedback_items")
        total_items = cursor.fetchone()[0]
        
        if total_items <= 30:
            cursor.execute("SELECT * FROM feedback_items")
            rows = cursor.fetchall()
            conn.close()
            
            sb = []
            sb.append("| Platform | Author | Date | Event | Sentiment | City | Feedback text |")
            sb.append("| --- | --- | --- | --- | --- | --- | --- |")
            for row in rows:
                upcoming_tag = " (Upcoming Planned Activity)" if row["isUpcoming"] else ""
                text_escaped = row["text"].replace("|", "\\|")
                sb.append(
                    f"| {row['platform']} | {row['author']} | {row['date']} | {row['event']}{upcoming_tag} | "
                    f"{row['sentiment']} | {row['city']} | {text_escaped} |"
                )
            return "\n".join(sb)
            
        conn.close()

        # If the database is larger, utilize Vector Search (ChromaDB)
        collection = get_chroma_collection()
        query_embedding = get_gemini_embedding(query_text)
        
        # Check if the query embedding is all zeros (invalid/expired API Key)
        is_dummy = all(v == 0.0 for v in query_embedding)
        
        if is_dummy:
            print("[RAG Fallback] Invalid API Key / Dummy Embedding detected. Falling back to SQL keyword search.")
            conn = get_db_connection()
            cursor = conn.cursor()
            words = [w.lower() for w in query_text.split() if len(w) > 2]
            query_str = "SELECT * FROM feedback_items WHERE 1=1"
            params = []
            if words:
                conditions = []
                for w in words:
                    conditions.append("(LOWER(text) LIKE ? OR LOWER(event) LIKE ?)")
                    params.extend([f"%{w}%", f"%{w}%"])
                query_str += " AND (" + " OR ".join(conditions) + ")"
            query_str += " LIMIT ?"
            params.append(k)
            cursor.execute(query_str, params)
            rows = cursor.fetchall()
            conn.close()
            
            if not rows:
                # Retrieve the latest k items as default fallback
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM feedback_items ORDER BY date DESC LIMIT ?", (k,))
                rows = cursor.fetchall()
                conn.close()
                
            sb = []
            sb.append("| Platform | Author | Date | Event | Sentiment | City | Feedback text |")
            sb.append("| --- | --- | --- | --- | --- | --- | --- |")
            for row in rows:
                upcoming_tag = " (Upcoming Planned Activity)" if row["isUpcoming"] else ""
                text_escaped = row["text"].replace("|", "\\|")
                sb.append(
                    f"| {row['platform']} | {row['author']} | {row['date']} | {row['event']}{upcoming_tag} | "
                    f"{row['sentiment']} | {row['city']} | {text_escaped} |"
                )
            return "\n".join(sb)

        # Standard Vector Query
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=k
        )
        
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        
        if not documents:
            return "No matching records found in vector database."
            
        sb = []
        sb.append("| Platform | Author | Date | Event | Sentiment | City | Feedback text |")
        sb.append("| --- | --- | --- | --- | --- | --- | --- |")
        
        for doc, meta in zip(documents, metadatas):
            upcoming_tag = " (Upcoming Planned Activity)" if meta.get("isUpcoming") else ""
            text_escaped = doc.replace("|", "\\|")
            sb.append(
                f"| {meta.get('platform', '')} | {meta.get('author', '')} | {meta.get('date', '')} | "
                f"{meta.get('event', '')}{upcoming_tag} | {meta.get('sentiment', '')} | "
                f"{meta.get('city', '')} | {text_escaped} |"
            )
            
        return "\n".join(sb)
    except Exception as e:
        print(f"ChromaDB Query Error: {e}")
        return "Error retrieving vector context database."

# Baseline Static Seeding Data
STATIC_ITEMS = [
    {
        "id": "1",
        "platform": "Twitter",
        "author": "@AmitBrum",
        "date": "2026-03-22",
        "event": "Midlands Holi Festival 2026",
        "text": "The Birmingham Holi Festival 2026 was absolutely spectacular! Incredible colors, lively dhol players, and such an amazing atmosphere at Ward End Park. The Midlands diaspora really showed up today! 🇮🇳✨",
        "sentiment": "Positive",
        "city": "Birmingham",
        "isUpcoming": 0
    },
    {
        "id": "2",
        "platform": "Facebook",
        "author": "Preeti Patel",
        "date": "2026-03-23",
        "event": "Midlands Holi Festival 2026",
        "text": "Extremely frustrated with the ticket prices for the Midlands Holi event. £25 per person is way too steep for families. Also, the queue for the food stalls was over an hour long! Kids were starving.",
        "sentiment": "Negative",
        "city": "Birmingham",
        "isUpcoming": 0
    },
    {
        "id": "3",
        "platform": "Quora",
        "author": "Rajan Sharma",
        "date": "2026-03-24",
        "event": "Midlands Holi Festival 2026",
        "text": "Attended the Holi festival in Birmingham last weekend. While the cultural performances were top-notch and the community spirit was strong, the parking situation was a total mess and there weren't enough washroom facilities for the crowd.",
        "sentiment": "Neutral",
        "city": "Birmingham",
        "isUpcoming": 0
    },
    {
        "id": "4",
        "platform": "Twitter",
        "author": "@Leicester_Sunita",
        "date": "2026-04-19",
        "event": "Birmingham Vaisakhi Mela 2026",
        "text": "So glad we made the trip from Leicester to Handsworth Park for Vaisakhi Mela 2026! The Langar (free kitchen) was served with so much love, and the energetic Bhangra acts had everyone dancing. Wonderful community engagement!",
        "sentiment": "Positive",
        "city": "Birmingham",
        "isUpcoming": 0
    },
    {
        "id": "5",
        "platform": "Facebook",
        "author": "Gurpreet Singh",
        "date": "2026-04-20",
        "event": "Birmingham Vaisakhi Mela 2026",
        "text": "The crowd management at the Handsworth Park Vaisakhi event was quite poor. It felt unsafe at times around the main stage area, and there was litter everywhere by 4 PM. We really need more waste bins and volunteers next year.",
        "sentiment": "Negative",
        "city": "Birmingham",
        "isUpcoming": 0
    },
    {
        "id": "6",
        "platform": "Twitter",
        "author": "@MidlandsIndSoc",
        "date": "2026-06-14",
        "event": "Midlands Indian Sports Day 2026",
        "text": "Huge congratulations to the organizers of the Indian Sports Day in Leicester! Seeing the youngsters play Kabaddi and Kho-Kho was pure nostalgia. Wonderful initiative to keep our cultural sports alive in the UK Midlands.",
        "sentiment": "Positive",
        "city": "Leicester",
        "isUpcoming": 0
    },
    {
        "id": "7",
        "platform": "Facebook",
        "author": "Vikram Rao",
        "date": "2026-06-15",
        "event": "Midlands Indian Sports Day 2026",
        "text": "Great concept, but the execution of the Sports Day was ruined by the typical British summer rain. There was no indoor backup plan for most matches, and the scheduling was delayed by 3 hours. Please plan better for wet weather in 2026!",
        "sentiment": "Negative",
        "city": "Leicester",
        "isUpcoming": 0
    },
    {
        "id": "8",
        "platform": "Quora",
        "author": "Anjali Desai",
        "date": "2026-07-02",
        "event": "Leicester Diwali Lights Switch-On 2026",
        "text": "What are the upcoming planned activities for the Leicester Diwali Lights Switch-On in October 2026? I heard they are introducing a massive drone light show on Belgrave Road this year instead of traditional fireworks. Is this true?",
        "sentiment": "Neutral",
        "city": "Leicester",
        "isUpcoming": 1
    },
    {
        "id": "9",
        "platform": "Twitter",
        "author": "@CoventryDesis",
        "date": "2026-07-10",
        "event": "Leicester Diwali Lights Switch-On 2026",
        "text": "So excited for the upcoming Diwali Lights Switch-On 2026 in Leicester! The drone light show sounds brilliant and eco-friendly. Leicester Belgrave Road is the place to be this autumn. Already planning our family get-together!",
        "sentiment": "Positive",
        "city": "Leicester",
        "isUpcoming": 1
    },
    {
        "id": "10",
        "platform": "Facebook",
        "author": "Neha Shah",
        "date": "2026-07-12",
        "event": "Leicester Diwali Lights Switch-On 2026",
        "text": "While the drone show sounds exciting for Diwali 2026, I am really worried about Belgrave Road traffic closures. Parking in Leicester during Diwali is already impossible. The council needs to provide park-and-ride shuttle buses.",
        "sentiment": "Neutral",
        "city": "Leicester",
        "isUpcoming": 1
    },
    {
        "id": "11",
        "platform": "Twitter",
        "author": "@GarbaCoventry",
        "date": "2026-07-11",
        "event": "Coventry Navratri Garba 2026",
        "text": "Navratri Garba tickets in Coventry sold out in literally 10 minutes! 😡 Now scalpers are reselling £12 tickets for £45 on Facebook groups. This is unfair to genuine community members who want to celebrate. Organizers need a better ticketing system!",
        "sentiment": "Negative",
        "city": "Coventry",
        "isUpcoming": 1
    },
    {
        "id": "12",
        "platform": "Facebook",
        "author": "Meera Joshi",
        "date": "2026-07-13",
        "event": "Coventry Navratri Garba 2026",
        "text": "Thrilled that Navratri Garba 2026 is moving to a larger venue in Coventry! The community has grown so fast in the West Midlands. This year is going to be magnificent with live musicians flying in from Gujarat. Can't wait!",
        "sentiment": "Positive",
        "city": "Coventry",
        "isUpcoming": 1
    },
    {
        "id": "13",
        "platform": "Quora",
        "author": "Devendra Patel",
        "date": "2026-05-10",
        "event": "Midlands Indian Food Festival 2026",
        "text": "Which was the best Indian food event in the Midlands in 2026? Hands down the Midlands Indian Food Festival in Birmingham. The street food variety was mind-blowing – everything from Lucknowi chaat to South Indian filter coffee. Extremely well organized!",
        "sentiment": "Positive",
        "city": "Birmingham",
        "isUpcoming": 0
    },
    {
        "id": "14",
        "platform": "Twitter",
        "author": "@BrumFoodie",
        "date": "2026-05-11",
        "event": "Midlands Indian Food Festival 2026",
        "text": "The Birmingham food festival had excellent culinary representation, but the venue (Digbeth Arena) was extremely cramped. Long lines made it hard to walk around. They should move it to a larger park area next year.",
        "sentiment": "Neutral",
        "city": "Birmingham",
        "isUpcoming": 0
    },
    {
        "id": "15",
        "platform": "Quora",
        "author": "Rohan Kapoor",
        "date": "2026-07-05",
        "event": "General Community Feedback 2026",
        "text": "The level of Indian diaspora community engagement in the East Midlands (Nottingham, Leicester) has spiked in 2026. The youth-led cultural societies are doing a fantastic job bridging generational gaps through regional festivals and sports.",
        "sentiment": "Positive",
        "city": "Nottingham",
        "isUpcoming": 0
    }
]

# Database Setup & Seeding
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create SQLite table
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
    conn.commit()
    
    # Check if empty, and seed static data
    cursor.execute("SELECT COUNT(*) FROM feedback_items")
    count = cursor.fetchone()[0]
    
    if count == 0:
        print("Seeding SQLite database with static baseline data...")
        for item in STATIC_ITEMS:
            cursor.execute("""
                INSERT INTO feedback_items (id, platform, author, date, event, text, sentiment, city, isUpcoming)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item["id"], item["platform"], item["author"], item["date"],
                item["event"], item["text"], item["sentiment"], item["city"],
                item["isUpcoming"]
            ))
            # Also index in ChromaDB
            index_item_in_chroma(item)
            
        conn.commit()
    conn.close()

# Dynamically generate markdown for entire table if requested
def get_markdown_summary() -> str:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM feedback_items")
    rows = cursor.fetchall()
    conn.close()
    
    lines = [
        "| Platform | Author | Date | Event | Sentiment | City | Feedback text |",
        "| --- | --- | --- | --- | --- | --- | --- |"
    ]
    for row in rows:
        upcoming_tag = " (Upcoming Planned Activity)" if row["isUpcoming"] else ""
        text_escaped = row["text"].replace("|", "\\|")
        lines.append(
            f"| {row['platform']} | {row['author']} | {row['date']} | {row['event']}{upcoming_tag} | {row['sentiment']} | {row['city']} | {text_escaped} |"
        )
    return "\n".join(lines)

# Run initialization on import
init_db()
