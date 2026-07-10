"""Read the Voice Pro safety gates. This script never changes campaigns or fulfilment."""

from __future__ import annotations

import argparse
import sys
import urllib.parse
from typing import Any

from ops_common import (
    FULFILMENT,
    REAL_CAMPAIGN_IDS,
    SEEDED_LEAD_ID,
    SEEDED_PHONE,
    SENDKIT,
    TEST_CAMPAIGN_ID,
    OpsError,
    kill_paused,
    request_json,
    user_env,
)


def seeded_lead_phone() -> str:
    """Read the seeded lead's STORED phone from SendKit — the number the dialer will
    actually call. Proving this equals the seed (not just that a constant equals itself)
    is what closes 'prove the destination before dialing'."""
    status, payload = request_json(
        f"{SENDKIT}/v1/leads/{SEEDED_LEAD_ID}",
        headers={"X-Api-Key": user_env("SENDKIT_WORKSPACE_API_KEY")},
    )
    if status != 200:
        raise OpsError(f"SendKit lead read returned HTTP {status}")
    lead = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(lead, dict):
        lead = payload if isinstance(payload, dict) else {}
    return str(lead.get("phoneNumber") or lead.get("phone") or "").strip()


def campaign_rows(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("campaigns", "items", "rows"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    for key in ("campaigns", "items", "rows"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def list_campaigns() -> list[dict[str, Any]]:
    url = f"{SENDKIT}/v1/campaigns?{urllib.parse.urlencode({'limit': 100})}"
    status, payload = request_json(
        url,
        headers={"X-Api-Key": user_env("SENDKIT_WORKSPACE_API_KEY")},
    )
    if status != 200:
        raise OpsError(f"SendKit campaign list returned HTTP {status}")
    rows = campaign_rows(payload)
    if not rows:
        raise OpsError("SendKit campaign list returned no readable campaigns")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-safe", action="store_true")
    args = parser.parse_args()

    status, health = request_json(f"{FULFILMENT}/health")
    stub = (
        status == 200 and isinstance(health, dict) and health.get("stub_mode") is True
    )
    print(f"fulfilment_http={status} stub_mode={str(stub).lower()}")

    campaigns = list_campaigns()
    by_id = {}
    test = None
    for campaign in campaigns:
        campaign_id = str(campaign.get("_id") or campaign.get("id") or "")
        name = str(campaign.get("name") or "<unnamed>")
        campaign_status = str(campaign.get("status") or "unknown").lower()
        if campaign_id == TEST_CAMPAIGN_ID or name == "ZZ-TEST Dry Run":
            test = (campaign_id, name, campaign_status)
            continue
        by_id[campaign_id] = (campaign_id, name, campaign_status)

    real = [
        by_id[campaign_id] for campaign_id in REAL_CAMPAIGN_IDS if campaign_id in by_id
    ]
    missing_real = [
        campaign_id for campaign_id in REAL_CAMPAIGN_IDS if campaign_id not in by_id
    ]

    for campaign_id, name, campaign_status in sorted(real, key=lambda row: row[1]):
        print(f"real_campaign id={campaign_id} status={campaign_status} name={name}")
    if test:
        print(f"test_campaign id={test[0]} status={test[2]} name={test[1]}")

    paused = kill_paused()
    print(f"kill_switch_on={str(paused).lower()}")
    try:
        lead_phone = seeded_lead_phone()
    except OpsError:
        lead_phone = ""
    destination_valid = SEEDED_PHONE == "+963998183191" and lead_phone == SEEDED_PHONE
    print(f"seeded_destination_valid={str(destination_valid).lower()}")

    campaign_safe = (
        not missing_real
        and len(real) == len(REAL_CAMPAIGN_IDS)
        and all(row[2] == "draft" for row in real)
    )
    safe = stub and paused and campaign_safe and destination_valid
    print(
        f"safe={str(safe).lower()} real_campaign_count={len(real)} "
        f"missing_real_campaigns={len(missing_real)} all_real_draft={str(campaign_safe).lower()}"
    )
    if args.require_safe and not safe:
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OpsError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
