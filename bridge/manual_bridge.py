# Copyright (c) 2026 Andrew Thomas (OPTD, onepersontradedesk.com)
# MIT License - see LICENSE, which also carries the risk disclosure.
# This code places real orders on a real account. You are responsible
# for every order it sends.
"""TopstepX manual-trade mirror bridge.

Tails the JSONL command files written by the Manual_Mirror.cpp Sierra Chart
study and translates each command into a TopstepX REST call. One-way: Sierra
Chart is the brain, TopstepX is the shadow. Nothing here feeds back into
Sierra Chart.

Command protocol (one JSON object per line):
  place_market:
    {"cmd":"place_market","tag":"manual-9421-v1","sc_id":"9421","version":1,
     "contract":"MNQ","side":"long","size":2}
  place_limit:
    {"cmd":"place_limit","tag":"manual-9422-v1","sc_id":"9422","version":1,
     "contract":"MNQ","side":"long","size":1,"price":21250.50}
  place_stop:
    {"cmd":"place_stop","tag":"manual-9423-v1","sc_id":"9423","version":1,
     "contract":"MNQ","side":"short","size":1,"stop_price":21300.00}
  cancel:
    {"cmd":"cancel","tag":"manual-9422-cancel","sc_id":"9422","contract":"MNQ"}
  cancel_replace:
    {"cmd":"cancel_replace","tag":"manual-9422-v2","sc_id":"9422","version":2,
     "contract":"MNQ","new_type":"limit","side":"long","size":1,"price":21251.00}
  close_if_open (a mirrored stop/limit filled on Sierra Chart; see handler):
    {"cmd":"close_if_open","tag":"manual-9423-v1-close","sc_id":"9423","version":1,
     "contract":"MNQ","side":"short","size":1}

State files written by the bridge (all under paths.state):
  cursor.json          outbox byte offset per file, plus "_last_file" for the
                       UTC-midnight rollover drain
  seen_tags.json       dedupe set (local tags already processed; tags are
                       never sent to TopstepX)
  sc_to_tsx.json       {"sc_id": {"tsx_order_id": 12345, "version": 2}}
                       keyed by Sierra Chart InternalOrderID, updated on every
                       place_* / cancel_replace, cleared on cancel.

Run (from the bridge/ folder). --config and --env default to the files
sitting next to this script, so normally there is nothing to pass:
  python manual_bridge.py --doctor         check the whole setup, then exit
  python manual_bridge.py --list-accounts  print account ids, then exit
  python manual_bridge.py --dry-run        full pipeline, no API calls
  python manual_bridge.py                  live

On Windows you can also just double-click start-bridge.bat in the repo root;
it runs --doctor first and refuses to start if anything is wrong.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

import yaml

# tsx_client.py sits next to this file. Make the import work no matter which
# working directory the bridge is launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tsx_client import (  # noqa: E402
    Credentials,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    ORDER_TYPE_STOP,
    SIDE_ASK,
    SIDE_BID,
    TSXClient,
    TSXError,
)


log = logging.getLogger("tsx.manual")


# ---------- shipped layout ----------

# This file lives in <repo>/bridge/. The study writes into <repo>/outbox/,
# which ships with the repo. So if the README layout is followed, both sides
# agree with nothing configured at all - paths.outbox only ever needs setting
# to point somewhere else.
BRIDGE_DIR = Path(__file__).resolve().parent
REPO_DIR = BRIDGE_DIR.parent
DEFAULT_OUTBOX = REPO_DIR / "outbox"
DEFAULT_CONFIG = BRIDGE_DIR / "manual_config.yaml"
DEFAULT_ENV = BRIDGE_DIR / "manual_bridge.env"

# CME month codes, for spotting a contract id left behind at the last roll.
MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
               "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}


# ---------- time ----------


def utc_now() -> dt.datetime:
    """Naive UTC 'now'. Kept naive so the ISO strings written to the ack log
    stay in the same "...Z" shape the Sierra Chart emitter uses."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def utc_day() -> str:
    return utc_now().strftime("%Y%m%d")


# ---------- config / state ----------


def load_env_file(path: Path) -> None:
    """Minimal KEY=VALUE .env loader (no quoting, no interpolation)."""
    if not path.exists():
        log.warning("env file not found: %s", path)
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, data: dict[str, Any]):
        try:
            self.account_id: int = int(data["accountId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(
                "accountId must be set to your TopstepX account integer "
                "(run with --list-accounts to find it)") from exc
        if self.account_id <= 0:
            raise ConfigError(
                "accountId is still the placeholder - run with --list-accounts "
                "and paste the integer id into manual_config.yaml")

        self.contracts: dict[str, str] = dict(data.get("contracts") or {})
        if not self.contracts:
            raise ConfigError("contracts map is empty - add at least one key")

        paths = data.get("paths") or {}
        outbox = paths.get("outbox")
        # OPTIONAL. Omitted, it resolves to the outbox/ folder that ships next
        # to bridge/ in this repo - correct by construction for anyone who
        # followed the README layout. Set it only to point somewhere else.
        self.outbox_is_default = not outbox
        self.outbox_dir = Path(outbox).expanduser() if outbox else DEFAULT_OUTBOX
        # acks and state default to siblings of the outbox so one folder
        # holds everything the mirror writes.
        self.ack_dir = Path(paths.get("acks") or (self.outbox_dir.parent / "manual-acks"))
        self.state_dir = Path(paths.get("state") or (self.outbox_dir.parent / "manual-state"))

        tuning = data.get("tuning") or {}
        self.poll_interval_s: float = max(0.05, float(tuning.get("pollIntervalS", 0.1)))
        # Burst circuit-breaker: if more than burst_max_places order-creating
        # commands arrive within burst_window_s, halt placing (cancels still
        # allowed) until the bridge is restarted. Backstop against an emitter
        # fault firing a storm. Default is generous so normal brackets
        # (3 orders) and aggressive scaling never trip it.
        self.burst_max_places: int = int(tuning.get("burstMaxPlaces", 12))
        self.burst_window_s: float = float(tuning.get("burstWindowS", 4.0))

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(data)


class JsonStore:
    """Tiny atomic-write JSON dict store for cursor / seen_tags / sc_to_tsx."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.error("%s corrupt, starting fresh", path.name)
                self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)


# ---------- preflight ----------


def stale_contracts(contracts: dict[str, str],
                    now: dt.datetime | None = None) -> list[str]:
    """Contract keys whose id names an expiry month that has already passed.

    ProjectX ids look like CON.F.US.ENQ.U26 - the last segment is a CME month
    code plus a 2-digit year. A stale id is the usual cause of errorCode 8,
    and it happens to everybody four times a year. Ids we cannot parse are
    left alone rather than guessed at.
    """
    now = now or utc_now()
    stale = []
    for key, contract_id in contracts.items():
        tail = str(contract_id).rsplit(".", 1)[-1]
        if len(tail) != 3 or tail[0] not in MONTH_CODES or not tail[1:].isdigit():
            continue
        if (2000 + int(tail[1:]), MONTH_CODES[tail[0]]) < (now.year, now.month):
            stale.append(key)
    return stale


def require_outbox(cfg: "Config") -> None:
    """The outbox belongs to Sierra Chart. Never create it here.

    Creating it is what turns "the two sides disagree about the path" into a
    bridge that logs in, prints a normal banner, and then mirrors nothing
    forever. Refuse to start instead, and say exactly what to fix.
    """
    if cfg.outbox_dir.is_dir():
        return
    source = ("the outbox/ folder that ships in this repo"
              if cfg.outbox_is_default else "paths.outbox in manual_config.yaml")
    raise ConfigError(
        "outbox folder does not exist: {dir}\n"
        "  (resolved from {src})\n"
        "  Sierra Chart WRITES this folder; the bridge only reads it, so the\n"
        "  study's 'Outbox Directory' input and paths.outbox must name the\n"
        "  same folder. Fix whichever one is wrong, then rerun.\n"
        "  'python manual_bridge.py --doctor' checks the whole setup."
        .format(dir=cfg.outbox_dir, src=source))


# ---------- ack log ----------


def write_ack(ack_dir: Path, record: dict[str, Any]) -> None:
    ack_dir.mkdir(parents=True, exist_ok=True)
    with open(ack_dir / f"{utc_day()}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------- helpers ----------


def resolve_contract(cfg: Config, key: str) -> str:
    if key not in cfg.contracts:
        raise KeyError(
            f"contract key '{key}' not in config map - edit manual_config.yaml "
            "(and check it at every contract roll)")
    return cfg.contracts[key]


def side_from_str(s: str) -> int:
    return SIDE_BID if s == "long" else SIDE_ASK


# ---------- burst circuit-breaker ----------


# Commands that can create a new TopstepX order. cancel_replace places a new
# order after cancelling, so it counts too; close_if_open can fire a catch-up
# market, so it counts (a tripped breaker halts ALL order creation - the
# premise of a trip is that the emitter cannot be trusted). Plain "cancel"
# never creates an order and is always allowed (so you can still flatten
# after a trip).
PLACE_KINDS = {"place_market", "place_limit", "place_stop", "cancel_replace",
               "close_if_open"}


class BurstBreaker:
    """Halt order-creating commands if they arrive faster than a sane manual
    rate. One-way latch: once tripped it stays tripped until the bridge is
    restarted (a storm means something is wrong; require a human to look).
    Plain cancels are never counted and never blocked.
    """

    def __init__(self, max_places: int, window_s: float):
        self.max_places = max_places
        self.window_s = window_s
        self._stamps: deque[float] = deque()
        self.tripped = False

    def allow(self, kind: str, now: float) -> bool:
        """Return True if this command may proceed. Records place-type commands
        and trips permanently if the windowed count exceeds the limit."""
        if kind not in PLACE_KINDS:
            return True
        if self.tripped:
            return False
        self._stamps.append(now)
        cutoff = now - self.window_s
        while self._stamps and self._stamps[0] < cutoff:
            self._stamps.popleft()
        if len(self._stamps) > self.max_places:
            self.tripped = True
            log.critical(
                "BURST BREAKER TRIPPED - %d order-creating commands within %.1fs "
                "(limit %d). Placing is HALTED until you restart this bridge; "
                "cancels still work. This usually means an emitter fault (e.g. "
                "order-history replay). CHECK THE TOPSTEPX PORTAL NOW.",
                len(self._stamps), self.window_s, self.max_places)
            return False
        return True


# ---------- command handlers ----------


def _place(
    *,
    cfg: Config,
    client: TSXClient,
    seen: JsonStore,
    sc_map: JsonStore,
    cmd: dict[str, Any],
    order_type: int,
    dry_run: bool,
) -> dict[str, Any]:
    tag = cmd["tag"]
    if tag in seen.data:
        log.info("%s: SKIP dedupe tag=%s", cmd["cmd"], tag)
        return {"status": "skipped", "reason": "dedupe", "tag": tag}

    contract_id = resolve_contract(cfg, cmd["contract"])
    side = side_from_str(cmd["side"])
    size = int(cmd["size"])
    sc_id = str(cmd["sc_id"])
    version = int(cmd.get("version", 1))

    existing = sc_map.data.get(sc_id)
    if existing and existing.get("version", 0) >= version:
        log.warning("%s: SKIP stale v%d for sc_id=%s (sc_map already at v%d)",
                    cmd["cmd"], version, sc_id, existing["version"])
        seen.data[tag] = {"cmd": cmd["cmd"], "skipped": "stale_version"}
        seen.save()
        return {"status": "skipped", "reason": "stale_version", "tag": tag,
                "sc_id": sc_id, "current_version": existing["version"]}

    limit_price = float(cmd["price"]) if "price" in cmd else None
    stop_price = float(cmd["stop_price"]) if "stop_price" in cmd else None

    if dry_run:
        log.info("%s: DRY-RUN tag=%s %s sz=%s lp=%s sp=%s",
                 cmd["cmd"], tag, cmd["side"], size, limit_price, stop_price)
        return {"status": "dry_run", "tag": tag, "contract": contract_id}

    body = client.place_order(
        account_id=cfg.account_id,
        contract_id=contract_id,
        order_type=order_type,
        side=side,
        size=size,
        local_label=tag,
        limit_price=limit_price,
        stop_price=stop_price,
    )
    tsx_order_id = body.get("orderId")
    seen.data[tag] = {"tsx_order_id": tsx_order_id, "cmd": cmd["cmd"]}
    seen.save()
    sc_map.data[sc_id] = {"tsx_order_id": tsx_order_id, "version": version,
                          "contract": contract_id, "last_cmd": cmd["cmd"]}
    sc_map.save()
    return {"status": "ok", "tag": tag, "sc_id": sc_id,
            "tsx_order_id": tsx_order_id}


def handle_place_market(cfg, client, seen, sc_map, cmd, dry_run):
    return _place(cfg=cfg, client=client, seen=seen, sc_map=sc_map,
                  cmd=cmd, order_type=ORDER_TYPE_MARKET, dry_run=dry_run)


def handle_place_limit(cfg, client, seen, sc_map, cmd, dry_run):
    return _place(cfg=cfg, client=client, seen=seen, sc_map=sc_map,
                  cmd=cmd, order_type=ORDER_TYPE_LIMIT, dry_run=dry_run)


def handle_place_stop(cfg, client, seen, sc_map, cmd, dry_run):
    return _place(cfg=cfg, client=client, seen=seen, sc_map=sc_map,
                  cmd=cmd, order_type=ORDER_TYPE_STOP, dry_run=dry_run)


def handle_cancel(cfg, client, seen, sc_map, cmd, dry_run):
    tag = cmd["tag"]
    if tag in seen.data:
        return {"status": "skipped", "reason": "dedupe", "tag": tag}
    sc_id = str(cmd["sc_id"])
    info = sc_map.data.get(sc_id)
    if info is None:
        log.warning("cancel: no tsx_order_id tracked for sc_id=%s - no-op", sc_id)
        seen.data[tag] = {"cmd": "cancel", "noop": True}
        seen.save()
        return {"status": "ok", "tag": tag, "detail": "not_tracked"}

    tsx_order_id = info.get("tsx_order_id")
    if dry_run:
        log.info("cancel: DRY-RUN tag=%s sc_id=%s tsx_oid=%s", tag, sc_id, tsx_order_id)
        return {"status": "dry_run", "tag": tag, "sc_id": sc_id}

    try:
        client.cancel_order(account_id=cfg.account_id, order_id=int(tsx_order_id))
        status = "ok"
        detail: dict[str, Any] = {}
    except TSXError as exc:
        # Already filled or cancelled is benign - log and ack clean.
        log.info("cancel: cancel_order(%s) failed (likely already gone): %s",
                 tsx_order_id, exc)
        status = "ok"
        detail = {"detail": f"cancel_failed: {exc.message}"}

    seen.data[tag] = {"cmd": "cancel", "tsx_order_id": tsx_order_id}
    seen.save()
    # Drop the mapping. A later place with the same sc_id is a fresh order.
    sc_map.data.pop(sc_id, None)
    sc_map.save()
    return {"status": status, "tag": tag, "sc_id": sc_id,
            "tsx_order_id": tsx_order_id, **detail}


def handle_cancel_replace(cfg, client, seen, sc_map, cmd, dry_run):
    tag = cmd["tag"]
    if tag in seen.data:
        return {"status": "skipped", "reason": "dedupe", "tag": tag}

    sc_id = str(cmd["sc_id"])
    version = int(cmd.get("version", 1))
    new_type = cmd["new_type"]
    contract_id = resolve_contract(cfg, cmd["contract"])
    side = side_from_str(cmd["side"])
    size = int(cmd["size"])
    limit_price = float(cmd["price"]) if "price" in cmd else None
    stop_price = float(cmd["stop_price"]) if "stop_price" in cmd else None

    type_map = {"market": ORDER_TYPE_MARKET,
                "limit":  ORDER_TYPE_LIMIT,
                "stop":   ORDER_TYPE_STOP}
    if new_type not in type_map:
        return {"status": "error", "tag": tag,
                "reason": f"unsupported new_type: {new_type}"}

    existing = sc_map.data.get(sc_id) or {}
    if existing.get("version", 0) > version:
        log.warning("cancel_replace: SKIP stale v%d for sc_id=%s (sc_map at v%d)",
                    version, sc_id, existing["version"])
        seen.data[tag] = {"cmd": "cancel_replace", "skipped": "stale_version"}
        seen.save()
        return {"status": "skipped", "reason": "stale_version", "tag": tag,
                "sc_id": sc_id, "current_version": existing["version"]}

    old_tsx = existing.get("tsx_order_id")

    if dry_run:
        log.info("cancel_replace: DRY-RUN tag=%s sc_id=%s old_tsx=%s new_type=%s size=%d lp=%s sp=%s",
                 tag, sc_id, old_tsx, new_type, size, limit_price, stop_price)
        return {"status": "dry_run", "tag": tag, "sc_id": sc_id}

    # Step 1: cancel the existing TopstepX order (best-effort).
    cancel_detail: str | bool = "no_prior"
    if old_tsx is not None:
        try:
            client.cancel_order(account_id=cfg.account_id, order_id=int(old_tsx))
            cancel_detail = True
        except TSXError as exc:
            log.info("cancel_replace: cancel_order(%s) failed - proceeding to re-place: %s",
                     old_tsx, exc)
            cancel_detail = f"cancel_failed: {exc.message}"

    # Step 2: place the new order with the new (versioned) tag.
    try:
        body = client.place_order(
            account_id=cfg.account_id,
            contract_id=contract_id,
            order_type=type_map[new_type],
            side=side,
            size=size,
            local_label=tag,
            limit_price=limit_price,
            stop_price=stop_price,
        )
    except TSXError as exc:
        # If the new place fails after we already cancelled the old, we've
        # effectively flattened that resting order. Log loudly and surface
        # the error. Re-place it by hand if needed.
        log.error("cancel_replace: new place FAILED after cancel sc_id=%s: %s",
                  sc_id, exc)
        seen.data[tag] = {"cmd": "cancel_replace", "error": exc.message,
                          "old_tsx_cancelled": cancel_detail}
        seen.save()
        return {"status": "error", "tag": tag, "sc_id": sc_id,
                "reason": "new_place_failed", "error": exc.message,
                "old_tsx_cancelled": cancel_detail}

    new_tsx = body.get("orderId")
    seen.data[tag] = {"cmd": "cancel_replace", "tsx_order_id": new_tsx,
                      "old_tsx_cancelled": cancel_detail}
    seen.save()
    sc_map.data[sc_id] = {"tsx_order_id": new_tsx, "version": version,
                          "contract": contract_id, "last_cmd": "cancel_replace"}
    sc_map.save()
    return {"status": "ok", "tag": tag, "sc_id": sc_id,
            "tsx_order_id": new_tsx, "old_tsx_cancelled": cancel_detail}


def handle_close_if_open(cfg, client, seen, sc_map, cmd, dry_run):
    """Sierra Chart filled a MIRRORED stop/limit. Decide whether TopstepX did too.

    1. If no twin is tracked for this sc_id -> no-op (bridge restarted without
       state, or the twin was already cancelled/consumed).
    2. Ask TopstepX for its open orders. Twin NOT open -> it already filled
       (or was cancelled) -> TopstepX is in sync -> no-op.
    3. Twin IS open -> cancel it. Cancel FAILS -> it filled in the gap ->
       no-op. Cancel SUCCEEDS -> the twin was still resting, so TopstepX did
       NOT fill -> fire a MARKET for the same side/size so the shadow catches
       up (the Sierra Chart fill was a reprice-into-market or a limit that
       TopstepX's queue position missed).

    The market is gated on the cancel succeeding, so it cannot double-fill.
    Two independent checks (searchOpen + cancel result) guard against a
    cancel API that reports success on an already-filled order. A TopstepX
    PARTIAL fill on the twin is not detectable here; the catch-up market is
    for the full size (documented limitation).
    """
    tag = cmd["tag"]
    if tag in seen.data:
        return {"status": "skipped", "reason": "dedupe", "tag": tag}
    sc_id = str(cmd["sc_id"])
    info = sc_map.data.get(sc_id)
    if info is None:
        log.warning("close_if_open: no tsx_order_id tracked for sc_id=%s - no-op", sc_id)
        seen.data[tag] = {"cmd": "close_if_open", "noop": "not_tracked"}
        seen.save()
        return {"status": "ok", "tag": tag, "sc_id": sc_id, "detail": "not_tracked"}

    tsx_order_id = info.get("tsx_order_id")
    contract_id = resolve_contract(cfg, cmd["contract"])
    side = side_from_str(cmd["side"])
    size = int(cmd["size"])

    if dry_run:
        log.info("close_if_open: DRY-RUN tag=%s sc_id=%s tsx_oid=%s %s sz=%d",
                 tag, sc_id, tsx_order_id, cmd["side"], size)
        return {"status": "dry_run", "tag": tag, "sc_id": sc_id}

    # Check 1: is the twin still on TopstepX's book?
    try:
        open_ids = {o.get("id") for o in client.search_open_orders(account_id=cfg.account_id)}
    except TSXError as exc:
        # Can't tell. Fall through to the cancel, whose result is the second
        # check; log so the ambiguity is visible.
        log.warning("close_if_open: searchOpen failed (%s) - relying on cancel result", exc.message)
        open_ids = None
    if open_ids is not None and int(tsx_order_id) not in open_ids:
        log.info("close_if_open: twin %s not open on TopstepX - already filled/cancelled, in sync",
                 tsx_order_id)
        seen.data[tag] = {"cmd": "close_if_open", "tsx_order_id": tsx_order_id, "noop": "twin_gone"}
        seen.save()
        sc_map.data.pop(sc_id, None)
        sc_map.save()
        return {"status": "ok", "tag": tag, "sc_id": sc_id,
                "tsx_order_id": tsx_order_id, "detail": "twin_gone"}

    # Check 2: cancel it. Failure = it filled in the gap = in sync.
    try:
        client.cancel_order(account_id=cfg.account_id, order_id=int(tsx_order_id))
    except TSXError as exc:
        log.info("close_if_open: cancel(%s) failed (%s) - twin already gone, in sync",
                 tsx_order_id, exc.message)
        seen.data[tag] = {"cmd": "close_if_open", "tsx_order_id": tsx_order_id,
                          "noop": "twin_gone_on_cancel"}
        seen.save()
        sc_map.data.pop(sc_id, None)
        sc_map.save()
        return {"status": "ok", "tag": tag, "sc_id": sc_id,
                "tsx_order_id": tsx_order_id, "detail": "twin_gone_on_cancel"}

    # Cancel succeeded: the twin was resting, TopstepX did NOT fill. Catch up.
    log.warning("close_if_open: twin %s was STILL RESTING - Sierra Chart filled, TopstepX did not. "
                "Firing market %s %s sz=%d", tsx_order_id, cmd["side"], contract_id, size)
    try:
        body = client.place_order(
            account_id=cfg.account_id,
            contract_id=contract_id,
            order_type=ORDER_TYPE_MARKET,
            side=side,
            size=size,
            local_label=tag,
        )
    except TSXError as exc:
        # Twin is cancelled and the catch-up failed: TopstepX is now OUT OF
        # SYNC with Sierra Chart for this order. Loudest line the bridge has.
        log.critical("close_if_open: CATCH-UP MARKET FAILED after cancelling twin %s "
                     "sc_id=%s - TopstepX is OUT OF SYNC, intervene in the portal: %s",
                     tsx_order_id, sc_id, exc.message)
        seen.data[tag] = {"cmd": "close_if_open", "cancelled_tsx_order_id": tsx_order_id,
                          "error": exc.message}
        seen.save()
        sc_map.data.pop(sc_id, None)
        sc_map.save()
        return {"status": "error", "tag": tag, "sc_id": sc_id,
                "reason": "catch_up_market_failed", "error": exc.message,
                "cancelled_tsx_order_id": tsx_order_id}

    new_tsx = body.get("orderId")
    seen.data[tag] = {"cmd": "close_if_open", "cancelled_tsx_order_id": tsx_order_id,
                      "market_tsx_order_id": new_tsx}
    seen.save()
    sc_map.data.pop(sc_id, None)
    sc_map.save()
    return {"status": "ok", "tag": tag, "sc_id": sc_id,
            "cancelled_tsx_order_id": tsx_order_id, "market_tsx_order_id": new_tsx,
            "detail": "caught_up"}


HANDLERS = {
    "place_market":   handle_place_market,
    "place_limit":    handle_place_limit,
    "place_stop":     handle_place_stop,
    "cancel":         handle_cancel,
    "cancel_replace": handle_cancel_replace,
    "close_if_open":  handle_close_if_open,
}


def dispatch(cfg, client, seen, sc_map, cmd, dry_run, breaker=None):
    kind = cmd.get("cmd")
    handler = HANDLERS.get(kind or "")
    if handler is None:
        return {"status": "error", "reason": f"unknown cmd: {kind}", "raw": cmd}
    if breaker is not None and not breaker.allow(kind or "", time.time()):
        log.warning("BLOCKED %s tag=%s - burst breaker tripped (placing halted; "
                    "restart bridge to clear)", kind, cmd.get("tag"))
        return {"status": "error", "cmd": kind, "tag": cmd.get("tag"),
                "reason": "burst_breaker_tripped"}
    try:
        return handler(cfg, client, seen, sc_map, cmd, dry_run)
    except TSXError as exc:
        log.error("%s: %s", kind, exc)
        if exc.error_code == 8:
            log.error("  -> errorCode 8 nearly always means the contract id "
                      "mapped to '%s' is wrong or has ROLLED. Fix contracts: "
                      "in manual_config.yaml, then restart the bridge.",
                      cmd.get("contract"))
        return {"status": "error", "cmd": kind, "tag": cmd.get("tag"),
                "error_code": exc.error_code, "error": exc.message}
    except KeyError as exc:
        # A config gap (unknown contract key) or a malformed line. One line,
        # no traceback - this is an operator error, not a crash.
        log.error("%s tag=%s: %s", kind, cmd.get("tag"), exc.args[0] if exc.args else exc)
        return {"status": "error", "cmd": kind, "tag": cmd.get("tag"),
                "error": str(exc.args[0] if exc.args else exc)}
    except Exception as exc:  # noqa: BLE001
        log.exception("%s: unexpected", kind)
        return {"status": "error", "cmd": kind, "tag": cmd.get("tag"),
                "error": str(exc)}


# ---------- outbox tailing ----------


LAST_FILE_KEY = "_last_file"  # cursor.json key: which outbox file we last tailed


def today_outbox_file(outbox_dir: Path) -> Path:
    return outbox_dir / f"{utc_day()}.jsonl"


def _tail_file(outbox: Path, cfg, client, seen, sc_map, cursor, dry_run, breaker) -> int:
    """Process every complete line in `outbox` from its cursor to EOF."""
    filename = outbox.name
    start = int(cursor.data.get(filename, 0))
    try:
        with open(outbox, "rb") as f:
            f.seek(start)
            raw = f.read()
            new_offset = f.tell()
    except OSError as exc:
        log.error("outbox read failed: %s", exc)
        return 0

    if not raw:
        return 0

    text = raw.decode("utf-8", errors="replace")
    if text.endswith("\n"):
        lines = text.splitlines()
        final_offset = new_offset
    else:
        parts = text.split("\n")
        lines = parts[:-1]
        partial = parts[-1].encode("utf-8")
        final_offset = new_offset - len(partial)

    processed = 0
    cmds = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError as exc:
            ack = {"status": "error", "reason": f"bad json: {exc}", "raw": line,
                   "ts": utc_now().isoformat() + "Z"}
            write_ack(cfg.ack_dir, ack)
            continue
        cmds.append(cmd)

    for cmd in cmds:
        bridge_read_ts = utc_now()
        ack = dispatch(cfg, client, seen, sc_map, cmd, dry_run, breaker)
        tsx_response_ts = utc_now()

        ack["sc_emit_ts"] = cmd.get("ts", "")
        ack["bridge_read_ts"] = bridge_read_ts.isoformat() + "Z"
        ack["tsx_response_ts"] = tsx_response_ts.isoformat() + "Z"
        ack["ts"] = ack["tsx_response_ts"]

        api_ms = (tsx_response_ts - bridge_read_ts).total_seconds() * 1000
        sc_ts_str = cmd.get("ts", "")
        detect_ms: float | None = None
        if sc_ts_str:
            try:
                sc_dt = dt.datetime.fromisoformat(sc_ts_str.replace("Z", "+00:00"))
                detect_ms = (bridge_read_ts.replace(tzinfo=dt.timezone.utc) - sc_dt).total_seconds() * 1000
            except ValueError:
                pass

        detect_part = f"detect={detect_ms:.0f}ms " if detect_ms is not None else ""
        log.info("LATENCY %s tag=%s %sapi=%.0fms total=%.0fms",
                 cmd.get("cmd", "?"), cmd.get("tag", "?"),
                 detect_part, api_ms,
                 (detect_ms or 0) + api_ms)

        write_ack(cfg.ack_dir, ack)
        processed += 1

    cursor.data[filename] = final_offset
    cursor.save()
    return processed


def tail_outbox(cfg, client, seen, sc_map, cursor, dry_run, breaker=None) -> int:
    today = today_outbox_file(cfg.outbox_dir)
    processed = 0

    # UTC-midnight rollover: Sierra Chart and this bridge each compute the UTC
    # date independently, so a line appended to yesterday's file between our
    # last poll and the date change would otherwise never be read. Drain the
    # previous file to EOF once before moving on. run() pins _last_file to
    # today at startup, so lines written while the bridge was OFF are never
    # drained this way.
    last_name = cursor.data.get(LAST_FILE_KEY)
    if last_name and last_name != today.name:
        prev = cfg.outbox_dir / last_name
        if prev.exists():
            n = _tail_file(prev, cfg, client, seen, sc_map, cursor, dry_run, breaker)
            if n:
                log.info("rollover: drained %d line(s) from %s", n, last_name)
            processed += n
        cursor.data[LAST_FILE_KEY] = today.name
        cursor.save()
    elif last_name is None:
        cursor.data[LAST_FILE_KEY] = today.name
        cursor.save()

    if today.exists():
        processed += _tail_file(today, cfg, client, seen, sc_map, cursor, dry_run, breaker)
    return processed


# ---------- startup recovery ----------


def recover_on_startup(cfg, client, seen, dry_run) -> None:
    """Verify connectivity and log today's order count as a sanity check.

    Tags are never sent to TopstepX, so nothing can be rebuilt from the API.
    Replay protection is purely local: seen_tags.json (tags already acted on)
    plus the startup fast-forward in run() that skips outbox lines written
    while the bridge was off.
    """
    if dry_run:
        log.info("recover: skipped in dry-run")
        return
    start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        orders = client.search_orders(
            account_id=cfg.account_id, start_ts=start.isoformat() + "Z")
    except TSXError as exc:
        log.error("recover: search_orders failed: %s", exc)
        return
    log.info("recover: %d orders on account today, %d tags in local seen_tags",
             len(orders), len(seen.data))


# ---------- main loop ----------


def run(cfg: Config, client: TSXClient, dry_run: bool) -> None:
    require_outbox(cfg)
    cfg.ack_dir.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    for key in stale_contracts(cfg.contracts):
        log.warning("contract %s = %s names a month that has PASSED. Placing on "
                    "it will fail with errorCode 8 - edit contracts: in "
                    "manual_config.yaml.", key, cfg.contracts[key])

    seen   = JsonStore(cfg.state_dir / "seen_tags.json")
    cursor = JsonStore(cfg.state_dir / "cursor.json")
    sc_map = JsonStore(cfg.state_dir / "sc_to_tsx.json")
    breaker = BurstBreaker(cfg.burst_max_places, cfg.burst_window_s)

    if not dry_run:
        client.login()
        recover_on_startup(cfg, client, seen, dry_run)

    # Skip any outbox lines written while the bridge was off. Only process
    # commands emitted AFTER startup - stale place/cancel lines for orders
    # that may already be cancelled in Sierra Chart would cause phantom
    # TopstepX orders.
    # Pinning _last_file to today also stops the rollover drain from ever
    # replaying a previous day's tail.
    outbox = today_outbox_file(cfg.outbox_dir)
    if outbox.exists():
        skip_bytes = outbox.stat().st_size
        cursor.data[outbox.name] = skip_bytes
        log.info("manual-bridge: skipped %d bytes of pre-existing outbox", skip_bytes)
    cursor.data[LAST_FILE_KEY] = outbox.name
    cursor.save()

    log.info("manual-bridge: started (dry_run=%s) account=%s", dry_run, cfg.account_id)
    log.info("manual-bridge: contract map = %s", cfg.contracts)
    log.info("manual-bridge: outbox=%s [%s] poll_interval=%.2fs",
             cfg.outbox_dir,
             "repo default" if cfg.outbox_is_default else "from config",
             cfg.poll_interval_s)
    log.info("manual-bridge: that folder MUST equal the study's "
             "'Outbox Directory' input")
    log.info("manual-bridge: burst breaker = max %d place cmds / %.1fs",
             cfg.burst_max_places, cfg.burst_window_s)

    # A bridge that is merely idle and a bridge that is misconfigured look
    # identical: both sit there logging nothing. If nothing has EVER come
    # through, say so periodically rather than letting it look healthy.
    processed = 0
    started = time.time()
    next_hint = started + 120.0
    while True:
        try:
            processed += tail_outbox(cfg, client, seen, sc_map, cursor,
                                     dry_run, breaker)
        except Exception:  # noqa: BLE001
            log.exception("tail_outbox crashed - continuing")
        now = time.time()
        if processed == 0 and now >= next_hint:
            log.warning(
                "nothing mirrored yet (%d min). If you HAVE placed an order "
                "since starting: check the study's Enable input is Yes, and "
                "that its 'Outbox Directory' is exactly %s. "
                "'--doctor' checks everything.",
                int((now - started) / 60), cfg.outbox_dir)
            next_hint = now + 300.0
        time.sleep(cfg.poll_interval_s)


def list_accounts(client: TSXClient) -> int:
    client.login()
    accounts = client.search_accounts(only_active=True)
    if not accounts:
        log.warning("no accounts returned - check API key permissions")
        return 0
    print()
    print(f"{'id':<12} {'name':<24} {'balance':>12}  sim  canTrade")
    print("-" * 66)
    for a in accounts:
        print(f"{a.get('id'):<12} {a.get('name', ''):<24} "
              f"{a.get('balance', 0):>12}  "
              f"{'Y' if a.get('simulated') else 'N':<3}  "
              f"{'Y' if a.get('canTrade') else 'N'}")
    print()
    print("Paste the integer id you want to mirror INTO as accountId in manual_config.yaml.")
    return 0


def doctor(args, user: str | None, api_key: str | None) -> int:
    """One command that checks every part of the setup and says what to fix.

    Read-only against TopstepX: it logs in and lists accounts, and never
    places, modifies or cancels anything.
    """
    # (status, label, detail). Only FAIL counts towards the exit code:
    # start-bridge.bat refuses to launch on a non-zero exit, so anything that
    # does not actually stop the bridge working must be a WARN.
    checks: list[tuple[str, str, str]] = []

    def check(ok, label, detail="", warn_only=False):
        ok = bool(ok)
        checks.append(("PASS" if ok else ("WARN" if warn_only else "FAIL"),
                       label, detail))
        return ok

    # --- credentials ------------------------------------------------------
    check(user, "TSX_USER set",
          user or "missing - set it in {}".format(args.env))
    check(api_key, "TSX_API_KEY set",
          "{} chars".format(len(api_key)) if api_key
          else "missing - set it in {}".format(args.env))

    # --- config -----------------------------------------------------------
    cfg = None
    if not args.config.exists():
        check(False, "config file",
              "not found: {} - copy manual_config.example.yaml".format(args.config))
    else:
        try:
            cfg = Config.load(args.config)
            check(True, "config file", str(args.config))
        except ConfigError as exc:
            check(False, "config file", str(exc).splitlines()[0])
        except Exception as exc:  # noqa: BLE001
            check(False, "config file", "unreadable: {}".format(exc))

    # --- paths ------------------------------------------------------------
    if cfg is not None:
        source = "repo default" if cfg.outbox_is_default else "paths.outbox"
        if check(cfg.outbox_dir.is_dir(), "outbox folder exists",
                 "{} [{}]".format(cfg.outbox_dir, source)):
            jsonl = list(cfg.outbox_dir.glob("*.jsonl"))
            if jsonl:
                newest = max(jsonl, key=lambda p: p.stat().st_mtime)
                mins = int((time.time() - newest.stat().st_mtime) / 60)
                check(True, "study has written here",
                      "{} ({} min ago)".format(newest.name, mins))
            else:
                check(False, "study has written here",
                      "no .jsonl yet - expected before the study's first "
                      "order; after that, its Outbox Directory points "
                      "elsewhere", warn_only=True)

            # acks/ and state/ are derived from the outbox path, so when the
            # outbox is wrong these fail too. Reporting them would bury the
            # one line that actually needs fixing.
            for label, folder in (("acks", cfg.ack_dir), ("state", cfg.state_dir)):
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                    probe = folder / ".doctor_write_test"
                    probe.write_text("ok", encoding="utf-8")
                    probe.unlink()
                    check(True, "{} folder writable".format(label), str(folder))
                except Exception as exc:  # noqa: BLE001
                    check(False, "{} folder writable".format(label),
                          "{}: {}".format(folder, exc))

        # A stale id only breaks the symbols it maps, so warn rather than
        # block someone who is trading a different one today.
        stale = stale_contracts(cfg.contracts)
        check(not stale, "contract months current",
              "EXPIRED: {} - edit contracts: in manual_config.yaml".format(
                  ", ".join("{}={}".format(k, cfg.contracts[k]) for k in stale))
              if stale else "{} mapped".format(len(cfg.contracts)),
              warn_only=True)

    # --- TopstepX ---------------------------------------------------------
    if user and api_key:
        client = TSXClient(Credentials(user_name=user, api_key=api_key))
        try:
            client.login()
            check(True, "TopstepX login", "as {}".format(user))
            accounts = client.search_accounts(only_active=True)
            check(accounts, "accounts visible",
                  "{} active".format(len(accounts)) if accounts
                  else "none - check API key permissions")
            if cfg is not None:
                match = [a for a in accounts if a.get("id") == cfg.account_id]
                if match:
                    acct = match[0]
                    check(acct.get("canTrade"),
                          "account {} tradable".format(cfg.account_id),
                          "{}  sim={}  canTrade={}".format(
                              acct.get("name", "?"),
                              "Y" if acct.get("simulated") else "N",
                              "Y" if acct.get("canTrade") else "N"))
                else:
                    check(False, "account {} found".format(cfg.account_id),
                          "not in your account list - run --list-accounts")
        except TSXError as exc:
            check(False, "TopstepX login", str(exc))
    else:
        check(False, "TopstepX login", "skipped - credentials missing")

    # --- report -----------------------------------------------------------
    print()
    print("OPTD manual mirror - setup check")
    print("=" * 72)
    failed = sum(1 for st, _, _ in checks if st == "FAIL")
    warned = sum(1 for st, _, _ in checks if st == "WARN")
    for status, label, detail in checks:
        print("  [{}] {:<26} {}".format(status, label, detail))
    print("=" * 72)
    if cfg is not None:
        print()
        if cfg.outbox_dir.is_dir():
            print("  Sierra Chart study input 'Outbox Directory' must be exactly:")
            print()
            print("      {}".format(cfg.outbox_dir))
        else:
            print("  The bridge is looking for its outbox at:")
            print()
            print("      {}".format(cfg.outbox_dir))
            print()
            print("  That folder does not exist, so nothing can ever arrive.")
            print("  Either point paths.outbox at the folder the study really")
            print("  writes to, or delete paths.outbox from manual_config.yaml")
            print("  to fall back to the outbox/ folder shipped in this repo:")
            print()
            print("      {}".format(DEFAULT_OUTBOX))
    print()
    if failed:
        print("{} check(s) FAILED. Fix the FAIL lines above, then run "
              "--doctor again.".format(failed))
    elif warned:
        print("Ready ({} warning(s) above - read them, but they do not stop "
              "the bridge).".format(warned))
        print("Start it with:  python manual_bridge.py")
    else:
        print("All checks passed. Start it with:  python manual_bridge.py")
    print()
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mirror manual Sierra Chart orders to a TopstepX account.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to manual_config.yaml "
                             "(default: next to this script)")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV,
                        help="Path to manual_bridge.env "
                             "(default: next to this script)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log only, no API calls")
    parser.add_argument("--doctor", action="store_true",
                        help="Check paths, credentials, account and contracts, "
                             "then exit")
    parser.add_argument("--list-accounts", action="store_true",
                        help="Log in, print the account ids visible to this API key, and exit")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--poll-interval", type=float, default=None,
                        help="Seconds between outbox tail reads (overrides config)")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.env.exists():
        load_env_file(args.env)
    elif args.env != DEFAULT_ENV:
        log.warning("env file not found: %s", args.env)

    user = os.environ.get("TSX_USER")
    api_key = os.environ.get("TSX_API_KEY")

    if args.list_accounts:
        if not user or not api_key:
            log.error("TSX_USER and TSX_API_KEY must be set in env (or manual_bridge.env)")
            return 2
        return list_accounts(TSXClient(Credentials(user_name=user, api_key=api_key)))

    if args.doctor:
        return doctor(args, user, api_key)

    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        log.error("config: %s", exc)
        return 2

    if args.poll_interval is not None:
        cfg.poll_interval_s = max(0.05, float(args.poll_interval))

    if not args.dry_run and (not user or not api_key):
        log.error("TSX_USER and TSX_API_KEY must be set in env (or manual_bridge.env)")
        return 2

    client = TSXClient(Credentials(user_name=user or "", api_key=api_key or ""))

    try:
        run(cfg, client, args.dry_run)
    except ConfigError as exc:
        # Raised by require_outbox: a setup problem, not a crash. One clear
        # block, no traceback.
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.info("manual-bridge: interrupted")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
