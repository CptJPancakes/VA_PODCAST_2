import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
GROUPS_PATH = BASE_DIR / "groups.json"
POSTS_PATH = BASE_DIR / "posts.json"
ACCESS_AUDIT_PATH = BASE_DIR / "access_audit.json"
SESSION_DIR = BASE_DIR / "session"


def load_json(path):
    if not path.exists():
        return [] if path.name.endswith(".json") and "posts" in path.name else {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def normalize_text(value):
    if value is None:
        return ""
    value = str(value).strip().replace("\r", " ").replace("\n", " ")
    return " ".join(value.split())


def stable_hash(value):
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def normalize_post(raw_post, source):
    post_text = normalize_text(raw_post.get("post_text") or raw_post.get("text") or "")
    post_url = raw_post.get("post_url") or raw_post.get("url") or ""
    published_at = raw_post.get("published_at") or raw_post.get("created_at")
    post_id = raw_post.get("post_id") or raw_post.get("id") or stable_hash(post_url or post_text)

    return {
        "post_id": str(post_id),
        "source_id": source.get("id"),
        "source_name": source.get("name"),
        "source_type": "facebook_" + str(source.get("type", "group")),
        "region": source.get("region"),
        "post_url": post_url,
        "post_text": post_text,
        "published_at": published_at,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "comments_count": int(raw_post.get("comments_count") or 0) if raw_post.get("comments_count") is not None else None,
        "reactions_count": int(raw_post.get("reactions_count") or 0) if raw_post.get("reactions_count") is not None else None,
        "shares_count": int(raw_post.get("shares_count") or 0) if raw_post.get("shares_count") is not None else None,
        "comments": [normalize_comment(comment) for comment in raw_post.get("comments", [])],
    }


def normalize_comment(comment):
    return {
        "comment_id": str(comment.get("comment_id") or comment.get("id") or stable_hash(comment.get("text") or "")),
        "text": normalize_text(comment.get("text") or comment.get("comment_text") or ""),
        "published_at": comment.get("published_at") or comment.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "reactions_count": int(comment.get("reactions_count") or 0),
    }


def normalize_post_url(raw_url):
    if not raw_url:
        return ""
    url = str(raw_url).strip()
    if url.startswith("/"):
        url = "https://www.facebook.com" + url
    url = url.split("?", 1)[0]
    url = url.replace("https://m.facebook.com", "https://www.facebook.com")
    return url


def parse_numeric_value(raw_value):
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    if not value:
        return None
    value = value.replace(",", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([kKmM]?)", value)
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "k":
        number *= 1000
    elif suffix == "m":
        number *= 1000000
    return int(number)


def parse_engagement_from_text(text):
    if not text:
        return None
    value = text.replace("\u202f", " ")
    for pattern in [r"(\d+(?:\.\d+)?[kKmM]?)\s*(?:comments?|replies?)", r"(\d+(?:\.\d+)?[kKmM]?)\s*(?:shares?)", r"(\d+(?:\.\d+)?[kKmM]?)\s*(?:likes?|reactions?)", r"(\d+(?:\.\d+)?[kKmM]?)\s*(?:people)"]:
        match = re.search(pattern, value, flags=re.I)
        if match:
            return parse_numeric_value(match.group(1))
    return None


def extract_post_text(raw_text):
    text = normalize_text(raw_text or "")
    if not text:
        return ""
    for junk in ["Like", "Comment", "Share", "See more", "Follow", "Notifications", "Menu", "Profile", "Home", "Groups", "Posts", "Privacy", "Terms", "Advertising", "Ad Choices"]:
        text = text.replace(junk, " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 20:
        return ""
    return text


def discover_real_posts(page, source_url):
    return page.evaluate(
        """
        () => {
          const pattern = /\/posts\/|\/permalink\/|story_fbid|fbid=|comment_id=|reply_comment_id=/i;
          const anchors = Array.from(document.querySelectorAll('a[href]'))
            .map((anchor) => {
              const href = (anchor.href || anchor.getAttribute('href') || '').trim();
              const text = (anchor.textContent || anchor.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
              return { href, text, aria: anchor.getAttribute('aria-label') || '', role: anchor.getAttribute('role') || '', element: anchor };
            })
            .filter((entry) => entry.href && pattern.test(entry.href) && !/\/photo\//i.test(entry.href));

          const results = [];
          const seen = new Set();
          for (const entry of anchors) {
            const href = entry.href;
            const normalized = href.split('?')[0];
            if (!normalized || seen.has(normalized)) continue;
            seen.add(normalized);

            let container = entry.element;
            let bestText = entry.text || entry.aria || '';
            let depth = 0;
            while (container && container !== document.body && depth < 12) {
              const currentText = (container.innerText || '').replace(/\s+/g, ' ').trim();
              const tag = (container.tagName || '').toLowerCase();
              const role = container.getAttribute('role') || '';
              const containsText = currentText.length > 80 && currentText.length < 3500;
              const isLikelyPost = /article|section|div|li/.test(tag) || role === 'article';
              const isNavNoise = /(Like|Comment|Share|Follow|Notifications|Menu|Privacy|Terms|About|Home|Groups|Trending|Posts)/i.test(currentText);
              if (isLikelyPost && containsText && !isNavNoise) {
                bestText = currentText;
                break;
              }
              container = container.parentElement;
              depth += 1;
            }

            const containerText = bestText || entry.text || entry.aria || '';
            if (!containerText || containerText.length < 20) continue;
            results.push({
              href: normalized,
              text: containerText,
              aria: entry.aria,
              role: entry.role,
            });
          }
          return results.slice(0, 8);
        }
        """
    )


def resolve_chrome_profile_path():
    explicit = os.getenv("FACEBOOK_CHROME_PROFILE")
    if explicit:
        path = Path(explicit).expanduser()
        if path.exists():
            return str(path)

    for base in [
        Path.home() / "Library/Application Support/Google/Chrome",
        Path.home() / "Library/Application Support/Chromium",
    ]:
        if not base.exists():
            continue
        for candidate in sorted(base.iterdir(), key=lambda item: item.name.lower()):
            if candidate.is_dir() and (candidate.name == "Default" or candidate.name.startswith("Profile")):
                return str(candidate)
    return None


def extract_live_posts_from_url(source, debug=False):
    url = (source.get("url") or "").strip()
    if not url:
        raise ValueError(f"Source {source.get('id')} does not have a verified URL.")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - environment may not have Playwright installed
        raise RuntimeError(f"Playwright is required for live Facebook collection: {exc}") from exc

    with sync_playwright() as playwright:
        profile_path = resolve_chrome_profile_path()
        try:
            if profile_path:
                context = playwright.chromium.launch_persistent_context(
                    profile_path,
                    channel="chrome",
                    headless=False,
                    viewport={"width": 1400, "height": 1800},
                )
                page = context.new_page()
            else:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1400, "height": 1800})
                context = None
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)

            candidates = discover_real_posts(page, url)
            if debug:
                print(f"{source.get('id')}: discovered candidates={len(candidates)}")
                for item in candidates[:5]:
                    print(f"  {item['href']}")
                    print(f"  preview={item['text'][:160]}")

            if not candidates:
                raise RuntimeError(f"No post permalink anchors were found in the live DOM for {source.get('name')}.")

            normalized_posts = []
            seen_urls = set()
            for item in candidates:
                post_url = normalize_post_url(item.get("href") or url)
                if not post_url or post_url in seen_urls:
                    continue
                seen_urls.add(post_url)

                post_text = extract_post_text(item.get("text") or "")
                if not post_text:
                    continue
                if post_url.rstrip("/") in {url.rstrip("/") for url in [source.get("url") or ""] if url}:
                    continue

                post_id_match = re.search(r"(?:pfbid|story_fbid|fbid|posts)/([^/?]+)", post_url)
                post_id = post_id_match.group(1) if post_id_match else stable_hash(post_url)
                published_at = None
                aria_text = (item.get("aria") or "").strip()
                if aria_text:
                    published_at = aria_text

                engagement_text = item.get("text") or ""
                reactions = parse_engagement_from_text(engagement_text)
                shares = parse_engagement_from_text(engagement_text)
                comments = parse_engagement_from_text(engagement_text)
                raw_post = {
                    "post_id": post_id,
                    "post_url": post_url,
                    "post_text": post_text,
                    "published_at": published_at,
                    "comments_count": comments,
                    "reactions_count": reactions,
                    "shares_count": shares,
                    "comments": [],
                }
                normalized_posts.append(normalize_post(raw_post, source))

            if not normalized_posts:
                raise RuntimeError(f"No valid live post bodies were extracted for {source.get('name')}.")

            return normalized_posts[:5]
        finally:
            if context is not None:
                context.close()
            elif 'browser' in locals():
                browser.close()


def load_group_watchlist():
    groups = load_json(GROUPS_PATH)
    return [group for group in groups if group.get("enabled") is True]


def save_access_audit(audit_results):
    save_json(ACCESS_AUDIT_PATH, audit_results)


def audit_source(source):
    url = (source.get("url") or "").strip()
    if not url:
        return {
            "source_id": source.get("id"),
            "name": source.get("name"),
            "status": "BLOCKED",
            "reason": "No URL provided",
        }

    if "facebook.com" not in url:
        return {
            "source_id": source.get("id"),
            "name": source.get("name"),
            "status": "BLOCKED",
            "reason": "URL is not a Facebook link",
        }

    return {
        "source_id": source.get("id"),
        "name": source.get("name"),
        "status": "PUBLIC_ACCESSIBLE",
        "reason": "URL is present and ready for public collection checks.",
    }




def collect_posts_for_source(source):
    source_id = source.get("id")
    live_posts = extract_live_posts_from_url(source)
    if not live_posts:
        raise RuntimeError(f"No live posts were collected for {source_id}.")

    filtered = [post for post in live_posts if str(post.get("source_id") or "") == str(source_id)]
    if not filtered:
        for post in live_posts:
            post["source_id"] = source_id
            post["source_name"] = source.get("name")
            post["source_type"] = "facebook_" + str(source.get("type", "group"))
            post["region"] = source.get("region")
        filtered = live_posts

    post_records = load_json(POSTS_PATH)
    if not isinstance(post_records, list):
        post_records = []

    source_records = [post for post in post_records if str(post.get("source_id") or "") != str(source_id)]
    source_records.extend(filtered)
    save_json(POSTS_PATH, source_records)
    return filtered[-1] if filtered else None


def run_access_audit():
    groups = load_json(GROUPS_PATH)
    results = []
    for source in groups:
        results.append(audit_source(source))
    save_access_audit(results)
    return results


def run_collection(source_id=None):
    groups = load_group_watchlist()
    if source_id:
        groups = [group for group in groups if group.get("id") == source_id]

    collected = []
    for source in groups:
        try:
            collected.append(collect_posts_for_source(source))
        except Exception as exc:  # pragma: no cover - keep collection robust
            collected.append({"source_id": source.get("id"), "error": str(exc)})
    return collected


def run_login_session():
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    print("Open a dedicated local Chrome/Firefox profile and sign into Facebook manually.")
    print(f"Session state is stored locally in: {SESSION_DIR}")
    print("Do not store credentials in Git or in source files.")


def main():
    parser = argparse.ArgumentParser(description="Simple Facebook evidence collector foundation")
    parser.add_argument("--audit", action="store_true", help="Audit access status for each watchlisted source")
    parser.add_argument("--source", help="Target a single enabled source by id")
    parser.add_argument("--login", action="store_true", help="Open a local browser profile for manual Facebook sign-in")
    args = parser.parse_args()

    if args.login:
        run_login_session()
        return

    if args.audit:
        results = run_access_audit()
        for item in results:
            print(f"{item['source_id']}: {item['status']} - {item['reason']}")
        return

    results = run_collection(source_id=args.source)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
