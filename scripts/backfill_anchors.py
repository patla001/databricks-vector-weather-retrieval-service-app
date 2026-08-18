#!/usr/bin/env python3
"""
Re-anchor documents harvested before geo_source existed.

Alerts written by earlier builds carry the coordinates of whichever city
requested them, so every alert in a state sits on one dot. New harvests fix
themselves - the id is stable, so an upsert overwrites the bad coordinates - but
an alert that has already gone inactive is never harvested again and would keep
its wrong position until it expired out of the corpus.

Nothing here calls NWS for the alert itself: the full GeoJSON feature is already
in the payload column, so the polygon and the affectedZones list are on hand.
Only zone centroids that the cache has never seen need the network.

    python scripts/backfill_anchors.py            # report only
    python scripts/backfill_anchors.py --apply    # write
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import lakebase  # noqa: E402
import weather_pipeline  # noqa: E402
import weather_zones  # noqa: E402
from weather_client import WeatherClient  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write the new anchors. Without it, only reports.")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--budget", type=int, default=None,
                        help="Max never-seen zones to fetch (default: module default).")
    args = parser.parse_args(argv)

    table = weather_pipeline.DEFAULT_DOCUMENTS_TABLE
    rows = lakebase.run_query(
        f"""
        SELECT id, latitude, longitude, payload
        FROM {table}
        WHERE source_type = 'alert' AND geo_source IS NULL
        ORDER BY issued_at DESC NULLS LAST
        LIMIT %(limit)s
        """,
        {"limit": args.limit},
    )
    print(f"alert rows without an anchor source: {len(rows)}")
    if not rows:
        return 0

    client = WeatherClient()
    resolver = weather_zones.ZoneCentroids(client, budget=args.budget, log=print)

    updates: list[tuple] = []
    tally: dict[str, int] = {}
    moved = 0
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            continue

        lat, lon, source = WeatherClient.alert_anchor(
            payload,
            zone_resolver=resolver,
            fallback=(row["latitude"], row["longitude"])
            if row["latitude"] is not None else None,
        )
        tally[source or "none"] = tally.get(source or "none", 0) + 1
        if lat is not None and (
            row["latitude"] is None or abs(float(row["latitude"]) - lat) > 1e-4
            or abs(float(row["longitude"]) - lon) > 1e-4
        ):
            moved += 1
        updates.append((lat, lon, source, row["id"]))

    print(f"resolved: {tally}")
    print(f"would move: {moved} of {len(updates)}")
    print(f"zones: {resolver.stats()}")

    if not args.apply:
        print("\ndry run - pass --apply to write")
        return 0

    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            for lat, lon, source, doc_id in updates:
                cur.execute(
                    f"UPDATE {table} SET latitude=%s, longitude=%s, geo_source=%s "
                    f"WHERE id=%s",
                    (lat, lon, source, doc_id),
                )
        conn.commit()
    print(f"\nupdated {len(updates)} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
