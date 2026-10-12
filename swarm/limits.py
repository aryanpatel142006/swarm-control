"""Account and model limits on this laptop, and the fallbacks around them.

Oct 11 2026 (HackRU): Opus hit its weekly limit mid-run and three sub-agents died at once; the second Claude account
answered "You've hit your session limit · resets 3:45pm" and its jobs had to be restarted by hand after the reset.
Agents' cooldowns already live on the board (their Agents row); this store adds what the board cannot show:
`swarm dispatch` accounts (which are not board agents) and per-model limits (an account that is out of Opus can still
run Sonnet). `swarm status` prints every active entry as "<account>[/<model>] limited until HH:MM".

The file is ~/.swarm/<project>/limits.json: one laptop's logins, written by the runner and by dispatch jobs.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

MODEL_FAMILIES = ("fable", "opus", "sonnet", "haiku")
# "Opus weekly limit reached ∙ resets Oct 14, 9am", "You've reached your Opus limit", "Sonnet 5-hour limit"
_MODEL_LIMIT_RE = re.compile(
    rf"\b({'|'.join(MODEL_FAMILIES)})\b[^\n]{{0,40}}\blimit|\blimit\b[^\n]{{0,25}}\b({'|'.join(MODEL_FAMILIES)})\b", re.I)
DEFAULT_MODEL_LIMIT_MIN = 300     # no reset time given for a model limit: try the model again after 5 h


def default_limits_path(project: str) -> Path:
    return Path.home() / ".swarm" / project / "limits.json"


def model_family(model: str) -> str:
    """'opus' for 'claude-opus-5-5' or 'opus'; any other id is its own family."""
    low = (model or "").lower()
    for fam in MODEL_FAMILIES:
        if re.search(rf"(?<![a-z]){fam}(?![a-z])", low):
            return fam
    return low


def limited_model(text: str) -> str:
    """The model family a limit message names ("Opus weekly limit reached"), or "" for an account-wide limit."""
    m = _MODEL_LIMIT_RE.search(text or "")
    return (m.group(1) or m.group(2)).lower() if m else ""


def fallback_chain(model: str, fallbacks: dict[str, str]) -> list[str]:
    """[model, its fallback, that one's fallback, …]; keys match the exact id or its family; no cycles."""
    chain = [model]
    cur = model
    while True:
        nxt = fallbacks.get(cur) or fallbacks.get(model_family(cur))
        if not nxt or nxt in chain or model_family(nxt) in {model_family(c) for c in chain}:
            return chain
        chain.append(nxt)
        cur = nxt


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


class LimitStore:
    """{"<account>|<model family or empty>": {"account", "model", "until", "reason", "at"}} in one JSON file, updated
    under an flock so a runner and a dispatch job can write at the same time."""

    def __init__(self, path: Path | str, now: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _update(self, fn) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = fn(self._load())
            tmp = self.path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
            tmp.replace(self.path)

    @staticmethod
    def _key(account: str, model: str = "") -> str:
        return f"{account}|{model_family(model) if model else ''}"

    def mark(self, account: str, until: datetime | None, reason: str, *, model: str = "") -> datetime:
        """Record a limit; with no reset time, 60 min for an account and 5 h for a model. Returns the until-time."""
        now = self.now()
        until = until if until and until > now else now + timedelta(
            minutes=DEFAULT_MODEL_LIMIT_MIN if model else 60)
        entry = {"account": account, "model": model_family(model) if model else "", "until": _iso(until),
                 "reason": (reason or "").strip().splitlines()[0][:200] if reason else "", "at": _iso(now)}
        self._update(lambda d: {**d, self._key(account, model): entry})
        return until

    def clear(self, account: str, model: str = "") -> None:
        self._update(lambda d: {k: v for k, v in d.items() if k != self._key(account, model)})

    def active(self, now: datetime | None = None) -> list[dict]:
        now = now or self.now()
        out = []
        for e in self._load().values():
            until = _parse(e.get("until")) if isinstance(e, dict) else None
            if until and until > now:
                out.append({**e, "until_dt": until})
        return sorted(out, key=lambda e: (e["until_dt"], e.get("account", ""), e.get("model", "")))

    def until(self, account: str, model: str = "", now: datetime | None = None) -> datetime | None:
        """When `account` can run `model` again: an account-wide limit covers every model. None = usable now."""
        hits = [e["until_dt"] for e in self.active(now) if e.get("account") == account
                and (not e.get("model") or (model and e.get("model") == model_family(model)))]
        return max(hits) if hits else None


def pick_model(account: str, model: str, fallbacks: dict[str, str], store: LimitStore,
               now: datetime | None = None) -> tuple[str, list[str]]:
    """The first model on the fallback chain that `account` is not limited on: (model, skipped). When every model is
    limited, the original is returned (the run will say why)."""
    chain = fallback_chain(model, fallbacks)
    skipped = []
    for m in chain:
        if store.until(account, m, now) is None:
            return m, skipped
        skipped.append(m)
    return model, []


def _when(t: datetime, now: datetime) -> str:
    return t.strftime("%H:%M UTC") if t - now < timedelta(hours=20) else t.strftime("%b %d %H:%M UTC")


def status_line(store: LimitStore, now: datetime | None = None) -> str:
    """'Limits: pro limited until 15:30 UTC (session limit) · claude-a/opus limited until Oct 14 14:00 UTC (…)'."""
    now = now or store.now()
    bits = []
    for e in store.active(now):
        who = e.get("account", "?") + (f"/{e['model']}" if e.get("model") else "")
        reason = f" ({e['reason'][:60]})" if e.get("reason") else ""
        bits.append(f"{who} limited until {_when(e['until_dt'], now)}{reason}")
    return ("Limits: " + " · ".join(bits)) if bits else ""
