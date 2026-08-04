#!/usr/bin/env python3
"""Audita cobertura e eventos locais em um intervalo de tempo."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def fmt(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and a_end > b_start


def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def find_gaps(
    start: datetime,
    end: datetime,
    covered: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    clipped = []
    for item_start, item_end in covered:
        if overlap(item_start, item_end, start, end):
            clipped.append((max(start, item_start), min(end, item_end)))

    merged = merge_intervals(clipped)
    gaps: list[tuple[datetime, datetime]] = []
    cursor = start
    for item_start, item_end in merged:
        if item_start > cursor:
            gaps.append((cursor, item_start))
        cursor = max(cursor, item_end)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", default="hubspot_eventos")
    parser.add_argument("--after", required=True)
    parser.add_argument("--before", required=True)
    args = parser.parse_args()

    base = Path(args.directory).expanduser().resolve()
    target_start = parse_dt(args.after)
    target_end = parse_dt(args.before)

    if target_start >= target_end:
        raise SystemExit("--after deve ser anterior a --before")
    if not base.exists():
        raise SystemExit(f"Pasta não encontrada: {base}")

    print("=" * 100)
    print("INTERVALO AUDITADO (UTC)")
    print(f"after : {fmt(target_start)}")
    print(f"before: {fmt(target_end)}")
    print(f"pasta : {base}")

    complete_windows: dict[str, list[tuple[datetime, datetime]]] = {}
    overlapping_manifests = 0

    print("\n" + "=" * 100)
    print("MANIFESTOS QUE SE SOBREPÕEM AO INTERVALO")

    for path in sorted((base / "_runs").glob("*.json")):
        manifest = load_json(path)
        if not manifest:
            continue
        window = manifest.get("window") or {}
        raw_start = window.get("occurred_after")
        raw_end = window.get("occurred_before")
        if not raw_start or not raw_end:
            continue
        try:
            run_start = parse_dt(raw_start)
            run_end = parse_dt(raw_end)
        except ValueError:
            continue
        if not overlap(run_start, run_end, target_start, target_end):
            continue

        overlapping_manifests += 1
        print(
            f"\n{path.name}\n"
            f"  run_type={manifest.get('run_type')} | status={manifest.get('status')}\n"
            f"  janela={fmt(run_start)} -> {fmt(run_end)}"
        )

        for event_type, state in (manifest.get("event_types") or {}).items():
            status = state.get("status")
            total = state.get("total_events", 0)
            output = state.get("output_file")
            print(f"    {event_type}: {status} | {total} evento(s) | {output}")
            if status == "complete":
                complete_windows.setdefault(event_type, []).append((run_start, run_end))

    if overlapping_manifests == 0:
        print("Nenhum manifesto local cobre ou toca esse intervalo.")

    print("\n" + "=" * 100)
    print("COBERTURA DECLARADA COMO COMPLETE, POR TIPO DE EVENTO")

    all_types = sorted(complete_windows)
    if not all_types:
        print("Nenhum tipo possui run completa sobreposta ao intervalo.")
    else:
        for event_type in all_types:
            gaps = find_gaps(target_start, target_end, complete_windows[event_type])
            if not gaps:
                print(f"{event_type}: COBERTURA COMPLETA")
            else:
                print(f"{event_type}: possui {len(gaps)} gap(s) de cobertura")
                for gap_start, gap_end in gaps:
                    print(f"  GAP: {fmt(gap_start)} -> {fmt(gap_end)}")

    print("\n" + "=" * 100)
    print("EVENTOS EFETIVAMENTE ENCONTRADOS NOS JSONL")

    file_rows: list[tuple[str, int, str, str]] = []
    type_counts: Counter[str] = Counter()
    unique_keys: set[str] = set()
    total_matches = 0

    for path in sorted(base.rglob("*.jsonl")):
        count = 0
        local_min: datetime | None = None
        local_max: datetime | None = None
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        occurred_at = event.get("occurredAt")
                        if not occurred_at:
                            continue
                        occurred = parse_dt(occurred_at)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    # Mesma lógica conceitual de occurredAfter/occurredBefore: bordas abertas.
                    if not (target_start < occurred < target_end):
                        continue

                    count += 1
                    total_matches += 1
                    local_min = occurred if local_min is None else min(local_min, occurred)
                    local_max = occurred if local_max is None else max(local_max, occurred)
                    event_type = str(event.get("eventType") or "sem_eventType")
                    type_counts[event_type] += 1
                    event_id = event.get("id")
                    if event_id:
                        unique_keys.add(f"id:{event_id}")
                    else:
                        unique_keys.add(
                            f"fallback:{path}:{line_number}:{occurred_at}:{event_type}"
                        )
        except OSError as exc:
            print(f"ERRO ao ler {path}: {exc}")
            continue

        if count:
            file_rows.append((str(path.relative_to(base)), count, fmt(local_min), fmt(local_max)))

    if not file_rows:
        print("Nenhum evento foi encontrado nos JSONL dentro do intervalo.")
    else:
        for filename, count, local_min, local_max in file_rows:
            print(f"{filename}\n  {count} evento(s) | min={local_min} | max={local_max}")

    print("\nRESUMO")
    print(f"Ocorrências encontradas (incluindo possíveis duplicatas): {total_matches}")
    print(f"Chaves únicas encontradas: {len(unique_keys)}")
    for event_type, count in sorted(type_counts.items()):
        print(f"  {event_type}: {count}")

    print("\nINTERPRETAÇÃO")
    print("- Manifesto COMPLETE sem gaps: o script afirma que consultou toda a janela.")
    print("- Eventos nos JSONL: há dados locais que podem ter faltado no upload ao Supabase.")
    print("- Sem eventos, mas com cobertura COMPLETE: pode ser uma janela realmente sem eventos;")
    print("  confirme comparando contagens por tipo no Supabase.")
    print("- Gap de cobertura nos manifestos: a janela não foi integralmente extraída localmente.")


if __name__ == "__main__":
    main()
