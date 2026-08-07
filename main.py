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
seja, sobrou qualquer coleta ou envio incompleto), a nova execução é
recusada — é preciso resolver manualmente ou rodar com --force.

Relatório central: status/main_orchestrator_status.json — um único arquivo,
sempre atualizado, com a run mais recente na posição 0 (histórico decrescente).
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
MAIN_LOG_FILE = MAIN_LOG_DIR / f"main_{os.getpid()}.log"


def _attach_dedicated_file_handler(module) -> None:
    """Dá a cada subcódigo seu próprio arquivo de log real (module.LOG_FILE),
    em vez de depender do basicConfig (que só funciona para o 1º import)."""
    log_file = getattr(module, "LOG_FILE", None)
    module_log = getattr(module, "log", None)
    if log_file is None or module_log is None:
        return
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    handler.setFormatter(_LOG_FORMATTER)
    module_log.addHandler(handler)
    module_log.propagate = True  # mensagens continuam aparecendo no console também


for _module in (dashspy_ads, dashspy_hubspot, hubspot_eventos, consolidate_hubspot_forms, consolidate_conversions):
    _attach_dedicated_file_handler(_module)

log = logging.getLogger("main_orchestrator")
_main_file_handler = logging.FileHandler(MAIN_LOG_FILE, mode="w", encoding="utf-8")
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

def run_ads_stage() -> dict[str, Any]:
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

def run_hubspot_stage(recording_ts: str) -> dict[str, Any]:
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
        def attempt(fn_collect=fn_collect, fn_resume=fn_resume, nome=nome, fn_send=fn_send):
            try:
                rows, path = fn_collect(sb, recording_ts)
            except dashspy_hubspot.RetryPointPending as exc:
                log.warning("%s: retry point pendente de execução anterior — tentando retomar: %s", nome, exc)
                try:
                    rows, path = fn_resume()
                except Exception as exc2:
                    return {"status": "error", "error": str(exc2)}
            except Exception as exc:
                return {"status": "error", "error": str(exc)}
            if not rows:
                return {"status": "up_to_date_or_retry_pending", "rows": 0}
            return {"status": "collected", "rows": len(rows), "local_file": path, "_rows": rows, "_send_fn": fn_send}

        entry = _retry_loop(f"dashspy_hubspot[{nome}] coleta", attempt)
        stage["sources"][key] = entry

    stage["finished_at"] = iso(utc_now())
    stage["status"] = _collect_rollup(stage["sources"].values())
    return stage


def _collect_rollup(units) -> str:
    units = list(units)
    if any(u.get("status") == "error" for u in units):
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


def run_events_stage(cutoff_iso: str, run_type: str) -> dict[str, Any]:
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


def run_send_all_stage(
    ads_stage: dict[str, Any],
    hubspot_stage: dict[str, Any],
    events_stage: dict[str, Any],
    forms_stage: dict[str, Any],
    conversions_build_result: dict[str, Any] | None,
    upstream_clean: bool,
) -> dict[str, Any]:
    stage: dict[str, Any] = {"name": "supabase_send_all", "started_at": iso(utc_now()), "artifacts": {}}

    if not upstream_clean:
        stage["status"] = "skipped_no_send_due_to_upstream_errors"
        stage["finished_at"] = iso(utc_now())
        log.warning("Fase 3 (envio) pulada por completo: pelo menos uma etapa da Fase 1 não terminou limpa.")
        return stage

    sb_ads = dashspy_ads.get_supabase_client()
    sb_hs = dashspy_hubspot.get_supabase_client()

    # 1) Ads (Meta/Google/LinkedIn) — envia o que foi coletado na Etapa A.
    _send_units("dashspy_ads", ads_stage["platforms"], sb_ads)

    # 2) HubSpot CRM (Contacts/Deals) — idem.
    _send_units("dashspy_hubspot", hubspot_stage["sources"], sb_hs)

    # 3) ad_interactions brutos (e_ad_interaction) -> data_hs_ad_interactions_v2
    for f in _event_type_files(events_stage, "e_ad_interaction"):
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
        stage["artifacts"][f"ads_interactions:{f}"] = result

    # 4) forms consolidados desta run -> data_hs_form_submissions_v2
    for run_id in _successful_form_run_ids(forms_stage):
        path = CONSOLIDATED_FORMS_DIR / f"{run_id}__forms_consolidated_v1.jsonl"
        if not path.exists():
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
        stage["artifacts"][f"forms:{path}"] = result

    # 5) conversions consolidadas -> data_hs_forms_conversions_consolidated_v1
    if conversions_build_result and conversions_build_result["consolidated_rows"]:
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
        stage["artifacts"][f"conversions:{conversions_build_result['output_jsonl']}"] = result

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


def append_run_to_status_file(run_report: dict[str, Any]) -> None:
    history = []
    if STATUS_FILE.exists():
        try:
            existing = load_json(STATUS_FILE)
            history = existing.get("runs", [])
        except Exception:
            log.warning("status file existente ilegível, será recriado.")

    history.insert(0, _strip_runtime_fields(run_report))
    history = history[:MAX_RUNS_KEPT]
    write_json_atomic(STATUS_FILE, {"runs": history})


# ---------------------------------------------------------------------------
# Orquestração principal
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Orquestrador do pipeline paidmedia-dash-backend.")
    parser.add_argument(
        "--historical",
        action="store_true",
        help="Roda hubspot_eventos em modo histórico em vez de daily (padrão: daily).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignora a trava de reexecução (última run pendente/com erro) e roda mesmo assim.",
    )
    args = parser.parse_args()

    last_run = load_last_run()
    if last_run and last_run.get("overall_status") != "success" and not args.force:
        log.error("=" * 90)
        log.error(
            "Execução recusada: a última run (%s, %s) não terminou 100%% "
            "(overall_status=%s).",
            last_run.get("run_id"), last_run.get("finished_at"), last_run.get("overall_status"),
        )
        for line in summarize_pending(last_run):
            log.error("  pendente -> %s", line)
        log.error("Resolva manualmente e rode de novo, ou use --force para ignorar esta trava.")
        log.error("=" * 90)
        return 3

    run_started_at = utc_now()
    run_id = "run_" + run_started_at.strftime("%Y%m%d_%H%M%S")
    cutoff_dt = compute_cutoff()
    cutoff_iso = iso(cutoff_dt)
    cutoff_rts = cutoff_recording_ts(cutoff_dt)
    run_type = "historical" if args.historical else "daily"

    log.info("=" * 90)
    log.info("Iniciando %s — cutoff compartilhado (now-%dmin): %s", run_id, SAFETY_BUFFER_MINUTES, cutoff_iso)
    log.info("hubspot_eventos run_type: %s", run_type)
    if args.force and last_run:
        log.warning("--force: ignorando trava de reexecução (última run era %s).", last_run.get("overall_status"))
    log.info("=" * 90)

    run_report: dict[str, Any] = {
        "run_id": run_id,
        "started_at": iso(run_started_at),
        "cutoff_ts": cutoff_iso,
        "safety_buffer_minutes": SAFETY_BUFFER_MINUTES,
        "hubspot_eventos_run_type": run_type,
        "forced": args.force,
        "stages": {},
    }

    # --- Fase 1 / Etapa A: dashspy_ads + dashspy_hubspot em paralelo (coleta apenas) ---
    log.info("--- Fase 1 / Etapa A: coleta dashspy_ads e dashspy_hubspot (paralelo, sem envio ainda) ---")
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_ads = pool.submit(run_ads_stage)
        future_hubspot = pool.submit(run_hubspot_stage, cutoff_rts)
        ads_stage = future_ads.result()
        hubspot_stage = future_hubspot.result()

    run_report["stages"]["dashspy_ads"] = ads_stage
    run_report["stages"]["dashspy_hubspot"] = hubspot_stage

    # --- Fase 1 / Etapa B: pipeline de eventos HubSpot (sequencial) ---
    log.info("--- Fase 1 / Etapa B: hubspot_eventos -> consolidate_hubspot_forms -> consolidate_conversions ---")
    events_stage = run_events_stage(cutoff_iso, run_type)
    run_report["stages"]["hubspot_eventos"] = events_stage

    forms_stage = run_consolidate_forms_stage()
    run_report["stages"]["consolidate_hubspot_forms"] = forms_stage

    conversions_stage, conversions_build_result = run_consolidate_conversions_stage()
    run_report["stages"]["consolidate_conversions_forms_localsrc"] = conversions_stage

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
    )
    run_report["stages"]["supabase_send_all"] = send_stage

    run_finished_at = utc_now()
    run_report["finished_at"] = iso(run_finished_at)
    run_report["duration_seconds"] = (run_finished_at - run_started_at).total_seconds()

    # Binário de propósito: qualquer coisa que não seja 100% bloqueia a próxima execução (ver load_last_run/--force).
    run_report["overall_status"] = "success" if (upstream_clean and send_stage["status"] in ("success", "nothing_to_send")) else "failed"

    append_run_to_status_file(run_report)

    log.info("=" * 90)
    log.info("%s finalizado — overall_status=%s", run_id, run_report["overall_status"])
    if run_report["overall_status"] != "success":
        for line in summarize_pending(run_report):
            log.error("  pendente -> %s", line)
    log.info("Relatório: %s", STATUS_FILE)
    log.info("=" * 90)

    return 0 if run_report["overall_status"] == "success" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.warning("Execução interrompida pelo usuário.")
        sys.exit(130)
