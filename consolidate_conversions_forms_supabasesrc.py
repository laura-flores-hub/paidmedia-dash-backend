#!/usr/bin/env python3

from __future__ import annotations

import bisect
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from rich.logging import RichHandler
from supabase import Client, create_client


# =============================================================================
# CONFIGURAÇÃO
# =============================================================================

load_dotenv()

TABLE_FORMS = "data_hs_form_submissions_v2"
TABLE_AD_INTERACTIONS = "data_hs_ad_interactions_v2"
TABLE_PAGE_VIEWS = "data_hs_page_views_v2"
TABLE_FUNNEL_VALIDATION = "validation_funnel_forms_kws_v2"

TARGET_TABLE = "data_hs_conversions_consolidated_v1"

PAGE_SIZE = 1000
UPSERT_BATCH_SIZE = 500

MATCH_WINDOW_MINUTES = 15
VALID_FUNNEL_STAGES = {"tofu", "mofu", "bofu"}

OUTPUT_DIR = Path(__file__).resolve().parent / "outputs/consolidate_conversions"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# LOGS
# =============================================================================

LOG_DIR = Path(__file__).resolve().parent / "logs/consolidate_conversions"
LOG_DIR.mkdir(parents=True, exist_ok=True)

RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE = LOG_DIR / f"consolidate_conversions_{RUN_TIMESTAMP}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        RichHandler(
            rich_tracebacks=True,
            markup=True,
            show_time=False,
        ),
        logging.FileHandler(
            LOG_FILE,
            mode="w",
            encoding="utf-8",
        ),
    ],
)

log = logging.getLogger(__name__)


# =============================================================================
# HELPERS
# =============================================================================

PAID_MEDIUMS = {
    "cpc",
    "ppc",
    "paid",
    "paid social",
    "paid_social",
    "paid-social",
    "paidsocial",
    "paid search",
    "paid_search",
    "paid-search",
    "paidsearch",
    "display",
    "programmatic",
    "remarketing",
    "retargeting",
}

PAID_SOURCES = {
    "google",
    "google ads",
    "google_ads",
    "googleads",
    "adwords",
    "facebook",
    "facebook ads",
    "facebook_ads",
    "fb",
    "instagram",
    "meta",
    "meta ads",
    "meta_ads",
    "linkedin",
    "linkedin ads",
    "linkedin_ads",
    "bing",
    "bing ads",
    "bing_ads",
    "microsoft",
    "microsoft ads",
    "microsoft_ads",
}

NON_PAID_MEDIUMS = {
    "organic",
    "organic social",
    "organic_social",
    "organic-social",
    "referral",
    "email",
    "direct",
    "none",
    "(none)",
}


def normalize_text(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def normalize_lower(value: Any) -> Optional[str]:
    text = normalize_text(value)
    return text.lower() if text else None


def first_nonempty(*values: Any) -> Any:
    for value in values:
        normalized = normalize_text(value)

        if normalized is not None:
            return normalized

    return None


def parse_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()

        if not text:
            return None

        try:
            parsed = datetime.fromisoformat(
                text.replace("Z", "+00:00")
            )
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def datetime_to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def has_ad_attribution_from_values(
    utm_source: Any,
    utm_medium: Any,
    hsa_acc: Any,
    hsa_cam: Any,
    hsa_grp: Any,
    hsa_ad: Any,
    hsa_src: Any,
) -> bool:
    source = normalize_lower(utm_source)
    medium = normalize_lower(utm_medium)

    has_hsa = any(
        normalize_text(value) is not None
        for value in (
            hsa_acc,
            hsa_cam,
            hsa_grp,
            hsa_ad,
            hsa_src,
        )
    )

    has_paid_medium = medium in PAID_MEDIUMS

    has_paid_source_and_medium = (
        source in PAID_SOURCES
        and medium is not None
        and medium not in NON_PAID_MEDIUMS
    )

    return bool(
        has_hsa
        or has_paid_medium
        or has_paid_source_and_medium
    )


def count_nonempty(values: list[Any]) -> int:
    return sum(normalize_text(value) is not None for value in values)


def split_batches(
    rows: list[dict[str, Any]],
    batch_size: int,
) -> list[list[dict[str, Any]]]:
    return [
        rows[index:index + batch_size]
        for index in range(0, len(rows), batch_size)
    ]


def write_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
            default=str,
        )


def write_jsonl(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
                + "\n"
            )


# =============================================================================
# SUPABASE
# =============================================================================

def get_supabase_client() -> Client:
    url = os.environ.get("SUPABASE_URL")

    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
    )

    if not url:
        raise RuntimeError("SUPABASE_URL não encontrado no .env.")

    if not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY ou SUPABASE_KEY "
            "não encontrado no .env."
        )

    return create_client(url, key)


def fetch_entire_table(
    sb: Client,
    table_name: str,
    columns: str = "*",
    order_columns: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 0

    log.info("Baixando tabela completa: %s", table_name)

    while True:
        end = start + PAGE_SIZE - 1

        query = (
            sb.table(table_name)
            .select(columns)
        )

        for column in order_columns or []:
            query = query.order(column)

        response = (
            query
            .range(start, end)
            .execute()
        )

        page = response.data or []
        rows.extend(page)

        log.info(
            "%s | intervalo %s–%s | recebidas: %s | acumulado: %s",
            table_name,
            start,
            end,
            len(page),
            len(rows),
        )

        if len(page) < PAGE_SIZE:
            break

        start += PAGE_SIZE

    log.info(
        "Download concluído: %s | total: %s",
        table_name,
        len(rows),
    )

    return rows


# =============================================================================
# ÍNDICES TEMPORAIS
# =============================================================================

class TemporalContactIndex:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        datetime_column: str,
    ) -> None:
        self.datetime_column = datetime_column
        self.rows_by_contact: dict[str, list[dict[str, Any]]] = {}
        self.times_by_contact: dict[str, list[datetime]] = {}

        grouped: dict[str, list[tuple[datetime, dict[str, Any]]]] = defaultdict(list)

        for row in rows:
            contact_id = normalize_text(row.get("contact_id"))
            occurred_at = parse_datetime(row.get(datetime_column))

            if not contact_id or occurred_at is None:
                continue

            grouped[contact_id].append((occurred_at, row))

        for contact_id, items in grouped.items():
            items.sort(
                key=lambda item: (
                    item[0],
                    normalize_text(item[1].get("event_id")) or "",
                )
            )

            self.times_by_contact[contact_id] = [
                item[0] for item in items
            ]

            self.rows_by_contact[contact_id] = [
                item[1] for item in items
            ]

    def latest_before(
        self,
        contact_id: str,
        target_time: datetime,
        window: timedelta,
    ) -> Optional[dict[str, Any]]:
        times = self.times_by_contact.get(contact_id)

        if not times:
            return None

        position = bisect.bisect_right(times, target_time) - 1

        if position < 0:
            return None

        candidate_time = times[position]

        if candidate_time < target_time - window:
            return None

        return self.rows_by_contact[contact_id][position]


# =============================================================================
# FUNNEL
# =============================================================================

def build_valid_form_ids(
    validation_rows: list[dict[str, Any]],
) -> set[str]:
    valid_form_ids: set[str] = set()

    for row in validation_rows:
        form_id = normalize_text(row.get("form_id"))
        stage = normalize_lower(row.get("selected_funnel_stage"))

        if form_id and stage in VALID_FUNNEL_STAGES:
            valid_form_ids.add(form_id)

    return valid_form_ids


# =============================================================================
# CONSOLIDAÇÃO
# =============================================================================

def fields_added_by_ads(
    form: dict[str, Any],
    ads: Optional[dict[str, Any]],
) -> list[str]:
    if not ads:
        return []

    equivalences = [
        (
            "forms_hs_utm_source",
            form.get("hs_utm_source"),
            ads.get("network"),
        ),
        (
            "forms_hs_utm_campaign",
            form.get("hs_utm_campaign"),
            first_nonempty(
                ads.get("campaign_name"),
                ads.get("utm_campaign"),
            ),
        ),
        (
            "forms_hs_utm_medium",
            form.get("hs_utm_medium"),
            ads.get("utm_medium"),
        ),
        (
            "forms_hsa_acc",
            form.get("hsa_acc"),
            ads.get("ad_account_id"),
        ),
        (
            "forms_hsa_cam",
            form.get("hsa_cam"),
            ads.get("campaign_id"),
        ),
        (
            "forms_hsa_grp",
            form.get("hsa_grp"),
            ads.get("adgroup_id"),
        ),
        (
            "forms_hsa_ad",
            form.get("hsa_ad"),
            ads.get("ad_id"),
        ),
        (
            "forms_hsa_src",
            form.get("hsa_src"),
            ads.get("utm_source"),
        ),
    ]

    return [
        target_column
        for target_column, form_value, ads_value in equivalences
        if normalize_text(form_value) is None
        and normalize_text(ads_value) is not None
    ]


def fields_added_by_pageview(
    values_after_ads: dict[str, Any],
    pageview: Optional[dict[str, Any]],
) -> list[str]:
    if not pageview:
        return []

    equivalences = [
        (
            "forms_hs_utm_source",
            values_after_ads.get("forms_hs_utm_source"),
            pageview.get("utm_source"),
        ),
        (
            "forms_hs_utm_campaign",
            values_after_ads.get("forms_hs_utm_campaign"),
            pageview.get("utm_campaign"),
        ),
        (
            "forms_hs_utm_medium",
            values_after_ads.get("forms_hs_utm_medium"),
            pageview.get("utm_medium"),
        ),
        (
            "forms_hsa_acc",
            values_after_ads.get("forms_hsa_acc"),
            pageview.get("hsa_acc"),
        ),
        (
            "forms_hsa_cam",
            values_after_ads.get("forms_hsa_cam"),
            pageview.get("hsa_cam"),
        ),
        (
            "forms_hsa_grp",
            values_after_ads.get("forms_hsa_grp"),
            pageview.get("hsa_grp"),
        ),
        (
            "forms_hsa_ad",
            values_after_ads.get("forms_hsa_ad"),
            pageview.get("hsa_ad"),
        ),
    ]

    return [
        target_column
        for target_column, current_value, pageview_value in equivalences
        if normalize_text(current_value) is None
        and normalize_text(pageview_value) is not None
    ]


def consolidate_row(
    form: dict[str, Any],
    ads: Optional[dict[str, Any]],
    pageview: Optional[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    ads_fields = fields_added_by_ads(form, ads)

    values_after_ads = {
        "forms_hs_utm_source": first_nonempty(
            form.get("hs_utm_source"),
            ads.get("network") if ads else None,
        ),
        "forms_hs_utm_campaign": first_nonempty(
            form.get("hs_utm_campaign"),
            ads.get("campaign_name") if ads else None,
            ads.get("utm_campaign") if ads else None,
        ),
        "forms_hs_utm_medium": first_nonempty(
            form.get("hs_utm_medium"),
            ads.get("utm_medium") if ads else None,
        ),
        "forms_hsa_acc": first_nonempty(
            form.get("hsa_acc"),
            ads.get("ad_account_id") if ads else None,
        ),
        "forms_hsa_cam": first_nonempty(
            form.get("hsa_cam"),
            ads.get("campaign_id") if ads else None,
        ),
        "forms_hsa_grp": first_nonempty(
            form.get("hsa_grp"),
            ads.get("adgroup_id") if ads else None,
        ),
        "forms_hsa_ad": first_nonempty(
            form.get("hsa_ad"),
            ads.get("ad_id") if ads else None,
        ),
        "forms_hsa_src": first_nonempty(
            form.get("hsa_src"),
            ads.get("utm_source") if ads else None,
        ),
    }

    pageview_fields = fields_added_by_pageview(
        values_after_ads,
        pageview,
    )

    final_values = {
        "forms_hs_utm_source": first_nonempty(
            values_after_ads["forms_hs_utm_source"],
            pageview.get("utm_source") if pageview else None,
        ),
        "forms_hs_utm_campaign": first_nonempty(
            values_after_ads["forms_hs_utm_campaign"],
            pageview.get("utm_campaign") if pageview else None,
        ),
        "forms_hs_utm_medium": first_nonempty(
            values_after_ads["forms_hs_utm_medium"],
            pageview.get("utm_medium") if pageview else None,
        ),
        "forms_hsa_acc": first_nonempty(
            values_after_ads["forms_hsa_acc"],
            pageview.get("hsa_acc") if pageview else None,
        ),
        "forms_hsa_cam": first_nonempty(
            values_after_ads["forms_hsa_cam"],
            pageview.get("hsa_cam") if pageview else None,
        ),
        "forms_hsa_grp": first_nonempty(
            values_after_ads["forms_hsa_grp"],
            pageview.get("hsa_grp") if pageview else None,
        ),
        "forms_hsa_ad": first_nonempty(
            values_after_ads["forms_hsa_ad"],
            pageview.get("hsa_ad") if pageview else None,
        ),
        # Page views não possuem hsa_src.
        "forms_hsa_src": values_after_ads["forms_hsa_src"],
    }

    original_has_ad = bool(form.get("has_ad_attribution"))

    final_has_ad = has_ad_attribution_from_values(
        utm_source=final_values["forms_hs_utm_source"],
        utm_medium=final_values["forms_hs_utm_medium"],
        hsa_acc=final_values["forms_hsa_acc"],
        hsa_cam=final_values["forms_hsa_cam"],
        hsa_grp=final_values["forms_hsa_grp"],
        hsa_ad=final_values["forms_hsa_ad"],
        hsa_src=final_values["forms_hsa_src"],
    )

    extracted_candidates = [
        parse_datetime(form.get("extracted_at")),
        parse_datetime(ads.get("extracted_at")) if ads else None,
        parse_datetime(pageview.get("extracted_at")) if pageview else None,
    ]

    extracted_candidates = [
        value
        for value in extracted_candidates
        if value is not None
    ]

    extracted_at = (
        datetime_to_iso(max(extracted_candidates))
        if extracted_candidates
        else datetime_to_iso(datetime.now(timezone.utc))
    )

    output = {
        "contact_id": normalize_text(form.get("contact_id")),
        "submitted_at": form.get("submitted_at"),
        "form_id": normalize_text(form.get("form_id")),
        "form_title": normalize_text(form.get("form_title")),

        "forms_page_url": normalize_text(form.get("page_url")),
        "forms_base_url": normalize_text(form.get("base_url")),
        "forms_hs_page_title": normalize_text(form.get("hs_page_title")),
        "forms_title": normalize_text(form.get("title")),
        "forms_hs_referrer": normalize_text(form.get("hs_referrer")),

        **final_values,

        "forms_has_ad_attribution": original_has_ad,

        "ads_event_id": normalize_text(
            ads.get("event_id") if ads else None
        ),
        "ads_network": normalize_text(
            ads.get("network") if ads else None
        ),
        "ads_interaction_type": normalize_text(
            ads.get("interaction_type") if ads else None
        ),
        "ads_campaign_id": normalize_text(
            ads.get("campaign_id") if ads else None
        ),
        "ads_campaign_name": normalize_text(
            ads.get("campaign_name") if ads else None
        ),
        "ads_adgroup_id": normalize_text(
            ads.get("adgroup_id") if ads else None
        ),
        "ads_adgroup_name": normalize_text(
            ads.get("adgroup_name") if ads else None
        ),
        "ads_ad_id": normalize_text(
            ads.get("ad_id") if ads else None
        ),
        "ads_ad_name": normalize_text(
            ads.get("ad_name") if ads else None
        ),
        "ads_ad_account_id": normalize_text(
            ads.get("ad_account_id") if ads else None
        ),
        "ads_utm_source": normalize_text(
            ads.get("utm_source") if ads else None
        ),
        "ads_utm_campaign": normalize_text(
            ads.get("utm_campaign") if ads else None
        ),
        "ads_utm_medium": normalize_text(
            ads.get("utm_medium") if ads else None
        ),

        "final_has_ad_attribution": final_has_ad,
        "extracted_at": extracted_at,
    }

    audit = {
        "ads_matched": ads is not None,
        "pageview_matched": pageview is not None,
        "ads_fields_added": ads_fields,
        "pageview_fields_added": pageview_fields,
        "original_has_ad": original_has_ad,
        "final_has_ad": final_has_ad,
    }

    return output, audit


# =============================================================================
# REVIEW
# =============================================================================

def build_review(
    source_counts: dict[str, int],
    eligible_forms: int,
    consolidated_rows: list[dict[str, Any]],
    audits: list[dict[str, Any]],
    skipped_missing_pk: int,
    duplicate_pk_count: int,
) -> dict[str, Any]:
    ads_matches = sum(
        audit["ads_matched"]
        for audit in audits
    )

    pageview_matches = sum(
        audit["pageview_matched"]
        for audit in audits
    )

    enriched_by_ads = sum(
        bool(audit["ads_fields_added"])
        for audit in audits
    )

    enriched_by_pageviews = sum(
        bool(audit["pageview_fields_added"])
        for audit in audits
    )

    enriched_by_both = sum(
        bool(audit["ads_fields_added"])
        and bool(audit["pageview_fields_added"])
        for audit in audits
    )

    not_enriched = sum(
        not audit["ads_fields_added"]
        and not audit["pageview_fields_added"]
        for audit in audits
    )

    changed_false_to_true = sum(
        not audit["original_has_ad"]
        and audit["final_has_ad"]
        for audit in audits
    )

    final_true = sum(
        row["final_has_ad_attribution"] is True
        for row in consolidated_rows
    )

    fields_added_ads: dict[str, int] = defaultdict(int)
    fields_added_pageviews: dict[str, int] = defaultdict(int)

    for audit in audits:
        for field in audit["ads_fields_added"]:
            fields_added_ads[field] += 1

        for field in audit["pageview_fields_added"]:
            fields_added_pageviews[field] += 1

    return {
        "status": "ready_for_review",
        "generated_at": datetime_to_iso(datetime.now(timezone.utc)),
        "match_window_minutes": MATCH_WINDOW_MINUTES,
        "valid_funnel_stages": sorted(VALID_FUNNEL_STAGES),
        "source_counts": source_counts,
        "eligible_main_forms": eligible_forms,
        "consolidated_rows": len(consolidated_rows),
        "skipped_missing_primary_key": skipped_missing_pk,
        "duplicate_primary_keys_removed": duplicate_pk_count,
        "matches": {
            "ads_matches": ads_matches,
            "pageview_matches": pageview_matches,
        },
        "enrichment": {
            "enriched_by_ads": enriched_by_ads,
            "enriched_by_pageviews_after_ads": enriched_by_pageviews,
            "enriched_by_both": enriched_by_both,
            "not_enriched": not_enriched,
            "changed_false_to_true": changed_false_to_true,
            "final_has_ad_true": final_true,
        },
        "fields_added_by_ads": dict(sorted(fields_added_ads.items())),
        "fields_added_by_pageviews": dict(
            sorted(fields_added_pageviews.items())
        ),
    }


def print_review(review: dict[str, Any]) -> None:
    source = review["source_counts"]
    matches = review["matches"]
    enrichment = review["enrichment"]

    log.info("")
    log.info("=" * 86)
    log.info("REVIEW DA CONSOLIDAÇÃO")
    log.info("=" * 86)

    log.info("Forms baixados:                  %s", source["forms"])
    log.info("Ad interactions baixadas:        %s", source["ad_interactions"])
    log.info("Page views baixadas:             %s", source["page_views"])
    log.info("Regras de funnel baixadas:       %s", source["funnel_validation"])

    log.info("")
    log.info("Forms TOFU/MOFU/BOFU elegíveis:  %s", review["eligible_main_forms"])
    log.info("Linhas consolidadas:             %s", review["consolidated_rows"])
    log.info("Linhas sem PK ignoradas:         %s", review["skipped_missing_primary_key"])
    log.info("PKs duplicadas removidas:        %s", review["duplicate_primary_keys_removed"])

    log.info("")
    log.info("Forms com match de ads:          %s", matches["ads_matches"])
    log.info("Forms com match de page view:    %s", matches["pageview_matches"])

    log.info("")
    log.info("Enriquecidos por ads:            %s", enrichment["enriched_by_ads"])
    log.info(
        "Enriquecidos por page views:     %s",
        enrichment["enriched_by_pageviews_after_ads"],
    )
    log.info("Enriquecidos pelas duas fontes:  %s", enrichment["enriched_by_both"])
    log.info("Sem enriquecimento adicional:    %s", enrichment["not_enriched"])
    log.info("Has ad FALSE → TRUE:             %s", enrichment["changed_false_to_true"])
    log.info("Final has ad = TRUE:             %s", enrichment["final_has_ad_true"])

    log.info("")
    log.info("Campos adicionados por ads:")

    for field, count in review["fields_added_by_ads"].items():
        log.info("  %-30s %s", field, count)

    log.info("")
    log.info("Campos adicionados por page views:")

    for field, count in review["fields_added_by_pageviews"].items():
        log.info("  %-30s %s", field, count)

    log.info("=" * 86)


# =============================================================================
# ENVIO
# =============================================================================

def confirm_upload() -> bool:
    while True:
        answer = input(
            "\nSubir essas linhas para "
            f"public.{TARGET_TABLE}? [s/N]: "
        ).strip().lower()

        if answer in {"s", "sim", "y", "yes"}:
            return True

        if answer in {"", "n", "nao", "não", "no"}:
            return False

        print("Resposta inválida. Digite s ou n.")


def upload_rows(
    sb: Client,
    rows: list[dict[str, Any]],
) -> int:
    batches = split_batches(rows, UPSERT_BATCH_SIZE)
    uploaded = 0

    log.info("")
    log.info(
        "Iniciando envio para %s em %s lote(s).",
        TARGET_TABLE,
        len(batches),
    )

    for batch_number, batch in enumerate(batches, start=1):
        try:
            response = (
                sb.table(TARGET_TABLE)
                .upsert(
                    batch,
                    on_conflict="contact_id,submitted_at",
                )
                .execute()
            )

            uploaded += len(batch)

            log.info(
                "Lote %s/%s enviado: %s linha(s). Total: %s.",
                batch_number,
                len(batches),
                len(batch),
                uploaded,
            )

        except Exception:
            log.exception(
                "Erro ao enviar lote %s/%s.",
                batch_number,
                len(batches),
            )
            raise

    return uploaded


# =============================================================================
# EXECUÇÃO
# =============================================================================

def main() -> None:
    log.info("=" * 86)
    log.info("CONSOLIDAÇÃO DE CONVERSÕES HUBSPOT")
    log.info("=" * 86)
    log.info("Janela de matching: %s minutos", MATCH_WINDOW_MINUTES)
    log.info("Tabela de destino: %s", TARGET_TABLE)
    log.info("Arquivo de log: %s", LOG_FILE)

    sb = get_supabase_client()

    forms = fetch_entire_table(
        sb,
        TABLE_FORMS,
    )

    ad_interactions = fetch_entire_table(
        sb,
        TABLE_AD_INTERACTIONS,
    )

    page_views = fetch_entire_table(
        sb,
        TABLE_PAGE_VIEWS,
    )

    funnel_validation = fetch_entire_table(
        sb,
        TABLE_FUNNEL_VALIDATION,
        columns="form_id,selected_funnel_stage",
    )

    source_counts = {
        "forms": len(forms),
        "ad_interactions": len(ad_interactions),
        "page_views": len(page_views),
        "funnel_validation": len(funnel_validation),
    }

    valid_form_ids = build_valid_form_ids(funnel_validation)

    eligible_forms = [
        form
        for form in forms
        if normalize_text(form.get("form_id")) in valid_form_ids
    ]

    log.info(
        "Forms principais elegíveis: %s de %s.",
        len(eligible_forms),
        len(forms),
    )

    ads_index = TemporalContactIndex(
        ad_interactions,
        datetime_column="occurred_at",
    )

    pageviews_index = TemporalContactIndex(
        page_views,
        datetime_column="viewed_at",
    )

    window = timedelta(minutes=MATCH_WINDOW_MINUTES)

    consolidated_by_pk: dict[
        tuple[str, str],
        tuple[dict[str, Any], dict[str, Any]],
    ] = {}

    skipped_missing_pk = 0
    duplicate_pk_count = 0

    for index, form in enumerate(eligible_forms, start=1):
        contact_id = normalize_text(form.get("contact_id"))
        submitted_at_raw = form.get("submitted_at")
        submitted_at = parse_datetime(submitted_at_raw)

        if not contact_id or submitted_at is None:
            skipped_missing_pk += 1
            log.warning(
                "Form ignorado por ausência de PK válida: "
                "contact_id=%s | submitted_at=%s",
                contact_id,
                submitted_at_raw,
            )
            continue

        ads_match = ads_index.latest_before(
            contact_id=contact_id,
            target_time=submitted_at,
            window=window,
        )

        pageview_match = pageviews_index.latest_before(
            contact_id=contact_id,
            target_time=submitted_at,
            window=window,
        )

        consolidated_row, audit = consolidate_row(
            form=form,
            ads=ads_match,
            pageview=pageview_match,
        )

        pk = (
            contact_id,
            str(consolidated_row["submitted_at"]),
        )

        if pk in consolidated_by_pk:
            duplicate_pk_count += 1

        consolidated_by_pk[pk] = (
            consolidated_row,
            audit,
        )

        if index % 5000 == 0:
            log.info(
                "Consolidação em andamento: %s/%s forms.",
                index,
                len(eligible_forms),
            )

    consolidated_rows = [
        item[0]
        for item in consolidated_by_pk.values()
    ]

    audits = [
        item[1]
        for item in consolidated_by_pk.values()
    ]

    consolidated_rows.sort(
        key=lambda row: (
            row["contact_id"],
            str(row["submitted_at"]),
        )
    )

    output_jsonl = (
        OUTPUT_DIR
        / f"conversions_consolidated_{RUN_TIMESTAMP}.jsonl"
    )

    report_json = (
        OUTPUT_DIR
        / f"conversions_consolidated_report_{RUN_TIMESTAMP}.json"
    )

    write_jsonl(output_jsonl, consolidated_rows)

    review = build_review(
        source_counts=source_counts,
        eligible_forms=len(eligible_forms),
        consolidated_rows=consolidated_rows,
        audits=audits,
        skipped_missing_pk=skipped_missing_pk,
        duplicate_pk_count=duplicate_pk_count,
    )

    review["output_jsonl"] = str(output_jsonl)
    review["log_file"] = str(LOG_FILE)
    review["target_table"] = TARGET_TABLE

    write_json(report_json, review)
    print_review(review)

    log.info("Arquivo consolidado: %s", output_jsonl)
    log.info("Relatório: %s", report_json)

    if not consolidated_rows:
        log.warning("Nenhuma linha disponível para envio.")
        return

    if not confirm_upload():
        log.info(
            "Envio cancelado. Os arquivos locais foram preservados."
        )
        return

    uploaded = upload_rows(
        sb=sb,
        rows=consolidated_rows,
    )

    review["status"] = "uploaded"
    review["uploaded_at"] = datetime_to_iso(
        datetime.now(timezone.utc)
    )
    review["uploaded_rows"] = uploaded

    write_json(report_json, review)

    log.info("")
    log.info("=" * 86)
    log.info("ENVIO CONCLUÍDO")
    log.info("Linhas enviadas: %s", uploaded)
    log.info("Tabela: %s", TARGET_TABLE)
    log.info("=" * 86)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.warning("Execução interrompida pelo usuário.")
        sys.exit(130)
    except Exception:
        log.exception("Erro fatal durante a consolidação.")
        sys.exit(1)