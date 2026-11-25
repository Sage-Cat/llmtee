#!/usr/bin/env python3
"""Compare UTC daily OpenAI charges for one API key with llmtee estimates."""
import argparse
from contextlib import closing
import csv
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

DAY_SECONDS = 86_400
COSTS_URL = "https://api.openai.com/v1/organization/costs"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def utc_start(day):
    return int(datetime.combine(day, time.min, timezone.utc).timestamp())


def provider_costs(start, end, api_key_id, admin_key):
    query = [
        ("start_time", start),
        ("end_time", end),
        ("bucket_width", "1d"),
        ("group_by", "api_key_id"),
        ("api_key_ids", api_key_id),
        ("limit", 180),
    ]
    opener = build_opener(NoRedirect())
    totals = {}
    seen_pages = set()
    while True:
        request = Request(f"{COSTS_URL}?{urlencode(query)}",
                          headers={"Authorization": f"Bearer {admin_key}"})
        with opener.open(request, timeout=30) as response:
            page = json.load(response, parse_float=Decimal)
        for bucket in page["data"]:
            day = bucket["start_time"]
            for result in bucket["results"]:
                if result.get("api_key_id") != api_key_id:
                    raise ValueError("OpenAI returned a cost without the requested API key attribution")
                amount = result["amount"]
                if amount["currency"].lower() != "usd":
                    raise ValueError("OpenAI returned a cost in a currency other than USD")
                totals[day] = totals.get(day, Decimal(0)) + Decimal(amount["value"])
        if not page["has_more"]:
            return totals
        cursor = page["next_page"]
        if not cursor or cursor in seen_pages:
            raise ValueError("Invalid pagination in OpenAI Costs response")
        seen_pages.add(cursor)
        query = [item for item in query if item[0] != "page"]
        query.append(("page", cursor))


def local_costs(db_path, agents, start, end):
    with closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        names = {name for name, in db.execute("SELECT name FROM keys")}
        if not set(agents) <= names:
            raise ValueError("One or more requested agent names are missing from llmtee keys")
        placeholders = ",".join("?" for _ in agents)
        rows = db.execute(
            f"""SELECT ts / 86400000 * 86400, COALESCE(SUM(cost_usd), 0), COUNT(*), COUNT(*) - COUNT(cost_usd)
                FROM requests JOIN keys ON keys.id = requests.key_id
                WHERE keys.name IN ({placeholders}) AND ts >= ? AND ts < ?
                GROUP BY ts / 86400000""",
            (*agents, start * 1000, end * 1000),
        )
        return {day: (Decimal(str(estimates)), requests, unpriced) for day, estimates, requests, unpriced in rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--api-key-id", required=True, help="OpenAI API key ID used for billing attribution")
    parser.add_argument("--agent", required=True, action="append", help="llmtee key name, repeat for a shared API key")
    parser.add_argument("--start", required=True, type=date.fromisoformat, help="First UTC day, inclusive")
    parser.add_argument("--end", required=True, type=date.fromisoformat, help="Last UTC day, exclusive")
    parser.add_argument("--admin-key-file", type=Path, help="Read the admin key here instead of OPENAI_ADMIN_KEY")
    args = parser.parse_args()
    if not args.start < args.end <= datetime.now(timezone.utc).date():
        parser.error("choose a range of complete UTC days")
    try:
        admin_key = args.admin_key_file.read_text().strip() if args.admin_key_file else os.environ.get("OPENAI_ADMIN_KEY")
    except (OSError, UnicodeError):
        parser.error("could not read the OpenAI admin key file")
    if not admin_key:
        parser.error("provide an OpenAI admin key via OPENAI_ADMIN_KEY or --admin-key-file")

    start, end = utc_start(args.start), utc_start(args.end)
    try:
        local = local_costs(args.db, args.agent, start, end)
        billed = provider_costs(start, end, args.api_key_id, admin_key)
    except HTTPError as error:
        return sys.exit(f"OpenAI Costs API returned HTTP {error.code}")
    except URLError:
        return sys.exit("Could not connect to the OpenAI Costs API")
    except (ValueError, sqlite3.Error) as error:
        return sys.exit(str(error))

    writer = csv.writer(sys.stdout)
    writer.writerow(["date_utc", "requests", "unpriced_requests", "llmtee_estimated_usd",
                     "openai_usd", "openai_minus_estimated_usd"])
    for offset in range((args.end - args.start).days):
        day = start + offset * DAY_SECONDS
        estimate, requests, unpriced = local.get(day, (Decimal(0), 0, 0))
        charged = billed.get(day, Decimal(0))
        writer.writerow([args.start + timedelta(days=offset), requests, unpriced, f"{estimate:.6f}",
                         f"{charged:.6f}", f"{charged - estimate:.6f}"])


if __name__ == "__main__":
    main()
