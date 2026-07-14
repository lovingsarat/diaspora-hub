"""
Export the current diaspora.db SQLite database to frontend/public/data.json
so that the GitHub Pages static deployment always shows the latest scraped data.

Deduplicates items before exporting to avoid duplicate feedback in the feed.

Run this after scraping new tweets:
    python export_data.py
"""
import os
import re
import json
import sqlite3
from collections import Counter
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "diaspora.db")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "frontend", "public", "data.json")

STOP_WORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "this", "that", "these", "those",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
    "us", "them", "my", "your", "his", "its", "our", "their", "and",
    "or", "but", "so", "yet", "for", "nor", "to", "of", "in", "on",
    "at", "by", "with", "from", "about", "as", "into", "through",
    "during", "before", "after", "above", "below", "between", "under",
    "again", "further", "then", "once", "here", "there", "when", "where",
    "why", "how", "all", "each", "few", "more", "most", "other", "some",
    "such", "no", "not", "only", "own", "same", "than", "too", "very",
    "just", "now", "get", "got", "go", "going", "went", "come", "came",
    "one", "two", "three", "first", "last", "good", "new", "old", "real",
    "dont", "didnt", "wasnt", "werent", "isnt", "arent", "hasnt",
    "havent", "hadnt", "wouldnt", "couldnt", "shouldnt", "cant",
    "cannot", "wont", "im", "its", "thats", "weve", "theyve", "youre",
    "ive", "s", "t", "m", "re", "ve", "ll", "d", "2026", "2025", "2024",
    "feedback", "general", "community", "event", "events",
}

ISSUE_KEYWORDS = {
    "parking": "Parking & Access",
    "traffic": "Traffic & Congestion",
    "queue": "Long Queues",
    "queues": "Long Queues",
    "tickets": "Ticketing",
    "ticket": "Ticketing",
    "price": "Pricing",
    "prices": "Pricing",
    "expensive": "Pricing",
    "cost": "Pricing",
    "food": "Food & Catering",
    "catering": "Food & Catering",
    "stalls": "Stalls & Vendors",
    "stall": "Stalls & Vendors",
    "waste": "Waste & Litter",
    "litter": "Waste & Litter",
    "bins": "Waste & Litter",
    "bin": "Waste & Litter",
    "safety": "Safety & Crowd Control",
    "crowd": "Crowd Management",
    "crowds": "Crowd Management",
    "weather": "Weather Planning",
    "rain": "Weather Planning",
    "wet": "Weather Planning",
    "delay": "Delays",
    "delays": "Delays",
    "venue": "Venue Size",
    "space": "Venue Size",
    "cramped": "Venue Size",
    "toilet": "Facilities",
    "toilets": "Facilities",
    "restroom": "Facilities",
    "washroom": "Facilities",
    "facilities": "Facilities",
    "transport": "Transport",
    "bus": "Transport",
    "shuttle": "Transport",
    "noise": "Noise",
    "noisy": "Noise",
}

PRAISE_KEYWORDS = {
    "dhol": "Dhol & Music",
    "music": "Dhol & Music",
    "colour": "Colours & Atmosphere",
    "colors": "Colours & Atmosphere",
    "color": "Colours & Atmosphere",
    "atmosphere": "Atmosphere",
    "community": "Community Spirit",
    "spirit": "Community Spirit",
    "langar": "Langar & Food",
    "food": "Food & Catering",
    "catering": "Food & Catering",
    "culture": "Culture",
    "cultural": "Culture",
    "family": "Family Friendly",
    "families": "Family Friendly",
    "organization": "Organisation",
    "organised": "Organisation",
    "organized": "Organisation",
    "venue": "Venue",
    "location": "Venue",
    "dance": "Dance & Performance",
    "performance": "Dance & Performance",
    "performances": "Dance & Performance",
    "lights": "Lights & Visuals",
    "drone": "Drone Show",
    "inclusive": "Inclusivity",
    "welcoming": "Inclusivity",
    "nostalgia": "Tradition",
    "traditional": "Tradition",
}

CITY_ALIASES = {
    "brum": "Birmingham",
    "bham": "Birmingham",
    "leic": "Leicester",
    "cov": "Coventry",
    "notts": "Nottingham",
    "wolves": "Wolverhampton",
}


def get_text_tokens(text):
    return set(re.findall(r"[a-z0-9£]+", text.lower()))


def has_near_duplicate_text(first, second):
    first_tokens = get_text_tokens(first)
    second_tokens = get_text_tokens(second)
    if not first_tokens and not second_tokens:
        return True
    shared = first_tokens & second_tokens
    union = first_tokens | second_tokens
    return len(union) > 0 and len(shared) / len(union) >= 0.85


def is_more_complete(candidate, existing):
    def score(item):
        return (
            (4 if item.get("isUpcoming") else 0)
            + (2 if item.get("event") not in ("Community Event", "General Community Feedback 2026") else 0)
            + (1 if item.get("sentiment") != "Neutral" else 0)
        )
    return score(candidate) > score(existing)


def deduplicate(items):
    unique = []
    for item in items:
        dup_idx = next(
            (
                i for i, existing in enumerate(unique)
                if existing["platform"].lower() == item["platform"].lower()
                and existing["author"].lower() == item["author"].lower()
                and existing["date"] == item["date"]
                and has_near_duplicate_text(existing["text"], item["text"])
            ),
            -1,
        )
        if dup_idx == -1:
            unique.append(item)
        elif is_more_complete(item, unique[dup_idx]):
            unique[dup_idx] = item
    return unique


def tokenize(text):
    """Return cleaned words suitable for counting."""
    tokens = re.findall(r"[a-z0-9£]+", text.lower())
    cleaned = []
    for t in tokens:
        if t in CITY_ALIASES:
            continue
        # drop common suffixes like 'ing', 'ed', 's' for grouping
        base = t.rstrip("s") if len(t) > 3 else t
        if base not in STOP_WORDS and len(base) > 2:
            cleaned.append(base)
    return cleaned


def get_trending_terms(items, top_n=15):
    """Top words across all feedback, with sentiment breakdown."""
    term_sentiments = {}
    for item in items:
        for token in tokenize(item["text"]):
            if token not in term_sentiments:
                term_sentiments[token] = {"Positive": 0, "Neutral": 0, "Negative": 0}
            term_sentiments[token][item["sentiment"]] += 1

    counts = {term: sum(v.values()) for term, v in term_sentiments.items()}
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:top_n]

    return [
        {
            "term": term,
            "count": counts[term],
            "positive": term_sentiments[term]["Positive"],
            "neutral": term_sentiments[term]["Neutral"],
            "negative": term_sentiments[term]["Negative"],
        }
        for term, _ in top
    ]


def get_theme_mentions(items, keywords, sentiments=None, top_n=8):
    """Group items by theme keyword and return most common themes."""
    theme_counts = Counter()
    theme_examples = {}
    for item in items:
        if sentiments and item["sentiment"] not in sentiments:
            continue
        text_lower = item["text"].lower()
        matched = set()
        for kw, label in keywords.items():
            if kw in text_lower:
                theme_counts[label] += 1
                if label not in theme_examples:
                    theme_examples[label] = item["text"][:180]
                matched.add(label)
    return [
        {"theme": theme, "count": count, "example": theme_examples.get(theme, "")}
        for theme, count in theme_counts.most_common(top_n)
    ]


def get_top_events(items, top_n=8):
    """Most discussed specific events, with sentiment breakdown."""
    generic = {"Community Event", "General Community Feedback 2026"}
    event_counts = {}
    event_sentiments = {}
    for item in items:
        event = item["event"]
        if event in generic:
            continue
        if event not in event_counts:
            event_counts[event] = 0
            event_sentiments[event] = {"Positive": 0, "Neutral": 0, "Negative": 0}
        event_counts[event] += 1
        event_sentiments[event][item["sentiment"]] += 1

    top = sorted(event_counts.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return [
        {
            "event": event,
            "count": event_counts[event],
            "positive": event_sentiments[event]["Positive"],
            "neutral": event_sentiments[event]["Neutral"],
            "negative": event_sentiments[event]["Negative"],
        }
        for event, _ in top
    ]


def get_top_authors(items, top_n=8):
    counts = Counter(i["author"] for i in items)
    return [{"author": author, "count": count} for author, count in counts.most_common(top_n)]


def get_sentiment_by_date(items):
    dates = {}
    for item in items:
        d = item["date"]
        if d not in dates:
            dates[d] = {"Positive": 0, "Neutral": 0, "Negative": 0}
        dates[d][item["sentiment"]] += 1
    # sort by date descending, keep last 14
    return dict(sorted(dates.items(), key=lambda x: x[0], reverse=True)[:14])


def get_city_sentiment(items):
    cities = {}
    for item in items:
        c = item["city"]
        if c not in cities:
            cities[c] = {"Positive": 0, "Neutral": 0, "Negative": 0, "total": 0}
        cities[c][item["sentiment"]] += 1
        cities[c]["total"] += 1
    return cities


def get_top_posts(items, sentiment, top_n=5):
    """Return most representative posts for a sentiment."""
    filtered = [i for i in items if i["sentiment"] == sentiment]
    # sort by length (more detailed) and then by token uniqueness
    filtered.sort(key=lambda x: len(x["text"]), reverse=True)
    return [
        {
            "id": i["id"],
            "author": i["author"],
            "event": i["event"],
            "city": i["city"],
            "text": i["text"][:250],
            "date": i["date"],
        }
        for i in filtered[:top_n]
    ]


def build_executive_summary(total, sentiment_pct, issues, praises, top_events, city_counts):
    """Create a short community-ready summary."""
    top_issues = [i["theme"] for i in issues[:3]]
    top_praises = [p["theme"] for p in praises[:3]]
    top_cities = sorted(city_counts.items(), key=lambda x: x[1], reverse=True)[:3]

    summary = (
        f"Across {total} posts, the conversation is "
        f"{sentiment_pct['Positive']:.0f}% positive, {sentiment_pct['Neutral']:.0f}% neutral, and "
        f"{sentiment_pct['Negative']:.0f}% negative. "
    )
    if top_issues:
        summary += f"The main concerns residents are raising are {', '.join(top_issues)}. "
    if top_praises:
        summary += f"The strongest positive feedback is around {', '.join(top_praises)}. "
    if top_events:
        summary += f"Most discussed events include {', '.join(e['event'] for e in top_events[:3])}. "
    if top_cities:
        summary += f"Top discussion cities: {', '.join(f'{c} ({n})' for c, n in top_cities)}."
    return summary


def build_analytics(items, sentiment_pct):
    total = len(items)
    platform_counts = {}
    city_counts = {}
    for i in items:
        platform_counts[i["platform"]] = platform_counts.get(i["platform"], 0) + 1
        city_counts[i["city"]] = city_counts.get(i["city"], 0) + 1

    issues = get_theme_mentions(items, ISSUE_KEYWORDS, sentiments=["Negative", "Neutral"], top_n=8)
    praises = get_theme_mentions(items, PRAISE_KEYWORDS, sentiments=["Positive", "Neutral"], top_n=8)
    top_events = get_top_events(items, top_n=8)

    return {
        "trendingTerms": get_trending_terms(items, top_n=15),
        "keyIssues": issues,
        "topPraises": praises,
        "topEvents": top_events,
        "topAuthors": get_top_authors(items, top_n=8),
        "sentimentByDate": get_sentiment_by_date(items),
        "citySentiment": get_city_sentiment(items),
        "topNegativePosts": get_top_posts(items, "Negative", top_n=5),
        "topPositivePosts": get_top_posts(items, "Positive", top_n=5),
        "executiveSummary": build_executive_summary(
            total, sentiment_pct, issues, praises, top_events, city_counts
        ),
    }


def export_to_json():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM feedback_items ORDER BY date DESC")
    rows = cursor.fetchall()
    conn.close()

    items = []
    for row in rows:
        items.append({
            "id": row["id"],
            "platform": row["platform"],
            "author": row["author"],
            "date": row["date"],
            "event": row["event"],
            "text": row["text"],
            "sentiment": row["sentiment"],
            "city": row["city"],
            "isUpcoming": bool(row["isUpcoming"])
        })

    # Deduplicate before exporting
    before = len(items)
    items = deduplicate(items)
    after = len(items)
    if before != after:
        print(f"Deduplicated: {before} -> {after} items (removed {before - after} duplicates)")

    # Compute stats
    total = len(items)
    positives = sum(1 for i in items if i["sentiment"] == "Positive")
    neutrals  = sum(1 for i in items if i["sentiment"] == "Neutral")
    negatives = sum(1 for i in items if i["sentiment"] == "Negative")

    platform_counts = {}
    city_counts = {}
    for i in items:
        platform_counts[i["platform"]] = platform_counts.get(i["platform"], 0) + 1
        city_counts[i["city"]] = city_counts.get(i["city"], 0) + 1

    sentiment_percentages = {
        "Positive": round((positives / total) * 100, 2) if total else 0,
        "Neutral":  round((neutrals  / total) * 100, 2) if total else 0,
        "Negative": round((negatives / total) * 100, 2) if total else 0,
    }

    output = {
        "exportedAt": datetime.utcnow().isoformat() + "Z",
        "totalFeedbackCount": total,
        "sentimentPercentages": sentiment_percentages,
        "platformCounts": platform_counts,
        "cityCounts": city_counts,
        "analytics": build_analytics(items, sentiment_percentages),
        "items": items
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Exported {total} items to {OUTPUT_PATH}")
    print(f"Sentiment: {positives} positive, {neutrals} neutral, {negatives} negative")
    print(f"Platforms: {platform_counts}")

if __name__ == "__main__":
    export_to_json()
