"""Deterministic extraction (extract-v1): evidence quote, entities and script detection.

The quote is always an exact substring of review_text (code guarantees it). Short reviews use the
full (whitespace-trimmed) text; longer reviews use the sentence span that best matches the chosen
topic/intent lexicon, falling back to the first sentence. Whether the quote *supports* the label is
evaluated separately on the golden set.
"""
import re

from . import settings

LEXICON = {
    "access": r"log ?in|logg|sign ?in|sign ?up|password|account|verif|code|hack|email|log ?out|locked",
    "usability": r"\bads?\b|advert|commercial|layout|\bui\b|interface|design|navigat|button|queue|playlist|library|widget|lock ?screen|home ?(page|screen)|update",
    "playback": r"crash|stop|paus|skip|freez|lag|buffer|connect|internet|bluetooth|\bcar\b|android auto|chromecast|sound|audio|volume|quality|battery|slow|play|error|close",
    "downloads": r"download|offline|saved|storage|sd card",
    "catalog": r"song|artist|album|search|recommend|algorithm|lyric|podcast|audiobook|find|missing|unavailable|grey|gray|remov|ban",
    "billing": r"premium|\bpay|price|money|charg|subscri|refund|\bfree\b|shuffle|skips?\b|trial|\bplan\b|student|family|card|\$|cost|expensive",
    "support": r"support|customer service|\bhelp|contact|respon|repl|chat|agent",
    "other": r"",
}
INTENT_CUES = {
    "cancellation": r"uninstall|cancel|delet|switch|leav|moving to|goodbye|bye|apple music|youtube music|never (use|again)|done with",
    "complaint": r"not|n't|never|can'?t|won'?t|bad|worst|terrible|annoy|hate|problem|issue|bug|fix|why",
    "request": r"please|add|wish|would be|should|hope|want",
    "praise": r"love|great|good|best|amazing|awesome|excellent|nice|perfect",
    "unclear": r"",
}
ENTITY_TERMS = [
    "smart shuffle", "shuffle", "lyrics", "ads", "premium", "offline", "downloads", "download", "podcasts", "podcast",
    "playlists", "playlist", "queue", "widget", "Android Auto", "Bluetooth", "Chromecast", "Spotify Connect", "Wrapped",
    "DJ", "Discover Weekly", "Daily Mix", "Blend", "audiobooks", "audiobook", "sleep timer", "equalizer", "lock screen",
    "Alexa", "Wear OS", "smartwatch", "Google Assistant", "family plan", "student", "Duo", "free trial", "search",
    "home screen", "radio", "autoplay", "crossfade", "Canvas", "video", "notification", "car", "skip", "skips",
    "liked songs", "library", "login", "password", "refund", "subscription", "customer service", "support",
]
_ENTITY_RE = re.compile(r"(?<![\w])(" + "|".join(re.escape(t) for t in sorted(ENTITY_TERMS, key=len, reverse=True)) + r")(?![\w])",
                        re.IGNORECASE)
_SENT_RE = re.compile(r"[^.!?\n]+[.!?]*")


def _trimmed_span(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def evidence_quote(text, topic, intent, max_chars=300):
    s, e = _trimmed_span(text, 0, len(text))
    if e - s <= settings.QUOTE_FULLTEXT_MAX_CHARS:
        return text[s:e]
    spans = [_trimmed_span(text, m.start(), m.end()) for m in _SENT_RE.finditer(text)]
    spans = [(a, b) for a, b in spans if b - a >= 3]
    if not spans:
        return text[s:min(e, s + max_chars)].strip()
    t_re = re.compile(LEXICON.get(topic, ""), re.I) if LEXICON.get(topic) else None
    i_re = re.compile(INTENT_CUES.get(intent, ""), re.I) if INTENT_CUES.get(intent) else None

    def score(span):
        seg = text[span[0]:span[1]]
        return 2 * (len(t_re.findall(seg)) if t_re else 0) + (len(i_re.findall(seg)) if i_re else 0)

    best = max(range(len(spans)), key=lambda k: (score(spans[k]), -k))
    a, b = spans[best]
    if b - a < 25 and best + 1 < len(spans):       # too short to be meaningful: extend to next sentence
        b = spans[best + 1][1]
    if b - a > max_chars:                            # cut at a word boundary, still an exact substring
        cut = text.rfind(" ", a, a + max_chars)
        b = cut if cut > a + 20 else a + max_chars
    a, b = _trimmed_span(text, a, b)
    return text[a:b]


def entities(text):
    seen, out = set(), []
    for m in _ENTITY_RE.finditer(text):
        k = m.group(1).lower()
        if k not in seen:
            seen.add(k)
            out.append(m.group(1))
    return out


def non_latin_share(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if ord(c) > 0x24F) / len(letters)


# ---- subtopic assignment (code, not a model): first matching rule inside the chosen topic ----
SUBTOPIC_RULES = {
    "access": [("account_locked_or_hacked", r"hack|stolen|disabled|banned|suspend|locked|someone else"),
               ("signup_verification", r"sign ?up|verif|code|facebook|google account|register|create (an )?account"),
               ("login_failure", r"log ?in|logg|sign ?in|password|log(s|ged)? (me )?out|account")],
    "usability": [("ads_frequency", r"\bads?\b|advert|commercial"),
                  ("library_playlist_queue", r"playlist|queue|library|liked songs|sort|add to"),
                  ("controls_widgets", r"button|widget|lock ?screen|notification|gesture|swipe|sleep timer|setting|control"),
                  ("ui_navigation", r"layout|ui\b|interface|design|update|navigat|home|feed|look|tab|menu|confus")],
    "playback": [("crash_freeze", r"crash|freez|won'?t open|doesn'?t open|not open|black screen|close[sd]? (itself|by itself)|keeps closing"),
                 ("devices_bluetooth_car_cast", r"bluetooth|\bcar\b|android auto|chromecast|cast|connect\b|speaker|watch|alexa|tv\b|headphone|earbud"),
                 ("connection_errors", r"internet|connection|offline|network|wifi|wi-fi|load|buffer|something went wrong|error"),
                 ("audio_quality_volume", r"sound|audio|volume|quality|equali|loud|quiet|glitch|static"),
                 ("performance_battery_data", r"slow|lag|battery|data usage|storage|heat|ram|memory|cache"),
                 ("stops_pauses_skips", r"stop|paus|skip|jump|won'?t play|not play|doesn'?t play|cut(s|ting)? out|play")],
    "downloads": [("downloads_disappear", r"disappear|gone|deleted|removed|re-?download|lost|vanish"),
                  ("download_failure_offline", r"download|offline|can'?t|won'?t|stuck|not work")],
    "catalog": [("lyrics", r"lyric"),
                ("podcasts_audiobooks", r"podcast|audiobook|episode|video"),
                ("recommendations", r"recommend|algorithm|suggest|discover|same songs|repeat|autoplay|radio|smart shuffle|dj\b"),
                ("search_discovery", r"search|find|browse"),
                ("content_policy", r"\bban|censor|remov(e|ed|ing) (artist|song|podcast)|politic|rogan|woke|conservative|christian"),
                ("missing_content", r"missing|unavailable|not available|grey|gray|region|can'?t find|removed|isn'?t on")],
    "billing": [("charges_refunds", r"charg|refund|money back|billed|payment|card|stole|scam|fraud|deduct"),
                ("premium_entitlement", r"premium (is )?not|didn'?t get|not activ|still (have|get) ads|trial|cancel (my )?(subscription|premium)|family|student|duo"),
                ("free_tier_restrictions", r"shuffle|skip|choose|pick|select|order|free (version|users?|tier|plan)|without premium|pay (for|to)|paywall|lyrics"),
                ("price_cost", r"price|expensive|cost|afford|increase|\$|rupee|worth|cheap")],
    "support": [("unreachable", r"no (way|one)|can'?t (contact|reach)|no repl|no response|ignor|bot|chat|email"),
                ("unhelpful", r"")],
}


def subtopic(text, topic, intent):
    if topic == "other":
        return {"praise": "other.general_positive", "unclear": "other.unrelated_unclear"}.get(intent, "other.general_negative")
    for sub, rx in SUBTOPIC_RULES.get(topic, []):
        if not rx or re.search(rx, text, re.I):
            return f"{topic}.{sub}"
    return f"{topic}.other" if f"{topic}.other" in settings.SUBTOPICS else f"{topic}.{SUBTOPIC_RULES[topic][-1][0]}"
