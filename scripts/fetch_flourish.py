import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

SOURCE_URL = "https://www.whitehouse.gov/wp-content/themes/whitehouse/static-assets/flourish/flourish-geo-embed/map/index.html"

ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / "data" / "latest"
ARCHIVE = ROOT / "data" / "archive"


def fetch_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def extract_js_object(html: str, var_name: str) -> str:
    start = html.find(f"var {var_name}")
    if start == -1:
        start = html.find(f"{var_name} =")
    if start == -1:
        raise ValueError(f"{var_name} not found")

    eq = html.find("=", start)
    i = html.find("{", eq)

    depth = 0
    in_str = None
    escape = False

    for j in range(i, len(html)):
        ch = html[j]

        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == in_str:
                in_str = None
            continue

        if ch in ("'", '"'):
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[i:j + 1]

    raise ValueError(f"Could not parse {var_name}")


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def get_events(data: dict):
    events = data.get("events")

    if isinstance(events, list):
        return events

    if isinstance(events, dict):
        for key in ("data", "rows", "values"):
            if isinstance(events.get(key), list):
                return events[key]

    raise ValueError(f"Could not find events table. Top-level keys: {list(data.keys())}")


def clean_events(events):
    cleaned = []

    for row in events:
        metadata = row.get("metadata") or []

        cleaned.append({
            "neighborhood": row.get("name"),
            "latitude": row.get("lat"),
            "longitude": row.get("lon"),
            "total_arrests": metadata[0] if len(metadata) > 0 else row.get("scale"),
            "dates_of_arrest": metadata[1] if len(metadata) > 1 else None,
            "criminal_charges": metadata[2] if len(metadata) > 2 else None,
            "countries_of_origin": metadata[3] if len(metadata) > 3 else None,
            "gang_affiliation": metadata[4] if len(metadata) > 4 else None,
        })

    return cleaned


def arrest_count(row) -> int:
    try:
        return int(str(row.get("total_arrests") or "0").replace(",", ""))
    except ValueError:
        return 0


def make_michigan_diff_summary(previous_rows, current_rows):
    def key(row):
        return (
            row.get("neighborhood") or "",
            row.get("latitude") or "",
            row.get("longitude") or "",
        )

    def is_michigan(row):
        value = row.get("neighborhood") or ""
        return value.strip().endswith(", MI")

    previous = {key(r): r for r in previous_rows if is_michigan(r)}
    current = {key(r): r for r in current_rows if is_michigan(r)}

    added_keys = current.keys() - previous.keys()
    removed_keys = previous.keys() - current.keys()
    shared_keys = current.keys() & previous.keys()

    changed = []
    for k in shared_keys:
        before = previous[k]
        after = current[k]

        if before != after:
            changed.append({
                "city": after.get("neighborhood"),
                "previous_total_arrests": before.get("total_arrests"),
                "current_total_arrests": after.get("total_arrests"),
                "previous": before,
                "current": after,
            })

    return {
        "michigan_added": sorted(
            [current[k] for k in added_keys],
            key=arrest_count,
            reverse=True,
        ),
        "michigan_removed": sorted(
            [previous[k] for k in removed_keys],
            key=arrest_count,
            reverse=True,
        ),
        "michigan_changed": sorted(
            changed,
            key=lambda r: arrest_count({"total_arrests": r.get("current_total_arrests")}),
            reverse=True,
        ),
        "michigan_current_top_cities": sorted(
            current.values(),
            key=arrest_count,
            reverse=True,
        ),
    }


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def write_csv(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main():
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y-%m-%dT%H-%M-%SZ")

    html = fetch_html(SOURCE_URL)
    raw_js = extract_js_object(html, "_Flourish_data")
    data = json.loads(raw_js)

    events = clean_events(get_events(data))

    raw_hash = sha256_text(json.dumps(data, sort_keys=True, ensure_ascii=False))
    clean_hash = sha256_text(json.dumps(events, sort_keys=True, ensure_ascii=False))

    old_meta_path = LATEST / "metadata.json"
    old_events_path = LATEST / "events_clean.json"

    old_hash = None
    previous_events = []

    if old_meta_path.exists():
        old_hash = json.loads(old_meta_path.read_text(encoding="utf-8")).get("clean_hash")

    if old_events_path.exists():
        previous_events = json.loads(old_events_path.read_text(encoding="utf-8"))

    changed = clean_hash != old_hash

    metadata = {
        "source_url": SOURCE_URL,
        "fetched_at_utc": now.isoformat(),
        "raw_hash": raw_hash,
        "clean_hash": clean_hash,
        "row_count": len(events),
        "changed": changed,
    }

    if changed:
        snapshot_dir = ARCHIVE / run_id
        diff_summary = make_michigan_diff_summary(previous_events, events)
        metadata["michigan_diff_summary"] = diff_summary

        write_json(snapshot_dir / "flourish_data_raw.json", data)
        write_json(snapshot_dir / "events_clean.json", events)
        write_csv(snapshot_dir / "events_clean.csv", events)
        write_json(snapshot_dir / "metadata.json", metadata)
        write_json(snapshot_dir / "michigan_diff_summary.json", diff_summary)

        write_json(LATEST / "flourish_data_raw.json", data)
        write_json(LATEST / "events_clean.json", events)
        write_csv(LATEST / "events_clean.csv", events)
        write_json(LATEST / "metadata.json", metadata)
        write_json(LATEST / "michigan_diff_summary.json", diff_summary)

        print(f"Changed: archived {len(events)} rows to {snapshot_dir}")
    else:
        print("No change detected")


if __name__ == "__main__":
    main()
