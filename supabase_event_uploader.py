#!/usr/bin/env python3
"""
Envia ao Supabase os três arquivos finais de eventos do HubSpot:

- e_ad_interaction JSONL -> data_hs_ad_interactions_v2
- e_visited_page JSONL -> data_hs_page_views_v2
- forms_consolidated_v1 JSONL -> data_hs_form_submissions_v2

Linhas que já existem são ignoradas e NÃO são atualizadas.
Usa as mesmas variáveis do .env do dashspy_v2.py:

    SUPABASE_URL
    SUPABASE_KEY

Exemplos:

    python supabase_event_uploader.py ads arquivo.jsonl --dry-run
    python supabase_event_uploader.py ads arquivo.jsonl
    python supabase_event_uploader.py pages arquivo.jsonl
    python supabase_event_uploader.py forms arquivo.jsonl

    python supabase_event_uploader.py all \
        --ads caminho/ad_interaction.jsonl \
        --pages caminho/visited_page.jsonl \
        --forms caminho/forms_consolidated_v1.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional
from urllib.parse import parse_qs

from dotenv import load_dotenv
from supabase import Client, create_client


load_dotenv()

TABLE_ADS = "data_hs_ad_interactions_v2"
# BLOQUEADO: envio de page views (e_visited_page) para o Supabase desativado
# de propósito — esses arquivos devem ficar só locais. Para reativar,
# descomente esta linha e os demais trechos marcados como "BLOQUEADO" abaixo.
# TABLE_PAGES = "data_hs_page_views_v2"
TABLE_FORMS = "data_hs_form_submissions_v2"
DEFAULT_BATCH_SIZE = 500


def get_supabase_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url:
        raise RuntimeError("SUPABASE_URL não encontrada no .env.")
    if not key:
        raise RuntimeError("SUPABASE_KEY não encontrada no .env.")
    return create_client(url, key)


def insert_rows_ignore_duplicates(
    sb: Client,
    table: str,
    rows: list[dict[str, Any]],
    on_conflict: str,
) -> None:
    """Insere e ignora conflitos; não altera linhas existentes."""
    if not rows:
        return
    (
        sb.table(table)
        .upsert(
            rows,
            on_conflict=on_conflict,
            ignore_duplicates=True,
        )
        .execute()
    )


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        return value
    return None


def as_text(value: Any) -> Optional[str]:
    value = first_nonempty(value)
    return str(value) if value is not None else None


def properties_of(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("properties")
    return value if isinstance(value, dict) else {}


def lookup(row: dict[str, Any], *names: str) -> Any:
    properties = properties_of(row)
    for source in (row, properties):
        for name in names:
            value = source.get(name)
            if value is not None and not (
                isinstance(value, str) and not value.strip()
            ):
                return value
    return None


def deterministic_event_id(row: dict[str, Any]) -> str:
    original_id = first_nonempty(row.get("event_id"), row.get("id"))
    if original_id is not None:
        return str(original_id)

    core = {
        "eventType": row.get("eventType"),
        "objectId": row.get("objectId"),
        "objectType": row.get("objectType"),
        "occurredAt": row.get("occurredAt"),
        "properties": row.get("properties"),
    }
    raw = json.dumps(core, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_query_params(row: dict[str, Any]) -> dict[str, Any]:
    raw = lookup(row, "query_params", "hs_query_params")
    if not raw:
        return {}

    parsed = raw if isinstance(raw, dict) else parse_qs(
        str(raw).lstrip("?"), keep_blank_values=True
    )

    def first(name: str) -> Any:
        value = parsed.get(name)
        return value[0] if isinstance(value, list) and value else value

    return {
        "utm_source": first("utm_source"),
        "utm_campaign": first("utm_campaign"),
        "utm_medium": first("utm_medium"),
        "hsa_acc": first("hsa_acc"),
        "hsa_cam": first("hsa_cam"),
        "hsa_grp": first("hsa_grp"),
        "hsa_ad": first("hsa_ad"),
        "hsa_src": first("hsa_src"),
    }


def clean(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value is not None}


def read_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"JSON inválido em {path}, linha {line_number}: {exc}"
                ) from exc
            if not isinstance(row, dict):
                raise RuntimeError(
                    f"Linha {line_number} de {path} não contém um objeto JSON."
                )
            yield line_number, row


def prepare_ad_row(source: dict[str, Any]) -> dict[str, Any]:
    query = parse_query_params(source)
    return clean({
        "event_id": deterministic_event_id(source),
        "contact_id": as_text(lookup(source, "contact_id", "objectId", "object_id")),
        "contact_email": as_text(lookup(source, "contact_email", "email", "hs_email")),
        "occurred_at": lookup(source, "occurred_at", "occurredAt"),
        "network": as_text(lookup(source, "network", "hs_ad_network", "ad_network")),
        "interaction_type": as_text(lookup(
            source,
            "interaction_type",
            "interactionType",
            "hs_interaction_type",
            "hs_ad_interaction_type",
        )),
        "campaign_id": as_text(lookup(source, "campaign_id", "hs_campaign_id")),
        "campaign_name": as_text(lookup(source, "campaign_name", "hs_campaign_name")),
        "adgroup_id": as_text(lookup(
            source, "adgroup_id", "ad_group_id", "hs_adgroup_id", "hs_ad_group_id"
        )),
        "adgroup_name": as_text(lookup(
            source, "adgroup_name", "ad_group_name", "hs_adgroup_name", "hs_ad_group_name"
        )),
        "ad_id": as_text(lookup(source, "ad_id", "hs_ad_id")),
        "ad_name": as_text(lookup(source, "ad_name", "hs_ad_name")),
        "ad_account_id": as_text(lookup(
            source, "ad_account_id", "account_id", "hs_ad_account_id"
        )),
        "utm_source": as_text(first_nonempty(
            lookup(source, "utm_source", "hs_utm_source"), query.get("utm_source")
        )),
        "utm_campaign": as_text(first_nonempty(
            lookup(source, "utm_campaign", "hs_utm_campaign"), query.get("utm_campaign")
        )),
        "utm_medium": as_text(first_nonempty(
            lookup(source, "utm_medium", "hs_utm_medium"), query.get("utm_medium")
        )),
        "extracted_at": lookup(source, "extracted_at"),
    })


def prepare_page_row(source: dict[str, Any]) -> dict[str, Any]:
    query = parse_query_params(source)
    hsa_cam = first_nonempty(lookup(source, "hsa_cam"), query.get("hsa_cam"))
    return clean({
        "event_id": deterministic_event_id(source),
        "contact_id": as_text(lookup(source, "contact_id", "objectId", "object_id")),
        "contact_email": as_text(lookup(source, "contact_email", "email", "hs_email")),
        "viewed_at": lookup(source, "viewed_at", "occurredAt", "occurred_at"),
        "page_url": as_text(lookup(source, "page_url", "hs_url", "url", "hs_page_url")),
        "page_title": as_text(lookup(source, "page_title", "hs_title", "title", "hs_page_title")),
        "referrer": as_text(lookup(source, "referrer", "hs_referrer")),
        "session_source": as_text(lookup(source, "session_source", "hs_session_source", "source")),
        "utm_source": as_text(first_nonempty(
            lookup(source, "utm_source", "hs_utm_source"), query.get("utm_source")
        )),
        "utm_campaign": as_text(first_nonempty(
            lookup(source, "utm_campaign", "hs_utm_campaign"), query.get("utm_campaign")
        )),
        "utm_medium": as_text(first_nonempty(
            lookup(source, "utm_medium", "hs_utm_medium"), query.get("utm_medium")
        )),
        "hsa_acc": as_text(first_nonempty(lookup(source, "hsa_acc"), query.get("hsa_acc"))),
        "hsa_cam": as_text(hsa_cam),
        "hsa_grp": as_text(first_nonempty(lookup(source, "hsa_grp"), query.get("hsa_grp"))),
        "hsa_ad": as_text(first_nonempty(lookup(source, "hsa_ad"), query.get("hsa_ad"))),
        "has_ad_attribution": bool(hsa_cam),
        "extracted_at": lookup(source, "extracted_at"),
    })


def prepare_form_row(source: dict[str, Any]) -> dict[str, Any]:
    return clean({
        "contact_id": as_text(source.get("contact_id")),
        "contact_email": as_text(source.get("contact_email")),
        "submitted_at": first_nonempty(source.get("submitted_at"), source.get("occurred_at")),
        "form_id": as_text(source.get("form_id")),
        "form_title": as_text(source.get("form_title")),
        "hs_form_type": as_text(first_nonempty(source.get("hs_form_type"), source.get("form_type"))),
        "page_url": as_text(source.get("page_url")),
        "base_url": as_text(source.get("base_url")),
        "hs_page_title": as_text(first_nonempty(source.get("hs_page_title"), source.get("page_title"))),
        "title": as_text(first_nonempty(source.get("title"), source.get("page_title"))),
        "hs_referrer": as_text(first_nonempty(source.get("hs_referrer"), source.get("referrer"))),
        "hs_visitor_type": as_text(first_nonempty(source.get("hs_visitor_type"), source.get("visitor_type"))),
        "lifecyclestage": as_text(source.get("lifecyclestage")),
        "hs_utm_source": as_text(first_nonempty(source.get("hs_utm_source"), source.get("utm_source"))),
        "hs_utm_campaign": as_text(first_nonempty(source.get("hs_utm_campaign"), source.get("utm_campaign"))),
        "hs_utm_medium": as_text(first_nonempty(source.get("hs_utm_medium"), source.get("utm_medium"))),
        "hsa_acc": as_text(source.get("hsa_acc")),
        "hsa_cam": as_text(source.get("hsa_cam")),
        "hsa_grp": as_text(source.get("hsa_grp")),
        "hsa_ad": as_text(source.get("hsa_ad")),
        "hsa_src": as_text(source.get("hsa_src")),
        "has_ad_attribution": bool(first_nonempty(
            source.get("has_ad_attribution"), source.get("hsa_cam")
        )),
        "extracted_at": source.get("extracted_at"),
    })


def validate_required(row: dict[str, Any], fields: Iterable[str]) -> Optional[str]:
    missing = [field for field in fields if row.get(field) in (None, "")]
    return "Campos obrigatórios ausentes: " + ", ".join(missing) if missing else None


def write_rejected(path: Path, rows: list[dict[str, Any]]) -> Optional[Path]:
    if not rows:
        return None
    rejected_path = path.with_name(path.name + ".rejected.jsonl")
    with rejected_path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return rejected_path


def upload_jsonl(
    *,
    input_path: Path,
    table: str,
    on_conflict: str,
    transform: Callable[[dict[str, Any]], dict[str, Any]],
    conflict_key: Callable[[dict[str, Any]], tuple[Any, ...]],
    required_fields: tuple[str, ...],
    batch_size: int,
    dry_run: bool,
    assume_yes: bool,
) -> dict[str, Any]:
    input_path = input_path.expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"Arquivo não encontrado: {input_path}")

    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    duplicated_in_file = 0
    total = 0

    for line_number, source in read_jsonl(input_path):
        total += 1
        try:
            row = transform(source)
            error = validate_required(row, required_fields)
            if error:
                rejected.append({"line_number": line_number, "error": error, "source": source})
                continue
            key = conflict_key(row)
            if key in seen:
                duplicated_in_file += 1
                continue
            seen.add(key)
            valid.append(row)
        except Exception as exc:
            rejected.append({
                "line_number": line_number,
                "error": f"{type(exc).__name__}: {exc}",
                "source": source,
            })

    rejected_path = write_rejected(input_path, rejected)

    print("\n" + "=" * 80)
    print(f"Arquivo:               {input_path}")
    print(f"Tabela:                {table}")
    print(f"Linhas lidas:          {total}")
    print(f"Linhas válidas:        {len(valid)}")
    print(f"Duplicadas no arquivo: {duplicated_in_file}")
    print(f"Linhas rejeitadas:     {len(rejected)}")
    print(f"Conflito considerado:  {on_conflict}")
    print("Conflitos no banco:    ignorar, sem atualizar a linha existente")
    if rejected_path:
        print(f"Arquivo de rejeitadas: {rejected_path}")

    summary: dict[str, Any] = {
        "input_path": str(input_path),
        "table": table,
        "lines_read": total,
        "valid_rows": len(valid),
        "duplicated_in_file": duplicated_in_file,
        "rejected_rows": len(rejected),
        "rejected_path": str(rejected_path) if rejected_path else None,
        "sent_rows": 0,
        "status": "pending",
    }

    if dry_run:
        print("\nDRY RUN: nenhuma linha foi enviada.")
        summary["status"] = "dry_run"
        return summary

    if not valid:
        print("\nNenhuma linha válida para enviar.")
        summary["status"] = "no_valid_rows"
        return summary

    if not assume_yes:
        answer = input(f"\nEnviar {len(valid)} linha(s) para {table}? [s/N]: ").strip().lower()
        if answer not in {"s", "sim", "y", "yes"}:
            print("Envio cancelado.")
            summary["status"] = "cancelled"
            return summary

    sb = get_supabase_client()
    processed = 0
    for start in range(0, len(valid), batch_size):
        batch = valid[start:start + batch_size]
        insert_rows_ignore_duplicates(sb, table, batch, on_conflict)
        processed += len(batch)
        print(f"Processadas {processed}/{len(valid)} linha(s) em {table}.")

    print("Concluído. Linhas existentes foram ignoradas e não alteradas.")
    summary["sent_rows"] = processed
    summary["status"] = "sent"
    return summary


def upload_ads(path: Path, args: argparse.Namespace) -> None:
    upload_jsonl(
        input_path=path,
        table=TABLE_ADS,
        on_conflict="event_id",
        transform=prepare_ad_row,
        conflict_key=lambda row: (row["event_id"],),
        required_fields=("event_id", "contact_id", "occurred_at", "extracted_at"),
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        assume_yes=args.yes,
    )


# BLOQUEADO: envio de page views para o Supabase desativado de propósito.
# def upload_pages(path: Path, args: argparse.Namespace) -> None:
#     upload_jsonl(
#         input_path=path,
#         table=TABLE_PAGES,
#         on_conflict="event_id",
#         transform=prepare_page_row,
#         conflict_key=lambda row: (row["event_id"],),
#         required_fields=("event_id", "contact_id", "viewed_at", "extracted_at"),
#         batch_size=args.batch_size,
#         dry_run=args.dry_run,
#         assume_yes=args.yes,
#     )


def upload_forms(path: Path, args: argparse.Namespace) -> None:
    upload_jsonl(
        input_path=path,
        table=TABLE_FORMS,
        on_conflict="contact_id,submitted_at",
        transform=prepare_form_row,
        conflict_key=lambda row: (row["contact_id"], row["submitted_at"]),
        required_fields=("contact_id", "submitted_at", "extracted_at"),
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        assume_yes=args.yes,
    )


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Envia eventos HubSpot ao Supabase sem alterar duplicatas."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command, help_text in (
        ("ads", "Envia e_ad_interaction."),
        # BLOQUEADO: envio de page views para o Supabase desativado de propósito.
        # ("pages", "Envia e_visited_page."),
        ("forms", "Envia forms_consolidated_v1."),
    ):
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument("file", type=Path)
        add_common_arguments(subparser)

    all_parser = subparsers.add_parser("all", help="Envia os três arquivos.")
    all_parser.add_argument("--ads", required=True, type=Path)
    # BLOQUEADO: envio de page views para o Supabase desativado de propósito.
    # all_parser.add_argument("--pages", required=True, type=Path)
    all_parser.add_argument("--forms", required=True, type=Path)
    add_common_arguments(all_parser)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.batch_size <= 0:
        raise SystemExit("--batch-size deve ser maior que zero.")

    if args.command == "ads":
        upload_ads(args.file, args)
    # BLOQUEADO: envio de page views para o Supabase desativado de propósito.
    # elif args.command == "pages":
    #     upload_pages(args.file, args)
    elif args.command == "forms":
        upload_forms(args.file, args)
    elif args.command == "all":
        upload_ads(args.ads, args)
        # BLOQUEADO: envio de page views para o Supabase desativado de propósito.
        # upload_pages(args.pages, args)
        upload_forms(args.forms, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExecução interrompida pelo usuário.")
        sys.exit(130)
    except Exception as exc:
        print(f"\nERRO: {exc}", file=sys.stderr)
        sys.exit(1)
