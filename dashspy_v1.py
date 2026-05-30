"""
dashspy_v1.py
Coleta dados de Meta Ads, Google Ads e HubSpot e centraliza no Supabase.
"""

import os
import time
import json
import logging
from datetime import datetime, timedelta, timezone, date
from dateutil.relativedelta import relativedelta

import requests
from dotenv import load_dotenv
from supabase import create_client, Client
from google.ads.googleads.client import GoogleAdsClient
from rich.logging import RichHandler

# ---------------------------------------------------------------------------
# Configuração de logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        RichHandler(rich_tracebacks=True, markup=True),
        logging.FileHandler("dashspy.log", mode="w", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Carrega variáveis de ambiente
# ---------------------------------------------------------------------------
load_dotenv()

META_ACCESS_TOKEN       = os.environ["META_ACCESS_TOKEN"]
META_AD_ACCOUNT_IDS     = [a.strip() for a in os.environ["META_AD_ACCOUNT_IDS"].split(",")]
HUBSPOT_TOKEN           = os.environ["HUBSPOT_TOKEN"]
LINKEDIN_ACCESS_TOKEN   = os.environ["LINKEDIN_ACCESS_TOKEN"]
LINKEDIN_AD_ACCOUNT_IDS = [a.strip() for a in os.environ["LINKEDIN_AD_ACCOUNT_IDS"].split(",")]
SUPABASE_URL            = os.environ["SUPABASE_URL"]
SUPABASE_KEY            = os.environ["SUPABASE_KEY"]
GOOGLE_ADS_YAML_PATH    = os.environ.get("GOOGLE_ADS_YAML_PATH", "google-ads.yaml")

# ---------------------------------------------------------------------------
# Constantes de Supabase (nomes das tabelas)
# ---------------------------------------------------------------------------
TABLE_META      = "data_meta_v2"
TABLE_GOOGLE    = "data_google_v2"
TABLE_LINKEDIN  = "data_linkedin_v2"
TABLE_HUB       = "data_hs_contacts_v2"
TABLE_DEALS     = "data_hs_deals_v2"

# Data de início histórico por fonte
META_HISTORY_START      = "2023-09-21"
GOOGLE_HISTORY_START    = "2021-11-22"
LINKEDIN_HISTORY_START  = "2023-09-01"
HUBSPOT_HISTORY_START   = "2025-08-01"

# ---------------------------------------------------------------------------
# Helpers de data
# ---------------------------------------------------------------------------

def yesterday() -> str:
    """Retorna a data de ontem no formato aaaa-mm-dd."""
    return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


def first_day_last_month_ms() -> int:
    """Retorna o primeiro dia do mês anterior em milissegundos (padrão HubSpot)."""
    today = date.today()
    first = (today.replace(day=1) - relativedelta(months=1)).replace(day=1)
    dt = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Supabase — cliente e utilitários
# ---------------------------------------------------------------------------

def get_supabase_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_last_date(sb: Client, table: str, date_col: str) -> str | None:
    """
    Retorna a última data registrada numa tabela Supabase ou None se a tabela estiver vazia.
    """
    response = sb.table(table).select(date_col).order(date_col, desc=True).limit(1).execute()
    if not response.data:
        return None
    val = response.data[0].get(date_col)
    if val is None:
        return None
    return str(val)[:10]  # garante formato YYYY-MM-DD


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
    from pathlib import Path

    OUTPUT_DIR = Path("/workspaces/teste-claude/dashboard_paid/outputs")
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
# META ADS
# Schema: date_start (DATE), campaign_name (STRING), cost (FLOAT),
#         dt_h_recording_data (TIMESTAMP)
# ---------------------------------------------------------------------------

META_BASE_URL         = "https://graph.facebook.com/v25.0"
META_RATE_LIMIT_CODES = {4, 17, 341}
META_BATCH_SIZE       = 50
META_RETRY_WAIT       = 60


META_RATE_LIMIT_CODES = {1, 4, 17, 341}
META_MAX_RETRIES      = 5

def _meta_fetch_page(url: str, params: dict | None = None) -> dict:
    """Faz uma requisição GET para a Meta API com retry automático em rate-limit."""
    retries = 0
    while True:
        resp = requests.get(url, params=params, timeout=60)
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise

        error = data.get("error", {})
        if error:
            code    = error.get("code")
            subcode = error.get("error_subcode")

            if code in META_RATE_LIMIT_CODES:
                retries += 1
                if retries > META_MAX_RETRIES:
                    raise RuntimeError(
                        f"Meta API error após {META_MAX_RETRIES} tentativas: {error}"
                    )
                log.warning(
                    "Meta erro transitório (código %s, subcode %s). "
                    "Tentativa %d/%d — aguardando %ss…",
                    code, subcode, retries, META_MAX_RETRIES, META_RETRY_WAIT,
                )
                time.sleep(META_RETRY_WAIT)
                continue

            raise RuntimeError(f"Meta API error: {error}")

        return data


def _fetch_meta_ads_account(account_id: str, data_inicial: str, data_final: str) -> list[dict]:
    """Busca insights de uma conta Meta Ads para o intervalo informado."""
    all_records: list[dict] = []
    current = datetime.strptime(data_inicial, "%Y-%m-%d").date()
    end     = datetime.strptime(data_final,   "%Y-%m-%d").date()

    while current <= end:
        chunk_end = min(current + relativedelta(years=1) - timedelta(days=1), end)
        log.info("  [%s] Janela: %s → %s", account_id, current, chunk_end)

        time_range  = json.dumps({"since": str(current), "until": str(chunk_end)})
        base_params = {
            "fields":         "campaign_id,campaign_name,spend,date_start",
            "level":          "campaign",
            "time_increment": 1,
            "time_range":     time_range,
            "access_token":   META_ACCESS_TOKEN,
            "limit":          META_BATCH_SIZE,
        }

        url  = f"{META_BASE_URL}/{account_id}/insights"
        page = 0

        while url:
            page += 1
            data    = _meta_fetch_page(url, params=base_params if page == 1 else None)
            records = data.get("data", [])
            for r in records:
                r["_account_id"] = account_id
            all_records.extend(records)
            log.info("    Página %d: %d registros (total: %d).", page, len(records), len(all_records))
            url = data.get("paging", {}).get("next")

        current = chunk_end + timedelta(days=1)

    return all_records


def fetch_meta_ads(data_inicial: str, data_final: str) -> list[dict]:
    """Busca insights de todas as contas Meta Ads configuradas."""
    log.info("Meta Ads: buscando de %s até %s (%d contas).", data_inicial, data_final, len(META_AD_ACCOUNT_IDS))
    all_records: list[dict] = []

    for account_id in META_AD_ACCOUNT_IDS:
        log.info("Meta Ads: processando conta %s.", account_id)
        records = _fetch_meta_ads_account(account_id, data_inicial, data_final)
        all_records.extend(records)
        log.info("Meta Ads: conta %s — %d registros.", account_id, len(records))

    log.info("Meta Ads: %d registros obtidos no total.", len(all_records))
    return all_records


def process_meta_records(raw: list[dict], recording_ts: str) -> list[dict]:
    """Converte os registros brutos da Meta para o schema da tabela."""
    rows = []
    for r in raw:
        spend_val = r.get("spend")
        rows.append({
            "date_start":          r.get("date_start"),
            "campaign_id":         r.get("campaign_id", ""),
            "campaign_name":       r.get("campaign_name", ""),
            "cost":                float(spend_val) if spend_val is not None else None,
            "ad_account_id":       r.get("_account_id", ""),
            "dt_h_recording_data": recording_ts,
        })
    return rows


def run_meta_collect(sb: Client, recording_ts: str) -> tuple[list[dict], str | None]:
    """Coleta, processa e salva os dados do Meta Ads. Retorna (rows, path)."""
    log.info("=== Coletando Meta Ads ===")
    last = get_last_date(sb, TABLE_META, "date_start")

    if last is None:
        data_inicial = META_HISTORY_START
        log.info("Tabela Meta vazia. Carga histórica desde %s.", data_inicial)
    else:
        data_inicial = (
            datetime.strptime(last, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")
        log.info("Última data Meta: %s. Buscando a partir de %s.", last, data_inicial)

    data_final = yesterday()

    if data_inicial > data_final:
        log.info("Meta Ads já está atualizado. Nada a coletar.")
        return [], None

    raw  = fetch_meta_ads(data_inicial, data_final)
    rows = process_meta_records(raw, recording_ts)
    path = save_temp("meta", rows, recording_ts)
    return rows, path


def send_meta(sb: Client, rows: list[dict]) -> None:
    insert_rows(sb, TABLE_META, rows)
    log.info("=== Meta Ads: %d linhas inseridas. ===", len(rows))


# ---------------------------------------------------------------------------
# GOOGLE ADS
# Schema: campaign_name (STRING), spend (FLOAT), date (DATE),
#         dt_h_recording_data (TIMESTAMP)
# ---------------------------------------------------------------------------

GOOGLE_CUSTOMER_IDS = ["1805339996", "9474287342", "6935705652", "4802217233"]

def fetch_google_ads(data_inicial: str, data_final: str) -> list[dict]:
    """
    Busca gastos por campanha na Google Ads API para o intervalo informado.
    Itera todas as sub-contas sob o manager account.
    """
    log.info("Google Ads: buscando de %s até %s.", data_inicial, data_final)

    client     = GoogleAdsClient.load_from_storage(GOOGLE_ADS_YAML_PATH)
    ga_service = client.get_service("GoogleAdsService")

    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            segments.date,
            metrics.cost_micros
        FROM campaign
        WHERE segments.date BETWEEN '{data_inicial}' AND '{data_final}'
          AND metrics.cost_micros > 0
        ORDER BY segments.date DESC
    """

    records: list[dict] = []
    for customer_id in GOOGLE_CUSTOMER_IDS:
        try:
            stream = ga_service.search_stream(customer_id=customer_id, query=query)
            count = 0
            for batch in stream:
                for row in batch.results:
                    cost = row.metrics.cost_micros / 1_000_000
                    records.append({
                        "campaign_id":    str(row.campaign.id),
                        "campaign_name":  row.campaign.name,
                        "spend":          cost,
                        "date":           row.segments.date,
                        "ad_account_id":  customer_id,
                    })
                    count += 1
            log.info("  Google Ads conta %s: %d registros.", customer_id, count)
        except Exception as exc:
            log.warning("  Google Ads conta %s: erro — %s", customer_id, exc)

    log.info("Google Ads: %d registros totais.", len(records))
    return records


def process_google_records(raw: list[dict], recording_ts: str) -> list[dict]:
    """Adiciona dt_h_recording_data aos registros do Google Ads."""
    return [{**r, "dt_h_recording_data": recording_ts} for r in raw]


def run_google_collect(sb: Client, recording_ts: str) -> tuple[list[dict], str | None]:
    """Coleta, processa e salva os dados do Google Ads. Retorna (rows, path)."""
    log.info("=== Coletando Google Ads ===")
    last = get_last_date(sb, TABLE_GOOGLE, "date")

    if last is None:
        data_inicial = GOOGLE_HISTORY_START
        log.info("Tabela Google vazia. Carga histórica desde %s.", data_inicial)
    else:
        data_inicial = (
            datetime.strptime(last, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")
        log.info("Última data Google: %s. Buscando a partir de %s.", last, data_inicial)

    data_final = yesterday()

    if data_inicial > data_final:
        log.info("Google Ads já está atualizado. Nada a coletar.")
        return [], None

    raw  = fetch_google_ads(data_inicial, data_final)
    rows = process_google_records(raw, recording_ts)
    path = save_temp("google", rows, recording_ts)
    return rows, path


def send_google(sb: Client, rows: list[dict]) -> None:
    insert_rows(sb, TABLE_GOOGLE, rows)
    log.info("=== Google Ads: %d linhas inseridas. ===", len(rows))


# ---------------------------------------------------------------------------
# LINKEDIN ADS
# Schema: date_start (DATE), campaign_name (STRING), cost (FLOAT),
#         ad_account_id (STRING), dt_h_recording_data (TIMESTAMP)
# ---------------------------------------------------------------------------

LINKEDIN_BASE_URL      = "https://api.linkedin.com/v2"
LINKEDIN_REST_BASE_URL = "https://api.linkedin.com/rest"
LINKEDIN_API_VERSION   = "202510"
LINKEDIN_RETRY_WAIT    = 60
LINKEDIN_MAX_RETRIES   = 5


def _linkedin_headers() -> dict:
    return {
        "Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}",
        "X-Restli-Protocol-Version": "2.0.0",
        "Linkedin-Version": LINKEDIN_API_VERSION,
    }


def _fetch_linkedin_campaign_names(campaign_urns: list[str]) -> dict[str, str]:
    """Retorna um mapa {urn: campaign_name} para os URNs fornecidos."""
    names: dict[str, str] = {}
    for urn in campaign_urns:
        campaign_id = urn.split(":")[-1]
        url = f"{LINKEDIN_BASE_URL}/adCampaignsV2/{campaign_id}"
        resp = requests.get(url, headers=_linkedin_headers(), timeout=60)
        if resp.status_code == 200:
            names[urn] = resp.json().get("name", urn)
        else:
            names[urn] = urn
    return names


def _fetch_linkedin_ads_account(account_id: str, data_inicial: str, data_final: str) -> list[dict]:
    """Busca insights diários por campanha de uma conta LinkedIn Ads."""
    all_records: list[dict] = []
    current = datetime.strptime(data_inicial, "%Y-%m-%d").date()
    end     = datetime.strptime(data_final,   "%Y-%m-%d").date()

    while current <= end:
        chunk_end = min(current + relativedelta(months=3) - timedelta(days=1), end)
        log.info("  [%s] Janela: %s → %s", account_id, current, chunk_end)

        retries = 0
        while True:
            import subprocess, json as _json
            cmd = [
                "curl", "--globoff", "-s", "-G",
                f"{LINKEDIN_REST_BASE_URL}/adAnalytics",
                "--data", "q=analytics",
                "--data", "pivot=CAMPAIGN",
                "--data", "timeGranularity=DAILY",
                "--data", f"accounts=List(urn%3Ali%3AsponsoredAccount%3A{account_id})",
                "--data", f"dateRange=(start:(day:{current.day},month:{current.month},year:{current.year}),end:(day:{chunk_end.day},month:{chunk_end.month},year:{chunk_end.year}))",
                "--data", "fields=dateRange,costInLocalCurrency,pivotValues",
                "-H", f"Authorization: Bearer {LINKEDIN_ACCESS_TOKEN}",
                "-H", f"Linkedin-Version: {LINKEDIN_API_VERSION}",
                "-H", "X-Restli-Protocol-Version: 2.0.0",
                "--max-time", "60",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            try:
                resp_data = _json.loads(result.stdout)
                resp_status = resp_data.get("status", 200)
            except Exception:
                resp_data = {}
                resp_status = 500
            if resp_status == 429:
                retries += 1
                if retries > LINKEDIN_MAX_RETRIES:
                    raise RuntimeError(f"LinkedIn rate limit após {LINKEDIN_MAX_RETRIES} tentativas.")
                log.warning("LinkedIn rate limit. Tentativa %d/%d — aguardando %ss…", retries, LINKEDIN_MAX_RETRIES, LINKEDIN_RETRY_WAIT)
                time.sleep(LINKEDIN_RETRY_WAIT)
                continue
            if resp_status not in (200, 429) and resp_status >= 400:
                raise RuntimeError(f"LinkedIn API error {resp_status}: {result.stdout[:200]}")
            break

        elements = resp_data.get("elements", [])

        # Coletamos os URNs de campanha para buscar os nomes em lote
        campaign_urns = list({
            pv for e in elements for pv in e.get("pivotValues", [])
        })
        campaign_names = _fetch_linkedin_campaign_names(campaign_urns)

        for e in elements:
            dr = e.get("dateRange", {})
            start = dr.get("start", {})
            date_str = f"{start.get('year'):04d}-{start.get('month'):02d}-{start.get('day'):02d}"
            cost = e.get("costInLocalCurrency")
            for urn in e.get("pivotValues", []):
                all_records.append({
                    "date_start":    date_str,
                    "campaign_id":   urn.split(":")[-1],
                    "campaign_name": campaign_names.get(urn, urn),
                    "cost":          float(cost) if cost is not None else None,
                    "_account_id":   account_id,
                })

        log.info("    %d registros obtidos.", len(elements))
        current = chunk_end + timedelta(days=1)

    return all_records


def fetch_linkedin_ads(data_inicial: str, data_final: str) -> list[dict]:
    """Busca insights de todas as contas LinkedIn Ads configuradas."""
    log.info("LinkedIn Ads: buscando de %s até %s (%d contas).", data_inicial, data_final, len(LINKEDIN_AD_ACCOUNT_IDS))
    all_records: list[dict] = []
    for account_id in LINKEDIN_AD_ACCOUNT_IDS:
        log.info("LinkedIn Ads: processando conta %s.", account_id)
        records = _fetch_linkedin_ads_account(account_id, data_inicial, data_final)
        all_records.extend(records)
        log.info("LinkedIn Ads: conta %s — %d registros.", account_id, len(records))
    log.info("LinkedIn Ads: %d registros no total.", len(all_records))
    return all_records


def process_linkedin_records(raw: list[dict], recording_ts: str) -> list[dict]:
    """Converte os registros brutos do LinkedIn para o schema da tabela."""
    return [{
        "date_start":          r.get("date_start"),
        "campaign_id":         r.get("campaign_id", ""),
        "campaign_name":       r.get("campaign_name", ""),
        "cost":                r.get("cost"),
        "ad_account_id":       r.get("_account_id", ""),
        "dt_h_recording_data": recording_ts,
    } for r in raw]


def run_linkedin_collect(sb: Client, recording_ts: str) -> tuple[list[dict], str | None]:
    """Coleta, processa e salva os dados do LinkedIn Ads. Retorna (rows, path)."""
    log.info("=== Coletando LinkedIn Ads ===")
    last = get_last_date(sb, TABLE_LINKEDIN, "date_start")

    if last is None:
        data_inicial = LINKEDIN_HISTORY_START
        log.info("Tabela LinkedIn vazia. Carga histórica desde %s.", data_inicial)
    else:
        data_inicial = (
            datetime.strptime(last, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")
        log.info("Última data LinkedIn: %s. Buscando a partir de %s.", last, data_inicial)

    data_final = yesterday()

    if data_inicial > data_final:
        log.info("LinkedIn Ads já está atualizado. Nada a coletar.")
        return [], None

    raw  = fetch_linkedin_ads(data_inicial, data_final)
    rows = process_linkedin_records(raw, recording_ts)
    path = save_temp("linkedin", rows, recording_ts)
    return rows, path


def send_linkedin(sb: Client, rows: list[dict]) -> None:
    insert_rows(sb, TABLE_LINKEDIN, rows)
    log.info("=== LinkedIn Ads: %d linhas inseridas. ===", len(rows))


# ---------------------------------------------------------------------------
# HUBSPOT
# Schema: ver tabela teste_01 no README
# ---------------------------------------------------------------------------

HUBSPOT_BASE_URL = "https://api.hubapi.com/crm/v3/objects"

CONTACT_PROPERTIES = [
    "hs_object_id",
    "createdate",
    "lastmodifieddate",
    "firstname",
    "lastname",
    "email",
    "phone",
    "company",
    "lifecyclestage",
    "hs_lead_status",
    "hubspot_owner_id",
    "num_associated_deals",
    "hs_analytics_source",
    "hs_analytics_last_touch_converting_campaign",
    "numemployees",
    "jobtitle",
    "not_qualified_reason",
    "estado_de_lead",
    "hs_object_source_detail_1",
    "hs_analytics_source_data_1",
    "hs_analytics_source_data_2",
    "stage_of_the_deal",
    "motivo_no_interesado",
    "conversion_de_lead",
    "hubspot_team_id",
    "form_submitted",
    "country",
    "region",
    "main_country",
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


def _fetch_hubspot_contacts_window(since_ms: int, until_ms: int, max_retries: int = 3) -> list[dict]:
    """
    Busca contacts em uma janela de tempo [since_ms, until_ms).
    A Search API limita a 10.000 resultados por query; janelas menores evitam o limite.
    Valida count do API vs resultados paginados; re-tenta se faltar algum.
    """
    url = f"{HUBSPOT_BASE_URL}/contacts/search"

    for attempt in range(max_retries):
        all_contacts: list[dict] = []
        after: str | None = None
        expected_total: int | None = None

        while True:
            payload: dict = {
                "filterGroups": [
                    {
                        "filters": [
                            {"propertyName": "createdate", "operator": "GTE", "value": str(since_ms)},
                            {"propertyName": "createdate", "operator": "LT",  "value": str(until_ms)},
                        ]
                    }
                ],
                "properties": CONTACT_PROPERTIES,
                "limit":      100,
            }
            if after:
                payload["after"] = after

            resp = requests.post(url, headers=_hub_headers(), json=payload, timeout=300)
            resp.raise_for_status()
            data = resp.json()

            if expected_total is None:
                expected_total = data.get("total", 0)

            results = data.get("results", [])
            all_contacts.extend(results)

            paging = data.get("paging", {})
            after  = paging.get("next", {}).get("after") if paging else None
            if not after:
                break

        if expected_total is not None and len(all_contacts) >= expected_total:
            return all_contacts

        log.warning("  Paginación incompleta: esperados %d, obtenidos %d. Reintento %d/%d.",
                     expected_total, len(all_contacts), attempt + 1, max_retries)

    log.warning("  Después de %d reintentos, obtenidos %d/%d. Continuando con lo que hay.",
                 max_retries, len(all_contacts), expected_total)
    return all_contacts


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
            "hs_object_id":        props.get("hs_object_id") or str(contact.get("id", "")),
            "createdate":          get_ts("createdate") or recording_ts,
            "lastmodifieddate":    get_ts("lastmodifieddate"),
            "firstname":           props.get("firstname"),
            "lastname":            props.get("lastname"),
            "email":               props.get("email"),
            "phone":               props.get("phone"),
            "company":             props.get("company"),
            "lifecyclestage":      props.get("lifecyclestage"),
            "hs_lead_status":      props.get("hs_lead_status"),
            "hubspot_owner_id":    props.get("hubspot_owner_id"),
            "num_associated_deals": (
                int(props["num_associated_deals"])
                if props.get("num_associated_deals") else None
            ),
            "hs_analytics_source":                          props.get("hs_analytics_source"),
            "hs_analytics_last_touch_converting_campaign":  props.get("hs_analytics_last_touch_converting_campaign"),
            "numemployees":        props.get("numemployees"),
            "jobtitle":            props.get("jobtitle"),
            "not_qualified_reason":       props.get("not_qualified_reason"),
            "estado_de_lead":             props.get("estado_de_lead"),
            "hs_object_source_detail_1":   props.get("hs_object_source_detail_1"),
            "hs_analytics_source_data_1":  props.get("hs_analytics_source_data_1"),
            "hs_analytics_source_data_2":  props.get("hs_analytics_source_data_2"),
            "stage_of_the_deal":          props.get("stage_of_the_deal"),
            "motivo_no_interesado":       props.get("motivo_no_interesado"),
            "conversion_de_lead":         props.get("conversion_de_lead"),
            "hubspot_team_id":            props.get("hubspot_team_id"),
            "form_submitted":             props.get("form_submitted"),
            "country":                    props.get("country"),
            "region":                     props.get("region"),
            "main_country":               props.get("main_country"),
            "has_valid_deal":             valid_flags.get(cid, True),
        }
        rows.append(row)

    return rows


def run_hubspot_collect(sb: Client, recording_ts: str) -> tuple[list[dict], str | None]:
    """Coleta HubSpot Contacts janela por janela e retorna todos para confirmação antes do envio."""
    log.info("=== Coletando HubSpot Contacts ===")
    last = get_last_date(sb, TABLE_HUB, "createdate")

    if last is None:
        start_date = HUBSPOT_HISTORY_START
        log.info("Tabela HubSpot vazia. Carga histórica desde %s.", start_date)
    else:
        start_date = (
            datetime.strptime(last[:10], "%Y-%m-%d") - timedelta(days=45)
        ).strftime("%Y-%m-%d")
        log.info("Último createdate HubSpot: %s. Lookback 45d → buscando a partir de %s.", last, start_date)

    dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    window_start = int(dt.timestamp() * 1000)
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    if window_start >= now_ms:
        log.info("HubSpot já está atualizado. Nada a coletar.")
        return [], None

    all_contacts: list[dict] = []
    while window_start < now_ms:
        window_end = min(window_start + 1 * 24 * 3600 * 1000, now_ms)
        try:
            batch = _fetch_hubspot_contacts_window(window_start, window_end)
        except Exception as exc:
            log.error("Timeout/erro na janela %s→%s: %s. Encerrando coleta.", window_start, window_end, exc)
            break
        log.info("  Janela %s→%s: %d contacts.", window_start, window_end, len(batch))
        all_contacts.extend(batch)
        window_start = window_end

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


def _fetch_hubspot_deals_window(since_ms: int, until_ms: int) -> list[dict]:
    """Busca deals creados en la ventana [since_ms, until_ms)."""
    url = f"{HUBSPOT_BASE_URL}/deals/search"
    all_deals: list[dict] = []
    after: str | None = None

    while True:
        payload: dict = {
            "filterGroups": [{
                "filters": [
                    {"propertyName": "createdate", "operator": "GTE", "value": str(since_ms)},
                    {"propertyName": "createdate", "operator": "LT",  "value": str(until_ms)},
                ]
            }],
            "properties": DEAL_PROPERTIES,
            "limit": 100,
        }
        if after:
            payload["after"] = after

        resp = requests.post(url, headers=_hub_headers(), json=payload, timeout=300)
        resp.raise_for_status()
        data = resp.json()
        all_deals.extend(data.get("results", []))
        paging = data.get("paging", {})
        after = paging.get("next", {}).get("after") if paging else None
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
    """Coleta HubSpot Deals janela por janela e retorna todos para confirmação antes do envio."""
    log.info("=== Coletando HubSpot Deals ===")
    last = get_last_date(sb, TABLE_DEALS, "createdate")

    if last is None:
        start_date = HUBSPOT_HISTORY_START
        log.info("Tabela Deals vazia. Carga histórica desde %s.", start_date)
    else:
        start_date = (
            datetime.strptime(last[:10], "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")
        log.info("Último createdate Deals: %s. Buscando a partir de %s.", last, start_date)

    dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    window_start = int(dt.timestamp() * 1000)
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    if window_start >= now_ms:
        log.info("Deals já está atualizado. Nada a coletar.")
        return [], None

    all_deals: list[dict] = []
    while window_start < now_ms:
        window_end = min(window_start + 1 * 24 * 3600 * 1000, now_ms)
        try:
            batch = _fetch_hubspot_deals_window(window_start, window_end)
        except Exception as exc:
            log.error("Timeout/erro na janela %s→%s: %s. Encerrando coleta.", window_start, window_end, exc)
            break
        log.info("  Janela %s→%s: %d deals.", window_start, window_end, len(batch))
        all_deals.extend(batch)
        window_start = window_end

    if not all_deals:
        return [], None

    rows = process_deal_records(all_deals, recording_ts)
    path = save_temp("deals", rows, recording_ts)
    log.info("HubSpot Deals: %d deals coletados.", len(rows))
    return rows, path


def send_deals(sb: Client, rows: list[dict]) -> None:
    insert_rows(sb, TABLE_DEALS, rows, on_conflict="hs_object_id")
    log.info("=== HubSpot Deals: %d linhas inseridas. ===", len(rows))


# ---------------------------------------------------------------------------
# PIPELINE PRINCIPAL
# ---------------------------------------------------------------------------

def main() -> None:
    recording_ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC")
    log.info("Iniciando dashspy_v1 — registro em: %s", recording_ts)

    sb = get_supabase_client()

    pipelines = [
        ("meta",     "Meta Ads",      run_meta_collect,     send_meta),
        ("google",   "Google Ads",    run_google_collect,   send_google),
        ("linkedin", "LinkedIn Ads",  run_linkedin_collect, send_linkedin),
        ("hubspot",  "HubSpot",       run_hubspot_collect,  send_hubspot),
        ("deals",    "HubSpot Deals", run_deals_collect,    send_deals),
    ]

    # --- Fase 1: coleta das plataformas ---
    coletados = {}
    log.info("--- Fase 1: coletando dados de todas as plataformas ---")
    for key, nome, fn_collect, fn_send in pipelines:
        try:
            rows, path = fn_collect(sb, recording_ts)
            if rows:
                coletados[key] = (nome, rows, path, fn_send)
            else:
                log.info("%s: nenhum dado novo. Pulando.", nome)
        except Exception as exc:
            log.error("Coleta [%s] falhou: %s", nome, exc, exc_info=True)

    if not coletados:
        log.warning("Nenhuma plataforma retornou dados novos. Encerrando.")
        return

    # --- Fase 2: confirmação e envio ---
    log.info("--- Fase 2: revisão e envio para o Supabase ---")
    falhas: dict[str, tuple[list[dict], object]] = {}
    for key, (nome, rows, path, fn_send) in coletados.items():
        if not aguardar_confirmacao(nome, path):
            log.warning("Envio do %s cancelado pelo usuário. Arquivo mantido em: %s", nome, path)
            continue
        try:
            fn_send(sb, rows)
        except Exception as exc:
            log.error("Envio [%s] falhou: %s", nome, exc, exc_info=True)
            falhas[nome] = (rows, fn_send)

    while falhas:
        log.warning("Envios que falharam: %s", ", ".join(falhas.keys()))
        resposta = input("Deseja tentar novamente os envios que falharam? [s/N]: ").strip().lower()
        if resposta != "s":
            break
        novas_falhas: dict[str, tuple[list[dict], object]] = {}
        for nome, (rows, fn_send) in falhas.items():
            try:
                fn_send(sb, rows)
                log.info("%s: enviado com sucesso no reenvio.", nome)
            except Exception as exc:
                log.error("Reenvio [%s] falhou: %s", nome, exc, exc_info=True)
                novas_falhas[nome] = (rows, fn_send)
        falhas = novas_falhas

    if falhas:
        log.warning("dashspy_v1 finalizado com falhas no envio: %s", ", ".join(falhas.keys()))
    else:
        log.info("dashspy_v1 finalizado com sucesso.")


PLATFORM_SEND_MAP = {
    "meta":     send_meta,
    "google":   send_google,
    "linkedin": send_linkedin,
    "hubspot":  send_hubspot,
    "deals":    send_deals,
}


def retry_from_outputs() -> None:
    """Carrega arquivos JSON de outputs/ e reenvia para o Supabase sem re-coletar."""
    from pathlib import Path

    output_dir = Path("outputs")
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

    _PIPELINES = {
        "meta":     ("Meta Ads",      run_meta_collect,     send_meta),
        "google":   ("Google Ads",    run_google_collect,   send_google),
        "linkedin": ("LinkedIn Ads",  run_linkedin_collect, send_linkedin),
        "hubspot":  ("HubSpot",       run_hubspot_collect,  send_hubspot),
        "deals":    ("HubSpot Deals", run_deals_collect,    send_deals),
    }

    _cmd = sys.argv[1] if len(sys.argv) > 1 else None

    if _cmd is None:
        main()
    elif _cmd == "--retry":
        retry_from_outputs()
    elif _cmd in _PIPELINES:
        _nome, _fn_collect, _fn_send = _PIPELINES[_cmd]
        _recording_ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC")
        log.info("Coleta individual: %s — registro em: %s", _nome, _recording_ts)
        _sb = get_supabase_client()
        try:
            _rows, _path = _fn_collect(_sb, _recording_ts)
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
        print("Uso: python dashspy_v1.py [meta|google|linkedin|hubspot|deals|--retry]")
        sys.exit(1)
