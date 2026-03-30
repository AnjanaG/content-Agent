"""
Content Filtering Agent — daily brief + email brainstorm pipeline
================================
Workflow learned from session on Mar 26, 2026:

1. Fetch latest articles/posts/podcasts from frontier AI companies,
   PM influencers, X accounts, tech publications
2. Filter for configured topics (agents, PM role, AI, Cursor, Claude, Replit, etc.)
3. Send beautiful HTML email digest with 10 items + source links
4. You reply by email with a raw angle or ask to weigh pros and cons on a topic
5. Agent brainstorms with Claude API—explore tradeoffs, challenge vague claims, sharpen thinking
6. Validates all claims — finds exact quotes, timestamps, source URLs
7. Optionally finalizes a short professional post in the configured voice when you ask
8. Sends the reply (and finalized post, if any) back to Gmail

Key guardrails from session:
- Every claim must have a verified source URL
- Every quote must be verbatim with timestamp/context
- Challenge vague statements ("OpenClaw would have done that in 10 actions" → verify first)
- Post must be concise — cut ruthlessly
- Always find primary sources (transcripts, official blogs) not just press coverage
- Production experience (TikTok: 200K advertisers, $150M revenue) = the credibility anchor
- Voice: Direct, opinionated, credible. No buzzword soup. Under 250 words.
"""

import os
import json
import time
import sqlite3
import logging
import hashlib
import schedule
import requests
import feedparser
import smtplib
import imaplib
import email
import anthropic

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import decode_header
from datetime import datetime, timedelta
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("content_agent.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
GMAIL_ADDRESS   = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASS  = os.getenv("GMAIL_APP_PASSWORD")
TO_EMAIL        = os.getenv("TO_EMAIL", GMAIL_ADDRESS)
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY")
SERPER_KEY      = os.getenv("SERPER_API_KEY")
DAILY_HOUR      = int(os.getenv("DAILY_HOUR", "8"))

# Public name for logs and email chrome (subjects/tags unchanged so Gmail filters keep working).
AGENT_NAME      = "Content Filtering Agent"

BRIEF_SUBJECT   = "📰 Your Daily Content Brief"
REPLY_TAG       = "[CONTENT-AGENT]"
# Max Reddit threads in one brief (rest come from RSS, LinkedIn, X, web, podcasts).
REDDIT_MAX_PER_BRIEF = 2
# LinkedIn discovery: more results + weekly window (indexing lags same-day search).
LINKEDIN_SERPER_NUM = 8
LINKEDIN_TBS = "qdr:w"


def stable_article_id(prefix: str, url: str) -> str:
    """Stable across runs (unlike built-in hash() for str)."""
    digest = hashlib.sha256((url or "").encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def brief_item_limit(config: dict) -> int:
    """
    Items per brief: ARTICLES_PER_BRIEF in .env overrides schedule.count in JSON, else JSON, else 10.
    """
    raw = os.getenv("ARTICLES_PER_BRIEF")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(1, min(50, int(str(raw).strip())))
        except ValueError:
            pass
    c = (config.get("schedule") or {}).get("count")
    if c is not None:
        try:
            return max(1, min(50, int(c)))
        except (TypeError, ValueError):
            pass
    return 10

# ─── EDITOR PROFILE (brainstorm system prompt) ───────────────────────────────
# Used for every reply-by-email brainstorm. Tune voice and credentials here.
EDITOR_SYSTEM = """
You are a sharp editorial partner: help the user deepen their understanding of a topic
(often by weighing pros and cons, tradeoffs, and evidence) and, when they want to publish,
craft professional short-form posts (e.g. LinkedIn).

ABOUT THE AUTHOR:
- Principal PM, shipped agentic AI in production at TikTok
- Built LLM Ad Assistant: $150M annual incremental revenue
- Built post-purchase AI support agents: CSAT 30% → 85%
- SMB portfolio: 200K+ monthly active advertisers, +15% retention
- Background: Atlassian (GPM, customer support platform from scratch), 
  AWS (inventory optimization with vision models), Cisco (B2B SaaS, 6 years)
- Currently: targeting Principal PM / Group PM at Anthropic, OpenAI, 
  Perplexity, Cursor, Harvey, Lovable, Replit

VOICE:
- Direct. Opinionated. Credible from REAL production experience.
- Not theoretical. Not generic. Not buzzword soup.
- Short punchy paragraphs. Under 250 words per post.
- Ends with a clear opinion or insight — never a question to the audience.
- References TikTok production experience when relevant.

YOUR JOB AS BRAINSTORM PARTNER:
1. If they are exploring (not publishing yet), map balanced pros and cons and what the
   sources actually support—then help them form a clear, defensible view.
2. Validate the angle — is it specific enough? Does it ring true from production?
3. Challenge vague claims — if they say something like "X would do Y faster", 
   push to verify it or remove it.
4. Ask ONE sharp question to get a more specific, credible detail.
5. When they ask to finalize a post, write it in the author's voice.
6. Always verify: are quotes verbatim? Do we have source URLs and timestamps?
7. Cut ruthlessly. Under 250 words for finalized posts. Every sentence must earn its place.

WHAT MAKES THESE POSTS STAND OUT:
- They've shipped what others are theorizing about
- A "two out of three" type insight — a named rule from real experience
- Not afraid to disagree with consensus
- Credibility numbers are specific: $150M, 200K advertisers, 30→85% CSAT

When the post is ready to finalize, output ONLY the final post text 
wrapped in [POST START] and [POST END] tags, followed by sources in
[SOURCES START] and [SOURCES END] tags.
"""

# ─── SOURCES ──────────────────────────────────────────────────────────────────
def load_config():
    """Load from dashboard export if exists, else use defaults."""
    if os.path.exists("content_agent_config.json"):
        with open("content_agent_config.json") as f:
            return json.load(f)
    return {
        "sources": [
            # Tier 1: Frontier AI company blogs
            {"name": "Anthropic",        "url": "https://www.anthropic.com/news/rss",              "type": "rss", "active": True},
            {"name": "OpenAI",           "url": "https://openai.com/blog/rss",                     "type": "rss", "active": True},
            {"name": "Cursor",           "url": "https://cursor.com/blog/rss",                     "type": "rss", "active": True},
            {"name": "Replit",           "url": "https://blog.replit.com/feed",                    "type": "rss", "active": True},
            {"name": "Perplexity",       "url": "https://blog.perplexity.ai/rss",                  "type": "rss", "active": True},
            {"name": "Google DeepMind",  "url": "https://deepmind.google/blog/rss.xml",            "type": "rss", "active": True},
            {"name": "Meta AI",          "url": "https://ai.meta.com/blog/rss/",                   "type": "rss", "active": True},
            # Tier 2: Tech publications
            {"name": "TechCrunch AI",    "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "type": "rss", "active": True},
            {"name": "The Verge AI",     "url": "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml", "type": "rss", "active": True},
            {"name": "Ars Technica",     "url": "https://feeds.arstechnica.com/arstechnica/technology-lab", "type": "rss", "active": True},
            {"name": "MIT Tech Review",  "url": "https://www.technologyreview.com/feed/",          "type": "rss", "active": True},
            {"name": "Wired AI",         "url": "https://www.wired.com/feed/tag/ai/latest/rss",    "type": "rss", "active": True},
            # Tier 3: PM + AI newsletters
            {"name": "Lenny Rachitsky",  "url": "https://www.lennysnewsletter.com/feed",           "type": "rss", "active": True},
            {"name": "The Neuron",       "url": "https://www.theneuron.ai/rss",                    "type": "rss", "active": True},
            {"name": "Ben's Bites",      "url": "https://bensbites.beehiiv.com/feed",              "type": "rss", "active": True},
            {"name": "Every.to",         "url": "https://every.to/feed",                           "type": "rss", "active": True},
            {"name": "Interconnects",    "url": "https://www.interconnects.ai/feed",               "type": "rss", "active": True},
            {"name": "Dept of Product",  "url": "https://departmentofproduct.substack.com/feed",   "type": "rss", "active": True},
            {"name": "SVPG",             "url": "https://www.svpg.com/feed/",                      "type": "rss", "active": True},
            {"name": "Pragmatic Engineer","url": "https://blog.pragmaticengineer.com/rss/",        "type": "rss", "active": True},
            # Tier 4: Web search for X/LinkedIn/WSJ (needs Serper key)
            {"name": "Harvey AI",        "url": None, "type": "web_search", "query": "Harvey AI agents news 2026",         "active": True},
            {"name": "Sierra AI",        "url": None, "type": "web_search", "query": "Sierra AI announcement OR launch Bret Taylor Clay Bavor conversational agents",  "active": True},
            {"name": "Decagon",          "url": None, "type": "web_search", "query": "Decagon AI announcement OR funding OR launch Jesse Zhang Ashwin Sreenivas customer agents",   "active": True},
            {"name": "Sunday Robotics",  "url": None, "type": "web_search", "query": "Sunday Robotics announcement OR funding Tony Zhao Cheng Chi home robot AI", "active": True},
            {"name": "Sunday voice AI",  "url": None, "type": "web_search", "query": "Sunday AI voice agents business CallSunday announcement", "active": True},
            {
                "name": "Agentic workflows innovation",
                "url": None,
                "type": "web_search",
                "query": "agentic workflow innovation OR agentic AI orchestration multi-step LLM agents 2026",
                "active": True,
            },
            {"name": "WSJ Tech",         "url": None, "type": "web_search", "query": "site:wsj.com AI agents product 2026","active": True},
        ],
        "x_accounts": [
            {"name": "Andrej Karpathy",  "handle": "karpathy",       "topic": "AI coding agents vibe"},
            {"name": "Sam Altman",       "handle": "sama",            "topic": "AI product AGI"},
            {"name": "Aravind Srinivas", "handle": "AravSrinivas",    "topic": "Perplexity AI product"},
            {"name": "Amjad Masad",      "handle": "amasad",          "topic": "Replit vibe coding agents"},
            {"name": "Shreyas Doshi",    "handle": "shreyasdoshi",    "topic": "product manager PM role"},
            {"name": "Bret Taylor",      "handle": "btaylor",         "topic": "Sierra AI agents"},
            {"name": "Clay Bavor",       "handle": "claybavor",       "topic": "Sierra AI agents product"},
            {"name": "Jesse Zhang",      "handle": "thejessezhang",   "topic": "Decagon AI customer agents"},
            {"name": "Tony Zhao",        "handle": "tonyzzhao",       "topic": "Sunday Robotics AI"},
            {"name": "Cheng Chi",        "handle": "chichengcc",    "topic": "Sunday Robotics AI"},
            {"name": "Michael Truell",   "handle": "mntruell",        "topic": "Cursor coding AI"},
            {"name": "Paul Graham",      "handle": "paulg",           "topic": "startups AI founders"},
            {"name": "Garry Tan",        "handle": "garrytan",        "topic": "YC AI startups coding"},
            {"name": "Yann LeCun",       "handle": "ylecun",          "topic": "AI opinion AGI"},
        ],
        "linkedin_searches": [
            "Shreyas Doshi LinkedIn product manager AI 2026",
            "Lenny Rachitsky LinkedIn PM job market AI",
            "Tomer Cohen LinkedIn full stack builder product",
            "Claire Vo LinkedIn AI product manager",
            "AI product management thought leadership agentic LLM",
            "enterprise AI adoption product strategy lessons learned",
            "chief product officer generative AI transformation",
            "principal product manager machine learning platform",
            "AI agent go-to-market B2B SaaS product",
            "LLM production deployment product manager",
            "frontier AI product launch company post",
            "AI trust safety responsible AI product leadership",
            "agentic workflow innovation product leadership",
            "multi-agent orchestration enterprise AI strategy",
        ],
        "daily_searches": [
            "agentic AI product manager 2026",
            "AI agents production trust safety 2026",
            "Claude Code Cursor Replit vibe coding latest",
            "PM role future AI diminishing 2026",
            "frontier AI company product launch this week",
            "Jensen Huang Karpathy Sam Altman AI opinion this week",
            "OpenClaw Claude computer use agents news",
            "site:reddit.com AI agents product management",
            "agentic workflow innovation announcement 2026",
            "AI agent orchestration tools multi-step workflow",
        ],
        "topics": [
            "agentic AI", "AI agents", "LLM", "agentic workflows",
            "agentic workflow", "orchestration", "multi-agent",
            "product manager", "PM role", "product management",
            "Cursor", "Claude Code", "Replit", "vibe coding",
            "frontier AI", "AI product", "computer use",
            "OpenClaw", "AGI", "AI slop", "responsible AI",
            "full-stack builder", "PM future",
        ],
        "schedule": {"hour": 8, "count": 10}
    }

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("content_agent.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_articles (
            id TEXT PRIMARY KEY,
            title TEXT,
            source TEXT,
            url TEXT,
            seen_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS threads (
            message_id TEXT PRIMARY KEY,
            article_json TEXT,
            conversation TEXT,
            final_post TEXT,
            sources TEXT,
            created_at TEXT,
            status TEXT DEFAULT 'brainstorming'
        )
    """)
    conn.commit()
    return conn

def already_seen(conn, article_id):
    return conn.execute(
        "SELECT 1 FROM seen_articles WHERE id=?", (article_id,)
    ).fetchone() is not None

def mark_seen(conn, article_id, title, source, url):
    conn.execute(
        "INSERT OR IGNORE INTO seen_articles VALUES (?,?,?,?,?)",
        (article_id, title, source, url, datetime.utcnow().isoformat())
    )
    conn.commit()

def save_thread(conn, message_id, article, conversation, final_post=None, sources=None, status="brainstorming"):
    conn.execute("""
        INSERT OR REPLACE INTO threads
        (message_id, article_json, conversation, final_post, sources, created_at, status)
        VALUES (?,?,?,?,?,?,?)
    """, (
        message_id, json.dumps(article), json.dumps(conversation),
        final_post, sources, datetime.utcnow().isoformat(), status
    ))
    conn.commit()

def get_thread(conn, message_id):
    row = conn.execute(
        "SELECT * FROM threads WHERE message_id=?", (message_id,)
    ).fetchone()
    if not row:
        return None
    cols = ["message_id","article_json","conversation","final_post","sources","created_at","status"]
    d = dict(zip(cols, row))
    d["article"] = json.loads(d["article_json"])
    d["conversation"] = json.loads(d["conversation"] or "[]")
    return d

# ─── FETCH ARTICLES ───────────────────────────────────────────────────────────
def is_relevant(text, topics):
    return any(t.lower() in text.lower() for t in topics)

def normalize_topics(topics):
    """
    Accept topics as either:
    - ["agentic AI", "product management"]
    - ["agentic AI, product management"]  # legacy single-line comma format
    """
    normalized = []
    for t in topics or []:
        if not t:
            continue
        for part in str(t).split(","):
            candidate = part.strip()
            if candidate:
                normalized.append(candidate)
    return normalized


def load_x_following_from_env():
    """
    Optional: comma-separated X handles from env (without @).
    Example: X_FOLLOWING_HANDLES=karpathy,sama,ylecun
    """
    raw = os.getenv("X_FOLLOWING_HANDLES", "")
    handles = []
    for part in raw.split(","):
        h = part.strip().lstrip("@")
        if h:
            handles.append(h)
    return handles


def build_discovery_queries(config, topics):
    """
    Build extra discovery searches from configured companies/people/topics so
    the agent can find adjacent content beyond strictly hardcoded items.
    """
    queries = []

    source_names = [s.get("name", "") for s in config.get("sources", []) if s.get("name")]
    people = [a.get("name", "") for a in config.get("x_accounts", []) if a.get("name")]
    companies = [n for n in source_names if n and n.lower() not in {"web search", "linkedin"}]
    topic_seeds = topics[:6]

    for topic in topic_seeds:
        queries.append(f"{topic} product launch this week")
        queries.append(f"{topic} startup funding announcement")

    for company in companies[:5]:
        queries.append(f"companies similar to {company} latest news")

    for person in people[:5]:
        queries.append(f"{person} recent interview podcast")

    # Agentic workflows — innovations in orchestration, multi-step agents, tooling.
    queries.extend(
        [
            "agentic workflow innovation multi-step AI agents enterprise 2026",
            "agentic AI orchestration tooling product launch announcement",
            "LLM agent workflow automation case study production",
            "multi-agent systems coordination product innovation",
        ]
    )

    # Deduplicate while preserving order.
    seen = set()
    deduped = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            deduped.append(q)
    return deduped[:24]


def build_surprise_queries(topics):
    """
    Broad trend queries for a small exploration slice in each brief.
    """
    base = [
        "emerging AI startup product launch this week",
        "developer tools product launch this week",
        "future of product management this week",
        "enterprise AI adoption case study this week",
        "technology leadership podcast episode this week",
        "agentic workflow breakthrough research or product this week",
    ]
    for topic in topics[:3]:
        base.append(f"unexpected trend in {topic} this week")
    return base


def fetch_rss(source, conn, topics, include_seen=False):
    articles = []
    try:
        feed = feedparser.parse(source["url"])
        cutoff = datetime.utcnow() - timedelta(hours=48)
        for entry in feed.entries[:20]:
            title   = entry.get("title", "")
            url     = entry.get("link", "")
            summary = entry.get("summary", "")[:500]
            article_id = stable_article_id("rss", url)
            if not include_seen and already_seen(conn, article_id):
                continue
            if not is_relevant(title + " " + summary, topics):
                continue
            articles.append({
                "id": article_id, "title": title, "url": url,
                "summary": summary, "source": source["name"],
                "type": "article",
                "published_ts": time.mktime(entry.published_parsed) if entry.get("published_parsed") else None,
            })
            if not include_seen:
                mark_seen(conn, article_id, title, source["name"], url)
    except Exception as e:
        log.warning(f"RSS failed [{source['name']}]: {e}")
    return articles

def _is_reddit_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return "reddit.com" in u or "redd.it" in u


def cap_reddit_articles(articles: list, max_reddit: int) -> list:
    """Keep at most max_reddit items whose URL is Reddit; drop extra Reddit, keep order."""
    out = []
    n = 0
    for a in articles:
        if _is_reddit_url(a.get("url", "")):
            if n < max_reddit:
                out.append(a)
                n += 1
        else:
            out.append(a)
    return out


def fetch_web(
    query,
    label,
    conn,
    topics,
    include_seen=False,
    enforce_relevance=True,
    num=6,
    tbs="qdr:d",
):
    articles = []
    if not SERPER_KEY:
        return articles
    qlow = (query or "").lower()
    if "site:reddit.com" in qlow or "site:www.reddit.com" in qlow:
        num = min(num, REDDIT_MAX_PER_BRIEF)
    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": num, "tbs": tbs},
            timeout=12
        )
        r.raise_for_status()
        for idx, result in enumerate(r.json().get("organic", []), start=1):
            title   = result.get("title", "")
            url     = result.get("link", "")
            snippet = result.get("snippet", "")
            article_id = stable_article_id("web", url)
            if not include_seen and already_seen(conn, article_id):
                continue
            if enforce_relevance and not is_relevant(title + " " + snippet, topics):
                continue
            articles.append({
                "id": article_id, "title": title, "url": url,
                "summary": snippet, "source": label, "type": "article",
                "search_position": idx,
            })
            if not include_seen:
                mark_seen(conn, article_id, title, label, url)
    except Exception as e:
        log.warning(f"Web search failed [{query[:40]}]: {e}")
    return articles

def fetch_podcast(query, label, conn, topics, include_seen=False):
    """Search for latest podcast episodes from key people."""
    return fetch_web(
        f"podcast episode {query} 2026",
        f"Podcast: {label}",
        conn,
        topics,
        include_seen=include_seen
    )

def fetch_all(config, conn, include_seen=False, limit=None):
    topics = normalize_topics(config.get("topics", []))
    all_articles = []

    # RSS sources
    for source in config.get("sources", []):
        if not source.get("active"):
            continue
        if source["type"] == "rss" and source.get("url"):
            all_articles.extend(fetch_rss(source, conn, topics, include_seen=include_seen))
        elif source["type"] == "web_search" and source.get("query"):
            all_articles.extend(fetch_web(source["query"], source["name"], conn, topics, include_seen=include_seen))
        time.sleep(0.3)

    # X accounts via Serper (configured + optional env-provided following handles).
    x_accounts = list(config.get("x_accounts", []))
    existing_handles = {a.get("handle", "").lower() for a in x_accounts}
    for h in load_x_following_from_env():
        if h.lower() not in existing_handles:
            x_accounts.append({"name": h, "handle": h, "topic": "AI product agents"})
            existing_handles.add(h.lower())

    for acct in x_accounts:
        q = f"from:{acct['handle']} {acct['topic']} site:twitter.com OR site:x.com"
        all_articles.extend(fetch_web(q, f"X / @{acct['handle']}", conn, topics, include_seen=include_seen))

    # LinkedIn searches (deeper pass: more organic results + weekly window)
    for query in config.get("linkedin_searches", []):
        q = (query or "").strip()
        if not q:
            continue
        linkedin_q = q if "site:linkedin.com" in q.lower() else q + " site:linkedin.com"
        all_articles.extend(
            fetch_web(
                linkedin_q,
                "LinkedIn",
                conn,
                topics,
                include_seen=include_seen,
                num=LINKEDIN_SERPER_NUM,
                tbs=LINKEDIN_TBS,
            )
        )

    # Daily web searches
    for query in config.get("daily_searches", []):
        all_articles.extend(fetch_web(query, "Web Search", conn, topics, include_seen=include_seen))

    # Discovery: similar companies, profiles, and adjacent topics.
    for query in build_discovery_queries(config, topics):
        all_articles.extend(fetch_web(query, "Discovery", conn, topics, include_seen=include_seen))

    # Surprise factor: intentionally broader searches, lightly filtered.
    for query in build_surprise_queries(topics):
        all_articles.extend(
            fetch_web(
                query,
                "Surprise",
                conn,
                topics,
                include_seen=include_seen,
                enforce_relevance=False,
            )
        )

    # Podcasts
    podcasts = [
        ("Lex Fridman AI podcast episode", "Lex Fridman Podcast"),
        ("Jensen Huang interview podcast 2026", "Jensen Huang"),
        ("Sam Altman podcast interview 2026", "Sam Altman"),
        ("Lenny Rachitsky podcast product AI", "Lenny's Podcast"),
        ("Andrej Karpathy interview talk 2026", "Karpathy"),
        ("Dwarkesh Patel interview podcast 2026", "Dwarkesh Patel"),
        ("Peter Yang product podcast interview 2026", "Peter Yang"),
        ("Andrew Ng interview podcast AI 2026", "Andrew Ng"),
        ("Geoffrey Hinton interview podcast AI 2026", "Geoffrey Hinton"),
        ("Yann LeCun interview podcast AI 2026", "Yann LeCun"),
        ("Dario Amodei interview 2026", "Dario Amodei"),
    ]
    for query, label in podcasts:
        all_articles.extend(fetch_podcast(query, label, conn, topics, include_seen=include_seen))

    # Deduplicate by URL
    seen_urls, unique = set(), []
    for a in all_articles:
        if a["url"] not in seen_urls:
            seen_urls.add(a["url"])
            unique.append(a)

    # Hard cap Reddit so brief stays professional (LinkedIn / RSS / press weighted higher).
    unique = cap_reddit_articles(unique, REDDIT_MAX_PER_BRIEF)

    if not unique:
        return []

    ranked = rank_articles(unique, topics)
    lim = limit if limit is not None else brief_item_limit(config)
    return ranked[:lim]


def _domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def _source_trust_weight(source: str, url: str) -> float:
    s = (source or "").lower()
    d = _domain(url)
    # High trust: frontier labs, top publications, and known expert channels.
    if any(x in d for x in ["anthropic.com", "openai.com", "deepmind.google", "technologyreview.com", "wsj.com", "techcrunch.com"]):
        return 1.0
    if "linkedin" in s:
        return 0.80
    if s.startswith("x / @"):
        return 0.82
    if s.startswith("podcast"):
        return 0.72
    if s in {"discovery", "surprise", "web search"}:
        return 0.68
    return 0.75


def _recency_score(article: dict) -> float:
    ts = article.get("published_ts")
    if ts:
        age_hours = max(0.0, (time.time() - float(ts)) / 3600.0)
        if age_hours <= 24:
            return 1.0
        if age_hours <= 72:
            return 0.8
        if age_hours <= 168:
            return 0.55
        return 0.35
    src = (article.get("source") or "").lower()
    if "linkedin" in src or src.startswith("x / @"):
        return 0.8
    if src.startswith("podcast"):
        return 0.55
    return 0.65


def _topic_coverage_score(article: dict, topics: list) -> float:
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    matches = 0
    for t in topics:
        if t.lower() in text:
            matches += 1
    return min(1.0, matches / 3.0)


def _engagement_hint_score(article: dict) -> float:
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    signals = [
        "launch", "announc", "funding", "benchmark", "case study",
        "viral", "repost", "comment", "interview", "podcast",
    ]
    hits = sum(1 for s in signals if s in text)
    pos = article.get("search_position") or 10
    position_bonus = max(0.0, (10 - min(int(pos), 10)) / 10.0)
    return min(1.0, (hits / 4.0) * 0.7 + position_bonus * 0.3)


def rank_articles(articles: list, topics: list) -> list:
    scored = []
    for a in articles:
        trust = _source_trust_weight(a.get("source", ""), a.get("url", ""))
        recency = _recency_score(a)
        topic = _topic_coverage_score(a, topics)
        engage = _engagement_hint_score(a)
        base = (0.38 * trust) + (0.27 * recency) + (0.20 * topic) + (0.15 * engage)
        b = dict(a)
        b["_base_score"] = round(base, 4)
        scored.append(b)

    scored.sort(key=lambda x: x["_base_score"], reverse=True)

    # Diversity/uniqueness: discourage too many from same domain/source in top slots.
    domain_count = {}
    source_count = {}
    reranked = []
    for a in scored:
        d = _domain(a.get("url", ""))
        s = a.get("source", "")
        d_penalty = 0.07 * max(0, domain_count.get(d, 0))
        s_penalty = 0.06 * max(0, source_count.get(s, 0))
        a["_score"] = round(a["_base_score"] - d_penalty - s_penalty, 4)
        reranked.append(a)
        domain_count[d] = domain_count.get(d, 0) + 1
        source_count[s] = source_count.get(s, 0) + 1

    reranked.sort(key=lambda x: x["_score"], reverse=True)
    for a in reranked:
        a.pop("_base_score", None)
        a.pop("_score", None)
    return reranked

# ─── CLAUDE BRAINSTORM ────────────────────────────────────────────────────────
def brainstorm(article, user_message, conversation_history):
    """
    Core brainstorm loop with guardrails:
    - Challenges vague claims
    - Verifies quotes and sources
    - Shapes the configured author voice
    - Finalizes when ready
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    article_context = f"""
ARTICLE/POST BEING DISCUSSED:
Title: {article.get('title', '')}
Source: {article.get('source', '')}
URL: {article.get('url', '')}
Type: {article.get('type', 'article')}
Summary: {article.get('summary', '')[:500]}
"""

    system = EDITOR_SYSTEM + "\n\n" + article_context

    messages = conversation_history + [
        {"role": "user", "content": user_message}
    ]

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=system,
            messages=messages
        )
        return resp.content[0].text
    except Exception as e:
        log.error(f"Claude API failed: {e}")
        return "Sorry, I had trouble connecting. Please try again."

# ─── EMAIL: SEND ──────────────────────────────────────────────────────────────
def send_email(to, subject, html_body, in_reply_to=None, references=None):
    try:
        msg = MIMEMultipart("alternative")
        msg["From"]    = GMAIL_ADDRESS
        msg["To"]      = to
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"]  = references or in_reply_to
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
            s.sendmail(GMAIL_ADDRESS, to, msg.as_string())
        log.info(f"Email sent: {subject[:60]}")
        return msg.get("Message-ID", "")
    except Exception as e:
        log.error(f"Email send failed: {e}")
        return ""

# ─── EMAIL: HTML TEMPLATES ────────────────────────────────────────────────────
ICON_MAP = {
    "article": "📄",
    "podcast": "🎙️",
    "X / @": "𝕏",
    "LinkedIn": "💼",
    "Web Search": "🔍",
    "Podcast:": "🎙️",
}

def get_icon(source):
    for key, icon in ICON_MAP.items():
        if key in source:
            return icon
    return "📰"

def build_brief_html(articles, date_str):
    items_html = ""
    for i, a in enumerate(articles, 1):
        icon = get_icon(a["source"])
        source_label = a["source"]
        items_html += f"""
        <tr>
          <td style="padding:18px 0;border-bottom:1px solid #f0ede8;vertical-align:top;">
            <table style="width:100%;border-collapse:collapse;">
              <tr>
                <td style="width:36px;vertical-align:top;padding-top:2px;">
                  <div style="width:28px;height:28px;background:#f0f9e0;border-radius:6px;
                    display:flex;align-items:center;justify-content:center;
                    font-size:14px;text-align:center;line-height:28px;">{icon}</div>
                </td>
                <td style="padding-left:12px;">
                  <div style="font-size:11px;color:#999;font-family:monospace;
                    text-transform:uppercase;letter-spacing:0.06em;margin-bottom:4px;">
                    {i}. {source_label}
                  </div>
                  <div style="font-size:16px;font-weight:600;color:#1a1a1a;
                    margin-bottom:6px;line-height:1.35;">{a['title']}</div>
                  <div style="font-size:13px;color:#666;line-height:1.6;margin-bottom:8px;">
                    {a['summary'][:220]}...
                  </div>
                  <a href="{a['url']}" style="font-size:12px;color:#3a7c10;
                    text-decoration:none;font-family:monospace;font-weight:500;">
                    → Read / Listen
                  </a>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#faf9f7;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:32px 16px;">

    <!-- Header -->
    <div style="background:#111;border-radius:14px;padding:26px 30px;margin-bottom:24px;">
      <div style="font-size:11px;color:#777;font-family:monospace;
        text-transform:uppercase;letter-spacing:0.12em;margin-bottom:6px;">
        {AGENT_NAME} · Daily Content Brief · {REPLY_TAG}
      </div>
      <div style="font-size:24px;font-weight:700;color:#fff;margin-bottom:4px;">
        {date_str}
      </div>
      <div style="font-size:13px;color:#aaa;">
        {len(articles)} items matched your topics — articles, podcasts, X posts, LinkedIn
      </div>
    </div>

    <!-- Articles -->
    <div style="background:#fff;border-radius:14px;padding:20px 26px;
      margin-bottom:20px;border:1px solid #ece9e3;">
      <table style="width:100%;border-collapse:collapse;">{items_html}</table>
    </div>

    <!-- How to use -->
    <div style="background:#f0f9e0;border-radius:12px;padding:20px 24px;
      border:1px solid #c8e89a;">
      <div style="font-size:13px;font-weight:700;color:#2d5a0e;margin-bottom:10px;">
        ↩ How to use this brief
      </div>
      <div style="font-size:13px;color:#3a6e1a;line-height:1.7;">
        <b>Reply to this email</b> with the number + your raw take.<br>
        <span style="font-family:monospace;background:#dff0c0;padding:2px 7px;
          border-radius:3px;font-size:12px;">
          4 Jensen's "two out of three" rule hit me — I was building this manually at TikTok
        </span><br><br>
        The agent will challenge your angle, verify all claims, and draft a post in your voice.<br>
        Reply <span style="font-family:monospace;background:#dff0c0;padding:1px 5px;
          border-radius:3px;font-size:12px;">finalize</span> when ready.
      </div>
    </div>

    <div style="text-align:center;margin-top:20px;font-size:11px;
      color:#ccc;font-family:monospace;">
      {REPLY_TAG} · {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC
    </div>
  </div>
</body></html>"""

def build_brainstorm_html(response, article_title, round_num):
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#faf9f7;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:32px 16px;">
    <div style="background:#111;border-radius:12px;padding:20px 26px;margin-bottom:20px;">
      <div style="font-size:11px;color:#777;font-family:monospace;
        text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px;">
        Brainstorm · Round {round_num} · {REPLY_TAG}
      </div>
      <div style="font-size:15px;font-weight:600;color:#fff;line-height:1.4;">
        {article_title[:80]}
      </div>
    </div>
    <div style="background:#fff;border-radius:12px;padding:26px;
      border:1px solid #ece9e3;">
      <div style="font-size:15px;color:#1a1a1a;line-height:1.75;white-space:pre-wrap;">
        {response}
      </div>
    </div>
    <div style="background:#f0f9e0;border-radius:10px;padding:16px 22px;
      margin-top:16px;border:1px solid #c8e89a;">
      <div style="font-size:13px;color:#3a6e1a;">
        ↩ Reply to keep refining, or reply
        <span style="font-family:monospace;background:#dff0c0;padding:1px 5px;
          border-radius:3px;">finalize</span> when ready.
      </div>
    </div>
    <div style="text-align:center;margin-top:16px;font-size:11px;
      color:#ccc;font-family:monospace;">{REPLY_TAG}</div>
  </div>
</body></html>"""

def build_final_html(post_text, sources_text, article_title):
    sources_html = sources_text.replace('\n', '<br>')
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#faf9f7;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:32px 16px;">

    <div style="background:#2d5a0e;border-radius:12px;padding:20px 26px;
      margin-bottom:20px;">
      <div style="font-size:11px;color:#a8d878;font-family:monospace;
        text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px;">
        ✓ LinkedIn Post Ready · {REPLY_TAG}
      </div>
      <div style="font-size:15px;font-weight:600;color:#fff;">
        Based on: {article_title[:70]}
      </div>
    </div>

    <!-- Post -->
    <div style="background:#fff;border-radius:12px;padding:28px;
      border:2px solid #c8e89a;margin-bottom:16px;">
      <div style="font-size:11px;color:#999;font-family:monospace;
        text-transform:uppercase;letter-spacing:0.08em;margin-bottom:16px;">
        Copy and paste to LinkedIn ↓
      </div>
      <div style="font-size:15px;color:#1a1a1a;line-height:1.8;
        white-space:pre-wrap;border-left:3px solid #8bc34a;padding-left:18px;">
        {post_text}
      </div>
    </div>

    <!-- Sources -->
    <div style="background:#f8f6f2;border-radius:10px;padding:20px 24px;
      border:1px solid #ece9e3;">
      <div style="font-size:11px;color:#999;font-family:monospace;
        text-transform:uppercase;letter-spacing:0.08em;margin-bottom:12px;">
        Sources — paste as first comment on LinkedIn
      </div>
      <div style="font-size:12px;color:#555;font-family:monospace;line-height:2;">
        {sources_html}
      </div>
    </div>

    <div style="text-align:center;margin-top:20px;font-size:11px;
      color:#ccc;font-family:monospace;">
      {REPLY_TAG} · Post generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC
    </div>
  </div>
</body></html>"""

# ─── EMAIL: RECEIVE & PARSE ───────────────────────────────────────────────────
def clean_reply(body):
    """Strip quoted text from email reply."""
    lines = body.split('\n')
    clean = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('>'):
            break
        if stripped.startswith('On ') and 'wrote:' in stripped:
            break
        if '-----Original Message-----' in stripped:
            break
        clean.append(line)
    return '\n'.join(clean).strip()

def get_email_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode("utf-8", errors="ignore")
    else:
        return msg.get_payload(decode=True).decode("utf-8", errors="ignore")
    return ""

def poll_replies(conn, articles_in_session):
    """Poll Gmail for replies, route to brainstorm or finalize."""
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
        mail.select("inbox")

        since = (datetime.utcnow() - timedelta(days=3)).strftime("%d-%b-%Y")
        # IMAP SEARCH must be ASCII; use tag only (all agent emails include REPLY_TAG).
        _, msg_ids = mail.search(None, f'(UNSEEN SINCE {since} SUBJECT "{REPLY_TAG}")')

        for msg_id in msg_ids[0].split():
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            subject      = decode_header(msg["Subject"] or "")[0][0]
            if isinstance(subject, bytes):
                subject = subject.decode("utf-8", errors="ignore")
            in_reply_to  = msg.get("In-Reply-To", "")
            references   = msg.get("References", "")
            body         = clean_reply(get_email_body(msg))

            if not body.strip():
                continue

            mail.store(msg_id, "+FLAGS", "\\Seen")
            log.info(f"Reply received: {subject[:50]} | {body[:60]}")

            lower_body = body.strip().lower()
            if any(
                phrase in lower_body
                for phrase in ["fetch more", "get more", "more articles", "fetch better", "better articles"]
            ):
                mode = "better" if "better" in lower_body else "more"
                send_requested_brief(mode=mode)
                continue

            # Check if continuing an existing thread
            thread = None
            if in_reply_to:
                thread = get_thread(conn, in_reply_to)

            if thread:
                handle_continuation(conn, thread, body, msg)
            else:
                handle_new_pick(conn, body, msg, articles_in_session)

        mail.logout()
    except Exception as e:
        log.error(f"Gmail poll failed: {e}")

def handle_new_pick(conn, body, msg, articles):
    """User picked an article from the brief."""
    parts = body.strip().split(" ", 1)
    if not parts[0].isdigit():
        log.info("Reply doesn't start with a number — ignoring.")
        return

    num = int(parts[0]) - 1
    opinion = parts[1].strip() if len(parts) > 1 else ""

    if num < 0 or num >= len(articles):
        send_email(TO_EMAIL, f"Re: {BRIEF_SUBJECT} {REPLY_TAG}",
            "<p>That article number wasn't in today's brief. Please pick a number from the email.</p>")
        return

    article = articles[num]
    conversation = []

    # First brainstorm turn
    response = brainstorm(article, opinion, conversation)
    conversation.append({"role": "user", "content": opinion})
    conversation.append({"role": "assistant", "content": response})

    message_id = msg.get("Message-ID", f"gen-{datetime.utcnow().isoformat()}")
    save_thread(conn, message_id, article, conversation)

    round_num = 1
    in_reply_to = msg.get("Message-ID", "")
    send_email(
        TO_EMAIL,
        f"Re: {BRIEF_SUBJECT} — Brainstorm Rd {round_num} {REPLY_TAG}",
        build_brainstorm_html(response, article["title"], round_num),
        in_reply_to=in_reply_to, references=in_reply_to
    )

def handle_continuation(conn, thread, body, msg):
    """User is continuing the brainstorm or finalizing."""
    article      = thread["article"]
    conversation = thread["conversation"]
    in_reply_to  = msg.get("Message-ID", "")
    references   = msg.get("References", "")

    conversation.append({"role": "user", "content": body})
    response = brainstorm(article, body, conversation[:-1])
    conversation.append({"role": "assistant", "content": response})

    save_thread(conn, thread["message_id"], article, conversation)

    round_num = len([m for m in conversation if m["role"] == "assistant"])

    # Check for finalized post
    if "[POST START]" in response and "[POST END]" in response:
        post = response.split("[POST START]")[1].split("[POST END]")[0].strip()
        sources = ""
        if "[SOURCES START]" in response and "[SOURCES END]" in response:
            sources = response.split("[SOURCES START]")[1].split("[SOURCES END]")[0].strip()

        save_thread(conn, thread["message_id"], article, conversation,
                   final_post=post, sources=sources, status="final")

        send_email(
            TO_EMAIL,
            f"✅ Your LinkedIn Post is Ready {REPLY_TAG}",
            build_final_html(post, sources, article["title"]),
            in_reply_to=in_reply_to, references=references
        )
        log.info(f"Final post sent for: {article['title'][:50]}")
    else:
        send_email(
            TO_EMAIL,
            f"Re: {BRIEF_SUBJECT} — Brainstorm Rd {round_num} {REPLY_TAG}",
            build_brainstorm_html(response, article["title"], round_num),
            in_reply_to=in_reply_to, references=references
        )

# ─── DAILY BRIEF ─────────────────────────────────────────────────────────────
# Store today's articles so reply handler can reference them
todays_articles = []

def run_daily_brief():
    global todays_articles
    log.info("Running daily brief...")
    config = load_config()
    limit = brief_item_limit(config)
    conn = init_db()
    articles = fetch_all(config, conn, include_seen=False, limit=limit)
    if len(articles) < limit:
        # Backfill from previously seen items when today's pool is sparse.
        fallback = fetch_all(config, conn, include_seen=True, limit=limit)
        seen_urls = {a.get("url") for a in articles}
        for a in fallback:
            if a.get("url") not in seen_urls:
                articles.append(a)
                seen_urls.add(a.get("url"))
            if len(articles) >= limit:
                break
    articles = articles[:limit]
    conn.close()

    if not articles:
        log.info("No new articles found today.")
        return

    todays_articles = articles
    date_str = datetime.now().strftime("%A, %B %d")
    html = build_brief_html(articles, date_str)
    send_email(TO_EMAIL, f"{BRIEF_SUBJECT} — {date_str} {REPLY_TAG}", html)
    log.info(f"Brief sent: {len(articles)} items.")


def send_requested_brief(mode="more"):
    """
    Send refreshed brief from email command.
    mode: "more" increases count, "better" keeps count but refreshes candidates.
    """
    global todays_articles
    config = load_config()
    base = brief_item_limit(config)
    effective = min(base + 5, 20) if mode == "more" else base

    conn = init_db()
    articles = fetch_all(config, conn, include_seen=True, limit=effective)
    conn.close()

    if not articles:
        send_email(
            TO_EMAIL,
            f"Re: {BRIEF_SUBJECT} {REPLY_TAG}",
            "<p>I couldn't find additional articles right now. Try again in a bit.</p>",
        )
        return

    todays_articles = articles
    date_str = datetime.now().strftime("%A, %B %d")
    html = build_brief_html(articles, date_str)
    send_email(TO_EMAIL, f"{BRIEF_SUBJECT} — Refreshed {date_str} {REPLY_TAG}", html)
    log.info(f"Refreshed brief sent: {len(articles)} items.")

# ─── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"🤖 {AGENT_NAME} starting...")

    # Send brief immediately on start
    run_daily_brief()

    # Schedule daily brief
    schedule.every().day.at(f"{DAILY_HOUR:02d}:00").do(run_daily_brief)
    log.info(f"Daily brief scheduled at {DAILY_HOUR}:00")

    # Poll Gmail for replies every 5 minutes
    def poll():
        conn = init_db()
        poll_replies(conn, todays_articles)
        conn.close()

    schedule.every(5).minutes.do(poll)
    log.info("Polling Gmail every 5 minutes for replies...")

    while True:
        schedule.run_pending()
        time.sleep(60)
