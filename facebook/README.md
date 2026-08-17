# Facebook Collector Foundation

This folder contains a deliberately simple Facebook evidence collector for VA_PODCAST.

## Purpose

The goal is to collect recent public Facebook community signal and store it as normalized JSON so a later topic engine can evaluate what people are discussing.

This is not topic ranking. It is not keyword extraction. It is not LLM processing. It is only evidence collection.

## Files

- `groups.json` — approved watchlist of Facebook Groups/Pages
- `posts.json` — normalized recent post evidence
- `config.py` — collection limits and window settings
- `collector.py` — simple collection and audit entry points
- `access_audit.json` — latest source access status results
- `session/` — local browser session storage for manual login only

## Verify group URLs

Before turning a source on, verify the public Facebook URL manually.

If the URL is not yet confirmed:

- keep `enabled: false`
- keep `status: "REQUIRES_URL_VERIFICATION"`

## Access audit

Run:

```bash
python facebook/collector.py --audit
```

This checks each configured source and records likely status values such as:

- `PUBLIC_ACCESSIBLE`
- `LOGIN_REQUIRED`
- `AUTHENTICATED_ACCESSIBLE`
- `BLOCKED`
- `NOT_FOUND`
- `ERROR`

## Manual Facebook login

If a source needs login, use a dedicated local browser profile and sign in manually.

Run:

```bash
python facebook/collector.py --login
```

This opens a local session flow without automating password entry or bypassing anti-bot protections.

## Collection

Run:

```bash
python facebook/collector.py
```

Optional single-source run:

```bash
python facebook/collector.py --source whats_up_front_royal_va
```

## Where posts are saved

Normalized public posts are saved to:

```text
facebook/posts.json
```

Each post stores:

- post_id
- source_id
- source_name
- region
- post_url
- post_text
- published_at
- collected_at
- comments_count
- reactions_count
- shares_count
- comments

## Current limitations

- No topic extraction yet
- No keyword scoring yet
- No ranking yet
- No database yet
- No scheduled automation yet
- No Meta Content Library API integration yet
- No GUI integration yet

## Future plan

The current collector intentionally keeps a schema that can later be replaced by a Meta Content Library API or another official connector while preserving the same normalized post format.
