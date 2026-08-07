"""
dashspy_hubspot.py
Coleta dados de HubSpot (Contacts e Deals) e centraliza no Supabase.
"""

import os
import time
import json
import logging
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client, Client
from rich.logging import RichHandler
from pathlib import Path
# ---------------------------------------------------------------------------
# Configuração de logging
# ---------------------------------------------------------------------------

LOG_DIR = Path(__file__).resolve().parent / "logs/dashspy"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"dashspy_hubspot_{os.getpid()}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        RichHandler(rich_tracebacks=True, markup=True),
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    ]
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Carrega variáveis de ambiente
# ---------------------------------------------------------------------------
load_dotenv()

HUBSPOT_TOKEN           = os.environ["HUBSPOT_TOKEN"]
SUPABASE_URL            = os.environ["SUPABASE_URL"]
SUPABASE_KEY            = os.environ["SUPABASE_KEY"]
PATH_OUTPUTS_M          = os.environ["PATH_OUTPUTS_M"]

# ---------------------------------------------------------------------------
# Constantes de Supabase (nomes das tabelas)
# ---------------------------------------------------------------------------
TABLE_HUB       = "data_hs_contacts_v2"
TABLE_DEALS     = "data_hs_deals_v2"

# Data de início histórico
HUBSPOT_HISTORY_START   = "2025-08-01"

DASHSPY_BUILD = "2026-07-06-adaptive-v3"

# ---------------------------------------------------------------------------
# Helpers de data
# ---------------------------------------------------------------------------

def _recording_ts_to_ms(recording_ts: str) -> int:
    """Converte 'YYYY-MM-DDTHH:MM:SS UTC' (formato do recording_ts) pra epoch ms UTC."""
    cleaned = recording_ts.replace(" UTC", "")
    dt = datetime.strptime(cleaned, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


DAY_MS = 24 * 60 * 60 * 1000

HUBSPOT_WINDOW_MAX_RETRIES = 3
HUBSPOT_WINDOW_RETRY_WAIT = 10
HUBSPOT_SEARCH_RESULT_LIMIT = 10_000
HUBSPOT_MIN_WINDOW_MS = 1


class RetryPointPending(RuntimeError):
    """Indica que uma coleta ficou incompleta e possui um retry point salvo."""


class HubSpotSearchLimitError(RuntimeError):
    """Indica que uma janela ultrapassou o limite de 10.000 resultados da Search API."""

    def __init__(
        self,
        object_label: str,
        since_ms: int,
        until_ms: int,
        after: str | int | None = None,
        total: int | None = None,
    ) -> None:
        self.object_label = object_label
        self.since_ms = since_ms
        self.until_ms = until_ms
        self.after = after
        self.total = total

        details = []
        if total is not None:
            details.append(f"total={total}")
        if after is not None:
            details.append(f"after={after}")

        suffix = f" ({', '.join(details)})" if details else ""
        super().__init__(
            f"{object_label}: janela {since_ms}→{until_ms} ultrapassou "
            f"o limite de {HUBSPOT_SEARCH_RESULT_LIMIT} resultados{suffix}."
        )


def _is_hubspot_search_limit_exception(exc: Exception) -> bool:
    """
    Detecta o limite de 10.000 mesmo se, por algum motivo, ele chegar ao
    coletor como requests.HTTPError em vez de HubSpotSearchLimitError.
    """
    if isinstance(exc, HubSpotSearchLimitError):
        return True

    if not isinstance(exc, requests.HTTPError):
        return False

    response = getattr(exc, "response", None)
    if response is None or response.status_code != 400:
        return False

    request = getattr(response, "request", None)
    body = getattr(request, "body", None)

    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")

    try:
        payload = json.loads(body) if isinstance(body, str) and body else {}
    except (TypeError, ValueError):
        payload = {}

    after = payload.get("after")
    try:
        return after is not None and int(after) >= HUBSPOT_SEARCH_RESULT_LIMIT
    except (TypeError, ValueError):
        return False


def _split_hubspot_window(
    *,
    object_label: str,
    current_start: int,
    current_end: int,
    windows: list[list[int]],
) -> bool:
    """Divide a janela atual ao meio e recoloca as metades no início da fila."""
    duration_ms = current_end - current_start

    if duration_ms <= HUBSPOT_MIN_WINDOW_MS:
        log.error(
            "%s: a janela mínima %s→%s ainda ultrapassa 10.000 resultados. "
            "Não é possível subdividir mais por tempo.",
            object_label,
            current_start,
            current_end,
        )
        return False

    midpoint = current_start + duration_ms // 2

    if midpoint <= current_start or midpoint >= current_end:
        log.error(
            "%s: não foi possível subdividir a janela %s→%s.",
            object_label,
            current_start,
            current_end,
        )
        return False

    log.warning(
        "%s: janela %s→%s ultrapassou 10.000 resultados. "
        "Dividindo em %s→%s e %s→%s.",
        object_label,
        current_start,
        current_end,
        current_start,
        midpoint,
        midpoint,
        current_end,
    )

    windows.insert(0, [midpoint, current_end])
    windows.insert(0, [current_start, midpoint])
    return True

def _ms_to_iso(timestamp_ms: int) -> str:
    """Converte epoch ms UTC para ISO 8601, apenas para exibição nos logs."""
    return datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=timezone.utc,
    ).isoformat()


def _retry_directory() -> Path:
    """Retorna a pasta onde ficam os retry points de Contacts e Deals."""
    path = Path(PATH_OUTPUTS_M) / "_retry_points"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _retry_state_path(source: str) -> Path:
    return _retry_directory() / f"{source}_retry_state.json"


def _retry_raw_path(source: str, recording_ts: str) -> Path:
    safe_ts = recording_ts.replace(":", "-").replace(" ", "_")
    return _retry_directory() / f"{source}_partial_raw_{safe_ts}.json"


def _write_json_atomic(path: Path, payload) -> None:
    """Grava JSON de modo atômico para reduzir risco de checkpoint corrompido."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")

    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, default=str)

    temp_path.replace(path)


def _read_json_file(path: Path):
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _load_retry_state(source: str) -> dict | None:
    path = _retry_state_path(source)
    if not path.exists():
        return None
    return _read_json_file(path)


def _save_retry_state(source: str, state: dict) -> None:
    payload = {
        **state,
        "source": source,
        "updated_at": datetime.now(
            tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S.%f UTC"),
    }
    _write_json_atomic(_retry_state_path(source), payload)


def _clear_retry_state(source: str) -> None:
    """Remove o retry point somente depois de um envio bem-sucedido."""
    state = _load_retry_state(source)

    if state:
        partial_raw_path = state.get("partial_raw_path")
        if partial_raw_path:
            Path(partial_raw_path).unlink(missing_ok=True)

    _retry_state_path(source).unlink(missing_ok=True)
    log.info("Retry point de %s removido.", source)


# ---------------------------------------------------------------------------
# Supabase — cliente e utilitários
# ---------------------------------------------------------------------------

def get_supabase_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_last_recording(sb: Client, table: str) -> int | None:
    """
    Retorna o timestamp (epoch ms UTC) da última coleta nessa tabela
    (max dt_h_recording_data), ou None se a tabela estiver vazia.

    Usado por run_hubspot_collect / run_deals_collect pra decidir entre
    carga full (tabela vazia) e incremental (delta desde a última coleta).
    """
    response = (
        sb.table(table)
        .select("dt_h_recording_data")
        .order("dt_h_recording_data", desc=True)
        .limit(1)
        .execute()
    )
    if not response.data:
        return None
    val = response.data[0].get("dt_h_recording_data")
    if not val:
        return None
    val_str = str(val).replace(" UTC", "").rstrip("Z")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(val_str, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(val_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError):
        log.warning("Não consegui parsear dt_h_recording_data='%s'", val)
        return None


def insert_rows(sb: Client, table: str, rows: list[dict], batch_size: int = 500, on_conflict: str | None = None) -> None:
    if not rows:
        log.info("Nenhuma linha para inserir em %s.", table)
        return
    total = len(rows)
    for i in range(0, total, batch_size):
        batch = rows[i:i + batch_size]
        if on_conflict:
            sb.table(table).upsert(batch, on_conflict=on_conflict).execute()
        else:
            sb.table(table).insert(batch).execute()
        log.info("Inseridas %d/%d linhas em %s.", min(i + batch_size, total), total, table)
    log.info("Total inserido em %s: %d linhas.", table, total)


# ---------------------------------------------------------------------------
# Utilitários de arquivo temporário e confirmação
# ---------------------------------------------------------------------------

def save_temp(platform: str, rows: list[dict], recording_ts: str) -> str:
    """Salva os registros em um arquivo JSON temporário e retorna o caminho."""
    ts = recording_ts.replace(":", "-").replace(" ", "_")

    OUTPUT_DIR = Path(PATH_OUTPUTS_M)
    OUTPUT_DIR.mkdir(exist_ok=True)

    path = OUTPUT_DIR / f"{platform}_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    log.info("Dados salvos em: %s (%d linhas)", path, len(rows))
    return str(path)


def aguardar_confirmacao(nome: str, path: str) -> bool:
    """Exibe o caminho do arquivo e pede confirmação manual no terminal."""
    print(f"\n  Arquivo: {path}")
    resposta = input(f"  Enviar dados do {nome} para o Supabase? [s/N]: ").strip().lower()
    return resposta == "s"


# ---------------------------------------------------------------------------
# HUBSPOT
# Schema: ver tabela teste_01 no README
# ---------------------------------------------------------------------------

HUBSPOT_BASE_URL = "https://api.hubapi.com/crm/v3/objects"

CONTACT_PROPERTIES = [
    "hs_object_id",
    "createdate",
    "lastmodifieddate",

    # Identificação / contato
    "firstname",
    "lastname",
    "email",
    "phone",
    "company",

    # Lifecycle / status / owner
    "lifecyclestage",
    "hs_lead_status",
    "hubspot_owner_id",
    "hubspot_owner_assigneddate",
    "hubspot_team_id",

    # Deals
    "num_associated_deals",
    "first_deal_created_date",
    "stage_of_the_deal",

    # Original Traffic Source
    "hs_analytics_source",
    "hs_analytics_source_data_1",
    "hs_analytics_source_data_2",

    # Latest Traffic Source
    "hs_latest_source",
    "hs_latest_source_data_1",
    "hs_latest_source_data_2",
    "hs_latest_source_timestamp",

    # Conversions
    "hs_analytics_last_touch_converting_campaign",
    "first_conversion_date",
    "recent_conversion_date",
    "conversion_de_lead",
    "form_submitted",

    # Record source
    "hs_object_source_label",
    "hs_object_source_detail_1",

    # UTMs
    "utm_term",
    "utm_medium",
    "utm_source",
    "utm_content",
    "utm_campaign",

    # Engajamento / atividades
    "hs_sa_first_engagement_date",
    "notes_last_updated",
    "notes_last_contacted",
    "hs_last_sales_activity_timestamp",

    # Qualificação / perfil
    "numemployees",
    "jobtitle",
    "not_qualified_reason",
    "estado_de_lead",
    "motivo_no_interesado",

    # Localização
    "country",
    "region",
    "main_country",

    # Campo customizado
    "como_ficou_sabendo_sobre_nos_",
]


def _hub_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type":  "application/json",
    }


def _parse_timestamp(val: str | None) -> str | None:
    """Converte timestamp HubSpot (milissegundos ou ISO 8601) para formato ISO 8601."""
    if not val:
        return None
    try:
        # Tenta milissegundos primeiro
        ts = int(val) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f UTC")
    except (ValueError, OSError):
        pass
    try:
        # Tenta ISO 8601
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f UTC")
    except (ValueError, AttributeError):
        return None


def _fetch_hubspot_contacts_window(since_ms: int, until_ms: int,
                                    filter_property: str = "createdate",
                                    max_retries: int = 3) -> list[dict]:
    """
    Busca contacts em uma janela de tempo [since_ms, until_ms) usando
    `filter_property` como coluna temporal.

    - `createdate` (default): pega só contatos criados no intervalo (carga full inicial).
    - `lastmodifieddate`: pega criados + modificados no intervalo (carga incremental).

    Quando a janela ultrapassa 10.000 resultados, levanta
    HubSpotSearchLimitError para que o coletor externo subdivida a janela.
    """
    url = f"{HUBSPOT_BASE_URL}/contacts/search"
    last_obtained = 0
    last_expected = 0

    for attempt in range(1, max_retries + 1):
        all_contacts: list[dict] = []
        after: str | None = None
        expected_total: int | None = None

        while True:
            payload: dict = {
                "filterGroups": [
                    {
                        "filters": [
                            {"propertyName": filter_property, "operator": "GTE", "value": str(since_ms)},
                            {"propertyName": filter_property, "operator": "LT",  "value": str(until_ms)},
                        ]
                    }
                ],
                "properties": CONTACT_PROPERTIES,
                "limit":      100,
            }
            if after:
                payload["after"] = after

            resp = requests.post(
                url,
                headers=_hub_headers(),
                json=payload,
                timeout=300,
            )

            if not resp.ok:
                log.error(
                    "HubSpot Contacts retornou erro.\n"
                    "  Status: %s\n"
                    "  Janela: %s → %s\n"
                    "  Propriedade: %s\n"
                    "  Cursor after: %s\n"
                    "  Resposta: %s",
                    resp.status_code,
                    since_ms,
                    until_ms,
                    filter_property,
                    after,
                    resp.text[:3000],
                )

                try:
                    after_int = int(after) if after is not None else None
                except (TypeError, ValueError):
                    after_int = None

                if resp.status_code == 400 and after_int is not None and after_int >= HUBSPOT_SEARCH_RESULT_LIMIT:
                    raise HubSpotSearchLimitError(
                        "Contacts",
                        since_ms,
                        until_ms,
                        after=after,
                        total=expected_total,
                    )

            resp.raise_for_status()
            data = resp.json()

            if expected_total is None:
                expected_total = int(data.get("total", 0))

                if expected_total > HUBSPOT_SEARCH_RESULT_LIMIT:
                    raise HubSpotSearchLimitError(
                        "Contacts",
                        since_ms,
                        until_ms,
                        total=expected_total,
                    )

            results = data.get("results", [])
            all_contacts.extend(results)

            paging = data.get("paging", {})
            next_after = paging.get("next", {}).get("after") if paging else None

            try:
                next_after_int = int(next_after) if next_after is not None else None
            except (TypeError, ValueError):
                next_after_int = None

            if next_after_int is not None and next_after_int >= HUBSPOT_SEARCH_RESULT_LIMIT:
                raise HubSpotSearchLimitError(
                    "Contacts",
                    since_ms,
                    until_ms,
                    after=next_after,
                    total=expected_total,
                )

            after = next_after
            if not after:
                break

        last_obtained = len(all_contacts)
        last_expected = expected_total or 0

        if last_obtained >= last_expected:
            return all_contacts

        log.warning(
            "  Paginação incompleta: esperados %d, obtidos %d. Reintento %d/%d.",
            last_expected,
            last_obtained,
            attempt,
            max_retries,
        )

    raise RuntimeError(
        "Paginação incompleta na janela de Contacts: "
        f"esperados={last_expected}, "
        f"obtidos={last_obtained}, "
        f"tentativas={max_retries}"
    )

def fetch_hubspot_contacts(since_ms: int) -> list[dict]:
    """
    Busca todos os contacts criados a partir de `since_ms` paginando por janelas
    mensais para contornar o limite de 10.000 resultados da Search API.
    """
    log.info("HubSpot: buscando contacts a partir de %s ms.", since_ms)
    all_contacts: list[dict] = []

    window_start = since_ms
    now_ms       = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    while window_start < now_ms:
        # Janela de 1 dia (evita timeout e o limite de 10.000 da Search API)
        window_end = min(window_start + 1 * 24 * 3600 * 1000, now_ms)
        batch = _fetch_hubspot_contacts_window(window_start, window_end)
        log.info("  Janela %s→%s: %d contacts.", window_start, window_end, len(batch))
        all_contacts.extend(batch)
        window_start = window_end

    log.info("HubSpot: %d contacts obtidos no total.", len(all_contacts))
    return all_contacts


PIPELINE_NAMES = {
    "3008170":   "Humand Customer Journey",
    "78973053":  "Revenue Expansions",
    "10631004":  "Partnerships",
    "79532978":  "Business Partner",
    "743780424": "BDRs",
}


EXCLUDED_PIPELINES = {"Business Partner", "BDRs", "Partnerships"}


def _fetch_valid_deal_flags(contact_ids: list[str]) -> dict[str, bool]:
    """
    Para cada contact ID, determina si tiene al menos un deal con pipeline válido
    (no Business Partner, BDRs, ni Partnerships) o no tiene deals.
    Retorna {contact_id: has_valid_deal}.
    - True: tiene deal válido O no tiene deals
    - False: todos sus deals están en pipelines excluidos
    """
    if not contact_ids:
        return {}

    result: dict[str, bool] = {}
    contact_to_deals: dict[str, list[str]] = {}

    # Step 1: Batch get associations contact -> deals (100 per call)
    assoc_url = "https://api.hubapi.com/crm/v4/associations/contacts/deals/batch/read"
    for i in range(0, len(contact_ids), 100):
        batch = contact_ids[i:i+100]
        payload = {"inputs": [{"id": cid} for cid in batch]}
        try:
            resp = requests.post(assoc_url, headers=_hub_headers(), json=payload, timeout=60)
            if resp.status_code != 200:
                continue
            for item in resp.json().get("results", []):
                from_id = str(item.get("from", {}).get("id", ""))
                deal_ids = [str(t.get("toObjectId", "")) for t in item.get("to", [])]
                if from_id:
                    if deal_ids:
                        contact_to_deals[from_id] = deal_ids
                    else:
                        result[from_id] = True  # no deals → valid
        except Exception:
            continue

    # Contacts not returned by API have no deals → valid
    for cid in contact_ids:
        if cid not in contact_to_deals and cid not in result:
            result[cid] = True

    if not contact_to_deals:
        return result

    # Step 2: Collect all unique deal IDs
    all_deal_ids = list({did for dids in contact_to_deals.values() for did in dids if did})

    # Step 3: Batch get deal pipeline property (100 per call)
    deal_pipelines: dict[str, str] = {}
    deals_url = "https://api.hubapi.com/crm/v3/objects/deals/batch/read"
    for i in range(0, len(all_deal_ids), 100):
        batch = all_deal_ids[i:i+100]
        payload = {"inputs": [{"id": did} for did in batch], "properties": ["pipeline"]}
        try:
            resp = requests.post(deals_url, headers=_hub_headers(), json=payload, timeout=60)
            if resp.status_code != 200:
                continue
            for deal in resp.json().get("results", []):
                did = str(deal.get("id", ""))
                pipe_id = deal.get("properties", {}).get("pipeline", "")
                deal_pipelines[did] = PIPELINE_NAMES.get(pipe_id, pipe_id)
        except Exception:
            continue

    # Step 4: For each contact, check if ANY deal has a valid pipeline
    for cid, deal_ids in contact_to_deals.items():
        pipelines = [deal_pipelines.get(did, "") for did in deal_ids]
        has_valid = any(p not in EXCLUDED_PIPELINES for p in pipelines)
        result[cid] = has_valid

    log.info("  Deal flags resolvidos: %d válidos, %d excluídos.",
             sum(v for v in result.values()), sum(1 for v in result.values() if not v))
    return result


def process_hubspot_records(contacts: list[dict], recording_ts: str) -> list[dict]:
    """Converte contacts do HubSpot para o schema da tabela teste_01."""
    contact_ids = [
        str(c.get("properties", {}).get("hs_object_id") or c.get("id", ""))
        for c in contacts
    ]
    valid_flags = _fetch_valid_deal_flags(contact_ids)

    rows = []
    for contact in contacts:
        props = contact.get("properties", {})

        def get_ts(field: str) -> str | None:
            return _parse_timestamp(props.get(field))

        cid = props.get("hs_object_id") or str(contact.get("id", ""))

        row = {
            "dt_h_recording_data": recording_ts,

            # Identificação
            "hs_object_id":     cid,
            "createdate":       get_ts("createdate") or recording_ts,
            "lastmodifieddate": get_ts("lastmodifieddate"),

            # Datas adicionais
            "hs_latest_source_timestamp":       get_ts("hs_latest_source_timestamp"),
            "first_deal_created_date":          get_ts("first_deal_created_date"),
            "first_conversion_date":            get_ts("first_conversion_date"),
            "hs_sa_first_engagement_date":      get_ts("hs_sa_first_engagement_date"),
            "notes_last_updated":               get_ts("notes_last_updated"),
            "notes_last_contacted":             get_ts("notes_last_contacted"),
            "hs_last_sales_activity_timestamp": get_ts("hs_last_sales_activity_timestamp"),
            "hubspot_owner_assigneddate":       get_ts("hubspot_owner_assigneddate"),
            "recent_conversion_date":           get_ts("recent_conversion_date"),

            # Dados básicos
            "firstname": props.get("firstname"),
            "lastname":  props.get("lastname"),
            "email":     props.get("email"),
            "phone":     props.get("phone"),
            "company":   props.get("company"),

            # Lifecycle / status / owner
            "lifecyclestage":   props.get("lifecyclestage"),
            "hs_lead_status":   props.get("hs_lead_status"),
            "hubspot_owner_id": props.get("hubspot_owner_id"),
            "hubspot_team_id":  props.get("hubspot_team_id"),

            # Deals
            "num_associated_deals": (
                int(props["num_associated_deals"])
                if props.get("num_associated_deals") else None
            ),
            "stage_of_the_deal": props.get("stage_of_the_deal"),
            "has_valid_deal":    valid_flags.get(cid, True),

            # Original Traffic Source
            "hs_analytics_source":         props.get("hs_analytics_source"),
            "hs_analytics_source_data_1":  props.get("hs_analytics_source_data_1"),
            "hs_analytics_source_data_2":  props.get("hs_analytics_source_data_2"),

            # Latest Traffic Source
            "hs_latest_source":            props.get("hs_latest_source"),
            "hs_latest_source_data_1":     props.get("hs_latest_source_data_1"),
            "hs_latest_source_data_2":     props.get("hs_latest_source_data_2"),

            # Conversions
            "hs_analytics_last_touch_converting_campaign": (
                props.get("hs_analytics_last_touch_converting_campaign")
            ),
            "conversion_de_lead": props.get("conversion_de_lead"),
            "form_submitted":     props.get("form_submitted"),

            # Record source
            "hs_object_source_label":    props.get("hs_object_source_label"),
            "hs_object_source_detail_1": props.get("hs_object_source_detail_1"),

            # UTMs
            "utm_term":     props.get("utm_term"),
            "utm_medium":   props.get("utm_medium"),
            "utm_source":   props.get("utm_source"),
            "utm_content":  props.get("utm_content"),
            "utm_campaign": props.get("utm_campaign"),

            # Qualificação / perfil
            "numemployees":         props.get("numemployees"),
            "jobtitle":             props.get("jobtitle"),
            "not_qualified_reason": props.get("not_qualified_reason"),
            "estado_de_lead":       props.get("estado_de_lead"),
            "motivo_no_interesado": props.get("motivo_no_interesado"),

            # Localização
            "country":      props.get("country"),
            "region":       props.get("region"),
            "main_country": props.get("main_country"),

            # Campo customizado
            "como_ficou_sabendo_sobre_nos_": props.get("como_ficou_sabendo_sobre_nos_"),
        }

        rows.append(row)

    return rows


def _build_daily_windows(window_start: int, cutoff_ms: int) -> list[list[int]]:
    """Cria a fila inicial de janelas diárias até o cutoff fixo da run."""
    windows: list[list[int]] = []
    current = window_start

    while current < cutoff_ms:
        window_end = min(current + DAY_MS, cutoff_ms)
        windows.append([current, window_end])
        current = window_end

    return windows


def _collect_hubspot_windows_with_retry_point(
    *,
    source: str,
    object_label: str,
    fetch_window,
    window_start: int,
    cutoff_ms: int,
    filter_property: str,
    recording_ts: str,
    existing_objects: list[dict] | None = None,
    pending_windows: list[list[int]] | None = None,
) -> tuple[list[dict], bool]:
    """
    Coleta objetos do HubSpot por janelas.

    - Janelas que ultrapassam 10.000 resultados são divididas ao meio.
    - Erros transitórios recebem até HUBSPOT_WINDOW_MAX_RETRIES tentativas.
    - Em caso de falha persistente, salva a fila exata de janelas restantes.
    - O cutoff e o recording_ts continuam sendo os da run original.
    """
    all_objects = list(existing_objects or [])

    if pending_windows is None:
        windows = _build_daily_windows(window_start, cutoff_ms)
    else:
        windows = [
            [int(start), int(end)]
            for start, end in pending_windows
            if int(start) < int(end) and int(start) < cutoff_ms
        ]

    while windows:
        current_start, current_end = windows.pop(0)
        current_end = min(current_end, cutoff_ms)

        if current_start >= current_end:
            continue

        batch: list[dict] | None = None
        last_exception: Exception | None = None

        for attempt in range(1, HUBSPOT_WINDOW_MAX_RETRIES + 1):
            try:
                batch = fetch_window(
                    current_start,
                    current_end,
                    filter_property=filter_property,
                )
                break

            except Exception as exc:
                # O limite de 10.000 não é transitório: repetir a mesma janela
                # não ajuda. Divide imediatamente, inclusive se o erro chegar
                # como requests.HTTPError em vez da exceção customizada.
                if _is_hubspot_search_limit_exception(exc):
                    if _split_hubspot_window(
                        object_label=object_label,
                        current_start=current_start,
                        current_end=current_end,
                        windows=windows,
                    ):
                        batch = []
                        last_exception = None
                    else:
                        last_exception = exc
                    break

                last_exception = exc

                if attempt < HUBSPOT_WINDOW_MAX_RETRIES:
                    log.warning(
                        "%s: erro na janela %s→%s. "
                        "Tentativa %d/%d — aguardando %ss. Erro: %s",
                        object_label,
                        current_start,
                        current_end,
                        attempt,
                        HUBSPOT_WINDOW_MAX_RETRIES,
                        HUBSPOT_WINDOW_RETRY_WAIT,
                        exc,
                    )
                    time.sleep(HUBSPOT_WINDOW_RETRY_WAIT)

        # A janela foi substituída por duas menores; não há lote a adicionar.
        if batch == [] and last_exception is None:
            continue

        if batch is None:
            remaining_windows = [[current_start, current_end], *windows]
            partial_raw_path = _retry_raw_path(source, recording_ts)

            # Só contém janelas concluídas integralmente.
            _write_json_atomic(partial_raw_path, all_objects)

            _save_retry_state(
                source,
                {
                    "status": "pending_collection",
                    "recording_ts": recording_ts,
                    "cutoff_ms": cutoff_ms,
                    "resume_from_ms": current_start,
                    "failed_window_end_ms": current_end,
                    "pending_windows": remaining_windows,
                    "filter_property": filter_property,
                    "partial_raw_path": str(partial_raw_path),
                    "complete_output_path": None,
                    "last_error": str(last_exception),
                },
            )

            log.error(
                "%s: coleta interrompida. "
                "Retry point=%s (%s); fim da janela=%s (%s); "
                "cutoff original=%s (%s); objetos preservados=%d; "
                "janelas pendentes=%d.",
                object_label,
                current_start,
                _ms_to_iso(current_start),
                current_end,
                _ms_to_iso(current_end),
                cutoff_ms,
                _ms_to_iso(cutoff_ms),
                len(all_objects),
                len(remaining_windows),
            )

            return all_objects, False

        log.info(
            "  Janela %s→%s: %d %s.",
            current_start,
            current_end,
            len(batch),
            object_label,
        )

        all_objects.extend(batch)

    return all_objects, True

def run_hubspot_collect(sb: Client, recording_ts: str) -> tuple[list[dict], str | None]:
    """
    Coleta HubSpot Contacts em dois modos:
      - FULL (tabela vazia): pega tudo desde HUBSPOT_HISTORY_START por `createdate`.
      - INCREMENTAL (tabela com dados): pega criados E modificados desde a última
        coleta (max dt_h_recording_data), filtrando por `lastmodifieddate`.
    O cutoff superior é fixo no `recording_ts` (snapshot consistente — events
    novos durante o run ficam pra próxima execução).

    Se uma janela falhar, cria um retry point e não retorna dados parciais
    para envio ao Supabase.
    """
    log.info("=== Coletando HubSpot Contacts ===")

    if _load_retry_state("hubspot"):
        raise RetryPointPending(
            "Existe um retry pendente de HubSpot Contacts. "
            "Execute: python dashspy_hubspot.py hubspot-resume"
        )

    # Cutoff superior fixo no início da run.
    now_ms = _recording_ts_to_ms(recording_ts)
    last_recording_ms = get_last_recording(sb, TABLE_HUB)

    if last_recording_ms is None:
        # Modo FULL — tabela vazia
        dt = datetime.strptime(HUBSPOT_HISTORY_START, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        window_start = int(dt.timestamp() * 1000)
        filter_property = "createdate"
        log.info("Tabela vazia → modo FULL: createdate >= %s.", HUBSPOT_HISTORY_START)
    else:
        # Modo INCREMENTAL
        window_start = last_recording_ms
        filter_property = "lastmodifieddate"
        last_dt = datetime.fromtimestamp(last_recording_ms / 1000, tz=timezone.utc)
        log.info("Última coleta: %s → modo INCREMENTAL: lastmodifieddate >= %s.",
                 last_dt.isoformat(), last_dt.isoformat())

    log.info("Cutoff fixo da run para Contacts: %s.", _ms_to_iso(now_ms))

    if window_start >= now_ms:
        log.info("Nada a coletar (window_start >= recording_ts).")
        return [], None

    all_contacts, completed = _collect_hubspot_windows_with_retry_point(
        source="hubspot",
        object_label="contacts",
        fetch_window=_fetch_hubspot_contacts_window,
        window_start=window_start,
        cutoff_ms=now_ms,
        filter_property=filter_property,
        recording_ts=recording_ts,
    )

    if not completed:
        raise RetryPointPending(
            "A coleta de HubSpot Contacts ficou incompleta. "
            "O retry point foi salvo. "
            "Execute: python dashspy_hubspot.py hubspot-resume"
        )

    if not all_contacts:
        return [], None

    rows = process_hubspot_records(all_contacts, recording_ts)
    path = save_temp("hubspot", rows, recording_ts)
    log.info("HubSpot: %d contacts coletados.", len(rows))
    return rows, path

def send_hubspot(sb: Client, rows: list[dict]) -> None:
    insert_rows(sb, TABLE_HUB, rows, on_conflict="hs_object_id")
    log.info("=== HubSpot: %d linhas inseridas. ===", len(rows))


# ---------------------------------------------------------------------------
# HUBSPOT DEALS
# Schema: hs_object_id, dealname, amount, createdate, closedate,
#         dealstage, pipeline, hubspot_owner_id, contact_ids,
#         dt_h_recording_data
# ---------------------------------------------------------------------------

PIPELINE_STAGE_NAMES = {
    "143507534": "Lead 🐣",
    "1226026162": "Early Stage 🌱",
    "143507535": "Discovery 🔍",
    "143507536": "Champion Engaged 🎯",
    "143507537": "Decision Maker Engaged 🚀",
    "143507538": "Pilot ⚠️",
    "143507539": "Final Negotiation 🥁",
    "143507540": "Won 🍾",
    "143507541": "Lost ♻️",
    "146348362": "Postponed ⏱️",
    "56232830": "Onboarding Churned ❤️‍🩹",
    "56458167": "Success Red List 🚨",
    "23755645": "Success Churned 💔",
    "1355084184": "Lead",
    "149683981": "Opportunity opened",
    "149807920": "Discovery",
    "149807921": "Champion Engaged",
    "149807922": "Decision Maker Engaged",
    "149807923": "Pilot",
    "149807924": "Final Negotiation",
    "149683986": "Won",
    "149683987": "Lost",
    "149807925": "Postponed",
    "1082330477": "Churned/Finished Upsell",
    "108636189": "Discovery",
    "108636190": "Proposal",
    "108636191": "Contract Signed",
    "108636193": "Active Partner",
    "952679525": "Postponed",
    "108636194": "Lost",
    "150776393": "Lead",
    "150776394": "Discovery",
    "150776395": "Champion Engaged",
    "150776396": "Decision Maker Engaged",
    "150776397": "Pilot",
    "150776398": "Final Negotiation",
    "150776399": "Won",
    "195922972": "Postponed",
    "195922971": "Lost",
    "1123558017": "Onboarding Churned",
    "1123558018": "Success Churned",
    "1082127189": "Prequalified",
    "1082127190": "Approaching",
    "1082127191": "Engagement",
    "1082127192": "Hot Nurturing",
    "1082127193": "Demo",
    "1088370993": "Recycling",
    "1082127195": "Lost/Stand by",
    "1095503240": "Red List",
}

DEAL_PROPERTIES = [
    "hs_object_id",
    "dealname",
    "origen_del_contacto__from_where_we_got_the_call_",
    "amount",
    "createdate",
    "closedate",
    "lastmodifieddate",
    "dealstage",
    "pipeline",
    "hubspot_owner_id",
    "ae_deal_won",
    "ae_squad",
    "first_meeting_status",
    "pais",
]


def _fetch_deal_contacts(deal_ids: list[str]) -> dict[str, list[str]]:
    """Retorna {deal_id: [contact_id, ...]} para los deals dados."""
    result: dict[str, list[str]] = {}
    url = "https://api.hubapi.com/crm/v4/associations/deals/contacts/batch/read"
    for i in range(0, len(deal_ids), 100):
        batch = deal_ids[i:i + 100]
        payload = {"inputs": [{"id": did} for did in batch]}
        try:
            resp = requests.post(url, headers=_hub_headers(), json=payload, timeout=60)
            if resp.status_code not in (200, 207):
                continue
            for item in resp.json().get("results", []):
                from_id = str(item.get("from", {}).get("id", ""))
                contact_ids = [str(t.get("toObjectId", "")) for t in item.get("to", [])]
                if from_id:
                    result[from_id] = contact_ids
        except Exception:
            continue
    return result


def _fetch_hubspot_deals_window(since_ms: int, until_ms: int,
                                  filter_property: str = "createdate") -> list[dict]:
    """
    Busca deals em uma janela [since_ms, until_ms) usando `filter_property`.

    Quando a janela ultrapassa 10.000 resultados, levanta
    HubSpotSearchLimitError para que o coletor externo subdivida a janela.
    """
    url = f"{HUBSPOT_BASE_URL}/deals/search"
    all_deals: list[dict] = []
    after: str | None = None
    expected_total: int | None = None

    while True:
        payload: dict = {
            "filterGroups": [{
                "filters": [
                    {"propertyName": filter_property, "operator": "GTE", "value": str(since_ms)},
                    {"propertyName": filter_property, "operator": "LT",  "value": str(until_ms)},
                ]
            }],
            "properties": DEAL_PROPERTIES,
            "limit": 100,
        }
        if after:
            payload["after"] = after

        resp = requests.post(
            url,
            headers=_hub_headers(),
            json=payload,
            timeout=300,
        )

        if not resp.ok:
            log.error(
                "HubSpot Deals retornou erro.\n"
                "  Status: %s\n"
                "  Janela: %s → %s\n"
                "  Propriedade: %s\n"
                "  Cursor after: %s\n"
                "  Resposta: %s",
                resp.status_code,
                since_ms,
                until_ms,
                filter_property,
                after,
                resp.text[:3000],
            )

            try:
                after_int = int(after) if after is not None else None
            except (TypeError, ValueError):
                after_int = None

            if resp.status_code == 400 and after_int is not None and after_int >= HUBSPOT_SEARCH_RESULT_LIMIT:
                raise HubSpotSearchLimitError(
                    "Deals",
                    since_ms,
                    until_ms,
                    after=after,
                    total=expected_total,
                )

        resp.raise_for_status()
        data = resp.json()

        if expected_total is None:
            expected_total = int(data.get("total", 0))

            if expected_total > HUBSPOT_SEARCH_RESULT_LIMIT:
                raise HubSpotSearchLimitError(
                    "Deals",
                    since_ms,
                    until_ms,
                    total=expected_total,
                )

        all_deals.extend(data.get("results", []))

        paging = data.get("paging", {})
        next_after = paging.get("next", {}).get("after") if paging else None

        try:
            next_after_int = int(next_after) if next_after is not None else None
        except (TypeError, ValueError):
            next_after_int = None

        if next_after_int is not None and next_after_int >= HUBSPOT_SEARCH_RESULT_LIMIT:
            raise HubSpotSearchLimitError(
                "Deals",
                since_ms,
                until_ms,
                after=next_after,
                total=expected_total,
            )

        after = next_after
        if not after:
            break

    return all_deals

def process_deal_records(deals: list[dict], recording_ts: str) -> list[dict]:
    """Convierte deals del HubSpot al schema de teste_data_deals_01."""
    deal_ids = [str(d.get("properties", {}).get("hs_object_id") or d.get("id", "")) for d in deals]
    deal_contacts = _fetch_deal_contacts(deal_ids)

    rows = []
    for deal in deals:
        props = deal.get("properties", {})
        did = props.get("hs_object_id") or str(deal.get("id", ""))
        stage_id = props.get("dealstage", "")
        pipeline_id = props.get("pipeline", "")

        rows.append({
            "dt_h_recording_data": recording_ts,
            "hs_object_id":        did,
            "dealname":            props.get("dealname"),
            "amount":              float(props["amount"]) if props.get("amount") else None,
            "createdate":          _parse_timestamp(props.get("createdate")),
            "closedate":           _parse_timestamp(props.get("closedate")),
            "lastmodifieddate":    _parse_timestamp(props.get("lastmodifieddate")),
            "dealstage":           PIPELINE_STAGE_NAMES.get(stage_id, stage_id),
            "pipeline":            PIPELINE_NAMES.get(pipeline_id, pipeline_id),
            "hubspot_owner_id":    props.get("hubspot_owner_id"),
            "ae_deal_won":         props.get("ae_deal_won"),
            "ae_squad":            props.get("ae_squad"),
            "first_meeting_status": props.get("first_meeting_status"),
            "deal_source":         props.get("origen_del_contacto__from_where_we_got_the_call_"),
            "pais":                props.get("pais"),
            "contact_ids":         deal_contacts.get(did, []),
        })
    return rows


def run_deals_collect(sb: Client, recording_ts: str) -> tuple[list[dict], str | None]:
    """
    Coleta HubSpot Deals em dois modos:
      - FULL (tabela vazia): pega tudo desde HUBSPOT_HISTORY_START por `createdate`.
      - INCREMENTAL (tabela com dados): pega criados E modificados desde a última
        coleta (max dt_h_recording_data), filtrando por `hs_lastmodifieddate`.
    Cutoff superior fixo no `recording_ts` (snapshot consistente).

    Se uma janela falhar, cria um retry point e não retorna dados parciais
    para envio ao Supabase.
    """
    log.info("=== Coletando HubSpot Deals ===")

    if _load_retry_state("deals"):
        raise RetryPointPending(
            "Existe um retry pendente de HubSpot Deals. "
            "Execute: python dashspy_hubspot.py deals-resume"
        )

    now_ms = _recording_ts_to_ms(recording_ts)
    last_recording_ms = get_last_recording(sb, TABLE_DEALS)

    if last_recording_ms is None:
        # Modo FULL
        dt = datetime.strptime(HUBSPOT_HISTORY_START, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        window_start = int(dt.timestamp() * 1000)
        filter_property = "createdate"
        log.info("Tabela vazia → modo FULL: createdate >= %s.", HUBSPOT_HISTORY_START)
    else:
        # Modo INCREMENTAL — em deals a propriedade certa é hs_lastmodifieddate (com prefixo)
        window_start = last_recording_ms
        filter_property = "hs_lastmodifieddate"
        last_dt = datetime.fromtimestamp(last_recording_ms / 1000, tz=timezone.utc)
        log.info("Última coleta: %s → modo INCREMENTAL: hs_lastmodifieddate >= %s.",
                 last_dt.isoformat(), last_dt.isoformat())

    log.info("Cutoff fixo da run para Deals: %s.", _ms_to_iso(now_ms))

    if window_start >= now_ms:
        log.info("Nada a coletar (window_start >= recording_ts).")
        return [], None

    all_deals, completed = _collect_hubspot_windows_with_retry_point(
        source="deals",
        object_label="deals",
        fetch_window=_fetch_hubspot_deals_window,
        window_start=window_start,
        cutoff_ms=now_ms,
        filter_property=filter_property,
        recording_ts=recording_ts,
    )

    if not completed:
        raise RetryPointPending(
            "A coleta de HubSpot Deals ficou incompleta. "
            "O retry point foi salvo. "
            "Execute: python dashspy_hubspot.py deals-resume"
        )

    if not all_deals:
        return [], None

    rows = process_deal_records(all_deals, recording_ts)
    path = save_temp("deals", rows, recording_ts)
    log.info("HubSpot Deals: %d deals coletados.", len(rows))
    return rows, path

# modifiquei isso aqui para remover duplicadas antes de enviar, mas talvez seja melhor adicionar um limite de "até 10min antes" nas execuções do hub

def send_deals(sb: Client, rows: list[dict]) -> None:
    deduplicated = {
        str(row["hs_object_id"]): row
        for row in rows
        if row.get("hs_object_id")
    }

    clean_rows = list(deduplicated.values())

    removed = len(rows) - len(clean_rows)

    if removed:
        log.warning(
            "HubSpot Deals: %d registros duplicados por hs_object_id removidos antes do envio.",
            removed,
        )

    insert_rows(
        sb,
        TABLE_DEALS,
        clean_rows,
        on_conflict="hs_object_id",
    )

    log.info(
        "=== HubSpot Deals: %d linhas únicas inseridas/atualizadas. ===",
        len(clean_rows),
    )


def _resume_hubspot_collection(
    *,
    source: str,
    object_label: str,
    fetch_window,
    process_records,
) -> tuple[list[dict], str | None]:
    """
    Retoma a partir da janela falhada e mantém o cutoff da run original.

    Não consulta novamente o último dt_h_recording_data do Supabase e não
    calcula um novo horário limite. Se falhar novamente, o mesmo retry point
    é atualizado para a nova janela falhada.
    """
    state = _load_retry_state(source)

    if not state:
        log.info("Não existe retry point pendente para %s.", object_label)
        return [], None

    status = state.get("status")

    # A coleta e o processamento já terminaram; falta apenas enviar.
    if status == "ready_to_send":
        complete_output_path = state.get("complete_output_path")
        if not complete_output_path:
            raise RuntimeError(
                f"Retry de {object_label} está marcado como ready_to_send, "
                "mas não possui complete_output_path."
            )

        output_path = Path(complete_output_path)
        if not output_path.exists():
            raise RuntimeError(f"Arquivo final do retry não encontrado: {output_path}")

        rows = _read_json_file(output_path)
        log.info(
            "%s: retry já coletado e processado. Arquivo pronto para envio: %s",
            object_label,
            output_path,
        )
        return rows, str(output_path)

    recording_ts = state["recording_ts"]
    cutoff_ms = int(state["cutoff_ms"])
    resume_from_ms = int(state["resume_from_ms"])
    filter_property = state["filter_property"]
    partial_raw_path = Path(state["partial_raw_path"])

    if not partial_raw_path.exists():
        raise RuntimeError(f"Arquivo parcial do retry não encontrado: {partial_raw_path}")

    all_objects = _read_json_file(partial_raw_path)
    if not isinstance(all_objects, list):
        raise RuntimeError(f"Arquivo parcial inválido: {partial_raw_path}")

    log.info(
        "=== Retomando %s === Início=%s (%s); cutoff original=%s (%s); "
        "recording_ts original=%s; objetos preservados=%d.",
        object_label,
        resume_from_ms,
        _ms_to_iso(resume_from_ms),
        cutoff_ms,
        _ms_to_iso(cutoff_ms),
        recording_ts,
        len(all_objects),
    )

    if status == "pending_collection":
        all_objects, completed = _collect_hubspot_windows_with_retry_point(
            source=source,
            object_label=object_label,
            fetch_window=fetch_window,
            window_start=resume_from_ms,
            cutoff_ms=cutoff_ms,
            filter_property=filter_property,
            recording_ts=recording_ts,
            existing_objects=all_objects,
            pending_windows=state.get("pending_windows"),
        )

        if not completed:
            raise RetryPointPending(
                f"O retry de {object_label} falhou novamente. "
                "O retry point foi atualizado para a nova janela."
            )

        _write_json_atomic(partial_raw_path, all_objects)
        state.update({
            "status": "collected",
            "resume_from_ms": cutoff_ms,
            "failed_window_end_ms": None,
            "pending_windows": [],
            "last_error": None,
        })
        _save_retry_state(source, state)

    elif status != "collected":
        raise RuntimeError(f"Status de retry desconhecido para {source}: {status}")

    # Mantém o recording_ts da run original.
    rows = process_records(all_objects, recording_ts)
    output_path = save_temp(f"{source}_retry_complete", rows, recording_ts)

    state.update({
        "status": "ready_to_send",
        "complete_output_path": output_path,
    })
    _save_retry_state(source, state)

    log.info(
        "%s: retry concluído até o cutoff original %s. "
        "%d registros prontos para envio.",
        object_label,
        _ms_to_iso(cutoff_ms),
        len(rows),
    )

    return rows, output_path


def resume_hubspot_contacts() -> tuple[list[dict], str | None]:
    return _resume_hubspot_collection(
        source="hubspot",
        object_label="HubSpot Contacts",
        fetch_window=_fetch_hubspot_contacts_window,
        process_records=process_hubspot_records,
    )


def resume_hubspot_deals() -> tuple[list[dict], str | None]:
    return _resume_hubspot_collection(
        source="deals",
        object_label="HubSpot Deals",
        fetch_window=_fetch_hubspot_deals_window,
        process_records=process_deal_records,
    )


def run_hubspot_all() -> None:
    """Coleta Contacts e Deals usando exatamente o mesmo cutoff da run."""
    recording_ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC")
    log.info(
        "Coleta conjunta HubSpot iniciada — cutoff compartilhado: %s",
        recording_ts,
    )

    sb = get_supabase_client()
    pipelines = [
        ("hubspot", "HubSpot Contacts", run_hubspot_collect, send_hubspot),
        ("deals", "HubSpot Deals", run_deals_collect, send_deals),
    ]

    coletados: dict[str, tuple[str, list[dict], str, object]] = {}

    # Primeiro coleta os dois; nenhum envio acontece antes de ambas as tentativas.
    for source, nome, fn_collect, fn_send in pipelines:
        try:
            rows, path = fn_collect(sb, recording_ts)
        except RetryPointPending as exc:
            log.error("%s não foi concluído: %s", nome, exc)
            continue
        except Exception as exc:
            log.error("Coleta [%s] falhou: %s", nome, exc, exc_info=True)
            continue

        if not rows or not path:
            log.info("%s: nenhum dado novo.", nome)
            continue

        coletados[source] = (nome, rows, path, fn_send)

    if not coletados:
        log.warning("Contacts e Deals não retornaram dados completos para envio.")
        return

    for source, (nome, rows, path, fn_send) in coletados.items():
        if not aguardar_confirmacao(nome, path):
            log.warning("Envio de %s cancelado. Arquivo mantido em: %s", nome, path)
            continue

        try:
            fn_send(sb, rows)
            log.info("%s enviado com sucesso.", nome)
        except Exception as exc:
            log.error("Envio [%s] falhou: %s", nome, exc, exc_info=True)

    log.info(
        "Coleta conjunta HubSpot finalizada. "
        "Cutoff utilizado por Contacts e Deals: %s",
        recording_ts,
    )


# ---------------------------------------------------------------------------
# PIPELINE PRINCIPAL
# ---------------------------------------------------------------------------

def main() -> None:
    run_hubspot_all()


PLATFORM_SEND_MAP = {
    "hubspot":  send_hubspot,
    "deals":    send_deals,
}


def retry_from_outputs() -> None:
    """Carrega arquivos JSON de outputs/ e reenvia para o Supabase sem re-coletar."""
    output_dir = Path(PATH_OUTPUTS_M)
    if not output_dir.exists():
        log.error("Diretório outputs/ não encontrado.")
        return

    json_files = sorted(output_dir.glob("*.json"))
    if not json_files:
        log.warning("Nenhum arquivo JSON encontrado em outputs/.")
        return

    print("\nArquivos disponíveis em outputs/:")
    for i, f in enumerate(json_files, 1):
        size = len(json.loads(f.read_text(encoding="utf-8")))
        print(f"  [{i}] {f.name}  ({size} linhas)")

    sel = input("\nNúmeros dos arquivos a enviar (ex: 1,3) ou 'todos': ").strip()
    if sel.lower() == "todos":
        selected = list(json_files)
    else:
        idxs = [int(x.strip()) - 1 for x in sel.split(",") if x.strip().isdigit()]
        selected = [json_files[i] for i in idxs if 0 <= i < len(json_files)]

    if not selected:
        log.warning("Nenhum arquivo selecionado. Encerrando.")
        return

    sb = get_supabase_client()

    falhas: list[str] = []
    for f in selected:
        platform = f.name.split("_")[0]
        fn_send = PLATFORM_SEND_MAP.get(platform)
        if fn_send is None:
            log.warning("Plataforma desconhecida para '%s'. Pulando.", f.name)
            continue

        rows = json.loads(f.read_text(encoding="utf-8"))
        log.info("Arquivo %s: %d linhas.", f.name, len(rows))

        resposta = input(f"  Enviar {f.name} ({len(rows)} linhas) para o Supabase? [s/N]: ").strip().lower()
        if resposta != "s":
            log.info("Envio de %s cancelado.", f.name)
            continue

        try:
            fn_send(sb, rows)
            log.info("%s enviado com sucesso.", f.name)
        except Exception as exc:
            log.error("Erro ao enviar %s: %s", f.name, exc, exc_info=True)
            falhas.append(f.name)

    if falhas:
        log.warning("retry_from_outputs finalizado com falhas: %s", ", ".join(falhas))
    else:
        log.info("retry_from_outputs finalizado com sucesso.")


if __name__ == "__main__":
    import sys

    log.info(
        "Código carregado — build=%s — arquivo=%s",
        DASHSPY_BUILD,
        Path(__file__).resolve(),
    )

    _PIPELINES = {
        "hubspot":  ("HubSpot",       run_hubspot_collect,  send_hubspot),
        "deals":    ("HubSpot Deals", run_deals_collect,    send_deals),
    }

    _RETRY_PIPELINES = {
        "hubspot-resume": (
            "hubspot",
            "HubSpot Contacts",
            resume_hubspot_contacts,
            send_hubspot,
        ),
        "deals-resume": (
            "deals",
            "HubSpot Deals",
            resume_hubspot_deals,
            send_deals,
        ),
    }

    _cmd = sys.argv[1] if len(sys.argv) > 1 else None

    if _cmd is None:
        main()
    elif _cmd == "--retry":
        retry_from_outputs()
    elif _cmd == "hubspot-all":
        run_hubspot_all()
    elif _cmd in _RETRY_PIPELINES:
        _source, _nome, _fn_resume, _fn_send = _RETRY_PIPELINES[_cmd]

        try:
            _rows, _path = _fn_resume()
        except RetryPointPending as _exc:
            log.error("%s", _exc)
            sys.exit(1)
        except Exception as _exc:
            log.error("Retry [%s] falhou: %s", _nome, _exc, exc_info=True)
            sys.exit(1)

        if not _rows:
            log.info("%s: nenhum retry pendente ou nenhum registro.", _nome)
            sys.exit(0)

        if not aguardar_confirmacao(f"{_nome} — retry completo", _path):
            log.warning(
                "Envio do retry de %s cancelado. O retry point foi mantido.",
                _nome,
            )
            sys.exit(0)

        _sb = get_supabase_client()
        try:
            _fn_send(_sb, _rows)
        except Exception as _exc:
            log.error("Envio do retry [%s] falhou: %s", _nome, _exc, exc_info=True)
            sys.exit(1)

        _clear_retry_state(_source)
        log.info("%s: retry enviado com sucesso.", _nome)
    elif _cmd in _PIPELINES:
        _nome, _fn_collect, _fn_send = _PIPELINES[_cmd]
        _recording_ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC")
        log.info("Coleta individual: %s — registro em: %s", _nome, _recording_ts)
        _sb = get_supabase_client()
        try:
            _rows, _path = _fn_collect(_sb, _recording_ts)
        except RetryPointPending as _exc:
            log.error("Coleta [%s] interrompida: %s", _nome, _exc)
            sys.exit(1)
        except Exception as _exc:
            log.error("Coleta [%s] falhou: %s", _nome, _exc, exc_info=True)
            sys.exit(1)
        if not _rows:
            log.info("%s: nenhum dado novo.", _nome)
            sys.exit(0)
        if not aguardar_confirmacao(_nome, _path):
            log.warning("Envio do %s cancelado. Arquivo mantido em: %s", _nome, _path)
            sys.exit(0)
        try:
            _fn_send(_sb, _rows)
            log.info("%s: enviado com sucesso.", _nome)
        except Exception as _exc:
            log.error("Envio [%s] falhou: %s", _nome, _exc, exc_info=True)
            sys.exit(1)
    else:
        print(f"Subcomando desconhecido: '{_cmd}'")
        print(
            "Uso: python dashspy_hubspot.py "
            "[hubspot|deals|hubspot-all|hubspot-resume|deals-resume|--retry]"
        )
        sys.exit(1)
