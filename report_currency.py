"""Format report CSV money columns as USD; keep raw collector data numeric."""
import argparse
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import tempfile

MONEY_COLUMNS = {"dollar_volume", "volume_usd", "avg_per_market", "rolling_6mo_avg"}


def money_columns(filename, columns):
    if Path(filename).name == "category_yearly.csv":
        return {name for name in columns if str(name).isdigit() and len(str(name)) == 4}
    return set(columns) & MONEY_COLUMNS


def parse_money(value):
    if value is None:
        return None
    text = str(value).strip().replace("$", "").replace(",", "")
    if not text or text.casefold() in {"nan", "n/a", "na", "none"}:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"Invalid monetary value: {value!r}") from error
    if not number.is_finite():
        raise ValueError(f"Non-finite monetary value: {value!r}")
    return number


def format_usd(value):
    number = parse_money(value)
    if number is None:
        return ""
    number = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return ("-" if number < 0 else "") + f"${abs(number):,.2f}"


def write_report_frame(frame, path):
    display = frame.copy()
    for column in money_columns(path, display.columns):
        display[column] = display[column].map(format_usd)
    display.to_csv(path)


def read_report_csv(path, **kwargs):
    """Read either numeric or currency-formatted reports into numeric pandas columns."""
    import pandas as pd
    frame = pd.read_csv(path, **kwargs)
    for column in money_columns(path, frame.columns):
        frame[column] = frame[column].map(
            lambda value: float(amount) if (amount := parse_money(value)) is not None else float("nan"))
    return frame


def format_file(path):
    temp_path = None
    try:
        with path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            fields = reader.fieldnames
            columns = money_columns(path, fields or [])
            if not columns:
                return False
            with tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8", dir=path.parent,
                                             prefix=path.name + ".", suffix=".tmp", delete=False) as target:
                temp_path = Path(target.name)
                writer = csv.DictWriter(target, fieldnames=fields, lineterminator="\n")
                writer.writeheader()
                changed = False
                for row in reader:
                    for column in columns:
                        value = format_usd(row[column])
                        changed |= value != row[column]
                        row[column] = value
                    writer.writerow(row)
        if changed:
            temp_path.replace(path)
        return changed
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", type=Path, default=Path(__file__).resolve().parent / "reports")
    args = parser.parse_args()
    changed = 0
    files = sorted(args.reports_dir.rglob("*.csv"))
    for path in files:
        if format_file(path):
            changed += 1
            print(f"Formatted {path.relative_to(args.reports_dir)}", flush=True)
    print(f"Updated {changed} of {len(files)} report CSVs; non-money fields and raw data left unchanged.")


if __name__ == "__main__":
    main()
