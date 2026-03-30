# Content Filtering Agent — builder prompt

Use this template to specify your own **content filtering agent** (sources, topics, schedule, delivery).

**Typical motivation:** read only from sources you trust, avoid doom scrolling, get a daily digest of what matters, then optionally brainstorm **pros and cons** (or drafts) with an assistant so you stay knowledgeable on topics you care about.

Copy everything inside the fence below into a new chat with an AI assistant (or use it yourself as a spec worksheet). Fill in the bracketed sections, then ask the assistant to **design and implement** a content agent that matches your answers.

---

```
You are helping me design and build a personal “content agent” that runs on a schedule, collects links and summaries from the sources I care about, and delivers a daily brief (usually by email). I want to make my own choices for sources, topics, schedule, and tone.

## 1. Purpose
- **Primary goal:** [e.g. curated reading from trusted influencers, avoid doom scrolling, stay current on specific topics, brainstorm pros and cons by email, optional short posts / job search / team briefings]
- **Audience:** [just me / my team — describe voice if writing drafts]
- **Output I want each run:** [numbered list of links + 1–2 line summaries / full digest / only top 3 items / include “surprise” picks]

## 2. Schedule & runtime
- **When to run:** [e.g. every weekday at 6:00 AM local time, timezone: …]
- **Where it should run:** [my laptop / always-on server / cloud e.g. Railway — I’m OK with CLI-only or a minimal admin UI]
- **How long to look back:** [e.g. last 24–48 hours for news; longer for newsletters]

## 3. Delivery
- **How I get the brief:** [email to … / Slack / file drop / webhook]
- **If email:** [Gmail with app password / other provider — note if I need reply-by-email workflows]

## 4. Topics & filters
- **Must-include themes:** [comma-separated or bullet list]
- **Must-exclude:** [companies, keywords, or categories to skip]
- **Language / region:** [English only / include non-English sources]

## 5. Sources I want tracked (pick any)
For each, say **name + URL or search intent**:
- **RSS / blogs:** [feeds or publication names]
- **X (Twitter):** [handles to follow — @…]
- **LinkedIn:** [people or search phrases — note: usually via public search, not logged-in scraping]
- **Reddit:** [subreddits or `site:reddit.com …` style queries]
- **Podcasts / YouTube:** [shows or search phrases]
- **Companies & founders:** [company names + any founder names for announcements and posts]
- **Web search “always on” queries:** [e.g. “agentic AI product launch this week”]

## 6. Variety
- **Surprise / exploration:** [none / ~10% off-topic but adjacent / “one wild card per brief”]
- **Similar companies / adjacent profiles:** [yes/no — if yes, name 1–2 anchor companies to find “similar” to]

## 7. Guardrails
- **Fact-checking:** [strict sources only / OK with opinion pieces if labeled]
- **Length:** [max items per brief: …]
- **Privacy:** [never log full API keys / redact emails in logs]

## 8. Brainstorming (optional)
- **Do I want the agent to reply to my email and help draft posts?** [yes/no]
- **If yes, voice notes:** [direct / punchy / academic / “my voice samples: …”]

## 9. Technical preferences
- **Stack:** [Python is fine / Node / other]
- **External APIs I can use:** [Anthropic / OpenAI / Serper or other search / none — I’ll paste keys locally]
- **Persistence:** [SQLite for “already seen” URLs / no DB]

## 10. Success criteria
I’ll consider this a success when: [e.g. “I get a brief every morning at 6am with 10 relevant items and 1 surprise item, without duplicates from yesterday.”]

---

**Your task:** Using my answers above, propose:
1. A minimal architecture (fetch → filter → dedupe → format → send).
2. A config format (e.g. JSON + `.env`) that I can edit without code changes.
3. Exact implementation steps, file layout, and run commands.
4. A sensible default if I left something blank.

**Constraints:** Keep secrets out of git; document what goes in `.env` vs config; prefer simple, maintainable code over frameworks unless I asked otherwise.
```

---

## How to use this repo with the prompt

This repository already implements a Python CLI agent with `content_agent.py`, `content_agent_config.json`, and `.env` (see `README.md`). After you fill the prompt, you can ask the AI to **map your answers onto** `content_agent_config.json` and `.env`, or to refactor only what’s needed.
