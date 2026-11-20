"""Read registered 17TRACK history into a resumable local SQLite database."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from http.client import HTTPException
import json
from pathlib import Path
import sqlite3
from ssl import SSLCertVerificationError
from threading import Lock
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import duckdb

from track17 import norm, token_of, tracking_utc
from app.waybill import key_sql, number_text_sql


API = "https://api.17track.net/track/v2.4/"
REGISTRY_BATCH = 160
DETAIL_BATCH = 40
WORKERS = 6

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS packages (
    tracking_no TEXT PRIMARY KEY,
    fulfillment_no TEXT NOT NULL,
    signed_date TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    checked_at TEXT
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS package_queue ON packages(state,signed_date,tracking_no);
CREATE TABLE IF NOT EXISTS tracking_details (
    tracking_no TEXT NOT NULL,
    carrier_code INTEGER NOT NULL,
    latest_status TEXT,
    latest_substatus TEXT,
    checked_at TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    error_code INTEGER,
    PRIMARY KEY (tracking_no,carrier_code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tracking_events (
    tracking_no TEXT NOT NULL,
    carrier_code INTEGER NOT NULL,
    provider_position INTEGER NOT NULL,
    event_position INTEGER NOT NULL,
    provider_code INTEGER,
    provider_name TEXT,
    event_time_utc TEXT,
    event_time_raw TEXT,
    stage TEXT,
    substatus TEXT,
    description TEXT,
    location TEXT,
    event_time_iso TEXT,
    PRIMARY KEY (tracking_no,carrier_code,provider_position,event_position)
) WITHOUT ROWID;
"""

QUEUE = """
SELECT o.单号归一, min(o.履约单号), min(o.签收时间::DATE)
FROM fct_order o
WHERE o.已签收=1 AND o.签收时间::DATE BETWEEN ? AND ?
  AND lower(coalesce(o.物流商,'')) <> 'postnet'
  AND length(o.单号归一) BETWEEN 8 AND 50
  AND NOT EXISTS (SELECT 1 FROM trk_status t WHERE t.单号=o.单号归一)
  AND NOT EXISTS (SELECT 1 FROM trk_missing m WHERE m.单号=o.单号归一)
GROUP BY o.单号归一 ORDER BY 3,1
"""

ALL_WAYBILLS_QUEUE = f"""
WITH waybills AS (
    SELECT {key_sql("o.快递单号")} AS tracking_no, o.履约单号,
           coalesce(o.签收时间::DATE,o.签出时间::DATE,o.创建日期) AS activity_day
    FROM fct_order o
    WHERE lower(coalesce(trim(o.物流商),''))<>'postnet'
      AND lower(coalesce(trim(o.C端承运商),''))<>'postnet'
      AND NOT contains({number_text_sql("o.快递单号")}, '+')
)
SELECT w.tracking_no,min(w.履约单号),max(w.activity_day)
FROM waybills w
WHERE w.activity_day BETWEEN ? AND ? AND length(w.tracking_no) BETWEEN 8 AND 50
  AND NOT EXISTS (SELECT 1 FROM trk_event e WHERE e.单号=w.tracking_no)
GROUP BY 1 ORDER BY 3,1
"""


def chunks(items, size):
    for offset in range(0, len(items), size):
        yield items[offset:offset + size]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RetryableRequestError(RuntimeError):
    """A transient API failure left the batch available for another attempt."""


class ReadOnlyClient:
    def __init__(self, token, requests_per_second):
        self.token = token
        self.interval = 1 / requests_per_second
        self.lock = Lock()
        self.next_call = 0.0

    def wait_for_turn(self):
        while True:
            with self.lock:
                delay = self.next_call - time.monotonic()
                if delay <= 0:
                    self.next_call = time.monotonic() + self.interval
                    return
            time.sleep(delay)

    def pause(self, seconds):
        with self.lock:
            self.next_call = max(self.next_call, time.monotonic() + seconds)

    def read(self, operation, payload):
        if operation not in {"gettracklist", "gettrackinfo", "getquota"}:
            raise ValueError("Only registered-history queries are supported")
        request = Request(API + operation, data=json.dumps(payload).encode(),
                          headers={"17token": self.token, "Content-Type": "application/json"})
        for attempt in range(6):
            self.wait_for_turn()
            try:
                with urlopen(request, timeout=30) as response:
                    answer = json.load(response)
            except HTTPError as error:
                retryable = error.code in (408, 429) or 500 <= error.code < 600
                if not retryable:
                    raise RuntimeError(f"{operation}: HTTP {error.code}") from None
                if error.code == 429:
                    retry_after = error.headers.get("Retry-After", "")
                    delay = int(retry_after) if retry_after.isdecimal() else 2 ** (attempt + 1)
                    self.pause(max(delay, 2))
                if attempt == 5:
                    raise RetryableRequestError(f"{operation}: HTTP {error.code}") from None
                if error.code != 429:
                    time.sleep(2 ** attempt)
                continue
            except (OSError, HTTPException, UnicodeError, json.JSONDecodeError) as error:
                if isinstance(getattr(error, "reason", error), SSLCertVerificationError):
                    raise RuntimeError(f"{operation}: certificate verification failed") from None
                if attempt == 5:
                    raise RetryableRequestError(f"{operation}: {type(error).__name__}") from None
                time.sleep(2 ** attempt)
                continue
            if answer.get("code") != 0:
                raise RuntimeError(f"{operation}: API code {answer.get('code')}")
            result = answer.get("data") or {}
            if operation == "gettracklist" and isinstance(answer.get("page"), dict):
                result["_page"] = answer["page"]
            return result
        raise RetryableRequestError(f"{operation}: retry limit reached")


def seed_queue(db, facts, since, through, *, all_waybills=False):
    db.executescript(SCHEMA)
    if "event_time_iso" not in {col[1] for col in db.execute("PRAGMA table_info(tracking_events)")}:
        db.execute("ALTER TABLE tracking_events ADD COLUMN event_time_iso TEXT")
    selected = json.dumps([str(facts.resolve()), str(since), str(through)])
    old = db.execute("SELECT value FROM metadata WHERE key='scope'").fetchone()
    if old:
        source, _, _ = json.loads(old[0])
        if Path(source).resolve() != facts.resolve() or (not all_waybills and old[0] != selected):
            raise RuntimeError("This local database belongs to a different source or history range")
    seed_key = f"all_waybills:{since}:{through}" if all_waybills else "seeded"
    with db:
        db.execute("INSERT OR IGNORE INTO metadata VALUES ('scope',?)", [selected])
    if db.execute("SELECT 1 FROM metadata WHERE key=?", [seed_key]).fetchone():
        return
    with closing(duckdb.connect(str(facts), read_only=True)) as source:
        candidates = source.execute(ALL_WAYBILLS_QUEUE if all_waybills else QUEUE, [since, through])
        while batch := candidates.fetchmany(15000):
            with db:
                db.executemany("INSERT OR IGNORE INTO packages(tracking_no,fulfillment_no,signed_date) VALUES (?,?,?)",
                               [(no, order, signed.isoformat()) for no, order, signed in batch])
    with db:
        db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", [seed_key, utc_now()])


def registered_pairs(client, numbers):
    wanted = set(numbers)
    seen = set()
    previous = set()
    page = 1
    while True:
        data = client.read("gettracklist", {"number": ",".join(numbers), "page_no": page,
                                            "order_by": "RegisterTimeDesc"})
        matches = data.get("accepted") or []
        current = {(norm(row.get("number")), int(row["carrier"])) for row in matches
                   if row.get("carrier") and norm(row.get("number")) in wanted}
        page_total = (data.get("_page") or {}).get("page_total")
        if isinstance(page_total, int) and page >= page_total:
            return sorted(seen | current)
        if len(matches) >= 40 and not current.difference(seen):
            if not current or current != previous:
                raise RuntimeError("Registry pagination did not advance")
            # An out-of-range page repeats the final full page. Probe remaining numbers separately.
            remaining = wanted - {number for number, _ in seen}
            if remaining:
                seen.update(registered_pairs(client, sorted(remaining)))
            return sorted(seen)
        seen.update(current)
        if len(matches) < 40:
            return sorted(seen)
        previous = current
        page += 1


def fetch_batch(client, numbers):
    pairs = registered_pairs(client, numbers)
    pending = set(pairs)
    accepted = {}
    failed = {}
    for round_no in range(2):
        for group in chunks(sorted(pending), DETAIL_BATCH):
            response = client.read("gettrackinfo", [{"number": number, "carrier": carrier}
                                                     for number, carrier in group])
            requested = set(group)
            for row in response.get("accepted") or []:
                pair = (norm(row.get("number")), int(row.get("carrier") or 0))
                if pair in requested:
                    accepted[pair] = row
                    pending.discard(pair)
            by_number = {}
            for number, carrier in group:
                by_number.setdefault(number, []).append(carrier)
            for row in response.get("rejected") or []:
                number = norm(row.get("number"))
                code = (row.get("error") or {}).get("code")
                carrier = row.get("carrier")
                available = by_number.get(number, [])
                pair = (number, int(carrier)) if carrier else (
                    (number, available[0]) if len(available) == 1 else None
                )
                if pair in requested and pair not in accepted:
                    failed[pair] = code
                    if code in {-18019902, -18019909, -18019910, -18019805}:
                        pending.discard(pair)
        if not pending:
            break
        if round_no == 0:
            time.sleep(1)
    return numbers, pairs, accepted, failed, pending


def event_time(event):
    value = tracking_utc(event)
    return value.isoformat() if value is not None else None


def persist(db, fetched):
    numbers, pairs, accepted, failed, pending = fetched
    checked = utc_now()
    events = []
    detail_rows = []
    by_number = {no: set() for no in numbers}
    for no, carrier in pairs:
        row = accepted.get((no, carrier))
        info = (row or {}).get("track_info") or {}
        latest = info.get("latest_status") or {}
        providers = (info.get("tracking") or {}).get("providers") or []
        count = 0
        for provider_pos, group in enumerate(providers):
            provider = group.get("provider") or {}
            for event_pos, event in enumerate(group.get("events") or []):
                raw = event.get("time_raw")
                events.append((no, carrier, provider_pos, event_pos, provider.get("key"), provider.get("name"),
                               event_time(event), json.dumps(raw, ensure_ascii=False) if raw else None,
                               event.get("stage"), event.get("sub_status"), event.get("description"),
                               event.get("location"), event.get("time_iso")))
                count += 1
        by_number[no].add("tracked" if count else "empty" if row else "detail_error")
        detail_rows.append((no, carrier, latest.get("status"), latest.get("sub_status"), checked, count,
                            None if row else failed.get((no, carrier))))

    update = []
    for no, statuses in by_number.items():
        state = next((key for key in ("tracked", "empty", "detail_error") if key in statuses), "registry_unmatched")
        update.append((state, checked, no))
    with db:
        db.executemany("INSERT OR REPLACE INTO tracking_details VALUES (?,?,?,?,?,?,?)", detail_rows)
        db.executemany("""
            INSERT OR REPLACE INTO tracking_events (
                tracking_no,carrier_code,provider_position,event_position,provider_code,provider_name,
                event_time_utc,event_time_raw,stage,substatus,description,location,event_time_iso
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, events)
        db.executemany("UPDATE packages SET state=?,checked_at=? WHERE tracking_no=?", update)
    return len(numbers), len(events), len(pending)


def counts(db):
    return dict(db.execute("SELECT state,count(*) FROM packages GROUP BY state").fetchall())


def run(args, token=None):
    with closing(sqlite3.connect(args.output, timeout=30)) as db:
        seed_queue(db, args.facts, args.since, args.through, all_waybills=args.all_waybills)
        count = counts(db)
        print(f"Local history {sum(count.values()):,} packages; current {count}", flush=True)
        if not count.get("pending") and db.execute("SELECT 1 FROM metadata WHERE key='finished'").fetchone():
            return
        client = ReadOnlyClient(token or token_of(args), args.rate)
        quota_before = client.read("getquota", {}).get("quota_used")
        print(f"Starting history queries {args.since} to {args.through}; quota snapshot recorded", flush=True)
        cutoff = max(args.since, args.through - timedelta(days=105)).isoformat()
        through = args.through.isoformat()
        with db:
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('quota_used_at_start',?)", [str(quota_before)])
        completed = events = errors = 0
        last_log = time.monotonic()
        retry_rounds = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as workers:
            while True:
                limit = REGISTRY_BATCH * WORKERS
                if args.limit:
                    limit = min(limit, args.limit - completed)
                if limit <= 0:
                    break
                rows = db.execute("""
                    SELECT tracking_no FROM packages WHERE state='pending' AND signed_date BETWEEN ? AND ?
                    ORDER BY signed_date,tracking_no LIMIT ?
                """, [cutoff, through, limit]).fetchall()
                if not rows:
                    rows = db.execute("""
                        SELECT tracking_no FROM packages WHERE state='pending' AND signed_date>=? AND signed_date<?
                        ORDER BY signed_date,tracking_no LIMIT ?
                    """, [args.since.isoformat(), cutoff, limit]).fetchall()
                if not rows:
                    break
                jobs = [workers.submit(fetch_batch, client, group)
                        for group in chunks([row[0] for row in rows], REGISTRY_BATCH)]
                failure = None
                transient = None
                completed_before_round = completed
                for job in as_completed(jobs):
                    try:
                        n, added, missing = persist(db, job.result())
                    except RetryableRequestError as error:
                        transient = error
                        continue
                    except Exception as error:
                        failure = error
                        continue
                    completed += n
                    events += added
                    errors += missing
                    now = time.monotonic()
                    if completed == n or now - last_log > 30:
                        print(f"{utc_now()} checked={completed:,} events={events:,} status={counts(db)}", flush=True)
                        last_log = now
                if failure:
                    raise failure
                if transient:
                    retry_rounds = 1 if completed > completed_before_round else retry_rounds + 1
                    delay = min(60, 5 * 2 ** min(retry_rounds - 1, 4))
                    print(f"{utc_now()} temporary API error ({transient}); "
                          f"retrying remaining batches in {delay}s", flush=True)
                    client.pause(delay)
                else:
                    retry_rounds = 0
        quota_after = client.read("getquota", {}).get("quota_used")
        delta = quota_after - quota_before if type(quota_after) is int and type(quota_before) is int else None
        with db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('quota_used_at_last_exit',?)", [str(quota_after)])
            if not counts(db).get("pending"):
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('finished',?)", [utc_now()])
        print(f"Finished; account quota-used delta={delta}; status={counts(db)}; "
              f"this run events={events:,}; unresolved responses={errors:,}", flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--since", type=date.fromisoformat, required=True)
    parser.add_argument("--through", type=date.fromisoformat, required=True)
    parser.add_argument("--rate", type=float, default=2.5)
    parser.add_argument("--limit", type=int, default=0, help="Only check this many queued numbers in this run")
    parser.add_argument("--all-waybills", action="store_true",
                        help="Check any waybill lacking detailed events, regardless of ERP status")
    args = parser.parse_args()
    if args.since > args.through or not 0 < args.rate <= 3 or args.limit < 0:
        parser.error("Check the date range, rate (0–3 requests/s), and limit")
    return args


if __name__ == "__main__":
    run(parse_args())
