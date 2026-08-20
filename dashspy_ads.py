"""
dashspy_ads.py
Coleta dados de Meta Ads, Google Ads e LinkedIn Ads e centraliza no Supabase.
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
from pathlib import Path
# ---------------------------------------------------------------------------
# Configuração de logging
# ---------------------------------------------------------------------------

LOG_DIR = Path(__file__).resolve().parent / "logs/dashspy"
LOG_DIR.mkdir(parents=True, exist_ok=True)
# Arquivo único e cumulativo (não mais um por PID) — cada execução nova
# entra em modo append, separada por um cabeçalho com data/hora, em vez de
# espalhar o histórico em dezenas de arquivos por processo.
LOG_FILE = LOG_DIR / "dashspy_ads.log"
with open(LOG_FILE, "a", encoding="utf-8") as _f:
    _f.write(
        f"\n{'=' * 90}\n"
        f"NOVA EXECUÇÃO — PID {os.getpid()} — "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"{'=' * 90}\n"
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        RichHandler(rich_tracebacks=True, markup=True),
        logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    ]
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Carrega variáveis de ambiente
# ---------------------------------------------------------------------------
load_dotenv()

META_ACCESS_TOKEN       = os.environ["META_ACCESS_TOKEN"]
META_AD_ACCOUNT_IDS     = [a.strip() for a in os.environ["META_AD_ACCOUNT_IDS"].split(",")]
LINKEDIN_ACCESS_TOKEN   = os.environ["LINKEDIN_ACCESS_TOKEN"]
LINKEDIN_AD_ACCOUNT_IDS = [a.strip() for a in os.environ["LINKEDIN_AD_ACCOUNT_IDS"].split(",")]
SUPABASE_URL            = os.environ["SUPABASE_URL"]
SUPABASE_KEY            = os.environ["SUPABASE_KEY"]
GOOGLE_ADS_YAML_PATH    = os.environ.get("GOOGLE_ADS_YAML_PATH", "google-ads.yaml")
PATH_OUTPUTS_M          = os.environ["PATH_OUTPUTS_M"]

# ---------------------------------------------------------------------------
# Constantes de Supabase (nomes das tabelas)
# ---------------------------------------------------------------------------
TABLE_META      = "data_meta_v2"
TABLE_GOOGLE    = "data_google_v2"
TABLE_LINKEDIN  = "data_linkedin_v2"

# Data de início histórico por fonte
META_HISTORY_START      = "2023-09-21"
GOOGLE_HISTORY_START    = "2021-11-22"
LINKEDIN_HISTORY_START  = "2023-09-01"

DASHSPY_BUILD = "2026-07-06-adaptive-v3"

# ---------------------------------------------------------------------------
# Helpers de data
# ---------------------------------------------------------------------------

def yesterday() -> str:
    """Retorna a data de ontem no formato aaaa-mm-dd."""
    return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


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
# META ADS
# Schema: date_start (DATE), campaign_name (STRING), cost (FLOAT),
#         dt_h_recording_data (TIMESTAMP)
# ---------------------------------------------------------------------------

META_BASE_URL         = "https://graph.facebook.com/v25.0"
META_RATE_LIMIT_CODES = {1, 4, 17, 341}
META_BATCH_SIZE       = 50
META_RETRY_WAIT       = 60
META_MAX_RETRIES      = 5

def _meta_fetch_page(url: str, params: dict | None = None) -> dict:
    """Faz uma requisição GET para a Meta API com retry automático em rate-limit e timeout."""
    retries = 0
    while True:
        try:
            resp = requests.get(url, params=params, timeout=60)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            retries += 1
            if retries > META_MAX_RETRIES:
                raise RuntimeError(
                    f"Meta API connection error após {META_MAX_RETRIES} tentativas."
                )
            log.warning(
                "Meta connection error. Tentativa %d/%d — aguardando %ss…",
                retries, META_MAX_RETRIES, META_RETRY_WAIT,
            )
            time.sleep(META_RETRY_WAIT)
            continue

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


def _fetch_meta_ads_account(account_id: str, data_inicial: str, data_final: str) -> tuple[list[dict], bool]:
    """Busca insights de uma conta Meta Ads. Retorna (records, completed).
    completed=False indica que a coleta foi interrompida por erro — os registros
    retornados correspondem apenas às janelas concluídas com sucesso."""
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
        chunk_records: list[dict] = []
        try:
            while url:
                page += 1
                data    = _meta_fetch_page(url, params=base_params if page == 1 else None)
                records = data.get("data", [])
                for r in records:
                    r["_account_id"] = account_id
                chunk_records.extend(records)
                log.info("    Página %d: %d registros.", page, len(records))
                url = data.get("paging", {}).get("next")
            all_records.extend(chunk_records)
        except Exception as exc:
            log.error(
                "Meta Ads: conta %s — erro na janela %s→%s: %s. "
                "Salvando %d registros das janelas concluídas até %s.",
                account_id, current, chunk_end, exc,
                len(all_records),
                current - timedelta(days=1) if all_records else "nenhum",
            )
            return all_records, False

        current = chunk_end + timedelta(days=1)

    return all_records, True




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


def run_meta_collect(
    sb: Client,
    recording_ts: str,
    account_ids: list[str] | None = None,
) -> tuple[list[dict], str | None]:
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

    targets = account_ids or META_AD_ACCOUNT_IDS
    log.info("Meta Ads: buscando de %s até %s (%d contas).", data_inicial, data_final, len(targets))
    all_rows: list[dict] = []
    paths: list[str] = []

    for account_id in targets:
        log.info("Meta Ads: processando conta %s.", account_id)
        raw, completed = _fetch_meta_ads_account(account_id, data_inicial, data_final)
        if raw:
            rows = process_meta_records(raw, recording_ts)
            path = save_temp(f"meta_{account_id}", rows, recording_ts)
            log.info("Meta Ads: conta %s — %d registros → %s", account_id, len(rows), path)
            all_rows.extend(rows)
            paths.append(path)
        if not completed:
            log.warning(
                "Meta Ads: coleta interrompida na conta %s. "
                "Use 'python dashspy_ads.py meta-resume' para retomar.",
                account_id,
            )
            break

    if not all_rows:
        return [], None

    return all_rows, ", ".join(paths)


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
LINKEDIN_API_VERSION   = "202606"
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
# PIPELINE PRINCIPAL
# ---------------------------------------------------------------------------

def main() -> None:
    recording_ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC")
    log.info("Iniciando dashspy_ads — registro em: %s", recording_ts)

    sb = get_supabase_client()

    pipelines = [
        ("meta",     "Meta Ads",      run_meta_collect,     send_meta),
        ("google",   "Google Ads",    run_google_collect,   send_google),
        ("linkedin", "LinkedIn Ads",  run_linkedin_collect, send_linkedin),
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
        log.warning("dashspy_ads finalizado com falhas no envio: %s", ", ".join(falhas.keys()))
    else:
        log.info("dashspy_ads finalizado com sucesso.")


PLATFORM_SEND_MAP = {
    "meta":     send_meta,
    "google":   send_google,
    "linkedin": send_linkedin,
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
        size_mb = f.stat().st_size / (1024 * 1024)
        print(f"  [{i}] {f.name}  ({size_mb:.1f} MB)")

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
        "meta":     ("Meta Ads",      run_meta_collect,     send_meta),
        "google":   ("Google Ads",    run_google_collect,   send_google),
        "linkedin": ("LinkedIn Ads",  run_linkedin_collect, send_linkedin),
    }

    _cmd = sys.argv[1] if len(sys.argv) > 1 else None

    if _cmd is None:
        main()
    elif _cmd == "--retry":
        retry_from_outputs()
    elif _cmd == "meta":
        print("\nContas Meta Ads disponíveis:")
        for _i, _acc in enumerate(META_AD_ACCOUNT_IDS, 1):
            print(f"  [{_i}] {_acc}")
        _sel = input("\nNúmero(s) da(s) conta(s) a coletar (ex: 1,2) ou 'todas': ").strip()
        if _sel.lower() in ("todas", "all", ""):
            _selected_accounts = None
        else:
            _idxs = [int(x.strip()) - 1 for x in _sel.split(",") if x.strip().isdigit()]
            _selected_accounts = [META_AD_ACCOUNT_IDS[i] for i in _idxs if 0 <= i < len(META_AD_ACCOUNT_IDS)]
            if not _selected_accounts:
                print("Nenhuma conta válida selecionada. Encerrando.")
                sys.exit(1)
        _recording_ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC")
        log.info("Coleta individual: Meta Ads — registro em: %s", _recording_ts)
        _sb = get_supabase_client()
        try:
            _rows, _path = run_meta_collect(_sb, _recording_ts, account_ids=_selected_accounts)
        except Exception as _exc:
            log.error("Coleta [Meta Ads] falhou: %s", _exc, exc_info=True)
            sys.exit(1)
        if not _rows:
            log.info("Meta Ads: nenhum dado novo.")
            sys.exit(0)
        if not aguardar_confirmacao("Meta Ads", _path):
            log.warning("Envio do Meta Ads cancelado. Arquivos mantidos em: %s", _path)
            sys.exit(0)
        try:
            send_meta(_sb, _rows)
            log.info("Meta Ads: enviado com sucesso.")
        except Exception as _exc:
            log.error("Envio [Meta Ads] falhou: %s", _exc, exc_info=True)
            sys.exit(1)
    elif _cmd == "meta-resume":
        _output_dir = Path("PATH_OUTPUTS_M")
        print("\nContas Meta Ads disponíveis:")
        for _i, _acc in enumerate(META_AD_ACCOUNT_IDS, 1):
            print(f"  [{_i}] {_acc}")
        _sel = input("\nNúmero da conta a retomar: ").strip()
        if not _sel.isdigit() or not (1 <= int(_sel) <= len(META_AD_ACCOUNT_IDS)):
            print("Seleção inválida. Encerrando.")
            sys.exit(1)
        _account_id = META_AD_ACCOUNT_IDS[int(_sel) - 1]
        _files = sorted(_output_dir.glob(f"meta_{_account_id}_*.json"))
        if not _files:
            print(f"Nenhum arquivo encontrado para a conta {_account_id} em outputs/.")
            sys.exit(1)
        _latest_file = _files[-1]
        _existing = json.loads(_latest_file.read_text(encoding="utf-8"))
        if not _existing:
            print(f"Arquivo {_latest_file.name} está vazio.")
            sys.exit(1)
        _last_date = max(r["date_start"] for r in _existing if r.get("date_start"))
        _next_date = (datetime.strptime(_last_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        _data_final_input = input(f"Data final (AAAA-MM-DD) [Enter para ontem, {yesterday()}]: ").strip()
        if _data_final_input:
            try:
                datetime.strptime(_data_final_input, "%Y-%m-%d")
                _data_final = _data_final_input
            except ValueError:
                print("Formato de data inválido. Use AAAA-MM-DD.")
                sys.exit(1)
        else:
            _data_final = yesterday()
        log.info(
            "Retomando conta %s — último date_start no arquivo: %s — coletando de %s até %s.",
            _account_id, _last_date, _next_date, _data_final,
        )
        if _next_date > _data_final:
            log.info("Conta %s já está completa até ontem.", _account_id)
            sys.exit(0)
        _recording_ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC")
        _sb = get_supabase_client()
        _raw, _completed = _fetch_meta_ads_account(_account_id, _next_date, _data_final)
        if not _raw:
            log.info("Nenhum dado novo para a conta %s a partir de %s.", _account_id, _next_date)
            sys.exit(0)
        _rows = process_meta_records(_raw, _recording_ts)
        _path = save_temp(f"meta_{_account_id}", _rows, _recording_ts)
        log.info("Conta %s: %d registros → %s", _account_id, len(_rows), _path)
        if not _completed:
            log.warning("Coleta ainda incompleta. Arquivo salvo em: %s", _path)
        if not aguardar_confirmacao("Meta Ads", _path):
            log.warning("Envio cancelado. Arquivo mantido em: %s", _path)
            sys.exit(0)
        try:
            send_meta(_sb, _rows)
            log.info("Meta Ads: enviado com sucesso.")
        except Exception as _exc:
            log.error("Envio [Meta Ads] falhou: %s", _exc, exc_info=True)
            sys.exit(1)
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
        print(
            "Uso: python dashspy_ads.py "
            "[meta|meta-resume|google|linkedin|--retry]"
        )
        sys.exit(1)
