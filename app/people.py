"""People directory: who attends meetings, what each person is called, and what they do.

The same person is often named differently in a recording ("Sam" is "Samuel Reed"), so every person has aliases. The directory is given to the model, and owners it returns are mapped back to the full name.
"""
import json
import re
import unicodedata

import db


def normalize(text: str) -> str:
    """Compare names loosely: Arabic/Persian letter variants, half-spaces and extra whitespace do not matter."""
    text = unicodedata.normalize("NFKC", text).replace("ي", "ی").replace("ك", "ک").replace("‌", " ")
    text = re.sub(r"[‏ً-ٟـ]", "", text)  # RTL mark, diacritics, tatweel
    return " ".join(text.split()).casefold()


def all_people() -> list[dict]:
    people = db.query("SELECT id, name, role, aliases, jira_username FROM people ORDER BY name")
    for person in people:
        person["aliases"] = json.loads(person["aliases"])
    return people


def _keys(person: dict) -> set[str]:
    return {normalize(n) for n in [person["name"], *person["aliases"]] if normalize(n)}


def conflict(names: list[str], people: list[dict], ignore_id: int | None = None) -> str | None:
    """The first name already taken by another person, or None. Two people sharing a name would make matching ambiguous."""
    wanted = {normalize(n) for n in names}
    for person in people:
        if person["id"] != ignore_id and (taken := wanted & _keys(person)):
            return next(n for n in names if normalize(n) in taken) + f" (برای «{person['name']}» ثبت شده است)"
    return None


def canonical(name: str | None, people: list[dict]) -> str | None:
    """Map any known name of a person to their full name; unknown names pass through unchanged."""
    if not name:
        return name
    key = normalize(name)
    return next((p["name"] for p in people if key in _keys(p)), name.strip())


def clean_participants(text: str, people: list[dict]) -> str:
    """"Sam, Alex" -> "Samuel Reed, Alex": full names, no duplicates, original order."""
    names = [canonical(n.strip(), people) for n in re.split(r"[,،\n]", text) if n.strip()]
    return ", ".join(dict.fromkeys(names))


def prompt_context(participants: str, people: list[dict]) -> str:
    """The participant list and the directory, as text for the model."""
    lines = [f"Participants of this meeting: {participants or 'not specified'}"]
    if people:
        lines.append("People directory (full name; role; other names they go by). Use the full names from this directory. "
                     "A person may be called by any of their other names, and people who are not participants may be mentioned:")
        for p in people:
            details = [p["name"]]
            if p["role"]:
                details.append(f"role: {p['role']}")
            if p["aliases"]:
                details.append("also called: " + ", ".join(p["aliases"]))
            lines.append("- " + "; ".join(details))
    return "\n".join(lines)
