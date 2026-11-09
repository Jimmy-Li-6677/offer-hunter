"""Activity log of the scheduled job search: what ran, for how long, what it found, what was applied to, and what it cost.

Events are appended as JSON lines to ONE FILE PER SCHEDULE SESSION in ../private/activity-logs/ (<start time>.jsonl, with
a readable <start time>.md report next to it, refreshed after every event). Applications logged while no schedule runs go to
outside-schedule.jsonl / .md. An older single activity-log.jsonl is split into session files the first time it is found.
Also: `python jobboard.py log [--all]`, the board's Activity log button.

Event types (written by jobboard.py / applog.py):
  schedule_start  the Schedule button was started (its settings)
  search          one scheduled search: button, seconds, status, new jobs that met the search filters, note
  round           one scheduled round finished: seconds, jobs sent to the apply queue / to AI analysis (with scores)
  schedule_end    the schedule finished or was stopped
  apply           a job was logged by applog.py add (Test / Submitted / Needs User / ...): when, how long, estimated cost

Estimated cost of an application = the Claude tokens used between the moment the job was marked "applying" and the moment
it was logged, summed over this project's Claude Code transcripts (the main session AND its subagents), priced per model.
It includes anything else the session did in that window, so it is an upper bound. Prices ($ per million tokens, from the
Claude API price list): input / output / cache read as below; cache writes cost 1.25x input (5 minute cache) or 2x input
(1 hour cache, assumed). Models not in the table are priced like Sonnet 5.5 and marked with ~.
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path

import applog  # same folder: paths + log entry matching

HERE = Path(__file__).resolve().parent
DIR = applog.PRIVATE / "activity-logs"
LEGACY = applog.PRIVATE / "activity-log.jsonl"
OUTSIDE = "outside-schedule"
MD = DIR  # the board shows this folder
BOARD = applog.PRIVATE / "job-board.json"

# $ per million tokens: (input, output, cache read) - longest key that appears in the model id wins
PRICES = {
    "sonnet-5-5": (2.0, 10.0, 0.20), "sonnet-5": (2.0, 10.0, 0.20), "opus-5-5": (4.0, 20.0, 0.20), "opus-5": (5.0, 25.0, 0.50),
    "haiku-5-5": (0.10, 0.50, 0.01), "fable": (10.0, 50.0, 0.25), "mythos": (10.0, 50.0, 0.25),
    "opus-4": (5.0, 25.0, 0.50), "sonnet-4": (3.0, 15.0, 0.30), "haiku-4": (1.0, 5.0, 0.10),
}
FALLBACK = (2.0, 10.0, 0.20)  # Sonnet 5.5
APPLY_STATUSES = ("Test", "Submitted", "Needs User", "Needs approval", "Skipped", "Pending", "Candidate")
BUTTONS = {"company": "Search company sites", "simplify": "Search SimplifyJobs", "simplifysite": "Search Simplify website"}


def now():
    return dt.datetime.now(dt.timezone.utc)


def parse(ts):
    d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def session_name(ts):
    """File name stem of the session that started at ts (local time)."""
    return parse(ts).astimezone().strftime("%Y-%m-%d_%H%M%S")


def _read(path):
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _write(name, evs):
    with (DIR / f"{name}.jsonl").open("w", encoding="utf-8") as f:
        for e in evs:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _stem_of(ev, open_session):
    if ev.get("type") == "schedule_start":
        return session_name(ev["ts"])
    if ev.get("schedule"):
        return session_name(ev["schedule"])
    return open_session or OUTSIDE


def migrate():
    """Split the old single activity-log.jsonl into one file per session (once)."""
    if not LEGACY.exists():
        return
    DIR.mkdir(parents=True, exist_ok=True)
    groups, open_session = {}, None
    for ev in _read(LEGACY):
        if ev.get("type") == "schedule_start":
            open_session = session_name(ev["ts"])
        stem = _stem_of(ev, open_session)
        groups.setdefault(stem, []).append(ev)
        if ev.get("type") == "schedule_end" and ev.get("schedule") and session_name(ev["schedule"]) == open_session:
            open_session = None
    for stem, evs in groups.items():
        _write(stem, _read(DIR / f"{stem}.jsonl") + evs)
        (DIR / f"{stem}.md").write_text(render(evs=evs), encoding="utf-8")
    LEGACY.rename(LEGACY.with_suffix(".jsonl.migrated"))


def session_files():
    return sorted(DIR.glob("*.jsonl")) if DIR.exists() else []


def open_session_stem():
    """The session that started last and has not ended (apply events belong to it), or None."""
    evs = events()
    starts = [e for e in evs if e["type"] == "schedule_start"]
    if not starts:
        return None
    last = starts[-1]
    ended = any(e["type"] == "schedule_end" and e.get("schedule") == last["ts"] for e in evs)
    return None if ended else session_name(last["ts"])


def log(event):
    """Append an event (adds ts) to its session's file and refresh that session's report. Never raises."""
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        migrate()
        ev = {"ts": now().isoformat(timespec="seconds"), **event}
        stem = _stem_of(ev, open_session_stem() if ev.get("type") == "apply" else None)
        path = DIR / f"{stem}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        (DIR / f"{stem}.md").write_text(render(evs=_read(path)), encoding="utf-8")
    except Exception as e:  # logging must never break a search or an application
        print(f"activity log: {type(e).__name__}: {e}", file=sys.stderr)


def events():
    """Every event of every session, oldest first."""
    try:
        migrate()
    except Exception:
        pass
    out = []
    for f in session_files():
        out += _read(f)
    out.sort(key=lambda e: e.get("ts", ""))
    return out


# ---------- cost of an application window ----------
def project_dir():
    """Where Claude Code keeps this folder's session transcripts (the folder path with non-alphanumerics as '-')."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(HERE))


def _price(model):
    best = None
    for key, p in PRICES.items():
        if key in (model or "") and (best is None or len(key) > len(best[0])):
            best = (key, p)
    return (best[1], False) if best else (FALLBACK, True)


def window_cost(start, end):
    """{'usd', 'calls', 'tokens': {...}, 'assumed': bool} for all model calls between start and end (datetimes)."""
    tot = {"usd": 0.0, "calls": 0, "read": 0, "write": 0, "fresh": 0, "out": 0, "assumed": False}
    folder = project_dir()
    if not folder.exists():
        return None
    for f in folder.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < start.timestamp() - 60:
                continue  # not touched since the window began
            seen = {}
            with f.open(encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh):
                    if '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    m, ts = rec.get("message") or {}, rec.get("timestamp")
                    u = m.get("usage")
                    if not isinstance(u, dict) or not ts:
                        continue
                    t = parse(ts)
                    if start <= t <= end:
                        seen[m.get("id") or f"{f.name}:{n}"] = (m.get("model") or "", u)  # last line of a message wins
        except OSError:
            continue
        for model, u in seen.values():
            (pin, pout, pread), assumed = _price(model)
            cc = u.get("cache_creation") or {}
            w1 = cc.get("ephemeral_1h_input_tokens", u.get("cache_creation_input_tokens", 0) if not cc else 0)
            w5 = cc.get("ephemeral_5m_input_tokens", 0)
            fresh, read, out = u.get("input_tokens", 0), u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0)
            tot["usd"] += (fresh * pin + w5 * 1.25 * pin + w1 * 2.0 * pin + read * pread + out * pout) / 1e6
            tot["calls"] += 1
            tot["fresh"] += fresh
            tot["write"] += w1 + w5
            tot["read"] += read
            tot["out"] += out
            tot["assumed"] = tot["assumed"] or assumed
    return tot


def _job_start(entry):
    """When the board marked this job 'applying' (jobboard.py applying <key>), or None."""
    try:
        board = json.loads(BOARD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for j in board.get("jobs", {}).values():
        if j.get("applyingAt") and applog.matches(entry, j.get("company", ""), j.get("title"), j.get("id") or None):
            return parse(j["applyingAt"])
    return None


def log_apply(entry):
    """Called by applog.py add for a job: record when it was logged, how long the application took and what it cost."""
    status = entry.get("status")
    if status not in APPLY_STATUSES:
        return
    end = now()
    start = _job_start(entry)
    ev = {"type": "apply", "company": entry.get("company"), "role": entry.get("role"), "status": status,
          "url": entry.get("url", ""), "notes": (entry.get("notes") or "")[:160]}
    if start and start < end:
        ev["started"] = start.isoformat(timespec="seconds")
        ev["seconds"] = round((end - start).total_seconds())
        cost = window_cost(start, end)
        if cost:
            ev.update(usd=round(cost["usd"], 4), calls=cost["calls"], assumed=cost["assumed"],
                      tokens={"cache_read": cost["read"], "cache_write": cost["write"], "fresh": cost["fresh"], "output": cost["out"]})
    log(ev)


# ---------- the readable report ----------
def local(ts, fmt="%b %d %H:%M:%S"):
    return parse(ts).astimezone().strftime(fmt)


def dur(sec):
    sec = int(sec or 0)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s" if sec >= 60 else f"{sec}s"


def usd(v):
    return "-" if v is None else f"${v:,.2f}"


def _apply_table(rows):
    if not rows:
        return ["_No applications logged in this period._", ""]
    out = ["| Time | Job | Result | Took | Model calls | Est. cost |", "|---|---|---|---|---|---|"]
    for e in rows:
        cost = usd(e.get("usd")) + ("~" if e.get("assumed") else "") if e.get("usd") is not None else "n/a"
        out.append(f"| {local(e['ts'])} | {e.get('company')} - {e.get('role')} | {e.get('status')} | "
                   f"{dur(e['seconds']) if e.get('seconds') is not None else 'n/a'} | {e.get('calls', '-')} | {cost} |")
    total = sum(e.get("usd") or 0 for e in rows)
    out += ["", f"**Applications: {len(rows)} | estimated token cost: {usd(total)}**", ""]
    return out


def render(show_all=True, evs=None):
    evs = events() if evs is None else evs
    lines = ["# Scheduled job search - activity log", "",
             "Times are local. Cost = Claude tokens between the job being marked 'applying' and being logged, priced per model "
             "(upper bound: includes anything else the session did meanwhile; `~` = model price assumed).", ""]
    starts = [e for e in evs if e["type"] == "schedule_start"]
    if not starts:
        lines += ["_No scheduled run has been logged yet._", ""]
    ends = {e.get("schedule"): e for e in evs if e["type"] == "schedule_end"}
    claimed = set()
    for st in (starts if show_all else starts[-1:]):
        sid = st["ts"]
        end = ends.get(sid)
        lines += [f"## Schedule started {local(sid)}" + (f" - ended {local(end['ts'])} ({end.get('reason', '')})" if end else " - running"),
                  f"Searches: {', '.join(BUTTONS.get(s, s) for s in st.get('sources', []))} | every {st.get('intervalMinutes', 0):g} min "
                  f"for {st.get('hours', 0):g} h | auto apply at {st.get('minScore', 0):g}%+ match (similar skills counted) | "
                  f"posted within {st.get('maxAgeHours', 0):g} h | AI analysis: {'on' if st.get('analyze') else 'off'}", ""]
        mine = [e for e in evs if e.get("schedule") == sid]
        for rnd in sorted({e.get("round") for e in mine if e.get("round")}):
            r = next((e for e in mine if e["type"] == "round" and e.get("round") == rnd), None)
            lines.append(f"### Round {rnd}" + (f" - {local(r['ts'])}, {dur(r.get('seconds'))}" if r else " - running"))
            lines += ["| Search button | Took | Status | New jobs that met your filters | Note |", "|---|---|---|---|---|"]
            for e in mine:
                if e["type"] == "search" and e.get("round") == rnd:
                    lines.append(f"| {e.get('button')} | {dur(e.get('seconds'))} | {e.get('status')} | {e.get('found', 0)} | {(e.get('note') or '')[:140]} |")
            if r:
                what = "sent to AI analysis" if r.get("mode") == "analysis" else "put in the apply queue"
                picked = r.get("jobs") or []
                lines += ["", f"Score filter (>= {st.get('minScore', 0):g}% with similar skills): **{len(picked)} {what}**" +
                          ("".join(f"\n- {j}" for j in picked) if picked else ""), ""]
            else:
                lines.append("")
        t0 = parse(sid)
        t1 = parse(end["ts"]) if end else now() + dt.timedelta(minutes=1)
        rows = [e for e in evs if e["type"] == "apply" and t0 <= parse(e["ts"]) <= t1]
        claimed |= {id(e) for e in rows}
        lines += ["### Applications during this schedule"] + _apply_table(rows)
    other = [e for e in evs if e["type"] == "apply" and id(e) not in claimed]
    if other and show_all:
        lines += ["## Applications outside a scheduled run"] + _apply_table(other)
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    print(render(show_all="--all" in sys.argv))
