#!/usr/bin/env python3
"""Daily 5-minute English lesson podcast generator.

Flow: weather (Open-Meteo) + kids news (BBC Newsround RSS) -> Claude writes a
JSON script -> TTS (Google or OpenAI) -> mp3 + podcast RSS feed in ./site

Usage:
    python generate.py              # real run (needs API keys, see README)
    python generate.py --dry-run    # no Claude / TTS calls, silent audio, tests the pipeline
    python generate.py --list-voices  # list Google TTS voices (needs GOOGLE_TTS_API_KEY)
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import datetime as dt
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import requests

ENV = os.environ.get
TZ = ZoneInfo("Asia/Taipei")

# ---------- configuration (all overridable with environment variables) ----------
SITE_DIR = Path(ENV("SITE_DIR") or "site")
SITE_URL = (ENV("SITE_URL") or "http://localhost:8000").rstrip("/")
SHOW_TITLE = ENV("SHOW_TITLE") or "Morning English"
KID_NAME = (ENV("KID_NAME") or "").strip()
KEEP_EPISODES = int(ENV("KEEP_EPISODES") or 30)
CLAUDE_MODEL = ENV("CLAUDE_MODEL") or "claude-sonnet-5-5"
TTS_PROVIDER = (ENV("TTS_PROVIDER") or "google").lower()  # "google" or "openai"
WEATHER_LAT = ENV("WEATHER_LAT") or "25.012"
WEATHER_LON = ENV("WEATHER_LON") or "121.466"
WEATHER_PLACE = ENV("WEATHER_PLACE") or "New Taipei City"
NEWS_FEED = ENV("NEWS_FEED") or "https://feeds.bbci.co.uk/newsround/rss.xml"
MUSIC_ON = (ENV("MUSIC") or "on").lower() != "off"      # set variable MUSIC=off to disable
MUSIC_GAIN_DB = float(ENV("MUSIC_GAIN_DB") or 0)         # make music louder (+) or softer (-)
MUSIC_DIR = Path(ENV("MUSIC_DIR") or "music")            # optional own files: intro/transition/outro .mp3

# Topic rotation (interleaved so similar topics are not back to back)
TOPICS = [
    "animals", "classical music", "space", "trains", "food",
    "painting", "inventions", "sports", "travel",
]
TOPIC_HINTS = {
    "animals": "surprising animal facts, how animals live, survive or communicate",
    "classical music": "a composer's childhood, instruments of the orchestra, "
                       "a famous piece and what it makes you imagine",
    "space": "planets, astronauts, rockets, stars, or a space mission",
    "trains": "famous trains, train history, high-speed rail, how trains work",
    "food": "food from around the world, where foods come from, how foods are made",
    "painting": "a famous painter, colours, how a painting was made, a painting story",
    "inventions": "inventors and how an everyday thing was invented",
    "sports": "an interesting sport, an athlete's story, or how a sport started",
    "travel": "an interesting place in the world (sometimes in Asia), what you can see and do there",
}

WMO = {
    0: "clear and sunny", 1: "mostly sunny", 2: "partly cloudy", 3: "cloudy",
    45: "foggy", 48: "foggy", 51: "drizzly", 53: "drizzly", 55: "drizzly",
    56: "drizzly", 57: "drizzly", 61: "rainy", 63: "rainy", 65: "very rainy",
    66: "rainy", 67: "rainy", 71: "snowy", 73: "snowy", 75: "very snowy",
    77: "snowy", 80: "showery", 81: "showery", 82: "very showery",
    85: "snowy", 86: "snowy", 95: "stormy with thunder", 96: "stormy with thunder",
    99: "stormy with thunder",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def need(name: str) -> str:
    v = ENV(name)
    if not v:
        raise SystemExit(f"Missing environment variable: {name}")
    return v


def post_with_retry(url: str, **kwargs) -> requests.Response:
    last = ""
    for i in range(4):
        try:
            r = requests.post(url, timeout=120, **kwargs)
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"{r.status_code} {r.text[:200]}"
                time.sleep(3 * 2 ** i)
                continue
            return r
        except requests.RequestException as e:
            last = str(e)
            time.sleep(3 * 2 ** i)
    raise RuntimeError(f"Request failed after retries: {last}")


# ---------- data sources ----------
def get_weather() -> dict | None:
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": WEATHER_LAT, "longitude": WEATHER_LON,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max",
                "timezone": "Asia/Taipei", "forecast_days": 1,
            },
            timeout=20,
        )
        r.raise_for_status()
        d = r.json()["daily"]
        return {
            "place": WEATHER_PLACE,
            "description": WMO.get(int(d["weather_code"][0]), "mixed"),
            "high": round(d["temperature_2m_max"][0]),
            "low": round(d["temperature_2m_min"][0]),
            "rain_chance": d["precipitation_probability_max"][0],
        }
    except Exception as e:  # noqa: BLE001
        log(f"[warn] weather failed: {e}")
        return None


def get_news(today: dt.date) -> list[dict]:
    """Recent kid-friendly headlines from BBC Newsround (last ~40 hours)."""
    try:
        r = requests.get(NEWS_FEED, timeout=20, headers={"User-Agent": "morning-english/1.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:  # noqa: BLE001
        log(f"[warn] news failed: {e}")
        return []
    now = dt.datetime.now(dt.timezone.utc)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", "", it.findtext("description") or "").strip()
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        if not title or now - pub > dt.timedelta(hours=40):
            continue
        days_ago = (today - pub.astimezone(TZ).date()).days
        when = {0: "today", 1: "yesterday"}.get(days_ago, "recently")
        items.append({"title": title, "summary": desc, "when": when})
    return items[:12]


# ---------- history ----------
def load_history() -> dict:
    p = SITE_DIR / "history.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("[warn] history.json unreadable, starting fresh")
    return {"episodes": []}


def save_history(h: dict) -> None:
    h["episodes"] = h["episodes"][-200:]
    (SITE_DIR / "history.json").write_text(
        json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- script writing (Claude) ----------
SYSTEM = """You write scripts for a 5-minute morning English audio lesson. The listener is a 10-year-old in Taiwan who just finished the Cambridge KET (A2) exam with a high score and is preparing for PET (B1) next year. A text-to-speech voice reads the script aloud while the family travels, so write for the ear.

Style rules:
- Warm, playful, curious teacher voice. Talk directly to the child ("you").
- Language level: mostly A2 with some B1. Short sentences, mostly 8 to 15 words. Let the child hear useful PET grammar naturally: past simple, present perfect, comparatives and superlatives, "used to", "will" and "going to", first conditional, "have to" and "must".
- Teach exactly 4 new target words or phrases at PET (B1) level that a KET-level child probably does not know yet. Use each one naturally in the story so the meaning is clear from context.
- Plain spoken text only. No markdown, no bullet points, no emojis, no brackets, no stage directions, no URLs. Write numbers as words (for example "twenty-eight degrees Celsius").
- Never invent facts. For the news, use only what the given headline and summary say. For the topic, share only facts you are very sure are true.
- Everything must be safe, calm and positive for children.

Required structure (use these segment ids, in this order):
1. "greeting" (about 60 words): greet the child, say today's weekday and date, describe the weather in simple English, give one practical tip (umbrella, sunscreen, jacket, water...). If no weather data is given, just greet warmly. pause_after 1.0
2. "news" (about 70 words): start with a phrase like "Now, a story from the news." and retell ONE suitable story from the given list in your own simple words, 4 to 6 short sentences, using the "when" label ("yesterday", "today" or "recently"). End with "That story comes from BBC Newsround." Choose only happy, interesting or science, nature, animal, culture, technology or sport stories. Skip anything about war, death, crime, disasters, accidents, politics, scary or sad events. If no story is suitable or the list is empty, set news_used to false and instead write a cheerful 3-sentence "fun fact of the day" without mentioning the BBC. pause_after 1.2
3. "story_1", "story_2", "story_3" (about 100 words each): the main part, about the given topic. A small story or a set of surprising facts with a clear beginning, middle and end. Include all 4 target words. pause_after 0.8
4. "vocab_1" to "vocab_4" (about 25 words each): in this pattern: the word or phrase, then a simple meaning, then one example sentence, then "Say it with me:" and the word again. pause_after 4.0
5. "outro" (about 35 words): praise the child, one sentence recalling today's topic, say goodbye and "see you tomorrow". pause_after 0.

The whole script must be about 600 words (between 540 and 680).

Output ONLY valid JSON, nothing else, in exactly this shape:
{
  "title": "short episode title, max 8 words",
  "topic": "the topic",
  "news_used": true,
  "news_headline": "the original headline you used, or empty string",
  "new_words": ["word1", "word2", "word3", "word4"],
  "segments": [{"id": "greeting", "text": "...", "pause_after": 1.0}]
}"""


def build_user_prompt(today, weather, news, topic, history) -> str:
    eps = history["episodes"]
    recent_topics = [e.get("topic", "") for e in eps[-9:]]
    recent_words = sorted({w.lower() for e in eps[-30:] for w in e.get("new_words", [])})
    recent_titles = [e.get("title", "") for e in eps[-14:]]
    recent_news = [e.get("news_headline", "") for e in eps[-14:] if e.get("news_headline")]

    if weather:
        w = (f"Weather today in {weather['place']}: {weather['description']}, "
             f"high {weather['high']} degrees Celsius, low {weather['low']} degrees Celsius, "
             f"chance of rain {weather['rain_chance']} percent.")
    else:
        w = "No weather data available today."

    if news:
        n = "\n".join(f"- [{x['when']}] {x['title']} :: {x['summary']}" for x in news)
    else:
        n = "(no news available)"

    return f"""Date: {today.strftime('%A, %B')} {today.day}, {today.year}
Child's name: {KID_NAME or '(unknown; say "my friend" if you need to address them)'}
{w}

Today's topic: {topic}
Topic ideas: {TOPIC_HINTS[topic]}

News candidates (headline :: summary):
{n}

Avoid repeating these:
- Recent episode titles: {recent_titles or 'none'}
- News already used: {recent_news or 'none'}
- Words already taught recently (pick different ones): {recent_words or 'none'}

Write today's script now."""


def extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("no JSON object found")
    return json.loads(text[a:b + 1])


def clean_text(t: str) -> str:
    t = re.sub(r"[*_#`~]+", "", t)
    return re.sub(r"\s+", " ", t).strip()


def validate_script(s: dict) -> tuple[bool, str]:
    segs = s.get("segments")
    if not isinstance(segs, list) or len(segs) < 10:
        return False, "segments missing or too few"
    if not isinstance(s.get("new_words"), list) or len(s["new_words"]) < 3:
        return False, "new_words missing"
    words = 0
    for seg in segs:
        if not isinstance(seg.get("text"), str) or not seg["text"].strip():
            return False, f"segment {seg.get('id')} has no text"
        seg["text"] = clean_text(seg["text"])
        try:
            seg["pause_after"] = min(max(float(seg.get("pause_after", 0.8)), 0.0), 6.0)
        except (TypeError, ValueError):
            seg["pause_after"] = 0.8
        words += len(seg["text"].split())
    s["_words"] = words
    if not 480 <= words <= 760:
        return False, f"script has {words} words; it must be between 540 and 680"
    return True, "ok"


def generate_script(today, weather, news, topic, history) -> dict:
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    prompt = build_user_prompt(today, weather, news, topic, history)
    feedback = ""
    for attempt in range(3):
        msg = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=4000, system=SYSTEM,
            messages=[{"role": "user", "content": prompt + feedback}],
        )
        raw = "".join(b.text for b in msg.content if b.type == "text")
        try:
            script = extract_json(raw)
            ok, why = validate_script(script)
        except (ValueError, json.JSONDecodeError) as e:
            ok, why = False, f"invalid JSON ({e})"
        if ok:
            log(f"[info] script ok: {script['_words']} words, title: {script.get('title')}")
            return script
        log(f"[warn] attempt {attempt + 1} rejected: {why}")
        feedback = f"\n\nYour previous attempt was rejected: {why}. Fix this and output the JSON again."
    raise SystemExit("Could not get a valid script from Claude")


SAMPLE_SCRIPT = {
    "title": "Dry Run Sample", "topic": "trains", "news_used": False,
    "news_headline": "", "new_words": ["journey", "engine", "platform", "ticket"],
    "segments": [
        {"id": "greeting", "text": "Hello! This is a test episode. " * 3, "pause_after": 1.0},
        {"id": "story_1", "text": "Trains are fun. " * 10, "pause_after": 0.8},
        {"id": "vocab_1", "text": "Journey. A trip from one place to another.", "pause_after": 4.0},
    ],
}


# ---------- text-to-speech ----------
def chunk_text(text: str, limit: int = 900) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) + 1 > limit:
            chunks.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def tts_google(text: str) -> bytes:
    key = need("GOOGLE_TTS_API_KEY")
    voice = ENV("GOOGLE_TTS_VOICE") or "en-US-Chirp3-HD-Leda"
    lang = "-".join(voice.split("-")[:2])
    cfg = {"audioEncoding": "MP3", "speakingRate": float(ENV("SPEAKING_RATE") or 0.92)}
    url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={key}"

    def call():
        return post_with_retry(url, json={
            "input": {"text": text},
            "voice": {"languageCode": lang, "name": voice},
            "audioConfig": cfg,
        })

    r = call()
    if r.status_code == 400 and "rate" in r.text.lower():
        log("[warn] voice rejected speakingRate; retrying without it")
        cfg.pop("speakingRate")
        r = call()
    if r.status_code != 200:
        raise RuntimeError(f"Google TTS error {r.status_code}: {r.text[:300]}")
    return base64.b64decode(r.json()["audioContent"])


def tts_openai(text: str) -> bytes:
    key = need("OPENAI_API_KEY")
    r = post_with_retry(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": ENV("OPENAI_TTS_MODEL") or "gpt-4o-mini-tts",
            "voice": ENV("OPENAI_TTS_VOICE") or "coral",
            "input": text,
            "instructions": "Speak as a warm, patient English teacher talking to a "
                            "10-year-old. Clear, friendly, a little slower than normal, "
                            "with natural expression.",
            "response_format": "mp3",
        },
    )
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI TTS error {r.status_code}: {r.text[:300]}")
    return r.content


def list_voices() -> None:
    key = need("GOOGLE_TTS_API_KEY")
    r = requests.get("https://texttospeech.googleapis.com/v1/voices",
                     params={"languageCode": "en-US", "key": key}, timeout=30)
    r.raise_for_status()
    for v in r.json().get("voices", []):
        if any(k in v["name"] for k in ("Chirp3", "Neural2", "Studio")):
            print(v["name"], v.get("ssmlGender", ""))


# ---------- music (synthesized music-box sounds; optional own files in ./music) ----------
# Notes: C major. Melodies are Beethoven's "Ode to Joy" (public domain composition).
NOTE = {"C4": 261.63, "D4": 293.66, "E4": 329.63, "F4": 349.23, "G4": 392.00,
        "A4": 440.00, "C5": 523.25, "E5": 659.25, "G5": 783.99, "C6": 1046.50,
        "C3": 130.81, "G3": 196.00}
ODE_INTRO = [("E4", 1), ("E4", 1), ("F4", 1), ("G4", 1), ("G4", 1), ("F4", 1), ("E4", 1), ("D4", 1),
             ("C4", 1), ("C4", 1), ("D4", 1), ("E4", 1), ("E4", 1.5), ("D4", 0.5), ("D4", 2)]
ODE_OUTRO = [("E4", 1), ("E4", 1), ("F4", 1), ("G4", 1), ("G4", 1), ("F4", 1), ("E4", 1), ("D4", 1),
             ("C4", 1), ("C4", 1), ("D4", 1), ("E4", 1), ("D4", 1.5), ("C4", 0.5), ("C4", 3)]


def _tone(freq: float, dur: float, sr: int, vol: float = 1.0):
    import numpy as np
    t = np.arange(int(sr * dur)) / sr
    env = np.exp(-t * 3.2) * np.minimum(1.0, t / 0.005)          # soft attack, bell-like decay
    wave = (np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(2 * np.pi * 2 * freq * t)
            + 0.12 * np.sin(2 * np.pi * 3 * freq * t))
    return vol * env * wave


def _render(events, sr: int = 24000, tail: float = 1.2):
    """events: list of (start_seconds, note_name, duration_seconds, volume)."""
    import numpy as np
    from pydub import AudioSegment
    total = max(s + d for s, _, d, _ in events) + tail
    buf = np.zeros(int(sr * total))
    for start, note, dur, vol in events:
        tone = _tone(NOTE[note], dur + tail, sr, vol)
        i = int(sr * start)
        buf[i:i + len(tone)] += tone[:len(buf) - i]
    peak = np.max(np.abs(buf)) or 1.0
    buf = buf / peak * 0.35                                       # about -9 dBFS peak
    seg = AudioSegment(data=(buf * 32767).astype(np.int16).tobytes(),
                       sample_width=2, frame_rate=sr, channels=1)
    return seg.fade_in(30).fade_out(int(tail * 700))


def _melody(notes, beat: float = 0.42, bass: bool = True):
    events, t = [], 0.0
    for name, beats in notes:
        events.append((t, name, beats * beat, 1.0))
        t += beats * beat
    if bass:  # gentle bass notes under the tune
        for k in range(0, int(t / (beat * 4)) + 1):
            events.append((k * beat * 4, "C3" if k % 2 == 0 else "G3", beat * 3.5, 0.45))
    return events


def load_music() -> dict:
    """Returns {"intro","transition","outro"} AudioSegments (own files override synthesized)."""
    from pydub import AudioSegment
    music = {
        "intro": _render(_melody(ODE_INTRO)),
        "outro": _render(_melody(ODE_OUTRO)),
        "transition": _render([(0.0, "C5", 0.5, 1.0), (0.22, "E5", 0.5, 1.0),
                               (0.44, "G5", 0.5, 1.0), (0.66, "C6", 0.9, 0.9)], tail=0.8),
    }
    for name in list(music):
        for ext in ("mp3", "wav", "m4a"):
            p = MUSIC_DIR / f"{name}.{ext}"
            if p.exists():
                music[name] = AudioSegment.from_file(p).set_channels(1).fade_in(50).fade_out(800)
                log(f"[info] using your own music file: {p}")
                break
    if MUSIC_GAIN_DB:
        music = {k: v.apply_gain(MUSIC_GAIN_DB) for k, v in music.items()}
    return music


def synthesize(script: dict, dry_run: bool):
    from pydub import AudioSegment

    tts = tts_openai if TTS_PROVIDER == "openai" else tts_google
    music = load_music() if MUSIC_ON else None
    audio = music["intro"] + AudioSegment.silent(duration=500) if music else AudioSegment.silent(duration=600)
    prev_group = None
    for seg in script["segments"]:
        group = str(seg.get("id", "")).split("_")[0]
        if music and prev_group is not None and group != prev_group:
            audio += music["transition"] + AudioSegment.silent(duration=400)   # section change
        prev_group = group
        for chunk in chunk_text(seg["text"]):
            if dry_run:
                clip = AudioSegment.silent(duration=int(len(chunk.split()) * 400))
            else:
                clip = AudioSegment.from_file(io.BytesIO(tts(chunk)), format="mp3")
            audio += clip + AudioSegment.silent(duration=250)
        audio += AudioSegment.silent(duration=int(seg["pause_after"] * 1000))
    if music:
        audio += AudioSegment.silent(duration=300) + music["outro"]
    else:
        audio += AudioSegment.silent(duration=800)
    return audio.set_channels(1)


# ---------- publishing ----------
def fmt_duration(sec: int) -> str:
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def write_feed(history: dict) -> None:
    eps = [e for e in history["episodes"] if (SITE_DIR / e["file"]).exists()]
    eps = sorted(eps, key=lambda e: e["date"], reverse=True)[:KEEP_EPISODES]
    items = []
    for e in eps:
        d = dt.date.fromisoformat(e["date"])
        pub = format_datetime(dt.datetime.combine(d, dt.time(5, 30), TZ))
        url = f"{SITE_URL}/{e['file']}"
        items.append(f"""    <item>
      <title>{escape(e['title'])}</title>
      <description>{escape(e['summary'])}</description>
      <pubDate>{pub}</pubDate>
      <guid isPermaLink="false">{escape(e['date'])}-morning-english</guid>
      <enclosure url="{escape(url)}" length="{e['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{fmt_duration(e['duration_sec'])}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")
    cover = ENV("COVER_URL")
    image = f'    <itunes:image href="{escape(cover)}"/>\n' if cover else ""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{escape(SHOW_TITLE)}</title>
    <link>{escape(SITE_URL)}</link>
    <language>en</language>
    <description>A short daily English lesson with weather, kids news and a fun story.</description>
    <itunes:author>{escape(SHOW_TITLE)}</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="Education"/>
{image}{chr(10).join(items)}
  </channel>
</rss>
"""
    (SITE_DIR / "feed.xml").write_text(xml, encoding="utf-8")

    rows = "\n".join(f"<li>{escape(e['date'])} - {escape(e['title'])}</li>" for e in eps)
    (SITE_DIR / "index.html").write_text(
        f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(SHOW_TITLE)}</title>
<h1>{escape(SHOW_TITLE)}</h1>
<p>Podcast feed: <a href="feed.xml">{escape(SITE_URL)}/feed.xml</a></p>
<ul>{rows}</ul>
""", encoding="utf-8")
    (SITE_DIR / ".nojekyll").touch()


def prune(history: dict) -> None:
    keep = {e["file"] for e in sorted(history["episodes"], key=lambda e: e["date"],
                                      reverse=True)[:KEEP_EPISODES]}
    for f in (SITE_DIR / "episodes").glob("*.mp3"):
        if f"episodes/{f.name}" not in keep:
            f.unlink()
            log(f"[info] removed old episode {f.name}")


# ---------- main ----------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list-voices", action="store_true")
    ap.add_argument("--preview-music", action="store_true", help="export music_preview.mp3 and exit")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today in Taipei)")
    ap.add_argument("--topic", choices=TOPICS)
    args = ap.parse_args()

    if args.list_voices:
        list_voices()
        return
    if args.preview_music:
        from pydub import AudioSegment
        m = load_music()
        gap = AudioSegment.silent(duration=1500)
        (m["intro"] + gap + m["transition"] + gap + m["outro"]).export("music_preview.mp3", format="mp3")
        print("wrote music_preview.mp3 (intro, transition, outro)")
        return

    today = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(TZ).date()
    (SITE_DIR / "episodes").mkdir(parents=True, exist_ok=True)
    history = load_history()
    topic = args.topic or ENV("TOPIC") or TOPICS[today.toordinal() % len(TOPICS)]
    log(f"[info] {today} topic: {topic}")

    weather = get_weather()
    news = get_news(today)
    log(f"[info] weather: {weather}; news candidates: {len(news)}")

    script = SAMPLE_SCRIPT if args.dry_run else generate_script(today, weather, news, topic, history)
    audio = synthesize(script, args.dry_run)

    rel = f"episodes/{today.isoformat()}.mp3"
    out = SITE_DIR / rel
    audio.export(out, format="mp3", bitrate="64k")
    duration = round(len(audio) / 1000)
    log(f"[info] wrote {out} ({duration}s, {out.stat().st_size} bytes)")
    if not args.dry_run and not 150 <= duration <= 480:
        log(f"[warn] unusual duration {duration}s - check this episode")

    summary = f"Topic: {script.get('topic', topic)}. New words: {', '.join(script['new_words'])}."
    if script.get("news_used"):
        summary += " News from BBC Newsround."
    entry = {
        "date": today.isoformat(), "file": rel, "title": f"{today:%b} {today.day}: {script['title']}",
        "topic": script.get("topic", topic), "new_words": script["new_words"],
        "news_headline": script.get("news_headline", "") if script.get("news_used") else "",
        "summary": summary, "duration_sec": duration, "bytes": out.stat().st_size,
    }
    history["episodes"] = [e for e in history["episodes"] if e["date"] != entry["date"]] + [entry]
    history["episodes"].sort(key=lambda e: e["date"])
    prune(history)
    save_history(history)
    write_feed(history)
    log("[done]")


if __name__ == "__main__":
    main()
