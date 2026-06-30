#!/usr/bin/env python3
"""
Consolida os três tipos de eventos de formulário extraídos do HubSpot:

- e_form_submission_v2: evento-base; cada evento gera uma submissão consolidada.
- e_submitted_form: enriquece com URL/título da página e outros campos.
- e_form_submission_metadata_v2: enriquece com título do formulário e lifecycle stage.

O script NÃO consulta a API e NÃO envia dados ao Supabase. Ele lê os JSONL
brutos e os manifestos produzidos por hubspot_eventos_daily_historical_retry.py.

Saídas por run:
- forms_consolidated: uma linha por e_form_submission_v2;
- unmatched_submitted_form: eventos e_submitted_form sem par na run;
- unmatched_metadata: eventos metadata sem par na run;
- consolidation_report: contagens e validações da consolidação.

Por padrão, somente runs em que os três tipos de forms estão com status
"complete" podem ser consolidadas.

Uso interativo:
    python consolidate_hubspot_forms.py

Consolidar uma run específica:
    python consolidate_hubspot_forms.py --run-id daily__after_...__before_...

Consolidar todas as runs prontas ainda não processadas:
    python consolidate_hubspot_forms.py --all-ready

Refazer uma consolidação existente:
    python consolidate_hubspot_forms.py --run-id RUN_ID --force
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs


# -----------------------------------------------------------------------------
# CONFIGURAÇÃO
# -----------------------------------------------------------------------------
OUTPUT_DIR = Path(
    "hubspot_eventos"
)

RUNS_DIR_NAME = "_runs"
CONSOLIDATED_DIR_NAME = "_consolidated/forms"
CONSOLIDATION_VERSION = "1.0.0"

FORM_EVENT_TYPES = {
    "e_submitted_form",
    "e_form_submission_v2",
    "e_form_submission_metadata_v2",
}

BASE_EVENT_TYPE = "e_form_submission_v2"
SUBMITTED_EVENT_TYPE = "e_submitted_form"
METADATA_EVENT_TYPE = "e_form_submission_metadata_v2"

# Distância máxima permitida entre eventos considerados parte da mesma submissão.
DEFAULT_MATCH_WINDOW_SECONDS = 60

# Ao consolidar uma run, também buscamos candidatos nos manifestos vizinhos
# dentro desta janela. Isso reduz perdas quando os três eventos caem em lados
# diferentes da fronteira entre duas runs.
DEFAULT_BOUNDARY_LOOKAROUND_SECONDS = 60


# -----------------------------------------------------------------------------
# HELPERS GERAIS
# -----------------------------------------------------------------------------
def utc_now_seconds() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_utc_iso_seconds(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_iso_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")

    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())

    temporary_path.replace(path)


def atomic_write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    count = 0

    with temporary_path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(
                json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
            count += 1

        file.flush()
        os.fsync(file.fileno())

    temporary_path.replace(path)
    return count


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def normalize_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def event_key(event: dict[str, Any]) -> str:
    event_id = normalize_id(event.get("id"))

    if event_id:
        return f"id:{event_id}"

    core = {
        "eventType": event.get("eventType"),
        "objectId": event.get("objectId"),
        "objectType": event.get("objectType"),
        "occurredAt": event.get("occurredAt"),
        "properties": event.get("properties"),
    }
    raw = json.dumps(core, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def event_datetime(event: dict[str, Any]) -> Optional[datetime]:
    value = event.get("occurredAt")

    if not value:
        return None

    try:
        return parse_iso_utc(str(value))
    except (TypeError, ValueError):
        return None


def event_properties(event: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not event:
        return {}

    properties = event.get("properties")
    return properties if isinstance(properties, dict) else {}


def event_contact_id(event: dict[str, Any]) -> Optional[str]:
    return normalize_id(event.get("objectId"))


def event_form_id(event: Optional[dict[str, Any]]) -> Optional[str]:
    properties = event_properties(event)
    return normalize_id(
        first_nonempty(
            properties.get("hs_form_id"),
            properties.get("form_id"),
        )
    )


def event_id(event: Optional[dict[str, Any]]) -> Optional[str]:
    if not event:
        return None
    return normalize_id(event.get("id"))


def signed_time_delta_seconds(
    base_event: dict[str, Any],
    candidate_event: Optional[dict[str, Any]],
) -> Optional[float]:
    if not candidate_event:
        return None

    base_dt = event_datetime(base_event)
    candidate_dt = event_datetime(candidate_event)

    if base_dt is None or candidate_dt is None:
        return None

    return (candidate_dt - base_dt).total_seconds()


def parse_attribution_from_query_params(query_params: Any) -> dict[str, Any]:
    if not query_params:
        return {}

    if isinstance(query_params, dict):
        parsed = query_params
    else:
        raw = str(query_params).lstrip("?")
        parsed = parse_qs(raw, keep_blank_values=True)

    def first_value(key: str) -> Any:
        value = parsed.get(key)
        if isinstance(value, list):
            return value[0] if value else None
        return value

    return {
        "utm_source": first_value("utm_source"),
        "utm_medium": first_value("utm_medium"),
        "utm_campaign": first_value("utm_campaign"),
        "utm_term": first_value("utm_term"),
        "utm_content": first_value("utm_content"),
        "hsa_acc": first_value("hsa_acc"),
        "hsa_cam": first_value("hsa_cam"),
        "hsa_grp": first_value("hsa_grp"),
        "hsa_ad": first_value("hsa_ad"),
        "hsa_src": first_value("hsa_src"),
    }


# -----------------------------------------------------------------------------
# LEITURA E DESCOBERTA DE MANIFESTOS
# -----------------------------------------------------------------------------
def discover_manifests(runs_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    manifests: list[tuple[Path, dict[str, Any]]] = []

    for path in runs_dir.glob("*.json"):
        try:
            manifest = load_json(path)
            window = manifest.get("window") or {}
            parse_iso_utc(window["occurred_after"])
            parse_iso_utc(window["occurred_before"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue

        manifests.append((path, manifest))

    manifests.sort(
        key=lambda item: parse_iso_utc(item[1]["window"]["occurred_after"])
    )
    return manifests


def forms_statuses(manifest: dict[str, Any]) -> dict[str, Optional[str]]:
    event_types = manifest.get("event_types") or {}
    return {
        event_type: (event_types.get(event_type) or {}).get("status")
        for event_type in FORM_EVENT_TYPES
    }


def manifest_ready_for_forms(manifest: dict[str, Any]) -> bool:
    statuses = forms_statuses(manifest)

    if not all(status == "complete" for status in statuses.values()):
        return False

    for event_type in FORM_EVENT_TYPES:
        output_file = (
            manifest.get("event_types", {}).get(event_type, {}).get("output_file")
        )

        if not output_file or not Path(output_file).exists():
            return False

    return True


def manifest_window(manifest: dict[str, Any]) -> tuple[datetime, datetime]:
    window = manifest["window"]
    return (
        parse_iso_utc(window["occurred_after"]),
        parse_iso_utc(window["occurred_before"]),
    )


def windows_overlap(
    first_after: datetime,
    first_before: datetime,
    second_after: datetime,
    second_before: datetime,
) -> bool:
    return first_after <= second_before and second_after <= first_before


def candidate_files_for_event_type(
    manifests: list[tuple[Path, dict[str, Any]]],
    event_type: str,
    target_after: datetime,
    target_before: datetime,
    lookaround_seconds: int,
) -> list[Path]:
    extended_after = target_after - timedelta(seconds=lookaround_seconds)
    extended_before = target_before + timedelta(seconds=lookaround_seconds)
    paths: list[Path] = []

    for _, manifest in manifests:
        source_after, source_before = manifest_window(manifest)

        if not windows_overlap(
            extended_after,
            extended_before,
            source_after,
            source_before,
        ):
            continue

        event_state = (manifest.get("event_types") or {}).get(event_type) or {}

        if event_state.get("status") != "complete":
            continue

        output_file = event_state.get("output_file")

        if output_file and Path(output_file).exists():
            paths.append(Path(output_file))

    # Remove caminhos repetidos sem alterar a ordem.
    return list(dict.fromkeys(paths))


# -----------------------------------------------------------------------------
# LEITURA DOS JSONL
# -----------------------------------------------------------------------------
def read_jsonl_events(
    paths: Iterable[Path],
    expected_event_type: str,
    occurred_after: Optional[datetime] = None,
    occurred_before: Optional[datetime] = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    events: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    stats = {
        "lines_read": 0,
        "invalid_json_lines": 0,
        "wrong_event_type": 0,
        "missing_occurred_at": 0,
        "outside_window": 0,
        "duplicates_removed": 0,
    }

    for path in paths:
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                stats["lines_read"] += 1
                line = line.strip()

                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    stats["invalid_json_lines"] += 1
                    continue

                if event.get("eventType") != expected_event_type:
                    stats["wrong_event_type"] += 1
                    continue

                occurred_at = event_datetime(event)

                if occurred_at is None:
                    stats["missing_occurred_at"] += 1
                    # Eventos-base sem timestamp ainda são preservados para auditoria.
                    if expected_event_type != BASE_EVENT_TYPE:
                        continue
                elif (
                    (occurred_after is not None and occurred_at < occurred_after)
                    or (occurred_before is not None and occurred_at > occurred_before)
                ):
                    stats["outside_window"] += 1
                    continue

                key = event_key(event)

                if key in seen_keys:
                    stats["duplicates_removed"] += 1
                    continue

                seen_keys.add(key)
                events.append(event)

    return events, stats


# -----------------------------------------------------------------------------
# MATCHING
# -----------------------------------------------------------------------------
def index_candidates_by_contact(
    events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for event in events:
        contact_id = event_contact_id(event)

        if contact_id:
            index[contact_id].append(event)

    for contact_events in index.values():
        contact_events.sort(
            key=lambda event: (
                event_datetime(event) or datetime.min.replace(tzinfo=timezone.utc),
                event_key(event),
            )
        )

    return dict(index)


def nearby_candidates(
    candidates: list[dict[str, Any]],
    base_dt: datetime,
    max_seconds: int,
) -> list[dict[str, Any]]:
    """Retorna somente candidatos temporalmente próximos usando bisect."""
    timestamps = [
        event_datetime(candidate) or datetime.min.replace(tzinfo=timezone.utc)
        for candidate in candidates
    ]

    lower = base_dt - timedelta(seconds=max_seconds)
    upper = base_dt + timedelta(seconds=max_seconds)
    start = bisect.bisect_left(timestamps, lower)
    end = bisect.bisect_right(timestamps, upper)
    return candidates[start:end]


def find_submitted_form_match(
    base_event: dict[str, Any],
    candidates_by_contact: dict[str, list[dict[str, Any]]],
    used_keys: set[str],
    max_seconds: int,
) -> Optional[dict[str, Any]]:
    contact_id = event_contact_id(base_event)
    base_dt = event_datetime(base_event)

    if not contact_id or base_dt is None:
        return None

    candidates = nearby_candidates(
        candidates_by_contact.get(contact_id, []),
        base_dt,
        max_seconds,
    )
    base_form_id = event_form_id(base_event)
    scored: list[tuple[int, float, datetime, str, dict[str, Any]]] = []

    for candidate in candidates:
        key = event_key(candidate)

        if key in used_keys:
            continue

        candidate_form_id = event_form_id(candidate)

        if base_form_id and candidate_form_id:
            if base_form_id != candidate_form_id:
                continue
            form_score = 0
        elif base_form_id or candidate_form_id:
            # Um lado não trouxe o form_id: permitimos como fallback.
            form_score = 1
        else:
            form_score = 2

        candidate_dt = event_datetime(candidate)

        if candidate_dt is None:
            continue

        difference = abs((candidate_dt - base_dt).total_seconds())
        scored.append((form_score, difference, candidate_dt, key, candidate))

    if not scored:
        return None

    scored.sort(key=lambda item: item[:4])
    return scored[0][4]


def find_metadata_match(
    base_event: dict[str, Any],
    candidates_by_contact: dict[str, list[dict[str, Any]]],
    used_keys: set[str],
    max_seconds: int,
) -> Optional[dict[str, Any]]:
    contact_id = event_contact_id(base_event)
    base_dt = event_datetime(base_event)

    if not contact_id or base_dt is None:
        return None

    candidates = nearby_candidates(
        candidates_by_contact.get(contact_id, []),
        base_dt,
        max_seconds,
    )
    scored: list[tuple[float, datetime, str, dict[str, Any]]] = []

    for candidate in candidates:
        key = event_key(candidate)

        if key in used_keys:
            continue

        candidate_dt = event_datetime(candidate)

        if candidate_dt is None:
            continue

        difference = abs((candidate_dt - base_dt).total_seconds())
        scored.append((difference, candidate_dt, key, candidate))

    if not scored:
        return None

    scored.sort(key=lambda item: item[:3])
    return scored[0][3]


def source_match_confidence(
    base_event: dict[str, Any],
    matched_event: Optional[dict[str, Any]],
    require_form_id: bool,
) -> str:
    if not matched_event:
        return "missing"

    delta = signed_time_delta_seconds(base_event, matched_event)

    if delta is None:
        return "low"

    absolute_delta = abs(delta)
    form_ids_equal = (
        event_form_id(base_event) is not None
        and event_form_id(base_event) == event_form_id(matched_event)
    )

    if absolute_delta <= 1 and (not require_form_id or form_ids_equal):
        return "high"

    if absolute_delta <= 5 and (not require_form_id or form_ids_equal):
        return "medium"

    return "low"


def overall_match_status(
    submitted_match: Optional[dict[str, Any]],
    metadata_match: Optional[dict[str, Any]],
) -> str:
    if submitted_match and metadata_match:
        return "complete"
    if submitted_match and not metadata_match:
        return "missing_metadata"
    if metadata_match and not submitted_match:
        return "missing_submitted_form"
    return "missing_both"


def overall_match_confidence(
    submitted_confidence: str,
    metadata_confidence: str,
) -> str:
    if "missing" in {submitted_confidence, metadata_confidence}:
        return "incomplete"

    ranking = {"high": 3, "medium": 2, "low": 1}
    minimum = min(
        ranking.get(submitted_confidence, 1),
        ranking.get(metadata_confidence, 1),
    )
    reverse = {3: "high", 2: "medium", 1: "low"}
    return reverse[minimum]


# -----------------------------------------------------------------------------
# CONSTRUÇÃO DA LINHA CONSOLIDADA
# -----------------------------------------------------------------------------
def build_consolidated_row(
    base_event: dict[str, Any],
    submitted_event: Optional[dict[str, Any]],
    metadata_event: Optional[dict[str, Any]],
    manifest: dict[str, Any],
    consolidated_at: str,
) -> dict[str, Any]:
    base_properties = event_properties(base_event)
    submitted_properties = event_properties(submitted_event)
    metadata_properties = event_properties(metadata_event)

    query_params = first_nonempty(
        base_properties.get("hs_query_params"),
        submitted_properties.get("hs_query_params"),
        metadata_properties.get("hs_query_params"),
    )
    query_attribution = parse_attribution_from_query_params(query_params)

    base_key = event_key(base_event)
    base_id = event_id(base_event)
    submission_id = base_id or base_key.removeprefix("hash:")

    submitted_delta = signed_time_delta_seconds(base_event, submitted_event)
    metadata_delta = signed_time_delta_seconds(base_event, metadata_event)

    submitted_confidence = source_match_confidence(
        base_event,
        submitted_event,
        require_form_id=True,
    )
    metadata_confidence = source_match_confidence(
        base_event,
        metadata_event,
        require_form_id=False,
    )

    window = manifest["window"]

    return {
        # Identificação principal
        "submission_id": submission_id,
        "contact_id": event_contact_id(base_event),
        "submitted_at": base_event.get("occurredAt"),
        "form_id": first_nonempty(
            base_properties.get("hs_form_id"),
            submitted_properties.get("hs_form_id"),
        ),
        # Formulário e contato no momento da submissão
        "form_title": first_nonempty(
            metadata_properties.get("hs_form_title"),
            base_properties.get("hs_form_title"),
        ),
        "form_type": first_nonempty(
            base_properties.get("hs_form_type"),
            submitted_properties.get("hs_form_type"),
            metadata_properties.get("hs_form_type"),
        ),
        "lifecyclestage": first_nonempty(
            metadata_properties.get("hs_contact_lifecyclestage"),
            base_properties.get("hs_contact_lifecyclestage"),
        ),
        "visitor_type": first_nonempty(
            base_properties.get("hs_visitor_type"),
            submitted_properties.get("hs_visitor_type"),
            metadata_properties.get("hs_visitor_type"),
        ),
        # Página e navegação
        "page_url": first_nonempty(
            submitted_properties.get("hs_url"),
            base_properties.get("hs_url"),
        ),
        "base_url": first_nonempty(
            base_properties.get("hs_base_url"),
            submitted_properties.get("hs_base_url"),
        ),
        "page_title": first_nonempty(
            submitted_properties.get("hs_title"),
            base_properties.get("hs_page_title"),
            submitted_properties.get("hs_page_title"),
        ),
        "referrer": first_nonempty(
            base_properties.get("hs_referrer"),
            submitted_properties.get("hs_referrer"),
            metadata_properties.get("hs_referrer"),
        ),
        "query_params": query_params,
        # UTMs e atribuição de anúncios
        "utm_source": first_nonempty(
            base_properties.get("hs_utm_source"),
            submitted_properties.get("hs_utm_source"),
            metadata_properties.get("hs_utm_source"),
            query_attribution.get("utm_source"),
        ),
        "utm_medium": first_nonempty(
            base_properties.get("hs_utm_medium"),
            submitted_properties.get("hs_utm_medium"),
            metadata_properties.get("hs_utm_medium"),
            query_attribution.get("utm_medium"),
        ),
        "utm_campaign": first_nonempty(
            base_properties.get("hs_utm_campaign"),
            submitted_properties.get("hs_utm_campaign"),
            metadata_properties.get("hs_utm_campaign"),
            query_attribution.get("utm_campaign"),
        ),
        "utm_term": query_attribution.get("utm_term"),
        "utm_content": query_attribution.get("utm_content"),
        "hsa_acc": query_attribution.get("hsa_acc"),
        "hsa_cam": query_attribution.get("hsa_cam"),
        "hsa_grp": query_attribution.get("hsa_grp"),
        "hsa_ad": query_attribution.get("hsa_ad"),
        "hsa_src": query_attribution.get("hsa_src"),
        "has_ad_attribution": bool(query_attribution.get("hsa_cam")),
        # Rastreabilidade dos eventos de origem
        "v2_event_id": event_id(base_event),
        "submitted_form_event_id": event_id(submitted_event),
        "metadata_event_id": event_id(metadata_event),
        "source_event_count": 1
        + int(submitted_event is not None)
        + int(metadata_event is not None),
        # Qualidade do pareamento
        "submitted_form_time_delta_seconds": submitted_delta,
        "metadata_time_delta_seconds": metadata_delta,
        "submitted_form_match_confidence": submitted_confidence,
        "metadata_match_confidence": metadata_confidence,
        "match_status": overall_match_status(submitted_event, metadata_event),
        "match_confidence": overall_match_confidence(
            submitted_confidence,
            metadata_confidence,
        ),
        # Janela e auditoria
        "request_run_id": manifest.get("run_id"),
        "request_run_type": manifest.get("run_type"),
        "request_occurred_after": window.get("occurred_after"),
        "request_occurred_before": window.get("occurred_before"),
        "extracted_at": first_nonempty(
            base_event.get("extracted_at"),
            manifest.get("extracted_at"),
        ),
        "consolidated_at": consolidated_at,
        "consolidation_version": CONSOLIDATION_VERSION,
    }


def build_unmatched_row(
    event: dict[str, Any],
    event_type: str,
    target_after: datetime,
    target_before: datetime,
    match_window_seconds: int,
    manifest: dict[str, Any],
    consolidated_at: str,
) -> dict[str, Any]:
    occurred_at = event_datetime(event)
    boundary = False

    if occurred_at is not None:
        boundary = (
            abs((occurred_at - target_after).total_seconds())
            <= match_window_seconds
            or abs((target_before - occurred_at).total_seconds())
            <= match_window_seconds
        )

    reason = "boundary_unresolved" if boundary else "no_v2_match_in_run"

    return {
        "event_id": event_id(event),
        "event_key": event_key(event),
        "event_type": event_type,
        "contact_id": event_contact_id(event),
        "occurred_at": event.get("occurredAt"),
        "form_id": event_form_id(event),
        "unmatched_reason": reason,
        "properties": event_properties(event),
        "request_run_id": manifest.get("run_id"),
        "request_occurred_after": manifest["window"].get("occurred_after"),
        "request_occurred_before": manifest["window"].get("occurred_before"),
        "extracted_at": first_nonempty(
            event.get("extracted_at"),
            manifest.get("extracted_at"),
        ),
        "consolidated_at": consolidated_at,
        "consolidation_version": CONSOLIDATION_VERSION,
    }


# -----------------------------------------------------------------------------
# CONSOLIDAÇÃO DE UMA RUN
# -----------------------------------------------------------------------------
def output_paths_for_run(
    consolidated_dir: Path,
    run_id: str,
) -> dict[str, Path]:
    safe_run_id = run_id.replace("/", "_")
    return {
        "consolidated": consolidated_dir
        / f"{safe_run_id}__forms_consolidated_v1.jsonl",
        "unmatched_submitted": consolidated_dir
        / f"{safe_run_id}__forms_unmatched_submitted_form_v1.jsonl",
        "unmatched_metadata": consolidated_dir
        / f"{safe_run_id}__forms_unmatched_metadata_v1.jsonl",
        "report": consolidated_dir
        / f"{safe_run_id}__forms_consolidation_report_v1.json",
    }


def consolidate_manifest(
    manifest_path: Path,
    manifest: dict[str, Any],
    all_manifests: list[tuple[Path, dict[str, Any]]],
    consolidated_dir: Path,
    match_window_seconds: int,
    boundary_lookaround_seconds: int,
    force: bool,
) -> dict[str, Any]:
    run_id = str(manifest.get("run_id") or manifest_path.stem)
    paths = output_paths_for_run(consolidated_dir, run_id)

    if paths["report"].exists() and not force:
        existing_report = load_json(paths["report"])
        print(f"\nIGNORADA: {run_id} já foi consolidada.")
        print(f"Use --force para refazer. Relatório: {paths['report']}")
        return existing_report

    if not manifest_ready_for_forms(manifest):
        statuses = forms_statuses(manifest)
        raise RuntimeError(
            f"A run {run_id} não está pronta para consolidação. "
            f"Status dos forms: {statuses}"
        )

    target_after, target_before = manifest_window(manifest)
    extended_after = target_after - timedelta(seconds=boundary_lookaround_seconds)
    extended_before = target_before + timedelta(seconds=boundary_lookaround_seconds)

    event_types = manifest["event_types"]
    base_path = Path(event_types[BASE_EVENT_TYPE]["output_file"])
    current_submitted_path = Path(event_types[SUBMITTED_EVENT_TYPE]["output_file"])
    current_metadata_path = Path(event_types[METADATA_EVENT_TYPE]["output_file"])

    submitted_candidate_paths = candidate_files_for_event_type(
        all_manifests,
        SUBMITTED_EVENT_TYPE,
        target_after,
        target_before,
        boundary_lookaround_seconds,
    )
    metadata_candidate_paths = candidate_files_for_event_type(
        all_manifests,
        METADATA_EVENT_TYPE,
        target_after,
        target_before,
        boundary_lookaround_seconds,
    )

    print("\n" + "=" * 90)
    print(f"CONSOLIDANDO: {run_id}")
    print(f"occurredAfter:  {manifest['window']['occurred_after']}")
    print(f"occurredBefore: {manifest['window']['occurred_before']}")
    print("=" * 90)

    base_events, base_read_stats = read_jsonl_events(
        [base_path],
        BASE_EVENT_TYPE,
        target_after,
        target_before,
    )

    submitted_candidates, submitted_candidate_stats = read_jsonl_events(
        submitted_candidate_paths,
        SUBMITTED_EVENT_TYPE,
        extended_after,
        extended_before,
    )
    metadata_candidates, metadata_candidate_stats = read_jsonl_events(
        metadata_candidate_paths,
        METADATA_EVENT_TYPE,
        extended_after,
        extended_before,
    )

    # Estes conjuntos são usados para gerar órfãos apenas da run atual.
    current_submitted, current_submitted_stats = read_jsonl_events(
        [current_submitted_path],
        SUBMITTED_EVENT_TYPE,
        target_after,
        target_before,
    )
    current_metadata, current_metadata_stats = read_jsonl_events(
        [current_metadata_path],
        METADATA_EVENT_TYPE,
        target_after,
        target_before,
    )

    submitted_index = index_candidates_by_contact(submitted_candidates)
    metadata_index = index_candidates_by_contact(metadata_candidates)

    used_submitted_keys: set[str] = set()
    used_metadata_keys: set[str] = set()
    consolidated_rows: list[dict[str, Any]] = []
    consolidated_at = to_utc_iso_seconds(utc_now_seconds())

    base_events.sort(
        key=lambda event: (
            event_datetime(event) or datetime.min.replace(tzinfo=timezone.utc),
            event_contact_id(event) or "",
            event_key(event),
        )
    )

    for base_event in base_events:
        submitted_match = find_submitted_form_match(
            base_event,
            submitted_index,
            used_submitted_keys,
            match_window_seconds,
        )
        metadata_match = find_metadata_match(
            base_event,
            metadata_index,
            used_metadata_keys,
            match_window_seconds,
        )

        if submitted_match:
            used_submitted_keys.add(event_key(submitted_match))

        if metadata_match:
            used_metadata_keys.add(event_key(metadata_match))

        consolidated_rows.append(
            build_consolidated_row(
                base_event,
                submitted_match,
                metadata_match,
                manifest,
                consolidated_at,
            )
        )

    current_submitted_keys = {event_key(event) for event in current_submitted}
    current_metadata_keys = {event_key(event) for event in current_metadata}

    unmatched_submitted_events = [
        event
        for event in current_submitted
        if event_key(event) not in used_submitted_keys
    ]
    unmatched_metadata_events = [
        event
        for event in current_metadata
        if event_key(event) not in used_metadata_keys
    ]

    unmatched_submitted_rows = [
        build_unmatched_row(
            event,
            SUBMITTED_EVENT_TYPE,
            target_after,
            target_before,
            match_window_seconds,
            manifest,
            consolidated_at,
        )
        for event in unmatched_submitted_events
    ]
    unmatched_metadata_rows = [
        build_unmatched_row(
            event,
            METADATA_EVENT_TYPE,
            target_after,
            target_before,
            match_window_seconds,
            manifest,
            consolidated_at,
        )
        for event in unmatched_metadata_events
    ]

    consolidated_count = atomic_write_jsonl(paths["consolidated"], consolidated_rows)
    unmatched_submitted_count = atomic_write_jsonl(
        paths["unmatched_submitted"], unmatched_submitted_rows
    )
    unmatched_metadata_count = atomic_write_jsonl(
        paths["unmatched_metadata"], unmatched_metadata_rows
    )

    status_counts: dict[str, int] = defaultdict(int)
    confidence_counts: dict[str, int] = defaultdict(int)

    for row in consolidated_rows:
        status_counts[str(row["match_status"])] += 1
        confidence_counts[str(row["match_confidence"])] += 1

    duplicate_submission_ids = len(consolidated_rows) - len(
        {row["submission_id"] for row in consolidated_rows}
    )

    boundary_unmatched_submitted = sum(
        row["unmatched_reason"] == "boundary_unresolved"
        for row in unmatched_submitted_rows
    )
    boundary_unmatched_metadata = sum(
        row["unmatched_reason"] == "boundary_unresolved"
        for row in unmatched_metadata_rows
    )

    report = {
        "status": "success",
        "consolidation_version": CONSOLIDATION_VERSION,
        "run_id": run_id,
        "run_type": manifest.get("run_type"),
        "source_manifest": str(manifest_path),
        "consolidated_at": consolidated_at,
        "window": manifest["window"],
        "settings": {
            "base_event_type": BASE_EVENT_TYPE,
            "match_window_seconds": match_window_seconds,
            "boundary_lookaround_seconds": boundary_lookaround_seconds,
        },
        "outputs": {key: str(value) for key, value in paths.items()},
        "counts": {
            "base_v2_events": len(base_events),
            "consolidated_rows": consolidated_count,
            "current_submitted_form_events": len(current_submitted),
            "current_metadata_events": len(current_metadata),
            "submitted_candidates_with_neighbors": len(submitted_candidates),
            "metadata_candidates_with_neighbors": len(metadata_candidates),
            "matched_submitted_form_events": len(
                current_submitted_keys & used_submitted_keys
            ),
            "matched_metadata_events": len(
                current_metadata_keys & used_metadata_keys
            ),
            "unmatched_submitted_form_events": unmatched_submitted_count,
            "unmatched_metadata_events": unmatched_metadata_count,
            "boundary_unresolved_submitted_form": boundary_unmatched_submitted,
            "boundary_unresolved_metadata": boundary_unmatched_metadata,
            "duplicate_submission_ids": duplicate_submission_ids,
        },
        "match_status_counts": dict(sorted(status_counts.items())),
        "match_confidence_counts": dict(sorted(confidence_counts.items())),
        "input_read_stats": {
            "base_v2": base_read_stats,
            "submitted_candidates": submitted_candidate_stats,
            "metadata_candidates": metadata_candidate_stats,
            "current_submitted": current_submitted_stats,
            "current_metadata": current_metadata_stats,
        },
        "candidate_source_files": {
            "submitted_form": [str(path) for path in submitted_candidate_paths],
            "metadata": [str(path) for path in metadata_candidate_paths],
        },
    }

    atomic_write_json(paths["report"], report)

    print(f"Base v2:                  {len(base_events)}")
    print(f"Linhas consolidadas:      {consolidated_count}")
    print(f"Pareamento completo:      {status_counts.get('complete', 0)}")
    print(f"Sem submitted_form:       {status_counts.get('missing_submitted_form', 0)}")
    print(f"Sem metadata:             {status_counts.get('missing_metadata', 0)}")
    print(f"Sem ambos:                {status_counts.get('missing_both', 0)}")
    print(f"Órfãos submitted_form:    {unmatched_submitted_count}")
    print(f"Órfãos metadata:          {unmatched_metadata_count}")
    print(f"IDs de submissão duplic.: {duplicate_submission_ids}")
    print(f"\nConsolidado: {paths['consolidated']}")
    print(f"Relatório:   {paths['report']}")

    return report


# -----------------------------------------------------------------------------
# SELEÇÃO DE RUNS E CLI
# -----------------------------------------------------------------------------
def report_exists_for_run(consolidated_dir: Path, run_id: str) -> bool:
    return output_paths_for_run(consolidated_dir, run_id)["report"].exists()


def choose_manifests_interactively(
    ready_manifests: list[tuple[Path, dict[str, Any]]],
    consolidated_dir: Path,
) -> list[tuple[Path, dict[str, Any]]]:
    if not ready_manifests:
        print("Nenhuma run com os três tipos de forms completos foi encontrada.")
        return []

    print("\nRuns prontas para consolidação:")

    for index, (_, manifest) in enumerate(ready_manifests, 1):
        run_id = str(manifest.get("run_id"))
        state = (
            "já consolidada"
            if report_exists_for_run(consolidated_dir, run_id)
            else "pendente"
        )
        print(
            f"  {index} - {run_id} | "
            f"{manifest['window']['occurred_after']} → "
            f"{manifest['window']['occurred_before']} | {state}"
        )

    print("  a - Todas as pendentes")
    print("  s - Sair")

    while True:
        choice = input("\nOpção: ").strip().lower()

        if choice in {"s", "sair", "exit"}:
            return []

        if choice in {"a", "all", "todas", "todos"}:
            return [
                item
                for item in ready_manifests
                if not report_exists_for_run(
                    consolidated_dir,
                    str(item[1].get("run_id")),
                )
            ]

        try:
            index = int(choice)
        except ValueError:
            print("Opção inválida.")
            continue

        if 1 <= index <= len(ready_manifests):
            return [ready_manifests[index - 1]]

        print("Opção inválida.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help="Pasta raiz dos eventos e manifestos.",
    )
    parser.add_argument(
        "--run-id",
        help="Consolida uma run específica pelo run_id.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Consolida diretamente o caminho de um manifesto.",
    )
    parser.add_argument(
        "--all-ready",
        action="store_true",
        help="Consolida todas as runs prontas ainda não processadas.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refaz e sobrescreve uma consolidação já existente.",
    )
    parser.add_argument(
        "--match-window-seconds",
        type=int,
        default=DEFAULT_MATCH_WINDOW_SECONDS,
    )
    parser.add_argument(
        "--boundary-lookaround-seconds",
        type=int,
        default=DEFAULT_BOUNDARY_LOOKAROUND_SECONDS,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.match_window_seconds < 0:
        raise SystemExit("--match-window-seconds não pode ser negativo.")

    if args.boundary_lookaround_seconds < args.match_window_seconds:
        raise SystemExit(
            "--boundary-lookaround-seconds deve ser maior ou igual a "
            "--match-window-seconds."
        )

    output_dir = args.output_dir.expanduser().resolve()
    runs_dir = output_dir / RUNS_DIR_NAME
    consolidated_dir = output_dir / CONSOLIDATED_DIR_NAME

    if not runs_dir.exists():
        raise SystemExit(f"Pasta de manifestos não encontrada: {runs_dir}")

    consolidated_dir.mkdir(parents=True, exist_ok=True)
    all_manifests = discover_manifests(runs_dir)
    ready_manifests = [
        item for item in all_manifests if manifest_ready_for_forms(item[1])
    ]

    selected: list[tuple[Path, dict[str, Any]]] = []

    if args.manifest:
        manifest_path = args.manifest.expanduser().resolve()
        selected = [(manifest_path, load_json(manifest_path))]
    elif args.run_id:
        selected = [
            item
            for item in all_manifests
            if str(item[1].get("run_id")) == args.run_id
        ]

        if not selected:
            raise SystemExit(f"run_id não encontrado: {args.run_id}")
    elif args.all_ready:
        selected = [
            item
            for item in ready_manifests
            if args.force
            or not report_exists_for_run(
                consolidated_dir,
                str(item[1].get("run_id")),
            )
        ]
    else:
        selected = choose_manifests_interactively(
            ready_manifests,
            consolidated_dir,
        )

    if not selected:
        print("Nenhuma run selecionada.")
        return

    failures = 0

    for manifest_path, manifest in selected:
        try:
            consolidate_manifest(
                manifest_path=manifest_path,
                manifest=manifest,
                all_manifests=all_manifests,
                consolidated_dir=consolidated_dir,
                match_window_seconds=args.match_window_seconds,
                boundary_lookaround_seconds=args.boundary_lookaround_seconds,
                force=args.force,
            )
        except Exception as exc:
            failures += 1
            print(
                f"\nERRO ao consolidar "
                f"{manifest.get('run_id', manifest_path.stem)}: {exc}"
            )

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nConsolidação interrompida pelo usuário.")
        sys.exit(130)
