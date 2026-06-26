#!/usr/bin/env python3
"""
Extração paginada de eventos do HubSpot com três modos:

1. daily
   Busca do último cursor diário salvo até o instante atual.

2. historical
   Busca os 3 meses imediatamente anteriores ao cursor histórico salvo.

3. retry
   Retoma apenas os tipos de evento incompletos de runs anteriores.

O script salva:
- um JSONL por tipo de evento e por janela;
- um manifesto JSON por run, com o cursor de paginação de cada tipo;
- um estado global com os cursores independentes das rotinas diária e histórica.

Retomada:
- Para runs criadas por este script, usa paging.next.after, salvo após cada página.
- Para arquivos antigos sem cursor salvo, lê o último occurredAt disponível,
  cria uma pequena sobreposição de 1 segundo e remove duplicatas pelo ID.

Uso interativo:
    python hubspot_eventos_daily_historical_retry.py

Uso em cron:
    python hubspot_eventos_daily_historical_retry.py --run-type daily

Outros modos:
    python hubspot_eventos_daily_historical_retry.py --run-type historical
    python hubspot_eventos_daily_historical_retry.py --run-type retry
"""

import argparse
import calendar
import hashlib
import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv


load_dotenv()

BASE_URL = "https://api.hubapi.com"

# ----------------------------------------------------------------------
# ALTERE ESTE CAMINHO CASO QUEIRA SALVAR EM OUTRA PASTA.
# ----------------------------------------------------------------------
OUTPUT_DIR = Path(
    "hubspot_eventos"
)

RUNS_DIR_NAME = "_runs"
STATE_FILENAME = "estado_extracao_eventos.json"

EVENT_TYPES = [
    "e_submitted_form",
    "e_form_submission_v2",
    "e_form_submission_metadata_v2",
    "e_ad_interaction",
    "e_visited_page",
]

PAGE_SIZE = 100

# Timeout de conexão e de leitura, em segundos.
REQUEST_TIMEOUT = (15, 120)

# Tentativas automáticas da mesma página antes de deixar a run pendente.
MAX_PAGE_RETRIES = 5

# Sobreposição usada somente para recuperar arquivos antigos sem cursor salvo.
LEGACY_OVERLAP_SECONDS = 1


def utc_now_seconds() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_iso_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def to_utc_iso_seconds(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def to_filename_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def subtract_months(value: datetime, months: int) -> datetime:
    """Subtrai meses de calendário, preservando horário, segundos e fuso."""
    target_month_index = value.year * 12 + (value.month - 1) - months
    target_year, target_month_zero_based = divmod(target_month_index, 12)
    target_month = target_month_zero_based + 1
    last_day = calendar.monthrange(target_year, target_month)[1]

    return value.replace(
        year=target_year,
        month=target_month,
        day=min(value.day, last_day),
    )


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Grava JSON sem deixar um arquivo parcialmente escrito."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")

    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())

    temporary_path.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def get_headers() -> dict[str, str]:
    token = os.environ.get("HUBSPOT_TOKEN")

    if not token:
        raise SystemExit("ERRO: HUBSPOT_TOKEN não encontrado no arquivo .env")

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def event_key(event: dict[str, Any]) -> str:
    """
    Usa o ID do evento quando disponível. O hash é apenas um fallback
    para respostas que eventualmente não tragam ID.
    """
    event_id = event.get("id")

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


def read_first_json_record(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return None

    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    return None


def read_tail_json_records(
    path: Path,
    max_records: int = 1000,
    max_bytes: int = 16 * 1024 * 1024,
) -> list[dict[str, Any]]:
    """
    Lê apenas o final do JSONL. Isso evita carregar arquivos grandes
    inteiros na memória.
    """
    if not path.exists() or path.stat().st_size == 0:
        return []

    records: deque[dict[str, Any]] = deque(maxlen=max_records)
    file_size = path.stat().st_size
    bytes_to_read = min(file_size, max_bytes)

    with path.open("rb") as file:
        file.seek(file_size - bytes_to_read)
        raw = file.read(bytes_to_read)

    # Se começamos no meio de uma linha, descartamos a primeira linha parcial.
    if bytes_to_read < file_size:
        first_newline = raw.find(b"\n")
        raw = raw[first_newline + 1 :] if first_newline >= 0 else b""

    for raw_line in raw.splitlines():
        try:
            line = raw_line.decode("utf-8").strip()
            if line:
                records.append(json.loads(line))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue

    return list(records)


def get_last_available_event(
    output_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tail_records = read_tail_json_records(output_path)

    if not tail_records:
        raise RuntimeError(
            f"Não foi possível encontrar eventos válidos em {output_path}"
        )

    return tail_records[-1], tail_records


def infer_file_order(
    first_record: dict[str, Any],
    tail_records: list[dict[str, Any]],
) -> str:
    """
    Retorna 'ascending' ou 'descending' comparando o primeiro e o último
    occurredAt válido do arquivo.
    """
    first_value = first_record.get("occurredAt")
    last_value = tail_records[-1].get("occurredAt")

    if not first_value or not last_value:
        raise RuntimeError("O arquivo não possui occurredAt suficiente para retomada.")

    first_dt = parse_iso_utc(first_value)
    last_dt = parse_iso_utc(last_value)

    if last_dt > first_dt:
        return "ascending"

    if last_dt < first_dt:
        return "descending"

    # Caso extremo: tenta localizar no final algum timestamp diferente.
    for record in reversed(tail_records[:-1]):
        value = record.get("occurredAt")
        if not value:
            continue

        candidate = parse_iso_utc(value)

        if last_dt > candidate:
            return "ascending"

        if last_dt < candidate:
            return "descending"

    raise RuntimeError(
        "Não foi possível inferir a ordem temporal do arquivo para retomá-lo."
    )


def build_run_id(run_type: str, occurred_after: str, occurred_before: str) -> str:
    after_stamp = to_filename_timestamp(parse_iso_utc(occurred_after))
    before_stamp = to_filename_timestamp(parse_iso_utc(occurred_before))
    return f"{run_type}__after_{after_stamp}__before_{before_stamp}"


def build_event_output_path(
    output_dir: Path,
    run_type: str,
    event_type: str,
    occurred_after: str,
    occurred_before: str,
) -> Path:
    after_stamp = to_filename_timestamp(parse_iso_utc(occurred_after))
    before_stamp = to_filename_timestamp(parse_iso_utc(occurred_before))

    return output_dir / (
        f"{run_type}__{event_type}"
        f"__after_{after_stamp}"
        f"__before_{before_stamp}.jsonl"
    )


def update_job_status(manifest: dict[str, Any]) -> None:
    statuses = [
        item.get("status")
        for item in manifest.get("event_types", {}).values()
    ]

    if statuses and all(status == "complete" for status in statuses):
        manifest["status"] = "complete"
        manifest["completed_at"] = to_utc_iso_seconds(utc_now_seconds())
    elif any(status == "running" for status in statuses):
        manifest["status"] = "running"
    else:
        manifest["status"] = "incomplete"


def create_new_manifest(
    runs_dir: Path,
    output_dir: Path,
    run_type: str,
    occurred_after: str,
    occurred_before: str,
) -> tuple[dict[str, Any], Path]:
    run_id = build_run_id(run_type, occurred_after, occurred_before)
    manifest_path = runs_dir / f"{run_id}.json"

    if manifest_path.exists():
        return load_json(manifest_path), manifest_path

    extraction_started_at = to_utc_iso_seconds(utc_now_seconds())

    event_types: dict[str, Any] = {}

    for event_type in EVENT_TYPES:
        output_path = build_event_output_path(
            output_dir=output_dir,
            run_type=run_type,
            event_type=event_type,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
        )

        event_types[event_type] = {
            "status": "pending",
            "output_file": str(output_path),
            "total_events": 0,
            "pages": 0,
            "next_after_cursor": None,
            "last_occurred_at": None,
            "active_occurred_after": occurred_after,
            "active_occurred_before": occurred_before,
            "resume_strategy": "paging_cursor",
            "last_error": None,
        }

    manifest = {
        "version": 2,
        "run_id": run_id,
        "run_type": run_type,
        "status": "pending",
        "created_at": extraction_started_at,
        "extracted_at": extraction_started_at,
        "window": {
            "occurred_after": occurred_after,
            "occurred_before": occurred_before,
            "timezone": "UTC",
            "precision": "seconds",
        },
        "event_types": event_types,
    }

    atomic_write_json(manifest_path, manifest)
    return manifest, manifest_path


def find_latest_legacy_summary(output_dir: Path) -> Optional[Path]:
    summaries = list(
        output_dir.glob("resumo_execucao__after_*__before_*.json")
    )

    if not summaries:
        return None

    return max(summaries, key=lambda path: path.stat().st_mtime)


def import_legacy_run_if_needed(
    summary_path: Path,
    runs_dir: Path,
) -> Optional[Path]:
    summary = load_json(summary_path)
    window = summary.get("global_request_window") or summary.get("period") or {}

    occurred_after = window.get("occurred_after")
    occurred_before = window.get("occurred_before")

    if not occurred_after or not occurred_before:
        return None

    run_id = build_run_id("legacy", occurred_after, occurred_before)
    manifest_path = runs_dir / f"{run_id}.json"

    if manifest_path.exists():
        return manifest_path

    manifest_event_types: dict[str, Any] = {}

    for result in summary.get("event_types", []):
        event_type = result.get("event_type")

        if event_type not in EVENT_TYPES:
            continue

        output_file = result.get("output_file")

        if not output_file:
            continue

        success = bool(result.get("success"))

        manifest_event_types[event_type] = {
            "status": "complete" if success else "pending",
            "output_file": output_file,
            "total_events": int(result.get("total_events", 0)),
            "pages": int(result.get("pages", 0)),
            "next_after_cursor": None,
            "last_occurred_at": None,
            "active_occurred_after": occurred_after,
            "active_occurred_before": occurred_before,
            "resume_strategy": (
                "complete_legacy" if success else "legacy_last_occurred_at"
            ),
            "last_error": result.get("error"),
        }

    # Completa event types ausentes, sem alterar os arquivos já existentes.
    for event_type in EVENT_TYPES:
        if event_type in manifest_event_types:
            continue

        legacy_file = (
            Path(summary_path).parent
            / (
                f"{event_type}"
                f"__after_{to_filename_timestamp(parse_iso_utc(occurred_after))}"
                f"__before_{to_filename_timestamp(parse_iso_utc(occurred_before))}"
                f".jsonl"
            )
        )

        manifest_event_types[event_type] = {
            "status": "pending",
            "output_file": str(legacy_file),
            "total_events": 0,
            "pages": 0,
            "next_after_cursor": None,
            "last_occurred_at": None,
            "active_occurred_after": occurred_after,
            "active_occurred_before": occurred_before,
            "resume_strategy": "legacy_last_occurred_at",
            "last_error": "Tipo não encontrado no resumo antigo.",
        }

    manifest = {
        "version": 2,
        "run_id": run_id,
        "run_type": "legacy",
        "status": "incomplete",
        "created_at": summary.get("extracted_at", occurred_before),
        "extracted_at": summary.get("extracted_at", occurred_before),
        "imported_from": str(summary_path),
        "window": {
            "occurred_after": occurred_after,
            "occurred_before": occurred_before,
            "timezone": "UTC",
            "precision": "seconds",
        },
        "event_types": manifest_event_types,
    }

    update_job_status(manifest)
    atomic_write_json(manifest_path, manifest)
    return manifest_path


def initialize_or_load_state(
    output_dir: Path,
    runs_dir: Path,
) -> tuple[dict[str, Any], Path]:
    state_path = output_dir / STATE_FILENAME

    if state_path.exists():
        return load_json(state_path), state_path

    latest_summary = find_latest_legacy_summary(output_dir)

    if latest_summary:
        summary = load_json(latest_summary)
        window = summary.get("global_request_window") or summary.get("period") or {}
        previous_after = window.get("occurred_after")
        previous_before = window.get("occurred_before")

        if previous_after and previous_before:
            import_legacy_run_if_needed(latest_summary, runs_dir)

            state = {
                "version": 2,
                "created_at": to_utc_iso_seconds(utc_now_seconds()),
                "daily_next_after": previous_before,
                "historical_next_before": previous_after,
                "historical_chunk_months": 3,
                "initialized_from_summary": str(latest_summary),
            }
            atomic_write_json(state_path, state)
            return state, state_path

    # Fallback para uma instalação sem nenhuma extração anterior.
    now = utc_now_seconds()
    state = {
        "version": 2,
        "created_at": to_utc_iso_seconds(now),
        "daily_next_after": to_utc_iso_seconds(now),
        "historical_next_before": to_utc_iso_seconds(now),
        "historical_chunk_months": 3,
        "initialized_from_summary": None,
    }
    atomic_write_json(state_path, state)

    print(
        "\nAVISO: nenhum resumo anterior foi encontrado. "
        "Os cursores diário e histórico foram inicializados no momento atual."
    )

    return state, state_path


def list_incomplete_manifests(runs_dir: Path) -> list[Path]:
    incomplete: list[Path] = []

    for path in sorted(runs_dir.glob("*.json")):
        try:
            manifest = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue

        update_job_status(manifest)

        if manifest.get("status") != "complete":
            incomplete.append(path)

    return incomplete


def prepare_legacy_time_resume(
    manifest: dict[str, Any],
    event_state: dict[str, Any],
) -> tuple[str, str, set[str]]:
    output_path = Path(event_state["output_file"])

    if not output_path.exists() or output_path.stat().st_size == 0:
        # Não há nada a preservar: reinicia somente esse tipo na janela original.
        event_state["active_occurred_after"] = manifest["window"]["occurred_after"]
        event_state["active_occurred_before"] = manifest["window"]["occurred_before"]
        event_state["resume_strategy"] = "restart_empty_legacy_file"
        return (
            event_state["active_occurred_after"],
            event_state["active_occurred_before"],
            set(),
        )

    first_record = read_first_json_record(output_path)
    last_record, tail_records = get_last_available_event(output_path)

    if not first_record:
        raise RuntimeError(f"Não foi possível ler o início de {output_path}")

    order = infer_file_order(first_record, tail_records)
    last_occurred_at = last_record.get("occurredAt")

    if not last_occurred_at:
        raise RuntimeError(
            f"O último evento de {output_path} não possui occurredAt."
        )

    last_dt = parse_iso_utc(last_occurred_at)
    original_after = parse_iso_utc(manifest["window"]["occurred_after"])
    original_before = parse_iso_utc(manifest["window"]["occurred_before"])

    if order == "ascending":
        resume_after_dt = max(
            original_after,
            last_dt - timedelta(seconds=LEGACY_OVERLAP_SECONDS),
        )
        resume_before_dt = original_before
    else:
        resume_after_dt = original_after
        resume_before_dt = min(
            original_before,
            last_dt + timedelta(seconds=LEGACY_OVERLAP_SECONDS),
        )

    resume_after = to_utc_iso_seconds(resume_after_dt)
    resume_before = to_utc_iso_seconds(resume_before_dt)

    # Como a retomada se sobrepõe em 1 segundo, evita repetir os eventos
    # já existentes no final do arquivo.
    recent_keys = {event_key(record) for record in tail_records}

    event_state["last_occurred_at"] = last_occurred_at
    event_state["detected_file_order"] = order
    event_state["active_occurred_after"] = resume_after
    event_state["active_occurred_before"] = resume_before
    event_state["resume_strategy"] = "legacy_last_occurred_at_with_overlap"

    print(f"  Último occurredAt disponível: {last_occurred_at}")
    print(f"  Ordem temporal detectada: {order}")
    print(f"  Retomada occurredAfter:  {resume_after}")
    print(f"  Retomada occurredBefore: {resume_before}")

    return resume_after, resume_before, recent_keys


def retry_delay_seconds(response: Optional[requests.Response], attempt: int) -> int:
    if response is not None and response.status_code == 429:
        retry_after = response.headers.get("Retry-After")

        if retry_after:
            try:
                return max(1, int(float(retry_after)))
            except ValueError:
                pass

    return min(60, 5 * (2 ** attempt))


def request_page(
    session: requests.Session,
    headers: dict[str, str],
    params: dict[str, Any],
) -> tuple[Optional[requests.Response], Optional[str], bool]:
    """
    Retorna:
    - response;
    - mensagem de erro;
    - se o erro é potencialmente temporário.
    """
    last_error: Optional[str] = None
    last_response: Optional[requests.Response] = None

    for attempt in range(MAX_PAGE_RETRIES + 1):
        try:
            response = session.get(
                f"{BASE_URL}/events/v3/events",
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            last_response = response

            if response.status_code == 200:
                return response, None, False

            retryable_status = response.status_code in {
                408,
                425,
                429,
                500,
                502,
                503,
                504,
            }

            if not retryable_status:
                try:
                    body = response.json()
                except ValueError:
                    body = response.text

                return (
                    response,
                    json.dumps(body, ensure_ascii=False),
                    False,
                )

            last_error = (
                f"HTTP {response.status_code} {response.reason}: {response.text}"
            )

        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        except requests.RequestException as exc:
            return None, f"{type(exc).__name__}: {exc}", False

        if attempt < MAX_PAGE_RETRIES:
            delay = retry_delay_seconds(last_response, attempt)
            print(
                f"  Falha temporária. Nova tentativa da mesma página "
                f"em {delay}s ({attempt + 1}/{MAX_PAGE_RETRIES})..."
            )
            time.sleep(delay)

    return last_response, last_error, True


def process_event_type(
    session: requests.Session,
    headers: dict[str, str],
    manifest: dict[str, Any],
    manifest_path: Path,
    event_type: str,
    allow_terminal_retry: bool,
) -> bool:
    event_state = manifest["event_types"][event_type]

    if event_state.get("status") == "complete":
        return True

    output_path = Path(event_state["output_file"])
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 90)
    print(f"RUN: {manifest['run_id']}")
    print(f"TIPO: {event_type}")
    print("=" * 90)

    recent_keys = {
        event_key(record)
        for record in read_tail_json_records(output_path, max_records=500)
    }

    cursor = event_state.get("next_after_cursor")
    active_after = event_state.get("active_occurred_after")
    active_before = event_state.get("active_occurred_before")

    if not active_after:
        active_after = manifest["window"]["occurred_after"]

    if not active_before:
        active_before = manifest["window"]["occurred_before"]

    is_legacy_without_cursor = (
        event_state.get("resume_strategy") == "legacy_last_occurred_at"
        and not cursor
    )

    if is_legacy_without_cursor:
        try:
            active_after, active_before, legacy_keys = prepare_legacy_time_resume(
                manifest,
                event_state,
            )
            recent_keys.update(legacy_keys)
            atomic_write_json(manifest_path, manifest)
        except Exception as exc:
            event_state["status"] = "error"
            event_state["last_error"] = str(exc)
            update_job_status(manifest)
            atomic_write_json(manifest_path, manifest)
            print(f"ERRO AO PREPARAR RETOMADA: {exc}")
            return False

    file_mode = "a" if output_path.exists() and output_path.stat().st_size > 0 else "w"
    event_state["status"] = "running"
    event_state["last_error"] = None
    update_job_status(manifest)
    atomic_write_json(manifest_path, manifest)

    with output_path.open(file_mode, encoding="utf-8") as output_file:
        while True:
            params: dict[str, Any] = {
                "eventType": event_type,
                "occurredAfter": active_after,
                "occurredBefore": active_before,
                "limit": PAGE_SIZE,
            }

            if cursor:
                params["after"] = cursor

            response, error_message, temporary_error = request_page(
                session=session,
                headers=headers,
                params=params,
            )

            if response is None or response.status_code != 200:
                # Um cursor pode deixar de ser aceito entre uma execução e outra.
                # Nesse caso, recupera a posição pela última data já gravada.
                if (
                    cursor
                    and response is not None
                    and response.status_code == 400
                    and output_path.exists()
                    and output_path.stat().st_size > 0
                ):
                    print(
                        "\nO HubSpot rejeitou o cursor salvo. "
                        "Tentando retomar pela última data do arquivo..."
                    )
                    cursor = None
                    event_state["next_after_cursor"] = None
                    event_state["resume_strategy"] = "legacy_last_occurred_at"

                    try:
                        active_after, active_before, fallback_keys = (
                            prepare_legacy_time_resume(
                                manifest,
                                event_state,
                            )
                        )
                        recent_keys.update(fallback_keys)
                        event_state["status"] = "running"
                        event_state["last_error"] = None
                        update_job_status(manifest)
                        atomic_write_json(manifest_path, manifest)
                        continue
                    except Exception as fallback_exc:
                        error_message = (
                            f"{error_message}; falha no fallback por data: "
                            f"{fallback_exc}"
                        )

                event_state["status"] = "error"
                event_state["last_error"] = error_message
                event_state["failed_at"] = to_utc_iso_seconds(utc_now_seconds())
                event_state["failed_request"] = {
                    "occurred_after": active_after,
                    "occurred_before": active_before,
                    "after_cursor": cursor,
                }
                update_job_status(manifest)
                atomic_write_json(manifest_path, manifest)

                print("\nERRO NA CONSULTA")
                print(error_message or "Erro sem mensagem.")
                print(f"Dados parciais preservados em: {output_path}")
                print(
                    "Retry point salvo no manifesto: "
                    f"{manifest_path}"
                )

                if allow_terminal_retry and temporary_error:
                    answer = input(
                        "\nTentar novamente este mesmo tipo agora? [s/N]: "
                    ).strip().lower()

                    if answer in {"s", "sim", "y", "yes"}:
                        event_state["status"] = "running"
                        event_state["last_error"] = None
                        atomic_write_json(manifest_path, manifest)
                        continue

                return False

            data = response.json()
            results = data.get("results", [])
            next_page = data.get("paging", {}).get("next")
            next_cursor = next_page.get("after") if next_page else None

            written_count = 0

            for original_event in results:
                key = event_key(original_event)

                if key in recent_keys:
                    continue

                event = dict(original_event)
                event["extracted_at"] = manifest["extracted_at"]
                event["request_run_type"] = manifest["run_type"]
                event["request_occurred_after"] = manifest["window"][
                    "occurred_after"
                ]
                event["request_occurred_before"] = manifest["window"][
                    "occurred_before"
                ]

                output_file.write(
                    json.dumps(
                        event,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )

                recent_keys.add(key)
                written_count += 1

            output_file.flush()
            os.fsync(output_file.fileno())

            event_state["pages"] = int(event_state.get("pages", 0)) + 1
            event_state["total_events"] = (
                int(event_state.get("total_events", 0)) + written_count
            )
            event_state["next_after_cursor"] = next_cursor
            event_state["last_occurred_at"] = (
                results[-1].get("occurredAt") if results else event_state.get(
                    "last_occurred_at"
                )
            )
            event_state["active_occurred_after"] = active_after
            event_state["active_occurred_before"] = active_before
            event_state["updated_at"] = to_utc_iso_seconds(utc_now_seconds())

            # Mantém apenas os IDs recentes. Isso protege contra repetição
            # da última página depois de uma interrupção sem crescer sem limite.
            if len(recent_keys) > 2000:
                recent_keys = {
                    event_key(record)
                    for record in read_tail_json_records(
                        output_path,
                        max_records=1000,
                    )
                }

            print(
                f"  Página {event_state['pages']}: "
                f"{len(results)} recebido(s), "
                f"{written_count} novo(s) | "
                f"total salvo: {event_state['total_events']}"
            )

            if not next_cursor:
                event_state["status"] = "complete"
                event_state["completed_at"] = to_utc_iso_seconds(
                    utc_now_seconds()
                )
                event_state["last_error"] = None
                update_job_status(manifest)
                atomic_write_json(manifest_path, manifest)

                print(
                    f"\nSUCESSO: {event_state['total_events']} evento(s) "
                    f"salvos em {output_path}"
                )
                return True

            cursor = next_cursor
            update_job_status(manifest)
            atomic_write_json(manifest_path, manifest)


def process_manifest(
    manifest_path: Path,
    allow_terminal_retry: bool,
) -> bool:
    manifest = load_json(manifest_path)
    headers = get_headers()

    with requests.Session() as session:
        for event_type in EVENT_TYPES:
            event_state = manifest["event_types"].get(event_type)

            if not event_state:
                continue

            if event_state.get("status") == "complete":
                continue

            process_event_type(
                session=session,
                headers=headers,
                manifest=manifest,
                manifest_path=manifest_path,
                event_type=event_type,
                allow_terminal_retry=allow_terminal_retry,
            )

    update_job_status(manifest)
    atomic_write_json(manifest_path, manifest)
    print_manifest_summary(manifest, manifest_path)

    return manifest.get("status") == "complete"


def print_manifest_summary(
    manifest: dict[str, Any],
    manifest_path: Path,
) -> None:
    print("\n" + "=" * 90)
    print("RESUMO DA RUN")
    print("=" * 90)
    print(f"Tipo: {manifest['run_type']}")
    print(f"occurredAfter:  {manifest['window']['occurred_after']}")
    print(f"occurredBefore: {manifest['window']['occurred_before']}")
    print(f"Status geral:   {manifest['status']}")

    for event_type in EVENT_TYPES:
        item = manifest["event_types"].get(event_type)

        if not item:
            continue

        print(
            f"{event_type:<40} "
            f"{int(item.get('total_events', 0)):>10} evento(s) | "
            f"{int(item.get('pages', 0)):>7} página(s) | "
            f"{item.get('status', 'unknown').upper()}"
        )

    print(f"\nManifesto/retry point: {manifest_path}")


def choose_run_type(incomplete_count: int) -> str:
    print("\nEscolha o tipo de run:")
    print("  1 - Diária")
    print("  2 - Histórica: 3 meses anteriores")
    print(f"  3 - Retomar runs incompletas ({incomplete_count})")
    print("  4 - Sair")

    while True:
        choice = input("\nOpção: ").strip().lower()

        mapping = {
            "1": "daily",
            "daily": "daily",
            "diaria": "daily",
            "diária": "daily",
            "2": "historical",
            "historical": "historical",
            "historica": "historical",
            "histórica": "historical",
            "3": "retry",
            "retry": "retry",
            "retomar": "retry",
            "4": "exit",
            "sair": "exit",
            "exit": "exit",
        }

        if choice in mapping:
            return mapping[choice]

        print("Opção inválida.")


def choose_incomplete_manifests(paths: list[Path]) -> list[Path]:
    if not paths:
        print("\nNão há runs incompletas.")
        return []

    print("\nRuns incompletas:")

    for index, path in enumerate(paths, 1):
        manifest = load_json(path)
        pending_types = [
            event_type
            for event_type, item in manifest.get("event_types", {}).items()
            if item.get("status") != "complete"
        ]

        print(
            f"  {index} - {manifest.get('run_type')} | "
            f"{manifest['window']['occurred_after']} → "
            f"{manifest['window']['occurred_before']} | "
            f"pendentes: {', '.join(pending_types)}"
        )

    print("  a - Retomar todas")

    while True:
        choice = input("\nRetry point: ").strip().lower()

        if choice in {"a", "all", "todas", "todos"}:
            return paths

        try:
            index = int(choice)
        except ValueError:
            print("Opção inválida.")
            continue

        if 1 <= index <= len(paths):
            return [paths[index - 1]]

        print("Opção inválida.")


def create_daily_run(
    state: dict[str, Any],
    state_path: Path,
    runs_dir: Path,
    output_dir: Path,
) -> Path:
    occurred_after = state["daily_next_after"]
    occurred_before = to_utc_iso_seconds(utc_now_seconds())

    if parse_iso_utc(occurred_after) >= parse_iso_utc(occurred_before):
        raise SystemExit(
            "Não existe intervalo diário novo: o cursor já está no instante atual."
        )

    manifest, manifest_path = create_new_manifest(
        runs_dir=runs_dir,
        output_dir=output_dir,
        run_type="daily",
        occurred_after=occurred_after,
        occurred_before=occurred_before,
    )

    # Avança o cursor diário mesmo se algum tipo falhar. O manifesto individual
    # preserva o retry point da janela incompleta.
    state["daily_next_after"] = occurred_before
    state["updated_at"] = to_utc_iso_seconds(utc_now_seconds())
    state["last_daily_run_id"] = manifest["run_id"]
    atomic_write_json(state_path, state)

    return manifest_path


def create_historical_run(
    state: dict[str, Any],
    state_path: Path,
    runs_dir: Path,
    output_dir: Path,
) -> Path:
    occurred_before = state["historical_next_before"]
    months = int(state.get("historical_chunk_months", 3))
    occurred_after_dt = subtract_months(
        parse_iso_utc(occurred_before),
        months,
    )
    occurred_after = to_utc_iso_seconds(occurred_after_dt)

    manifest, manifest_path = create_new_manifest(
        runs_dir=runs_dir,
        output_dir=output_dir,
        run_type="historical",
        occurred_after=occurred_after,
        occurred_before=occurred_before,
    )

    # Avança o cursor histórico para a borda mais antiga da janela criada.
    # Qualquer falha permanece recuperável no manifesto desta run.
    state["historical_next_before"] = occurred_after
    state["updated_at"] = to_utc_iso_seconds(utc_now_seconds())
    state["last_historical_run_id"] = manifest["run_id"]
    atomic_write_json(state_path, state)

    return manifest_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-type",
        choices=["daily", "historical", "retry"],
        help=(
            "Evita o menu interativo. Use daily no cron diário, "
            "historical para retroagir 3 meses ou retry para pendências."
        ),
    )
    parser.add_argument(
        "--retry-all",
        action="store_true",
        help="No modo retry, retoma todas as runs incompletas sem perguntar.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    output_dir = OUTPUT_DIR.expanduser().resolve()
    runs_dir = output_dir / RUNS_DIR_NAME

    output_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    state, state_path = initialize_or_load_state(
        output_dir=output_dir,
        runs_dir=runs_dir,
    )

    incomplete = list_incomplete_manifests(runs_dir)

    run_type = args.run_type or choose_run_type(len(incomplete))

    if run_type == "exit":
        return

    interactive = args.run_type is None

    if run_type == "daily":
        manifest_path = create_daily_run(
            state=state,
            state_path=state_path,
            runs_dir=runs_dir,
            output_dir=output_dir,
        )
        process_manifest(
            manifest_path=manifest_path,
            allow_terminal_retry=interactive,
        )
        return

    if run_type == "historical":
        manifest_path = create_historical_run(
            state=state,
            state_path=state_path,
            runs_dir=runs_dir,
            output_dir=output_dir,
        )
        process_manifest(
            manifest_path=manifest_path,
            allow_terminal_retry=interactive,
        )
        return

    if run_type == "retry":
        incomplete = list_incomplete_manifests(runs_dir)

        if args.retry_all or not interactive:
            selected = incomplete
        else:
            selected = choose_incomplete_manifests(incomplete)

        for manifest_path in selected:
            process_manifest(
                manifest_path=manifest_path,
                allow_terminal_retry=interactive,
            )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExecução interrompida pelo usuário. Retry points preservados.")
        sys.exit(130)
