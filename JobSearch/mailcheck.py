"""Verification emails / codes from job sites, read from the job-application inbox - READ-ONLY, no AI tokens.

When a job site sends "verify your email" or a one-time code, Claude runs `wait` in the background, keeps working on
the next queued job, and when the email arrives opens its link (or types its code) in the application and carries
on - no waiting on the user.

How it reads mail: Microsoft Graph (the official Outlook.com / Microsoft 365 mail API) with the delegated Mail.Read
permission only - it can read messages, it cannot send, move or delete anything. You sign in once (`login`); the
sign-in is saved in ..\\private\\mail-token.json (gitignored) and renewed automatically.

One-time setup (about 5 minutes):
  1. Register a free app: https://entra.microsoft.com -> Applications -> App registrations -> New registration.
     Name: offer-hunter mail. Supported account types: "Personal Microsoft accounts only" (outlook.com / hotmail)
     or "Accounts in any organizational directory and personal Microsoft accounts". Redirect URI: platform
     "Public client/native (mobile & desktop)", value http://localhost  -> Register.
  2. API permissions -> Add a permission -> Microsoft Graph -> Delegated -> Mail.Read -> Add.
  3. Copy the "Application (client) ID" into ..\\private\\.env as MAIL_CLIENT_ID=<id>.
  4. python mailcheck.py login   (a browser window opens - sign in to the job-application mailbox once)

  python mailcheck.py login
  python mailcheck.py latest [--match pnc,myworkday] [--minutes 30]     newest matching mail (link / code)
  python mailcheck.py wait --match pnc,myworkday [--since <ISO time>] [--every 120] [--timeout 1200]
        checks every --every seconds (default 120 = 2 minutes) until a matching mail newer than --since arrives;
        prints one JSON line {"from", "subject", "received", "link", "code"} and exits 0, or exits 1 on timeout.
"""
import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request

import applog  # same folder: ..\private paths + .env reader

TOKEN = applog.PRIVATE / "mail-token.json"
SCOPES = ["Mail.Read"]
GRAPH = "https://graph.microsoft.com/v1.0"
UTC = dt.timezone.utc
# links that do the verifying (not the "privacy policy" / "unsubscribe" links in the same mail)
VERIFY_LINK = re.compile(r"verif|confirm|activat|validat|magic|sign[-_ ]?in|login|token|auth|account", re.I)
SKIP_LINK = re.compile(r"unsubscribe|privacy|terms|preferences|help|support|facebook|twitter|linkedin\.com/company|"
                       r"\.png|\.jpg|\.gif|mailto:", re.I)
CODE_WORD = re.compile(r"\b(?:code|passcode|pin|one[- ]time|verification|security|otp)\b", re.I)
CODE_TOKEN = re.compile(r"\b(?=[A-Z0-9]*\d)[A-Z0-9]{4,8}\b", re.I)  # 4-8 letters / digits with at least one digit


def _env(name):
    env = {k: v for k, (v, _q) in applog.read_env(applog.ENV).items()} if applog.ENV.exists() else {}
    return (env.get(name) or "").strip()


def _app():
    import msal  # pip install msal
    client_id = _env("MAIL_CLIENT_ID")
    if not client_id:
        sys.exit("MAIL_CLIENT_ID is empty in private/.env - see the one-time setup at the top of mailcheck.py")
    cache = msal.SerializableTokenCache()
    if TOKEN.exists():
        cache.deserialize(TOKEN.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(client_id, authority="https://login.microsoftonline.com/common",
                                       token_cache=cache)
    return app, cache


def _save(cache):
    if cache.has_state_changed:
        TOKEN.write_text(cache.serialize(), encoding="utf-8")


def login():
    app, cache = _app()
    result = app.acquire_token_interactive(SCOPES, prompt="select_account")
    _save(cache)
    if "access_token" not in result:
        sys.exit(f"sign-in failed: {result.get('error_description') or result}")
    print(f"signed in as {(result.get('id_token_claims') or {}).get('preferred_username', '?')} - read-only mail access saved")


def token():
    app, cache = _app()
    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    _save(cache)
    if not result or "access_token" not in result:
        sys.exit("not signed in (or the sign-in expired) - run: python mailcheck.py login")
    return result["access_token"]


def _get(url, tok):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json",
                                               "Prefer": 'outlook.body-content-type="html"'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def messages(since, tok):
    """Messages received after `since` in the inbox and the junk folder, newest first."""
    stamp = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urllib.parse.urlencode({"$filter": f"receivedDateTime ge {stamp}", "$orderby": "receivedDateTime desc",
                                    "$select": "subject,from,receivedDateTime,body,bodyPreview", "$top": "25"})
    out = []
    for folder in ("inbox", "junkemail"):
        try:
            out += _get(f"{GRAPH}/me/mailFolders/{folder}/messages?{query}", tok).get("value", [])
        except OSError:
            continue
    return sorted(out, key=lambda m: m.get("receivedDateTime", ""), reverse=True)


def _matches(m, words):
    sender = (m.get("from") or {}).get("emailAddress") or {}
    hay = " ".join([sender.get("address", ""), sender.get("name", ""), m.get("subject", ""),
                    m.get("bodyPreview", "")]).lower()
    return not words or any(w.lower() in hay for w in words)


def extract(m):
    """{'link', 'code'} from a verification mail: the verifying link (first one whose address or text says verify /
    confirm / activate / sign in) and a one-time code (4-8 letters / digits next to 'code')."""
    body = (m.get("body") or {}).get("content") or ""
    links = []
    for href, text in re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', body, re.I | re.S):
        href = html.unescape(href)
        label = re.sub(r"<[^>]+>", " ", text)
        if href.startswith("http") and not SKIP_LINK.search(href):
            links.append((bool(VERIFY_LINK.search(href) or VERIFY_LINK.search(label)), href))
    if not links:  # plain-text mail
        links = [(bool(VERIFY_LINK.search(u)), u) for u in re.findall(r"https?://[^\s<>\"']+", body)
                 if not SKIP_LINK.search(u)]
    link = next((u for good, u in links if good), "")
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"\s+", " ", text)
    code = ""
    for w in CODE_WORD.finditer(text):  # the first code-like word within ~80 characters after "code" / "verification"
        for c in CODE_TOKEN.findall(text[w.end():w.end() + 80]):
            if not re.fullmatch(r"(19|20)\d\d", c):  # not a year
                code = c
                break
        if code:
            break
    return {"link": link, "code": code}


def _result(m):
    sender = (m.get("from") or {}).get("emailAddress") or {}
    return {"from": sender.get("address", ""), "subject": m.get("subject", ""),
            "received": m.get("receivedDateTime", ""), **extract(m)}


def latest(words, minutes):
    tok = token()
    since = dt.datetime.now(UTC) - dt.timedelta(minutes=minutes)
    found = [m for m in messages(since, tok) if _matches(m, words)]
    return _result(found[0]) if found else None


def wait(words, since, every, timeout):
    """Check every `every` seconds until a matching mail newer than `since` arrives (or `timeout` seconds pass)."""
    end = time.time() + timeout
    while True:
        tok = token()
        found = [m for m in messages(since, tok) if _matches(m, words)]
        if found:
            return _result(found[0])
        if time.time() + every > end:
            return None
        time.sleep(every)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["login", "latest", "wait"])
    ap.add_argument("--match", default="", help="comma-separated words: sender / subject must contain one (e.g. pnc,myworkday)")
    ap.add_argument("--minutes", type=int, default=30, help="latest: how far back to look")
    ap.add_argument("--since", help="wait: only mail received after this ISO time (default: 2 minutes ago)")
    ap.add_argument("--every", type=int, default=120, help="wait: seconds between checks (default 120)")
    ap.add_argument("--timeout", type=int, default=1200, help="wait: give up after this many seconds (default 1200)")
    a = ap.parse_args()
    words = [w.strip() for w in a.match.split(",") if w.strip()]
    if a.cmd == "login":
        return login()
    if a.cmd == "latest":
        r = latest(words, a.minutes)
    else:
        since = dt.datetime.fromisoformat(a.since) if a.since else dt.datetime.now(UTC) - dt.timedelta(minutes=2)
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        r = wait(words, since, a.every, a.timeout)
    print(json.dumps(r) if r else json.dumps({"found": False}))
    sys.exit(0 if r else 1)


if __name__ == "__main__":
    main()
