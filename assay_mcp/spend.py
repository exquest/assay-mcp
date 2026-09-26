"""The user's daily spending limit (Assay founder decision 2026-09-26: required, set by the user, surviving
restarts). Every payment is reserved in a local append-only file *before* it is signed, and closed afterwards as
settled or failed. A reservation whose outcome is unknown (the process died, the network dropped) keeps counting:
the limit errs toward spending less, never more."""
from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

try:
    import fcntl
    msvcrt = None
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt


class LimitReached(Exception):
    """This call would take today's spending past the user's limit; nothing was paid."""


class SpendFileDamaged(Exception):
    """A line of the spend file cannot be read, so today's spend is not known; nothing is paid until it is fixed."""


def _usd(v: Decimal) -> str:
    return f"{v:.2f}" if v == v.quantize(Decimal("0.01")) else f"{v.normalize():f}"


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class SpendBook:
    def __init__(self, path: str | Path, daily_limit_usd: Decimal, today=None) -> None:
        self.path = Path(path).expanduser()
        self.limit = Decimal(daily_limit_usd)
        if not self.limit.is_finite() or self.limit <= 0:
            raise ValueError("the daily limit must be a positive amount in USD")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._lockfile = self.path.with_name(self.path.name + ".lock")
        self._today = today or _today  # injectable for tests of the UTC rollover

    def _lines(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for n, raw in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                out.append(json.loads(raw))
            except ValueError:
                # a torn write may have been a reservation: skipping it could under-count today's spend
                raise SpendFileDamaged(f"line {n} of {self.path} cannot be read (a write was interrupted?); nothing "
                                       "is paid until it is fixed or removed") from None
        return out

    @contextmanager
    def _exclusive(self):
        """One reserver at a time across every process sharing this spend file, not only threads."""
        with self._lock, open(self._lockfile, "a+") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover - Windows
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

    def _append(self, rec: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def spent_today(self) -> Decimal:
        """Every payment that is not known to have failed counts against the UTC day it was reserved on, and also
        against the day it settled on if that is later: a payment reserved before midnight and settled after it
        cannot escape the new day's limit (review round 2). The rare double count errs toward spending less."""
        today = self._today()
        reserved: dict[str, tuple[str, Decimal]] = {}
        closed: dict[str, tuple[str, str]] = {}
        for r in self._lines():
            if r.get("state") == "reserved":
                reserved[r["id"]] = (r.get("day"), Decimal(r["amount_usd"]))
            elif r.get("state") in ("settled", "failed"):
                closed[r["id"]] = (r["state"], r.get("day"))
        total = Decimal(0)
        for rid, (day, amount) in reserved.items():
            state, closed_day = closed.get(rid, ("open", None))
            if state == "failed":
                continue
            if day == today or (state == "settled" and closed_day == today):
                total += amount
        return total

    def spent_today_locked(self) -> Decimal:
        with self._exclusive():
            return self.spent_today()

    def reserve(self, route: str, amount_usd: Decimal) -> str:
        with self._exclusive():
            spent = self.spent_today()
            if spent + Decimal(amount_usd) > self.limit:
                raise LimitReached(f"this call costs ${_usd(Decimal(amount_usd))}; ${_usd(spent)} of your "
                                   f"${_usd(self.limit)} daily limit is already spent today (UTC); nothing was paid")
            rid = str(uuid.uuid4())
            self._append({"id": rid, "day": self._today(), "at": datetime.now(timezone.utc).isoformat(), "route": route,
                          "amount_usd": str(amount_usd), "state": "reserved"})
            return rid

    def close(self, rid: str, state: str, tx: str | None = None) -> None:
        assert state in ("settled", "failed")
        with self._exclusive():
            self._append({"id": rid, "day": self._today(), "at": datetime.now(timezone.utc).isoformat(),
                          "state": state, "tx": tx})
