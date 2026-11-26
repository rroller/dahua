"""How many requests each device has been sent, and what they asked for.

**Why this exists.** #603: two cameras stopped accepting logins, their owner
counted the Login and Logout lines in the cameras' own logs, and the count was
six and a half times what it had been ten weeks earlier. The camera's log has
no URLs and nothing here recorded what had been asked, so the only way to find
out which reads those were was to ask him for his version, his settings and his
capabilities and then work it out by hand. This is the missing half: the
diagnostics file now says what each device was asked, by name, over the last
ten minutes and the last hour.

**What is counted** is a request this integration sent, at the point it is
sent: after the read cache, which is the whole point, since a read answered
from the cache costs the device nothing. One count per request, however many
round trips the digest exchange took to make it, so a device that logs a login
per connection may show more lines than this shows requests. The two should
move together, and it is the names that are the answer.

**What is kept** is a name and a number. Never a value: a write is recorded as
the table it wrote to, and a URL's query is read only for `action` and `name`.
A request's name therefore carries nothing a device was told and nothing it
answered.

Held per device for the last hour in whole-minute buckets, so it is small and
bounded whatever the request rate. Not persisted: it describes this run.
"""

import time
from collections import Counter

WINDOW_MINUTES = 60
SHORT_WINDOW_MINUTES = 10

# device -> {"since": first minute seen, "total": int, "minutes": {minute: Counter}}
_TALLY: dict = {}


def _table(text: str) -> str:
    """`Lighting[0][1].Mode` -> `Lighting`."""
    for stop in ("[", "."):
        text = text.split(stop, 1)[0]
    return text


def request_name(url: str) -> str:
    """What a CGI URL asks for, without anything it carries.

    `/cgi-bin/configManager.cgi?action=getConfig&name=Lighting[0][1]`
        -> `configManager getConfig Lighting`
    `/cgi-bin/configManager.cgi?action=setConfig&ChannelTitle[0].Name=Porch`
        -> `configManager setConfig ChannelTitle`
    `/cgi-bin/coaxialControlIO.cgi?action=getStatus&channel=1`
        -> `coaxialControlIO getStatus`
    `/cgi-bin/snapshot.cgi?channel=1` -> `snapshot`

    Anything that does not look like one is `other`, so a name can never be a
    raw URL.
    """
    try:
        path, _, query = str(url).partition("?")
        endpoint = path.rstrip("/").rsplit("/", 1)[-1]
        if endpoint.endswith(".cgi"):
            endpoint = endpoint[: -len(".cgi")]
        if not endpoint or not endpoint.replace("_", "").isalnum():
            return "other"
        action = None
        tables = []
        written = None
        for pair in query.split("&") if query else ():
            key, _, value = pair.partition("=")
            if key == "action" and action is None:
                action = value
            elif key == "name":
                tables.append(_table(value))
            elif written is None and key not in ("channel", "httptype", "heartbeat"):
                written = _table(key)
        if action is None:
            return endpoint
        if not action.isalnum():
            return "%s other" % endpoint
        parts = [endpoint, action]
        if action.startswith("get"):
            parts.extend(table for table in tables if table.isalnum())
        elif action == "setConfig" and written and written.isalnum():
            parts.append(written)
        return " ".join(parts)
    except Exception:  # pylint: disable=broad-except
        # Counting must never be the reason a request was not made.
        return "other"


def rpc2_request_name(method, params=None) -> str:
    """`configManager.getConfig` with `{"name": "Encode"}` becomes
    `rpc2 configManager.getConfig Encode`; any other method is its own name."""
    name = "rpc2 %s" % (method if isinstance(method, str) else "other")
    # Only for the config manager, where `name` is a table. Elsewhere it can
    # be something a person typed, and no such thing is kept here.
    if (
        name.startswith("rpc2 configManager.")
        and isinstance(params, dict)
        and isinstance(params.get("name"), str)
    ):
        table = _table(params["name"])
        if table.isalnum():
            name += " " + table
    return name


def note(device, name: str, now=None) -> None:
    """Count one request to `device`. Nothing at all for a device with no key."""
    if device is None:
        return
    minute = int((time.time() if now is None else now) // 60)
    tally = _TALLY.get(device)
    if tally is None:
        tally = _TALLY[device] = {"since": minute, "total": 0, "minutes": {}}
    tally["total"] += 1
    bucket = tally["minutes"].get(minute)
    if bucket is None:
        bucket = tally["minutes"][minute] = Counter()
        for old in [m for m in tally["minutes"] if m <= minute - WINDOW_MINUTES]:
            del tally["minutes"][old]
    bucket[name] += 1


def _window(tally: dict, minute: int, minutes: int) -> dict:
    counted = Counter()
    for when, bucket in tally["minutes"].items():
        if minute - minutes < when <= minute:
            counted.update(bucket)
    # How much of the window there has actually been something to count in.
    covered = max(1, min(minutes, minute - tally["since"] + 1))
    total = sum(counted.values())
    return {
        "minutes_covered": covered,
        "total": total,
        "per_minute": round(total / covered, 2),
        "by_request": dict(
            sorted(counted.items(), key=lambda item: (-item[1], item[0]))
        ),
    }


def summary(device, now=None) -> dict:
    """What `device` has been asked, for the diagnostics file.

    Minutes are whole clock minutes and the current one is included, so the
    newest minute of each window is partial.
    """
    tally = _TALLY.get(device)
    if tally is None:
        return {"tracked": False}
    minute = int((time.time() if now is None else now) // 60)
    return {
        "tracked": True,
        "tracked_for_minutes": minute - tally["since"] + 1,
        "total_since_start": tally["total"],
        "last_%d_minutes"
        % SHORT_WINDOW_MINUTES: _window(tally, minute, SHORT_WINDOW_MINUTES),
        "last_%d_minutes" % WINDOW_MINUTES: _window(tally, minute, WINDOW_MINUTES),
    }


def clear() -> None:
    _TALLY.clear()
