"""Export the ten highest-volume markets per category without API requests."""
import argparse
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import heapq
from pathlib import Path
import re


def export(source, output):
    groups = {}
    counts = {}
    with source.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        required = {"category", "title", "volume_usd"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("Input must be the collector's polymarket_markets.csv with category, title, volume_usd")
        for position, row in enumerate(reader):
            raw = row["volume_usd"]
            if not raw:
                continue
            try:
                volume = Decimal(raw)
            except InvalidOperation as error:
                raise ValueError(f"Invalid volume on CSV record {position + 1}") from error
            if not volume.is_finite() or volume <= 0:
                continue
            category = row["category"] or "Unknown"
            counts[category] = counts.get(category, 0) + 1
            heap = groups.setdefault(category, [])
            # Keep only ten rows per category in memory, even for millions of markets.
            item = (volume, -position, row["title"])
            if len(heap) < 10:
                heapq.heappush(heap, item)
            elif item > heap[0]:
                heapq.heapreplace(heap, item)
            if (position + 1) % 100_000 == 0:
                print(f"Scanned {position + 1:,} markets", flush=True)
    # Finish reading successfully before replacing any existing exports.
    output.mkdir(parents=True, exist_ok=True)
    text = ["TOP 10 MARKETS PER CATEGORY BY REPORTED LIFETIME USD VOLUME",
            "Source: collector market CSV; includes collected open and closed markets.",
            "This ranks trading activity, not individual bets, payouts, or profits.", ""]
    used = set()
    for category in sorted(groups, key=str.casefold):
        slug = re.sub(r"[^a-z0-9]+", "_", category.casefold()).strip("_") or "unclassified"
        if slug in used:
            slug += "_" + hashlib.sha256(category.encode()).hexdigest()[:8]
        used.add(slug)
        rows = sorted(groups[category], reverse=True)
        target = output / (slug + ".csv")
        temp = target.with_suffix(".csv.tmp")
        with temp.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(["title", "volume_usd"])
            writer.writerows((title, str(volume)) for volume, _, title in rows)
        temp.replace(target)
        text.extend([category.strip() or "Unknown", "-" * 60])
        for volume, _, title in rows:
            text.extend([title, f"Volume: ${volume:,.2f}", ""])
    report = output.parent / "top10_by_category.txt"
    temp = report.with_suffix(".txt.tmp")
    temp.write_text("\n".join(text), encoding="utf-8")
    temp.replace(report)
    print(f"Exported {len(groups)} categories, {sum(len(v) for v in groups.values())} markets to {output}")
    print(f"Readable list: {report}")
    return groups


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=root / "data" / "polymarket_markets.csv")
    parser.add_argument("--output-dir", type=Path, default=root / "reports" / "top10_by_category")
    args = parser.parse_args()
    export(args.input, args.output_dir)


if __name__ == "__main__":
    main()
