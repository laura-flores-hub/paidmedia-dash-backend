#!/usr/bin/env python3
"""
main.py — Orquestrador do pipeline paidmedia-dash-backend.

Fases (dentro de uma única execução):

  Fase 1 — Coleta/consolidação (nada é enviado ao Supabase ainda)
    Etapa A (paralelo):
      - dashspy_ads      (Meta/Google/LinkedIn; corte = ontem, já embutido no script)
      - dashspy_hubspot  (Contacts + Deals; corte = cutoff_ts compartilhado)
    Etapa B (sequencial, mesmo cutoff_ts do HubSpot):
      - hubspot_eventos_daily_historical_retry  (daily por padrão; --historical força histórico)
      - consolidate_hubspot_forms                (--all-ready)
      - consolidate_conversions_forms_localsrc    (build local, sem upload)

  Fase 2 — Gate: só segue para o envio se TODAS as etapas acima terminaram
    limpas (nenhuma unidade/etapa com erro). Se qualquer coisa falhou, NADA
    é enviado ao Supabase nesta run — nem ads, nem hubspot CRM, nem forms/
    ad_interactions/conversions.

  Fase 3 — Envio único ao Supabase (só roda se a Fase 2 aprovou):
    - Meta/Google/LinkedIn Ads  -> data_meta_v2 / data_google_v2 / data_linkedin_v2
    - HubSpot Contacts/Deals    -> data_hs_contacts_v2 / data_hs_deals_v2
    - ad_interactions brutos    -> data_hs_ad_interactions_v2
    - forms consolidados        -> data_hs_form_submissions_v2
    - conversions consolidadas  -> data_hs_forms_conversions_consolidated_v1

Retry automático: qualquer unidade/etapa cujo erro pareça transitório/padrão
(timeout, conexão, rate limit, 5xx) é tentada de novo até 2 vezes extras
(3 tentativas no total), com espera entre tentativas. Erros que não batem
com esse padrão falham na primeira tentativa — não tentamos adivinhar
correção para erro desconhecido.

Trava de reexecução: no início, o main.py verifica a run mais recente no
relatório central. Se ela não terminou com overall_status="success" (ou
seja, sobrou qualquer coleta/envio incompleto, ou o processo foi
interrompido no meio), a nova execução:
  - com --force: ignora a run anterior e começa 100% do zero;
  - com --resume: retoma automaticamente, reaproveitando as unidades
    (plataformas de ads, contacts/deals do HubSpot) e etapas (eventos/
    forms/conversions) já concluídas na tentativa anterior — só roda de
    novo o que ainda não foi feito;
  - sem nenhuma flag, em terminal interativo: pergunta ao usuário se quer
    retomar (mesmo comportamento de --resume) ou recusa a execução;
  - sem nenhuma flag, fora de terminal interativo (cron etc.): recusa a
    execução e orienta a rodar com --resume ou --force.

Relatório central: status/main_orchestrator_status.json — um único arquivo,
sempre atualizado, com a run mais recente na posição 0 (histórico decrescente).
O relatório da run em andamento é gravado incrementalmente (a cada etapa
concluída, não só no final) — se o processo for interrompido no meio,
o relatório já reflete exatamente o que foi feito até ali.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)  # todos os subcódigos resolvem paths relativos (outputs/, hubspot_eventos/, logs/) a partir daqui
sys.path.insert(0, str(BASE_DIR))

import dashspy_ads
import dashspy_hubspot
import hubspot_eventos_daily_historical_retry as hubspot_eventos
import consolidate_hubspot_forms
import consolidate_conversions_forms_localsrc as consolidate_conversions
import supabase_event_uploader as sb_uploader

# Cada um dos módulos acima chama logging.basicConfig() no import — só a
# primeira chamada (dashspy_ads) tem efeito real sobre o logger root, então
# sem isto os outros módulos escreveriam seus logs dentro do arquivo de log
# do dashspy_ads em vez dos seus próprios arquivos (logs/<componente>/*.log).
# Resetamos o root e religamos, na mão, o FileHandler dedicado de cada
# módulo ao logger daquele módulo (por nome), preservando um único console
# handler compartilhado.
_ROOT_LOGGER = logging.getLogger()
for _h in list(_ROOT_LOGGER.handlers):
    _ROOT_LOGGER.removeHandler(_h)

from rich.logging import RichHandler  # mesmo handler de console usado pelos subcódigos

_ROOT_LOGGER.setLevel(logging.INFO)
_ROOT_LOGGER.addHandler(RichHandler(rich_tracebacks=True, markup=True))

_LOG_FORMATTER = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")

MAIN_LOG_DIR = BASE_DIR / "logs" / "main"
MAIN_LOG_DIR.mkdir(parents=True, exist_ok=True)
# Arquivo único e cumulativo (não mais um por PID) — cada execução nova
# entra em modo append, separada por um cabeçalho com data/hora.
MAIN_LOG_FILE = MAIN_LOG_DIR / "main.log"


def _write_log_run_separator(log_file: Path) -> None:
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(
            f"\n{'=' * 90}\n"
            f"NOVA EXECUÇÃO — PID {os.getpid()} — "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"{'=' * 90}\n"
        )


def _attach_dedicated_file_handler(module) -> None:
    """Dá a cada subcódigo seu próprio arquivo de log real (module.LOG_FILE),
    em vez de depender do basicConfig (que só funciona para o 1º import).
    O módulo já escreveu seu próprio separador de execução no import (ver
    LOG_FILE de cada um) — aqui só reabrimos em append, sem truncar."""
    log_file = getattr(module, "LOG_FILE", None)
    module_log = getattr(module, "log", None)
    if log_file is None or module_log is None:
        return
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    handler.setFormatter(_LOG_FORMATTER)
    module_log.addHandler(handler)
    module_log.propagate = True  # mensagens continuam aparecendo no console também


for _module in (
    dashspy_ads, dashspy_hubspot, hubspot_eventos,
    consolidate_hubspot_forms, consolidate_conversions,
):
    _attach_dedicated_file_handler(_module)

log = logging.getLogger("main_orchestrator")
_write_log_run_separator(MAIN_LOG_FILE)
_main_file_handler = logging.FileHandler(MAIN_LOG_FILE, mode="a", encoding="utf-8")
_main_file_handler.setFormatter(_LOG_FORMATTER)
log.addHandler(_main_file_handler)

STATUS_DIR = BASE_DIR / "status"
STATUS_FILE = STATUS_DIR / "main_orchestrator_status.json"
MAX_RUNS_KEPT = 30
SAFETY_BUFFER_MINUTES = 10

# Retry automático a nível de orquestrador: só para erros que parecem
# transitórios/padrão (rede, timeout, rate limit, 5xx). 2 tentativas extras
# = 3 tentativas no total. Ajuste as listas abaixo se surgirem novos padrões
# conhecidos de erro transitório.
MAX_EXTRA_RETRIES = 2
RETRY_BACKOFFS_SECONDS = (10, 30)
RETRYABLE_TEXT_PATTERNS = (
    "timeout", "timed out", "connection", "temporarily unavailable",
    "rate limit", "429", "500", "502", "503", "504",
    "reset by peer", "connection aborted", "read timed out",
    "max retries exceeded", "econnreset", "broken pipe",
)

CONSOLIDATED_FORMS_DIR = BASE_DIR / "hubspot_eventos" / "_consolidated" / "forms"


# ---------------------------------------------------------------------------
# Helpers de tempo, JSON e retry
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compute_cutoff() -> datetime:
    """now() - 10min, para dar margem contra dados ainda em ingestão no HubSpot."""
    return utc_now() - timedelta(minutes=SAFETY_BUFFER_MINUTES)


def cutoff_recording_ts(dt: datetime) -> str:
    """Formato usado pelo dashspy_hubspot.py/dashspy_ads.py: 'YYYY-MM-DDTHH:MM:SS UTC'."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S UTC")


def parse_cutoff_iso(cutoff_ts: str) -> datetime:
    """Parseia de volta o formato produzido por iso() ('...Z')."""
    return datetime.strptime(cutoff_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_json(path: Path) -> Any:
    import json
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data: Any) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.fsync(os.open(tmp, os.O_RDONLY))
    tmp.replace(path)


def _load_local_rows(path_field: str) -> list[dict]:
    """Recarrega as linhas já coletadas e salvas localmente numa tentativa
    anterior, pra reaproveitar em vez de recoletar da API.

    `path_field` pode ser um único caminho ou vários separados por ", "
    (é assim que run_meta_collect devolve — um arquivo por conta). Usa o
    leitor em streaming do dashspy_hubspot (item a item, com intern() nas
    chaves) pra qualquer arquivo, grande ou pequeno — é seguro e evita
    duplicar essa lógica aqui.
    """
    rows: list[dict] = []
    for part in path_field.split(","):
        p = part.strip()
        if not p:
            continue
        rows.extend(dashspy_hubspot._read_json_array_streaming(Path(p)))
    return rows


def _strip_runtime_fields(obj: Any) -> Any:
    """Remove do relatório persistido qualquer chave que comece com '_'
    (usadas para carregar dados em memória entre fases, como listas de
    linhas a enviar — não fazem sentido gravadas no status file)."""
    if isinstance(obj, dict):
        return {k: _strip_runtime_fields(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_runtime_fields(v) for v in obj]
    return obj


def _is_retryable_text(text: str | None) -> bool:
    t = (text or "").lower()
    return any(p in t for p in RETRYABLE_TEXT_PATTERNS)


def _retry_loop(label: str, attempt_fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Chama attempt_fn() repetidamente. attempt_fn deve retornar um dict
    com 'status' == 'error' (+ 'error': mensagem) em caso de falha, ou
    qualquer outro status em caso de sucesso/estado terminal não-erro.
    Só tenta de novo (até MAX_EXTRA_RETRIES vezes) se o texto do erro
    combinar com um padrão conhecido de falha transitória."""
    attempt = 0
    result: dict[str, Any] = {}
    while True:
        attempt += 1
        result = attempt_fn()
        result["attempts"] = attempt
        if result.get("status") != "error":
            if attempt > 1:
                log.info("%s: recuperado na tentativa %d.", label, attempt)
            return result

        err = result.get("error", "")
        if attempt > MAX_EXTRA_RETRIES or not _is_retryable_text(err):
            if attempt > 1:
                log.error("%s: falhou definitivamente após %d tentativa(s). Erro: %s", label, attempt, err)
            else:
                log.error("%s: falhou (erro não reconhecido como transitório, sem retry automático). Erro: %s", label, err)
            return result

        wait = RETRY_BACKOFFS_SECONDS[min(attempt - 1, len(RETRY_BACKOFFS_SECONDS) - 1)]
        log.warning(
            "%s: falha parece transitória (tentativa %d/%d) — nova tentativa em %ss. Erro: %s",
            label, attempt, MAX_EXTRA_RETRIES + 1, wait, str(err)[:200],
        )
        time.sleep(wait)


# ---------------------------------------------------------------------------
# Fase 1 / Etapa A: dashspy_ads (coleta apenas — sem envio)
# ---------------------------------------------------------------------------

def run_ads_stage(prior_platforms: dict[str, Any] | None = None) -> dict[str, Any]:
    stage: dict[str, Any] = {"name": "dashspy_ads", "started_at": iso(utc_now()), "platforms": {}}
    sb = dashspy_ads.get_supabase_client()
    recording_ts = utc_now().strftime("%Y-%m-%dT%H:%M:%S UTC")
    stage["recording_ts"] = recording_ts
    stage["window_end_expected"] = dashspy_ads.yesterday()  # ads sempre 1 dia atrás do HubSpot

    pipelines = [
        ("meta", "Meta Ads", dashspy_ads.run_meta_collect, dashspy_ads.send_meta),
        ("google", "Google Ads", dashspy_ads.run_google_collect, dashspy_ads.send_google),
        ("linkedin", "LinkedIn Ads", dashspy_ads.run_linkedin_collect, dashspy_ads.send_linkedin),
    ]

    for key, nome, fn_collect, fn_send in pipelines:
        prior = (prior_platforms or {}).get(key)

        if prior and prior.get("status") == "sent":
            log.info("dashspy_ads[%s]: já enviado numa tentativa anterior — pulando.", nome)
            stage["platforms"][key] = prior
            continue

        if prior and prior.get("status") == "collected" and prior.get("local_file"):
            try:
                rows = _load_local_rows(prior["local_file"])
                log.info(
                    "dashspy_ads[%s]: reaproveitando %d linhas já coletadas em %s "
                    "(tentativa anterior) — não vai recoletar.",
                    nome, len(rows), prior["local_file"],
                )
                stage["platforms"][key] = {
                    "status": "collected", "rows": len(rows), "local_file": prior["local_file"],
                    "_rows": rows, "_send_fn": fn_send, "attempts": 0,
                    "reused_from_previous_run": True,
                }
                continue
            except Exception as exc:
                log.warning(
                    "dashspy_ads[%s]: não consegui reaproveitar %s (%s) — recoletando do zero.",
                    nome, prior.get("local_file"), exc,
                )

        def attempt(fn_collect=fn_collect, nome=nome):
            try:
                rows, path = fn_collect(sb, recording_ts)
            except Exception as exc:
                return {"status": "error", "error": str(exc)}
            if not rows:
                return {"status": "up_to_date", "rows": 0}
            return {"status": "collected", "rows": len(rows), "local_file": path, "_rows": rows, "_send_fn": fn_send}

        entry = _retry_loop(f"dashspy_ads[{nome}] coleta", attempt)
        stage["platforms"][key] = entry

    stage["finished_at"] = iso(utc_now())
    stage["status"] = _collect_rollup(stage["platforms"].values())
    return stage


# ---------------------------------------------------------------------------
# Fase 1 / Etapa A: dashspy_hubspot (coleta apenas — sem envio)
# ---------------------------------------------------------------------------

def run_hubspot_stage(recording_ts: str, prior_sources: dict[str, Any] | None = None) -> dict[str, Any]:
    stage: dict[str, Any] = {
        "name": "dashspy_hubspot",
        "started_at": iso(utc_now()),
        "cutoff_used": recording_ts,
        "sources": {},
    }
    sb = dashspy_hubspot.get_supabase_client()

    sources = [
        ("hubspot", "HubSpot Contacts", dashspy_hubspot.run_hubspot_collect,
         dashspy_hubspot.resume_hubspot_contacts, dashspy_hubspot.send_hubspot),
        ("deals", "HubSpot Deals", dashspy_hubspot.run_deals_collect,
         dashspy_hubspot.resume_hubspot_deals, dashspy_hubspot.send_deals),
    ]

    for key, nome, fn_collect, fn_resume, fn_send in sources:
        prior = (prior_sources or {}).get(key)

        if prior and prior.get("status") == "sent" and not prior.get("incomplete_window"):
            log.info("dashspy_hubspot[%s]: já enviado numa tentativa anterior — pulando.", nome)
            stage["sources"][key] = prior
            continue

        if prior and prior.get("status") == "sent" and prior.get("incomplete_window"):
            # Foi enviado, mas só cobria uma janela antiga/incompleta (ver
            # incomplete_window) — não é seguro pular: ainda falta coletar o
            # intervalo real até o cutoff. Recoleta do zero (fn_collect vai
            # naturalmente pegar só o que falta, a partir do que já está no
            # Supabase).
            log.info(
                "dashspy_hubspot[%s]: envio anterior só cobriu até %s — recoletando "
                "a partir daí para fechar a lacuna.",
                nome, prior["incomplete_window"].get("covered_until"),
            )

        if prior and prior.get("status") == "collected" and prior.get("local_file"):
            try:
                rows = _load_local_rows(prior["local_file"])
                log.info(
                    "dashspy_hubspot[%s]: reaproveitando %d linhas já coletadas em %s "
                    "(tentativa anterior) — não vai recoletar.",
                    nome, len(rows), prior["local_file"],
                )
                stage["sources"][key] = {
                    "status": "collected", "rows": len(rows), "local_file": prior["local_file"],
                    "_rows": rows, "_send_fn": fn_send, "attempts": 0,
                    "reused_from_previous_run": True,
                }
                continue
            except Exception as exc:
                log.warning(
                    "dashspy_hubspot[%s]: não consegui reaproveitar %s (%s) — recoletando do zero.",
                    nome, prior.get("local_file"), exc,
                )

        def attempt(fn_collect=fn_collect, fn_resume=fn_resume, key=key, nome=nome, fn_send=fn_send):
            incomplete_window = None
            try:
                rows, path = fn_collect(sb, recording_ts)
            except dashspy_hubspot.RetryPointPending as exc:
                log.warning("%s: retry point pendente de execução anterior — tentando retomar: %s", nome, exc)
                # O retry point resolve só a janela ANTIGA presa (até o cutoff que
                # ele tinha guardado) — não continua coletando até o cutoff desta
                # run. Sem isto, a etapa fica marcada como concluída mesmo tendo
                # um intervalo real (cutoff antigo -> cutoff desta run) que nunca
                # foi buscado, e o relatório mentiria dizendo "success".
                retry_state_before = dashspy_hubspot._load_retry_state(key)
                try:
                    rows, path = fn_resume()
                except Exception as exc2:
                    return {"status": "error", "error": str(exc2)}
                if retry_state_before:
                    old_cutoff_ms = int(retry_state_before.get("cutoff_ms") or 0)
                    now_ms = dashspy_hubspot._recording_ts_to_ms(recording_ts)
                    if old_cutoff_ms and old_cutoff_ms < now_ms:
                        incomplete_window = {
                            "reason": "retry point resolveu só até o cutoff antigo — janela seguinte ainda não coletada",
                            "covered_until": dashspy_hubspot._ms_to_iso(old_cutoff_ms),
                            "still_missing_from": dashspy_hubspot._ms_to_iso(old_cutoff_ms),
                            "still_missing_to": dashspy_hubspot._ms_to_iso(now_ms),
                        }
                        log.warning(
                            "%s: retry point resolvido só cobre até %s — ainda falta coletar "
                            "%s até %s (fica pendente pra próxima execução).",
                            nome, incomplete_window["covered_until"],
                            incomplete_window["still_missing_from"], incomplete_window["still_missing_to"],
                        )
            except Exception as exc:
                return {"status": "error", "error": str(exc)}
            if not rows:
                return {"status": "up_to_date_or_retry_pending", "rows": 0}
            result_entry = {"status": "collected", "rows": len(rows), "local_file": path, "_rows": rows, "_send_fn": fn_send}
            if incomplete_window:
                result_entry["incomplete_window"] = incomplete_window
            return result_entry

        entry = _retry_loop(f"dashspy_hubspot[{nome}] coleta", attempt)
        stage["sources"][key] = entry

    stage["finished_at"] = iso(utc_now())
    stage["status"] = _collect_rollup(stage["sources"].values())
    return stage


def _collect_rollup(units) -> str:
    units = list(units)
    if any(u.get("status") == "error" for u in units):
        return "failed"
    if any(u.get("incomplete_window") for u in units):
        return "failed"
    return "success"


# ---------------------------------------------------------------------------
# Fase 1 / Etapa B: hubspot_eventos_daily_historical_retry
# ---------------------------------------------------------------------------

def _manifest_error_text(manifest_summary: dict[str, Any] | None) -> str:
    if not manifest_summary:
        return "manifest ausente"
    errs = [
        f"{event_type}: {item.get('last_error')}"
        for event_type, item in (manifest_summary.get("event_types") or {}).items()
        if item.get("status") == "error" and item.get("last_error")
    ]
    return "; ".join(errs) or f"status={manifest_summary.get('status')}"


def run_events_stage(cutoff_iso: str, run_type: str, prior_stage: dict[str, Any] | None = None) -> dict[str, Any]:
    if prior_stage and prior_stage.get("status") in ("success", "nothing_to_do"):
        log.info("hubspot_eventos: já concluído (%s) numa tentativa anterior — pulando.", prior_stage.get("status"))
        return prior_stage

    stage: dict[str, Any] = {"name": "hubspot_eventos", "run_type": run_type, "started_at": iso(utc_now())}
    box: dict[str, Any] = {}
    attempt_counter = {"n": 0}

    def attempt():
        attempt_counter["n"] += 1
        if attempt_counter["n"] == 1:
            try:
                result = hubspot_eventos.run_orchestrated(
                    run_type=run_type,
                    cutoff_iso=cutoff_iso if run_type == "daily" else None,
                    retry_pending_first=True,
                )
            except SystemExit as exc:
                return {"status": "nothing_to_do", "detail": str(exc)}
            except Exception as exc:
                return {"status": "error", "error": str(exc)}
            box["result"] = result
            manifest = result.get("manifest")
            if manifest and manifest.get("status") == "complete":
                return {"status": "ok"}
            return {"status": "error", "error": _manifest_error_text(manifest)}

        # tentativas extras: reprocessa só o manifesto desta run, sem criar janela nova
        result = box.get("result")
        if not result or not result.get("manifest"):
            return {"status": "error", "error": "sem manifesto para retomar"}
        try:
            updated = hubspot_eventos.reprocess_manifest_by_path(result["manifest"]["manifest_path"])
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        result["manifest"] = updated
        box["result"] = result
        if updated.get("status") == "complete":
            return {"status": "ok"}
        return {"status": "error", "error": _manifest_error_text(updated)}

    outcome = _retry_loop("hubspot_eventos", attempt)
    stage["result"] = box.get("result")

    if outcome["status"] == "nothing_to_do":
        stage["status"] = "nothing_to_do"
        stage["detail"] = outcome.get("detail")
    elif outcome["status"] == "ok":
        stage["status"] = "success"
    else:
        stage["status"] = "failed"
        stage["error"] = outcome.get("error")

    stage["finished_at"] = iso(utc_now())
    return stage


# ---------------------------------------------------------------------------
# Fase 1 / Etapa B: consolidate_hubspot_forms
# ---------------------------------------------------------------------------

def run_consolidate_forms_stage() -> dict[str, Any]:
    stage: dict[str, Any] = {"name": "consolidate_hubspot_forms", "started_at": iso(utc_now())}
    box: dict[str, Any] = {}

    def attempt():
        try:
            result = consolidate_hubspot_forms.consolidate_all_ready()
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        box["result"] = result
        if result["failures"] == 0:
            return {"status": "ok"}
        errs = "; ".join(p.get("error", "") for p in result["processed"] if p["status"] == "error")
        return {"status": "error", "error": errs}

    outcome = _retry_loop("consolidate_hubspot_forms", attempt)
    stage["result"] = box.get("result")
    stage["status"] = "success" if outcome["status"] == "ok" else "failed"
    if outcome["status"] != "ok":
        stage["error"] = outcome.get("error")

    stage["finished_at"] = iso(utc_now())
    return stage


# ---------------------------------------------------------------------------
# Fase 1 / Etapa B: consolidate_conversions_forms_localsrc (build local; sem upload)
# ---------------------------------------------------------------------------

def run_consolidate_conversions_stage() -> tuple[dict[str, Any], dict[str, Any] | None]:
    stage: dict[str, Any] = {"name": "consolidate_conversions_forms_localsrc", "started_at": iso(utc_now())}
    box: dict[str, Any] = {}

    def attempt():
        try:
            build_result = consolidate_conversions.build_consolidation()
        except consolidate_conversions.PendingFormRunsError as exc:
            return {"status": "error", "error": str(exc), "pending_runs": exc.pending_runs}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        box["build_result"] = build_result
        return {"status": "ok"}

    outcome = _retry_loop("consolidate_conversions_forms_localsrc", attempt)
    build_result = box.get("build_result")

    if outcome["status"] == "ok":
        stage["status"] = "no_rows" if not build_result["consolidated_rows"] else "ready_for_review"
        stage["review"] = build_result["review"]
        stage["output_jsonl"] = str(build_result["output_jsonl"])
    else:
        stage["status"] = "failed"
        stage["error"] = outcome.get("error")
        if outcome.get("pending_runs"):
            stage["pending_runs"] = outcome["pending_runs"]

    stage["finished_at"] = iso(utc_now())
    return stage, build_result


# ---------------------------------------------------------------------------
# Fase 2: gate de limpeza (tudo-ou-nada para o envio)
# ---------------------------------------------------------------------------

def _is_stage_clean(stage_name: str, stage: dict[str, Any]) -> bool:
    status = stage.get("status")
    if stage_name in ("dashspy_ads", "dashspy_hubspot"):
        return status == "success"
    if stage_name == "hubspot_eventos":
        return status in ("success", "nothing_to_do")
    if stage_name == "consolidate_hubspot_forms":
        return status == "success"
    if stage_name == "consolidate_conversions_forms_localsrc":
        return status in ("ready_for_review", "no_rows")
    return False


# ---------------------------------------------------------------------------
# Fase 3: envio único ao Supabase (tudo ou nada)
# ---------------------------------------------------------------------------

def _event_type_files(events_stage: dict[str, Any], event_type: str) -> list[str]:
    result = events_stage.get("result") or {}
    manifests = list(result.get("retried_manifests") or [])
    if result.get("manifest"):
        manifests.append(result["manifest"])

    files = []
    for manifest in manifests:
        item = (manifest.get("event_types") or {}).get(event_type)
        if item and item.get("status") == "complete" and item.get("output_file"):
            files.append(item["output_file"])
    return files


def _successful_form_run_ids(forms_stage: dict[str, Any]) -> list[str]:
    result = forms_stage.get("result") or {}
    return [p["run_id"] for p in result.get("processed", []) if p["status"] == "success"]


def _send_units(label_prefix: str, units: dict[str, Any], sb: Any) -> None:
    """Envia in-place cada unidade coletada (status=='collected') usando a
    função de envio guardada em '_send_fn'/'_rows'. Atualiza 'status' para
    'sent' ou 'error' (com retry automático para erros transitórios)."""
    for key, entry in units.items():
        if entry.get("status") != "collected":
            continue
        fn_send = entry.pop("_send_fn", None)
        rows = entry.pop("_rows", None)
        if fn_send is None or rows is None:
            entry["status"] = "error"
            entry["error"] = "dados de envio ausentes (bug interno do orquestrador)"
            continue

        def attempt(fn_send=fn_send, rows=rows, sb=sb):
            try:
                fn_send(sb, rows)
            except Exception as exc:
                return {"status": "error", "error": str(exc)}
            return {"status": "sent"}

        result = _retry_loop(f"{label_prefix}[{key}] envio", attempt)
        entry["status"] = result["status"]
        entry["send_attempts"] = result["attempts"]
        if result["status"] == "error":
            entry["error"] = result.get("error")
        elif result["status"] == "sent" and key in ("hubspot", "deals"):
            # dashspy_hubspot mantém seu próprio retry point por fonte
            # (hubspot/deals), independente do relatório do orquestrador.
            # Sem isto, um retry point antigo já enviado com sucesso nunca
            # é limpo e volta a ser detectado como pendente em toda run futura.
            if dashspy_hubspot._load_retry_state(key):
                dashspy_hubspot._clear_retry_state(key)


def run_send_all_stage(
    ads_stage: dict[str, Any],
    hubspot_stage: dict[str, Any],
    events_stage: dict[str, Any],
    forms_stage: dict[str, Any],
    conversions_build_result: dict[str, Any] | None,
    upstream_clean: bool,
    prior_send_stage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stage: dict[str, Any] = {"name": "supabase_send_all", "started_at": iso(utc_now()), "artifacts": {}}

    if not upstream_clean:
        stage["status"] = "skipped_no_send_due_to_upstream_errors"
        stage["finished_at"] = iso(utc_now())
        log.warning("Fase 3 (envio) pulada por completo: pelo menos uma etapa da Fase 1 não terminou limpa.")
        return stage

    # ad_interactions/forms/conversions não têm rastreio por unidade (como
    # ads/hubspot têm) — eles são reconstruídos a cada vez a partir do que
    # hubspot_eventos/forms/conversions listam como "completo", mesmo quando
    # essas etapas foram puladas por reaproveitamento (nada de novo). Sem
    # isto, cada retomada reenviaria de novo tudo já confirmado enviado.
    prior_artifacts = (prior_send_stage or {}).get("artifacts", {})

    sb_ads = dashspy_ads.get_supabase_client()
    sb_hs = dashspy_hubspot.get_supabase_client()

    # 1) Ads (Meta/Google/LinkedIn) — envia o que foi coletado na Etapa A.
    _send_units("dashspy_ads", ads_stage["platforms"], sb_ads)

    # 2) HubSpot CRM (Contacts/Deals) — idem.
    _send_units("dashspy_hubspot", hubspot_stage["sources"], sb_hs)

    # 3) ad_interactions brutos (e_ad_interaction) -> data_hs_ad_interactions_v2
    for f in _event_type_files(events_stage, "e_ad_interaction"):
        artifact_key = f"ads_interactions:{f}"
        if prior_artifacts.get(artifact_key, {}).get("status") == "sent":
            log.info("%s: já enviado numa tentativa anterior — pulando.", artifact_key)
            stage["artifacts"][artifact_key] = prior_artifacts[artifact_key]
            continue

        def attempt(f=f):
            try:
                summary = sb_uploader.upload_jsonl(
                    input_path=Path(f),
                    table=sb_uploader.TABLE_ADS,
                    on_conflict="event_id",
                    transform=sb_uploader.prepare_ad_row,
                    conflict_key=lambda row: (row["event_id"],),
                    required_fields=("event_id", "contact_id", "occurred_at", "extracted_at"),
                    batch_size=sb_uploader.DEFAULT_BATCH_SIZE,
                    dry_run=False,
                    assume_yes=True,
                )
            except Exception as exc:
                return {"status": "error", "error": str(exc)}
            if summary["status"] not in ("sent", "no_valid_rows"):
                return {"status": "error", "error": f"upload_jsonl status inesperado: {summary['status']}"}
            return {"status": "sent", "summary": summary}

        result = _retry_loop(f"ad_interactions[{f}] envio", attempt)
        stage["artifacts"][artifact_key] = result

    # 4) forms consolidados desta run -> data_hs_form_submissions_v2
    for run_id in _successful_form_run_ids(forms_stage):
        path = CONSOLIDATED_FORMS_DIR / f"{run_id}__forms_consolidated_v1.jsonl"
        if not path.exists():
            continue

        artifact_key = f"forms:{path}"
        if prior_artifacts.get(artifact_key, {}).get("status") == "sent":
            log.info("%s: já enviado numa tentativa anterior — pulando.", artifact_key)
            stage["artifacts"][artifact_key] = prior_artifacts[artifact_key]
            continue

        def attempt(path=path):
            try:
                summary = sb_uploader.upload_jsonl(
                    input_path=path,
                    table=sb_uploader.TABLE_FORMS,
                    on_conflict="contact_id,submitted_at",
                    transform=sb_uploader.prepare_form_row,
                    conflict_key=lambda row: (row["contact_id"], row["submitted_at"]),
                    required_fields=("contact_id", "submitted_at", "extracted_at"),
                    batch_size=sb_uploader.DEFAULT_BATCH_SIZE,
                    dry_run=False,
                    assume_yes=True,
                )
            except Exception as exc:
                return {"status": "error", "error": str(exc)}
            if summary["status"] not in ("sent", "no_valid_rows"):
                return {"status": "error", "error": f"upload_jsonl status inesperado: {summary['status']}"}
            return {"status": "sent", "summary": summary}

        result = _retry_loop(f"forms[{path.name}] envio", attempt)
        stage["artifacts"][artifact_key] = result

    # 5) conversions consolidadas -> data_hs_forms_conversions_consolidated_v1
    if conversions_build_result and conversions_build_result["consolidated_rows"]:
        artifact_key = f"conversions:{conversions_build_result['output_jsonl']}"

        if prior_artifacts.get(artifact_key, {}).get("status") == "sent":
            log.info("%s: já enviado numa tentativa anterior — pulando.", artifact_key)
            stage["artifacts"][artifact_key] = prior_artifacts[artifact_key]
        else:
            def attempt():
                try:
                    uploaded = consolidate_conversions.upload_rows(
                        sb=conversions_build_result["sb"],
                        rows=conversions_build_result["consolidated_rows"],
                    )
                except Exception as exc:
                    return {"status": "error", "error": str(exc)}
                return {"status": "sent", "uploaded_rows": uploaded}

            result = _retry_loop("conversions envio", attempt)
            if result["status"] == "sent":
                consolidate_conversions.finalize_upload(conversions_build_result, result["uploaded_rows"])
            stage["artifacts"][artifact_key] = result

    unit_statuses = [u.get("status") for u in ads_stage["platforms"].values() if u.get("status") in ("sent", "error")]
    unit_statuses += [u.get("status") for u in hubspot_stage["sources"].values() if u.get("status") in ("sent", "error")]
    artifact_statuses = [a.get("status") for a in stage["artifacts"].values()]
    all_statuses = unit_statuses + artifact_statuses

    if not all_statuses:
        stage["status"] = "nothing_to_send"
    elif all(s == "sent" for s in all_statuses):
        stage["status"] = "success"
    else:
        stage["status"] = "failed"

    stage["finished_at"] = iso(utc_now())
    return stage


# ---------------------------------------------------------------------------
# Trava de reexecução e relatório central
# ---------------------------------------------------------------------------

def load_last_run() -> dict[str, Any] | None:
    if not STATUS_FILE.exists():
        return None
    try:
        data = load_json(STATUS_FILE)
    except Exception:
        log.warning("status file existente ilegível — tratando como inexistente.")
        return None
    runs = data.get("runs") or []
    return runs[0] if runs else None


def summarize_pending(run_report: dict[str, Any]) -> list[str]:
    """Lista humanamente legível do que ficou pendente numa run, para o
    operador saber o que resolver antes de rodar de novo (ou usar --force)."""
    lines: list[str] = []
    stages = run_report.get("stages", {})

    for key in ("dashspy_ads", "dashspy_hubspot"):
        stage = stages.get(key, {})
        units = stage.get("platforms") or stage.get("sources") or {}
        for unit_key, entry in units.items():
            if entry.get("status") == "error":
                lines.append(f"[{key}] {unit_key}: {entry.get('error')}")

    events = stages.get("hubspot_eventos", {})
    if events.get("status") == "failed":
        lines.append(f"[hubspot_eventos] {events.get('error')}")

    forms = stages.get("consolidate_hubspot_forms", {})
    if forms.get("status") == "failed":
        lines.append(f"[consolidate_hubspot_forms] {forms.get('error')}")

    conversions = stages.get("consolidate_conversions_forms_localsrc", {})
    if conversions.get("status") == "failed":
        lines.append(f"[consolidate_conversions_forms_localsrc] {conversions.get('error')}")

    send = stages.get("supabase_send_all", {})
    if send.get("status") in ("failed", "skipped_no_send_due_to_upstream_errors"):
        for art_key, art in (send.get("artifacts") or {}).items():
            if art.get("status") == "error":
                lines.append(f"[supabase_send_all] {art_key}: {art.get('error')}")
        if send.get("status") == "skipped_no_send_due_to_upstream_errors":
            lines.append("[supabase_send_all] envio inteiro pulado por erro upstream — ver etapas acima.")

    return lines


def _describe_prior_run(run_report: dict[str, Any]) -> list[str]:
    """Lista humanamente legível do que já foi feito vs. pendente numa run,
    usada no prompt de retomada."""
    lines: list[str] = []
    stages = run_report.get("stages", {})

    for key, label in (("dashspy_ads", "dashspy_ads"), ("dashspy_hubspot", "dashspy_hubspot")):
        stage = stages.get(key)
        if not stage:
            lines.append(f"  [{label}] não iniciado")
            continue
        units = stage.get("platforms") or stage.get("sources") or {}
        for unit_key, entry in units.items():
            line = f"  [{label}.{unit_key}] {entry.get('status', '?')}"
            gap = entry.get("incomplete_window")
            if gap:
                line += f" (INCOMPLETO — falta coletar {gap.get('still_missing_from')} até {gap.get('still_missing_to')})"
            lines.append(line)

    for key, label in (
        ("hubspot_eventos", "hubspot_eventos"),
        ("consolidate_hubspot_forms", "consolidate_hubspot_forms"),
        ("consolidate_conversions_forms_localsrc", "consolidate_conversions_forms_localsrc"),
        ("supabase_send_all", "supabase_send_all"),
    ):
        stage = stages.get(key)
        lines.append(f"  [{label}] {stage.get('status', '?') if stage else 'não iniciado'}")

    return lines


def upsert_running_report(run_report: dict[str, Any]) -> None:
    """Grava/atualiza o relatório da run atual na posição 0 do histórico
    (mais recente primeiro, empurrando os anteriores pra baixo).

    Chamada repetidas vezes ao longo de uma mesma run (não só no final) —
    se `run_report["run_id"]` já é o da posição 0, atualiza no lugar; caso
    contrário, insere uma entrada nova. Isso garante que o relatório
    sempre reflete o estado mais atual possível, mesmo que o processo seja
    interrompido no meio.
    """
    history = []
    if STATUS_FILE.exists():
        try:
            existing = load_json(STATUS_FILE)
            history = existing.get("runs", [])
        except Exception:
            log.warning("status file existente ilegível, será recriado.")

    stripped = _strip_runtime_fields(run_report)
    if history and history[0].get("run_id") == run_report.get("run_id"):
        history[0] = stripped
    else:
        history.insert(0, stripped)
    history = history[:MAX_RUNS_KEPT]
    write_json_atomic(STATUS_FILE, {"runs": history})


# ---------------------------------------------------------------------------
# --retry-only: resolve uma pendência específica sem rodar o resto do
# pipeline. Substitui os comandos de retry que existiam nos scripts
# individuais — a partir de agora só existem por aqui, e sempre atualizam
# o mesmo relatório único (nunca um estado paralelo que main.py não veja).
# ---------------------------------------------------------------------------

def _update_report_after_retry_only(source: str, status: str, rows: int, path: str | None) -> None:
    """Se essa fonte estiver referenciada na última run registrada, atualiza
    o relatório único no lugar — em vez de deixar um estado que só o
    dashspy_hubspot sabe (o problema original que causou tudo isso)."""
    last = load_last_run()
    if not last:
        return
    hs_stage = last.get("stages", {}).get("dashspy_hubspot")
    if not hs_stage or source not in hs_stage.get("sources", {}):
        return

    unit = hs_stage["sources"][source]
    unit["status"] = status
    unit["rows"] = rows
    unit["local_file"] = path
    unit.pop("incomplete_window", None)
    hs_stage["status"] = _collect_rollup(hs_stage["sources"].values())

    upstream_clean = all(
        _is_stage_clean(name, last["stages"].get(name, {}))
        for name in ("dashspy_ads", "dashspy_hubspot", "hubspot_eventos",
                      "consolidate_hubspot_forms", "consolidate_conversions_forms_localsrc")
    )
    send_ok = last.get("stages", {}).get("supabase_send_all", {}).get("status") in ("success", "nothing_to_send")
    last["overall_status"] = "success" if (upstream_clean and send_ok) else "failed"

    upsert_running_report(last)
    log.info("Relatório único atualizado (%s).", STATUS_FILE)


def _run_retry_only(target: str) -> int:
    if target in ("hubspot-contacts", "hubspot-deals"):
        source = "hubspot" if target == "hubspot-contacts" else "deals"
        state = dashspy_hubspot._load_retry_state(source)
        if not state:
            log.info("Não há retry point pendente para %s.", target)
            return 0

        # Se o relatório único já confirma que esse retry point específico
        # (mesmo complete_output_path) foi enviado com sucesso antes, ele é
        # lixo órfão — nunca foi limpo, mas não há nada de fato pendente.
        # Sem esta checagem, reenviaria à toa (upsert é seguro, mas lento
        # e desnecessário para centenas de milhares de linhas).
        last = load_last_run()
        prior_unit = ((last or {}).get("stages", {}).get("dashspy_hubspot", {}).get("sources", {}).get(source))
        if (
            prior_unit
            and prior_unit.get("status") == "sent"
            and prior_unit.get("local_file") == state.get("complete_output_path")
        ):
            log.info(
                "%s: o relatório já confirma que %s foi enviado com sucesso — "
                "retry point é órfão, limpando sem reenviar.",
                target, state.get("complete_output_path"),
            )
            dashspy_hubspot._clear_retry_state(source)
            return 0

        fn_resume = dashspy_hubspot.resume_hubspot_contacts if source == "hubspot" else dashspy_hubspot.resume_hubspot_deals
        fn_send = dashspy_hubspot.send_hubspot if source == "hubspot" else dashspy_hubspot.send_deals

        log.info("--retry-only %s: retomando retry point (status=%s)...", target, state.get("status"))
        try:
            rows, path = fn_resume()
        except Exception as exc:
            log.error("Retry de %s falhou: %s", target, exc, exc_info=True)
            return 1

        if not rows:
            log.info("%s: retomada não produziu linhas novas.", target)
            return 0

        sb = dashspy_hubspot.get_supabase_client()
        try:
            fn_send(sb, rows)
        except Exception as exc:
            log.error("Envio de %s falhou: %s", target, exc, exc_info=True)
            return 1

        dashspy_hubspot._clear_retry_state(source)
        log.info("%s: %d linhas enviadas, retry point limpo.", target, len(rows))
        _update_report_after_retry_only(source, "sent", len(rows), path)
        return 0

    if target == "events":
        log.info("--retry-only events: reprocessando manifestos incompletos (sem criar janela nova)...")
        summaries = hubspot_eventos.retry_incomplete_manifests()
        if not summaries:
            log.info("Nenhum manifesto incompleto encontrado.")
            return 0
        for s in summaries:
            log.info("  run_id=%s status=%s", s.get("run_id"), s.get("status"))
        if any(s.get("status") != "complete" for s in summaries):
            log.error("Algum manifesto continua incompleto — rode de novo depois de investigar.")
            return 1
        log.info("Todos os manifestos pendentes foram reprocessados com sucesso.")
        return 0

    log.error("Alvo de --retry-only desconhecido: %s", target)
    return 2


# ---------------------------------------------------------------------------
# Orquestração principal
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Orquestrador do pipeline paidmedia-dash-backend.")
    parser.add_argument(
        "--events-run-type",
        choices=["daily", "historical"],
        default="daily",
        help="Modo do hubspot_eventos nesta run completa (padrão: daily). Para só "
             "reprocessar manifestos incompletos sem rodar o resto do pipeline, use "
             "--retry-only events.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignora a trava de reexecução e começa uma run nova do zero, sem reaproveitar "
             "nada da run anterior incompleta. Exige confirmação interativa mesmo assim — "
             "nunca roda desassistido.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Retoma automaticamente a última run incompleta, sem perguntar — reaproveita "
             "unidades/etapas já concluídas (ads, contacts/deals, eventos) e só roda de novo "
             "o que ainda falta.",
    )
    parser.add_argument(
        "--retry-only",
        choices=["hubspot-contacts", "hubspot-deals", "events"],
        help="Resolve só essa pendência específica (sem rodar o resto do pipeline) e sai. "
             "Substitui os comandos de retry que existiam nos scripts individuais "
             "(dashspy_hubspot.py hubspot-resume/deals-resume, hubspot_eventos ...--run-type retry) "
             "— agora tudo passa pelo relatório único do main.py.",
    )
    args = parser.parse_args()

    if args.retry_only:
        return _run_retry_only(args.retry_only)

    if args.force and args.resume:
        log.error("Use --force OU --resume, não os dois ao mesmo tempo.")
        return 2

    last_run = load_last_run()
    resume_from: dict[str, Any] | None = None

    # dashspy_hubspot mantém, por fonte (hubspot/deals), seu próprio retry
    # point em disco — um mecanismo separado do relatório do orquestrador,
    # que existia antes deste e não é apagado automaticamente por ele. Uma
    # run pendente aqui tem que travar a próxima execução do main.py da
    # mesma forma que uma run do orquestrador incompleta trava — não pode
    # ser só resolvida em silêncio no meio de uma etapa sem o operador saber.
    pending_retry_sources = [s for s in ("hubspot", "deals") if dashspy_hubspot._load_retry_state(s)]
    orchestrator_pending = bool(last_run and last_run.get("overall_status") != "success")

    if orchestrator_pending or pending_retry_sources:
        log.warning("=" * 90)
        if orchestrator_pending:
            log.warning(
                "Última run (%s) não terminou 100%% (overall_status=%s%s).",
                last_run.get("run_id"),
                last_run.get("overall_status"),
                f", parou em {last_run.get('finished_at')}" if last_run.get("finished_at") else " — foi interrompida no meio",
            )
            for line in _describe_prior_run(last_run):
                log.warning(line)
        for source in pending_retry_sources:
            state = dashspy_hubspot._load_retry_state(source)
            log.warning(
                "  [dashspy_hubspot retry point] %s: status=%s (última atualização: %s) — "
                "não foi enviado/limpo ainda, independente do relatório do orquestrador.",
                source, state.get("status"), state.get("updated_at"),
            )
        log.warning("=" * 90)

        if args.force:
            # --force é destrutivo (descarta uma run incompleta e tudo que
            # ela já tinha feito) — precisa de um humano confirmando na hora,
            # nunca pode disparar sozinho (cron, script, --force "esquecido"
            # numa automação). Sem terminal interativo, é recusado mesmo com
            # a flag.
            if not sys.stdin.isatty():
                log.error("=" * 90)
                log.error(
                    "Execução recusada: --force exige confirmação humana em terminal "
                    "interativo — não pode ser usado de forma desassistida (cron, script "
                    "automatizado, etc.)."
                )
                log.error("=" * 90)
                return 3
            try:
                resposta = input(
                    "\n--force vai DESCARTAR a run anterior incompleta e começar 100% do "
                    "zero (nada do que já foi feito é reaproveitado). Confirma? [s/N]: "
                ).strip().lower()
            except EOFError:
                resposta = "n"
            if resposta != "s":
                log.error("Execução recusada. --force não confirmado.")
                return 3
            log.warning("--force confirmado: ignorando a run anterior, começando 100%% do zero.")
        elif args.resume:
            log.info("--resume: retomando a partir da run anterior.")
            resume_from = last_run
        elif sys.stdin.isatty():
            try:
                resposta = input(
                    "\nRetomar a partir de onde parou, reaproveitando o que já foi concluído? [s/N]: "
                ).strip().lower()
            except EOFError:
                resposta = "n"
            if resposta == "s":
                resume_from = last_run
            else:
                log.error(
                    "Execução recusada. Rode de novo e responda 's', ou use --resume "
                    "(retomar sem perguntar) / --force (começar do zero)."
                )
                return 3
        else:
            log.error("=" * 90)
            log.error(
                "Execução recusada (sem terminal interativo pra perguntar). "
                "Rode com --resume pra continuar de onde parou, ou --force pra começar do zero."
            )
            log.error("=" * 90)
            return 3

        # Se o relatório do orquestrador já confirma que essa fonte foi
        # enviada com sucesso numa run anterior, o retry point é lixo órfão
        # (nunca foi limpo) — sem isso, a lógica de "pular unidade já
        # enviada" nem chegaria a tocar em dashspy_hubspot de novo, e o
        # arquivo ficaria preso pra sempre. Limpa aqui, antes de decidir
        # o que reaproveitar/pular.
        for source in pending_retry_sources:
            prior_sources = ((last_run or {}).get("stages", {}).get("dashspy_hubspot", {}).get("sources", {}))
            if prior_sources.get(source, {}).get("status") == "sent":
                log.info(
                    "dashspy_hubspot retry point de %s já foi enviado com sucesso antes — "
                    "limpando arquivo órfão.", source,
                )
                dashspy_hubspot._clear_retry_state(source)

    run_started_at = utc_now()
    run_id = resume_from["run_id"] if resume_from else "run_" + run_started_at.strftime("%Y%m%d_%H%M%S")
    run_type = args.events_run_type

    # Ao retomar, o cutoff é o MESMO da run original que falhou — não um
    # recalculado com "agora". O objetivo é terminar exatamente a run que
    # ficou pendente (contacts/deals/eventos no mesmo corte), não esticá-la
    # até o instante atual. Uma run nova (sem --resume) sempre calcula um
    # cutoff fresco.
    if resume_from and resume_from.get("cutoff_ts"):
        cutoff_dt = parse_cutoff_iso(resume_from["cutoff_ts"])
    else:
        cutoff_dt = compute_cutoff()
    cutoff_iso = iso(cutoff_dt)
    cutoff_rts = cutoff_recording_ts(cutoff_dt)

    log.info("=" * 90)
    if resume_from:
        log.info(
            "Retomando %s — usando o cutoff ORIGINAL da run incompleta: %s",
            run_id, cutoff_iso,
        )
    else:
        log.info("Iniciando %s — cutoff compartilhado (now-%dmin): %s", run_id, SAFETY_BUFFER_MINUTES, cutoff_iso)
    log.info("hubspot_eventos run_type: %s", run_type)
    log.info("=" * 90)

    run_report: dict[str, Any] = {
        "run_id": run_id,
        "started_at": (resume_from or {}).get("started_at", iso(run_started_at)),
        "resumed_at": iso(run_started_at) if resume_from else None,
        "cutoff_ts": cutoff_iso,
        "safety_buffer_minutes": SAFETY_BUFFER_MINUTES,
        "hubspot_eventos_run_type": run_type,
        "forced": args.force,
        "overall_status": "in_progress",
        "stages": dict((resume_from or {}).get("stages", {})),
    }
    upsert_running_report(run_report)

    prior_stages = (resume_from or {}).get("stages", {})

    try:
        # --- Fase 1 / Etapa A: dashspy_ads + dashspy_hubspot em paralelo (coleta apenas) ---
        log.info("--- Fase 1 / Etapa A: coleta dashspy_ads e dashspy_hubspot (paralelo, sem envio ainda) ---")
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_ads = pool.submit(run_ads_stage, prior_stages.get("dashspy_ads", {}).get("platforms"))
            future_hubspot = pool.submit(
                run_hubspot_stage, cutoff_rts, prior_stages.get("dashspy_hubspot", {}).get("sources"),
            )
            ads_stage = future_ads.result()
            hubspot_stage = future_hubspot.result()

        run_report["stages"]["dashspy_ads"] = ads_stage
        run_report["stages"]["dashspy_hubspot"] = hubspot_stage
        upsert_running_report(run_report)

        # --- Fase 1 / Etapa B: pipeline de eventos HubSpot (sequencial) ---
        log.info("--- Fase 1 / Etapa B: hubspot_eventos -> consolidate_hubspot_forms -> consolidate_conversions ---")
        prior_events_stage = prior_stages.get("hubspot_eventos")
        events_stage = run_events_stage(cutoff_iso, run_type, prior_events_stage)
        run_report["stages"]["hubspot_eventos"] = events_stage
        upsert_running_report(run_report)

        # consolidate_hubspot_forms/consolidate_conversions releem e reprocessam
        # TODO o histórico local acumulado (todos os JSONL de forms/ad_interactions/
        # page_views já baixados, não só o incremento novo) — não são baratos de
        # "só rodar de novo por garantia". Se hubspot_eventos foi pulado (nada novo
        # desde a tentativa anterior) e essas duas etapas já tinham terminado bem
        # antes, não há nada de novo pra consolidar — reaproveita o resultado
        # anterior em vez de refazer o trabalho inteiro pra chegar no mesmo lugar.
        events_was_skipped = events_stage is prior_events_stage

        prior_forms_stage = prior_stages.get("consolidate_hubspot_forms")
        if events_was_skipped and prior_forms_stage and prior_forms_stage.get("status") == "success":
            log.info("consolidate_hubspot_forms: eventos não trouxe nada novo — reaproveitando resultado anterior, pulando.")
            forms_stage = prior_forms_stage
        else:
            forms_stage = run_consolidate_forms_stage()
        run_report["stages"]["consolidate_hubspot_forms"] = forms_stage
        upsert_running_report(run_report)

        prior_conversions_stage = prior_stages.get("consolidate_conversions_forms_localsrc")
        if events_was_skipped and prior_conversions_stage and prior_conversions_stage.get("status") in ("ready_for_review", "no_rows"):
            log.info("consolidate_conversions_forms_localsrc: eventos não trouxe nada novo — reaproveitando resultado anterior, pulando.")
            conversions_stage = prior_conversions_stage
            conversions_build_result = None  # já foi enviado (ou não havia nada a enviar) na tentativa anterior
        else:
            conversions_stage, conversions_build_result = run_consolidate_conversions_stage()
        run_report["stages"]["consolidate_conversions_forms_localsrc"] = conversions_stage
        upsert_running_report(run_report)

        # --- Fase 2: gate tudo-ou-nada ---
        upstream_clean = all(
            _is_stage_clean(name, run_report["stages"][name])
            for name in ("dashspy_ads", "dashspy_hubspot", "hubspot_eventos",
                          "consolidate_hubspot_forms", "consolidate_conversions_forms_localsrc")
        )
        log.info("--- Fase 2: gate de envio — upstream_clean=%s ---", upstream_clean)

        # --- Fase 3: envio único ao Supabase (tudo ou nada) ---
        send_stage = run_send_all_stage(
            ads_stage=ads_stage,
            hubspot_stage=hubspot_stage,
            events_stage=events_stage,
            forms_stage=forms_stage,
            conversions_build_result=conversions_build_result,
            upstream_clean=upstream_clean,
            prior_send_stage=prior_stages.get("supabase_send_all"),
        )
        run_report["stages"]["supabase_send_all"] = send_stage

        run_finished_at = utc_now()
        run_report["finished_at"] = iso(run_finished_at)
        run_report["duration_seconds"] = (run_finished_at - run_started_at).total_seconds()

        # Binário de propósito: qualquer coisa que não seja 100% bloqueia a próxima execução (ver load_last_run/--force/--resume).
        run_report["overall_status"] = "success" if (upstream_clean and send_stage["status"] in ("success", "nothing_to_send")) else "failed"
        upsert_running_report(run_report)

    except KeyboardInterrupt:
        run_report["overall_status"] = "in_progress"
        run_report["interrupted_at"] = iso(utc_now())
        upsert_running_report(run_report)
        log.warning(
            "Execução interrompida pelo usuário. Progresso salvo em %s — "
            "rode de novo com --resume pra continuar de onde parou.",
            STATUS_FILE,
        )
        return 130
    except Exception as exc:
        run_report["overall_status"] = "failed"
        run_report["crashed_at"] = iso(utc_now())
        run_report["crash_error"] = str(exc)
        upsert_running_report(run_report)
        log.error("Execução interrompida por erro inesperado: %s", exc, exc_info=True)
        return 1

    log.info("=" * 90)
    log.info("%s finalizado — overall_status=%s", run_id, run_report["overall_status"])
    if run_report["overall_status"] != "success":
        for line in summarize_pending(run_report):
            log.error("  pendente -> %s", line)
    log.info("Relatório: %s", STATUS_FILE)
    log.info("=" * 90)

    return 0 if run_report["overall_status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
