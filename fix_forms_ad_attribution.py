#!/usr/bin/env python3

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from rich.logging import RichHandler
from supabase import Client, create_client


# =============================================================================
# CONFIGURAÇÃO
# =============================================================================

load_dotenv()

TABLE_NAME = "data_hs_form_submissions_v2"

PAGE_SIZE = 1000

# True: apenas verifica e mostra o que mudaria.
# False: atualiza efetivamente o Supabase.
DRY_RUN = False


# =============================================================================
# LOGS
# =============================================================================

LOG_DIR = Path(__file__).resolve().parent / "logs/fix_forms_ad_attribution"
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOG_DIR / (
    f"fix_forms_ad_attribution_{datetime.now():%Y%m%d_%H%M%S}.log"
)

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
# REGRAS DE ATRIBUIÇÃO
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


def normalize(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip().lower()
    return text or None


def has_value(value: Any) -> bool:
    if value is None:
        return False

    if isinstance(value, str):
        return bool(value.strip())

    return True


def calculate_has_ad_attribution(row: dict[str, Any]) -> bool:
    source = normalize(row.get("hs_utm_source"))
    medium = normalize(row.get("hs_utm_medium"))

    has_hsa = any(
        has_value(value)
        for value in (
            row.get("hsa_acc"),
            row.get("hsa_cam"),
            row.get("hsa_grp"),
            row.get("hsa_ad"),
            row.get("hsa_src"),
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


# =============================================================================
# SUPABASE
# =============================================================================

def get_supabase_client() -> Client:
    supabase_url = os.environ.get("SUPABASE_URL")

    supabase_key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
    )

    if not supabase_url:
        raise RuntimeError("SUPABASE_URL não encontrado no .env.")

    if not supabase_key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY ou SUPABASE_KEY "
            "não encontrado no .env."
        )

    return create_client(supabase_url, supabase_key)


def fetch_all_forms(sb: Client) -> list[dict[str, Any]]:
    columns = ",".join(
        [
            "contact_id",
            "submitted_at",
            "hs_utm_source",
            "hs_utm_campaign",
            "hs_utm_medium",
            "hsa_acc",
            "hsa_cam",
            "hsa_grp",
            "hsa_ad",
            "hsa_src",
            "has_ad_attribution",
        ]
    )

    rows: list[dict[str, Any]] = []
    start = 0

    while True:
        end = start + PAGE_SIZE - 1

        log.info(
            "Buscando linhas %s até %s da tabela %s...",
            start,
            end,
            TABLE_NAME,
        )

        response = (
            sb.table(TABLE_NAME)
            .select(columns)
            .order("contact_id")
            .order("submitted_at")
            .range(start, end)
            .execute()
        )

        page = response.data or []
        rows.extend(page)

        log.info(
            "Página recebida: %s linha(s). Total acumulado: %s.",
            len(page),
            len(rows),
        )

        if len(page) < PAGE_SIZE:
            break

        start += PAGE_SIZE

    return rows


def update_row(
    sb: Client,
    contact_id: str,
    submitted_at: str,
    new_value: bool,
) -> None:
    (
        sb.table(TABLE_NAME)
        .update({"has_ad_attribution": new_value})
        .eq("contact_id", contact_id)
        .eq("submitted_at", submitted_at)
        .execute()
    )


# =============================================================================
# EXECUÇÃO
# =============================================================================

def main() -> None:
    log.info("=" * 80)
    log.info("CORREÇÃO GLOBAL DE HAS_AD_ATTRIBUTION")
    log.info("=" * 80)
    log.info("Tabela: %s", TABLE_NAME)
    log.info("DRY_RUN: %s", DRY_RUN)
    log.info("Log: %s", LOG_FILE)

    sb = get_supabase_client()
    rows = fetch_all_forms(sb)

    already_correct = 0
    changed_to_true = 0
    changed_to_false = 0
    missing_pk = 0
    errors = 0

    total_to_update = 0

    for row in rows:
        contact_id = row.get("contact_id")
        submitted_at = row.get("submitted_at")

        if not contact_id or not submitted_at:
            missing_pk += 1
            log.warning(
                "Linha ignorada por ausência de PK: contact_id=%s | submitted_at=%s",
                contact_id,
                submitted_at,
            )
            continue

        calculated_value = calculate_has_ad_attribution(row)
        current_value = row.get("has_ad_attribution")

        if current_value == calculated_value:
            already_correct += 1
            continue

        total_to_update += 1

        log.info(
            "Alteração necessária | contact_id=%s | submitted_at=%s | "
            "atual=%s | novo=%s | source=%s | medium=%s | campaign=%s",
            contact_id,
            submitted_at,
            current_value,
            calculated_value,
            row.get("hs_utm_source"),
            row.get("hs_utm_medium"),
            row.get("hs_utm_campaign"),
        )

        if DRY_RUN:
            if calculated_value:
                changed_to_true += 1
            else:
                changed_to_false += 1

            continue

        try:
            update_row(
                sb=sb,
                contact_id=str(contact_id),
                submitted_at=str(submitted_at),
                new_value=calculated_value,
            )

            if calculated_value:
                changed_to_true += 1
            else:
                changed_to_false += 1

        except Exception:
            errors += 1
            log.exception(
                "Erro ao atualizar contact_id=%s | submitted_at=%s",
                contact_id,
                submitted_at,
            )

    log.info("")
    log.info("=" * 80)
    log.info("RESUMO")
    log.info("=" * 80)
    log.info("Total de linhas lidas: %s", len(rows))
    log.info("Já estavam corretas: %s", already_correct)
    log.info("Precisavam de alteração: %s", total_to_update)
    log.info("Alterações para TRUE: %s", changed_to_true)
    log.info("Alterações para FALSE: %s", changed_to_false)
    log.info("Linhas sem PK: %s", missing_pk)
    log.info("Erros: %s", errors)

    if DRY_RUN:
        log.info("DRY RUN concluído. Nenhuma linha foi alterada.")
    else:
        log.info(
            "Atualização concluída. %s linha(s) alterada(s).",
            changed_to_true + changed_to_false,
        )

    log.info("=" * 80)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.warning("Execução interrompida pelo usuário.")
        raise SystemExit(130)
    except Exception:
        log.exception("Erro fatal durante a correção.")
        raise SystemExit(1)