"""
Deduplicate an existing frontend/public/data.json file in-place.

Run this after manually editing data.json or when you see duplicates:
    python dedup_data.py
"""
import os
import re
import json
from datetime import datetime

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "frontend", "public", "data.json")


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


def main():
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    before = len(data["items"])
    data["items"] = deduplicate(data["items"])
    after = len(data["items"])

    # Recompute stats
    total = len(data["items"])
    positives = sum(1 for i in data["items"] if i["sentiment"] == "Positive")
    neutrals = sum(1 for i in data["items"] if i["sentiment"] == "Neutral")
    negatives = sum(1 for i in data["items"] if i["sentiment"] == "Negative")

    platform_counts = {}
    city_counts = {}
    for i in data["items"]:
        platform_counts[i["platform"]] = platform_counts.get(i["platform"], 0) + 1
        city_counts[i["city"]] = city_counts.get(i["city"], 0) + 1

    data["exportedAt"] = datetime.utcnow().isoformat() + "Z"
    data["totalFeedbackCount"] = total
    data["sentimentPercentages"] = {
        "Positive": round((positives / total) * 100, 2) if total else 0,
        "Neutral": round((neutrals / total) * 100, 2) if total else 0,
        "Negative": round((negatives / total) * 100, 2) if total else 0,
    }
    data["platformCounts"] = platform_counts
    data["cityCounts"] = city_counts

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Deduplicated: {before} -> {after} items (removed {before - after} duplicates)")
    print(f"Stats: {positives} positive, {neutrals} neutral, {negatives} negative")
    print(f"Platforms: {platform_counts}")
    print(f"Cities: {city_counts}")


if __name__ == "__main__":
    main()
