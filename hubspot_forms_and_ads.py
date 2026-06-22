#!/usr/bin/env python3
"""
Extrai, por contato, FORMS submetidos + AD INTERACTIONS + PAGE VIEWS
via Events API v3 do HubSpot.

Lógica:
    - Fonte principal: form submissions (merge de 3 event types relacionados)
    - Fallback 1: e_ad_interaction (campaign/adset/ad com IDs e nomes)
    - Fallback 2: e_visited_page (URL pode revelar origem via hsa_* ou utm)

Uso:
    pip install requests python-dotenv
    # .env precisa ter: HUBSPOT_TOKEN=pat-na1-xxxx

    # Análises do dia a dia:
    python hubspot_forms_and_ads.py --last-1h        # última hora
    python hubspot_forms_and_ads.py --last-24h       # últimas 24 horas

    # Janelas customizadas:
    python hubspot_forms_and_ads.py --hours 6        # últimas 6 horas
    python hubspot_forms_and_ads.py --months 1       # último mês

    # Default (3 meses, tudo):
    python hubspot_forms_and_ads.py

Saída: 3 CSVs
    - forms_per_contact.csv
    - ad_interactions_fallback.csv
    - page_views_fallback.csv
"""

import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.hubapi.com"

# event types que juntos descrevem 1 submissão de form
FORM_EVENT_TYPES = {
    "e_submitted_form",
    "e_form_submission_v2",
    "e_form_submission_metadata_v2",
}
AD_EVENT_TYPE = "e_ad_interaction"
PAGE_VIEW_EVENT_TYPE = "e_visited_page"


# ----------------------------------------------------------------------
def get_headers():
    token = os.environ.get("HUBSPOT_TOKEN")
    if not token:
        sys.exit("ERRO: HUBSPOT_TOKEN não encontrado no .env")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ----------------------------------------------------------------------
def search_converted_contacts(headers, after_dt, window_label,
                              max_contacts=0, filter_marketing=True):
    """
    Pagina por todos os contatos que tiveram conversão (form submission)
    a partir de after_dt. Usa recent_conversion_date como filtro temporal,
    o que pega tanto leads novos quanto reengajamentos.
    """
    print(f"\n→ Buscando contatos com conversão {window_label}...")
    if filter_marketing:
        print(f"  ↳ filtro: hs_latest_source != OFFLINE")
    if max_contacts:
        print(f"  ↳ teto de {max_contacts} contatos (use --limit 0 pra ilimitado)")

    after_ms = int(after_dt.timestamp() * 1000)

    filters = [{
        "propertyName": "recent_conversion_date",
        "operator": "GTE",
        "value": str(after_ms),
    }]
    if filter_marketing:
        filters.append({
            "propertyName": "hs_latest_source",
            "operator": "NEQ",
            "value": "OFFLINE",
        })

    all_contacts = []
    after = None
    page = 0

    while True:
        page += 1
        body = {
            "filterGroups": [{"filters": filters}],
            "sorts": [{"propertyName": "recent_conversion_date", "direction": "DESCENDING"}],
            "properties": ["email", "firstname", "createdate",
                           "hs_analytics_source", "hs_latest_source",
                           "recent_conversion_date"],
            "limit": 100,
        }
        if after:
            body["after"] = after

        r = requests.post(f"{BASE_URL}/crm/v3/objects/contacts/search",
                          headers=headers, json=body)
        if r.status_code != 200:
            sys.exit(f"Erro buscando contatos (página {page}): "
                     f"{r.status_code} {r.text[:300]}")

        data = r.json()
        page_contacts = data.get("results", [])
        all_contacts.extend(page_contacts)
        after = data.get("paging", {}).get("next", {}).get("after")

        print(f"  página {page:>3}: +{len(page_contacts):>3}  "
              f"(total acumulado: {len(all_contacts)})")

        if not after or not page_contacts:
            break
        if max_contacts and len(all_contacts) >= max_contacts:
            all_contacts = all_contacts[:max_contacts]
            print(f"  ↳ atingido teto de {max_contacts}, parando paginação")
            break
        time.sleep(0.1)

    print(f"  ✓ Total: {len(all_contacts)} contatos")
    return all_contacts


def fetch_contact_events(headers, contact_id, after_dt, before_dt):
    """
    Pagina TODOS os eventos do contato no intervalo [after_dt, before_dt].
    before_dt é fixado no início do run pra garantir snapshot consistente:
    todos os contatos têm o mesmo cutoff temporal, independente da hora
    em que cada um foi processado.
    """
    after_iso = after_dt.isoformat()
    before_iso = before_dt.isoformat()
    all_events = []
    after = None

    while True:
        params = {
            "objectType": "contact",
            "objectId": contact_id,
            "occurredAfter": after_iso,
            "occurredBefore": before_iso,
            "limit": 100,
        }
        if after:
            params["after"] = after

        r = requests.get(f"{BASE_URL}/events/v3/events", headers=headers, params=params)
        if r.status_code != 200:
            print(f"   [contact {contact_id}] erro {r.status_code}")
            return all_events

        data = r.json()
        all_events.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.05)

    return all_events


# ----------------------------------------------------------------------
def parse_ad_attribution_from_query_params(qs):
    """Extrai hsa_acc/cam/grp/ad/src do hs_query_params."""
    if not qs:
        return {}
    parsed = parse_qs(qs)
    return {
        "hsa_acc": (parsed.get("hsa_acc") or [None])[0],
        "hsa_cam": (parsed.get("hsa_cam") or [None])[0],
        "hsa_grp": (parsed.get("hsa_grp") or [None])[0],
        "hsa_ad":  (parsed.get("hsa_ad")  or [None])[0],
        "hsa_src": (parsed.get("hsa_src") or [None])[0],
    }


def round_to_minute(iso_ts):
    """Arredonda timestamp ISO ao minuto pra agrupar events relacionados."""
    if not iso_ts:
        return ""
    return iso_ts[:16]  # "2026-06-12T02:38"


def consolidate_form_submissions(events, contact_email, contact_id, extracted_at):
    """
    Junta os 3 form events relacionados (mesmo form_id + mesmo minuto) em
    uma única linha por submissão.
    """
    groups = defaultdict(dict)

    for ev in events:
        et = ev.get("eventType")
        if et not in FORM_EVENT_TYPES:
            continue

        props = ev.get("properties") or {}
        # Agrupa por minuto (não por form_id, porque e_form_submission_metadata_v2
        # não traz hs_form_id — só timestamp e form_title).
        # Como já estamos no escopo de UM contato, minuto é chave suficiente.
        minute = round_to_minute(ev.get("occurredAt"))
        key = minute

        # form_id vem do e_submitted_form ou e_form_submission_v2 (não do metadata)
        if props.get("hs_form_id") and not groups[key].get("form_id"):
            groups[key]["form_id"] = props["hs_form_id"]

        # campos base
        if "submitted_at" not in groups[key] or ev.get("occurredAt") < groups[key]["submitted_at"]:
            groups[key]["submitted_at"] = ev.get("occurredAt")
        groups[key]["contact_email"] = contact_email
        groups[key]["contact_id"] = contact_id

        # campos comuns (qualquer um dos 3 events pode ter)
        for field in ["hs_form_type", "hs_page_title", "hs_referrer",
                      "hs_utm_source", "hs_utm_campaign", "hs_utm_medium",
                      "hs_visitor_type"]:
            if props.get(field) and not groups[key].get(field):
                groups[key][field] = props[field]

        # campos específicos por event type
        if et == "e_submitted_form":
            groups[key]["page_url"] = props.get("hs_url") or groups[key].get("page_url")
            groups[key]["title"] = props.get("hs_title") or groups[key].get("title")

        elif et == "e_form_submission_v2":
            groups[key]["base_url"] = props.get("hs_base_url")
            # parse do query_params pra pegar hsa_*
            ad_attr = parse_ad_attribution_from_query_params(props.get("hs_query_params"))
            for k, v in ad_attr.items():
                if v:
                    groups[key][k] = v

        elif et == "e_form_submission_metadata_v2":
            groups[key]["form_title"] = props.get("hs_form_title")
            groups[key]["lifecyclestage"] = props.get("hs_contact_lifecyclestage")

    submissions = []
    for key, data in groups.items():
        data["has_ad_attribution"] = bool(data.get("hsa_cam"))
        data["extracted_at"] = extracted_at
        submissions.append(data)
    return submissions


def extract_ad_interactions(events, contact_email, contact_id, extracted_at):
    """Extrai e_ad_interaction como linhas planas."""
    interactions = []
    for ev in events:
        if ev.get("eventType") != AD_EVENT_TYPE:
            continue
        p = ev.get("properties") or {}
        interactions.append({
            "event_id": ev.get("id"),
            "contact_email": contact_email,
            "contact_id": contact_id,
            "occurred_at": ev.get("occurredAt"),
            "network": p.get("hs_ad_network"),
            "interaction_type": p.get("hs_interaction_type"),
            "campaign_id": p.get("hs_ad_campaign_id"),
            "campaign_name": p.get("hs_ad_campaign_name"),
            "adgroup_id": p.get("hs_ad_group_id"),
            "adgroup_name": p.get("hs_ad_group_name"),
            "ad_id": p.get("hs_ad_id"),
            "ad_name": p.get("hs_ad_name"),
            "ad_account_id": p.get("hs_ad_account_id"),
            "utm_source": p.get("hs_utm_source"),
            "utm_campaign": p.get("hs_utm_campaign"),
            "utm_medium": p.get("hs_utm_medium"),
            "extracted_at": extracted_at,
        })
    return interactions


def extract_page_views(events, contact_email, contact_id, extracted_at):
    """
    Extrai e_visited_page como linhas planas.
    Fallback do fallback: quando o contato não tem form nem ad_interaction,
    a URL da página visitada (e seus query_params/UTMs) pode revelar a origem.
    """
    views = []
    for ev in events:
        if ev.get("eventType") != PAGE_VIEW_EVENT_TYPE:
            continue
        p = ev.get("properties") or {}
        ad_attr = parse_ad_attribution_from_query_params(p.get("hs_query_params"))
        views.append({
            "event_id": ev.get("id"),
            "contact_email": contact_email,
            "contact_id": contact_id,
            "viewed_at": ev.get("occurredAt"),
            "page_url": p.get("hs_url"),
            "page_title": p.get("hs_title"),
            "referrer": p.get("hs_referrer"),
            "session_source": p.get("hs_session_source"),
            "utm_source": p.get("hs_utm_source"),
            "utm_campaign": p.get("hs_utm_campaign"),
            "utm_medium": p.get("hs_utm_medium"),
            "hsa_acc": ad_attr.get("hsa_acc"),
            "hsa_cam": ad_attr.get("hsa_cam"),
            "hsa_grp": ad_attr.get("hsa_grp"),
            "hsa_ad":  ad_attr.get("hsa_ad"),
            "has_ad_attribution": bool(ad_attr.get("hsa_cam")),
            "extracted_at": extracted_at,
        })
    return views


# ----------------------------------------------------------------------
def write_csv(rows, path):
    if not rows:
        print(f"  ⚠ Nada pra salvar em {path}")
        return
    keys = sorted({k for r in rows for k in r.keys()})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    print(f"  ✓ {path}  ({len(rows)} linhas, {len(keys)} colunas)")


# ----------------------------------------------------------------------
def resolve_window(args):
    """
    Decide a janela temporal e devolve (after_dt, label_humano).
    Prioridade: --last-1h > --last-24h > --hours > --months.
    """
    now = datetime.now(timezone.utc)
    if args.last_1h:
        return now - timedelta(hours=1), "na última hora"
    if args.last_24h:
        return now - timedelta(hours=24), "nas últimas 24h"
    if args.hours:
        return now - timedelta(hours=args.hours), f"nas últimas {args.hours}h"
    return now - timedelta(days=args.months * 30), f"nos últimos {args.months} meses"


def main():
    p = argparse.ArgumentParser()
    # janela temporal — flags em ordem de prioridade
    p.add_argument("--last-1h", action="store_true",
                   help="atalho: última 1 hora (análise quase-real-time)")
    p.add_argument("--last-24h", action="store_true",
                   help="atalho: últimas 24 horas (revisão diária)")
    p.add_argument("--hours", type=int, default=0,
                   help="janela em horas (sobrescreve --months se > 0)")
    p.add_argument("--months", type=int, default=3,
                   help="janela em meses (default: 3)")
    # outros
    p.add_argument("--limit", type=int, default=0,
                   help="teto de contatos a processar (0 = ilimitado, default: 0)")
    p.add_argument("--all-contacts", action="store_true",
                   help="desliga o filtro de hs_latest_source != OFFLINE")
    p.add_argument("--forms-output", type=str, default="forms_per_contact.csv")
    p.add_argument("--ads-output", type=str, default="ad_interactions_fallback.csv")
    p.add_argument("--pages-output", type=str, default="page_views_fallback.csv")
    args = p.parse_args()

    headers = get_headers()
    after_dt, window_label = resolve_window(args)
    # before_dt = cutoff superior fixo (garante snapshot consistente entre contatos)
    # também é o valor canônico de extracted_at — todos os registros do mesmo
    # run carregam exatamente esse timestamp.
    before_dt = datetime.now(timezone.utc)
    extracted_at = before_dt.isoformat()

    contacts = search_converted_contacts(
        headers, after_dt, window_label, args.limit,
        filter_marketing=not args.all_contacts,
    )

    if not contacts:
        sys.exit("Nenhum contato encontrado.")

    # estimativa de tempo (assumindo ~0.4s por contato com paginação leve)
    est_min = (len(contacts) * 0.4) / 60
    print(f"\n→ Puxando timeline de {len(contacts)} contatos "
          f"(estimativa: ~{est_min:.1f} min)...")
    verbose = len(contacts) < 50  # printa todos só se for amostra pequena

    all_forms = []
    all_ads = []
    all_pages = []
    stats = {"with_form": 0, "with_ad": 0, "with_page_only": 0,
             "with_both": 0, "with_neither": 0,
             "forms_with_ad_attr": 0, "forms_without_ad_attr": 0,
             "pages_with_ad_attr": 0}

    for i, contact in enumerate(contacts, 1):
        cid = contact["id"]
        email = contact["properties"].get("email") or "(sem email)"
        latest_source = contact["properties"].get("hs_latest_source") or "(vazio)"
        events = fetch_contact_events(headers, cid, after_dt, before_dt)

        forms = consolidate_form_submissions(events, email, cid, extracted_at)
        ads = extract_ad_interactions(events, email, cid, extracted_at)
        pages = extract_page_views(events, email, cid, extracted_at)

        all_forms.extend(forms)
        all_ads.extend(ads)
        all_pages.extend(pages)

        if forms and ads:   stats["with_both"] += 1
        elif forms:         stats["with_form"] += 1
        elif ads:           stats["with_ad"] += 1
        elif pages:         stats["with_page_only"] += 1
        else:               stats["with_neither"] += 1
        for f in forms:
            if f.get("has_ad_attribution"):
                stats["forms_with_ad_attr"] += 1
            else:
                stats["forms_without_ad_attr"] += 1
        for pv in pages:
            if pv.get("has_ad_attribution"):
                stats["pages_with_ad_attr"] += 1

        if verbose or i % 25 == 0 or i == len(contacts):
            print(f"  [{i:>4}/{len(contacts)}] {email[:36]:<36} "
                  f"[{latest_source[:14]:<14}] → "
                  f"{len(forms)}F {len(ads)}A {len(pages)}P")
        time.sleep(0.1)

    # ------------------------------------------------------------------
    print(f"\n{'=' * 60}")
    print("RESUMO")
    print(f"{'=' * 60}")
    print(f"  Contatos processados:             {len(contacts)}")
    print(f"  ↳ com forms:                      {stats['with_form']}")
    print(f"  ↳ com ad_interaction:             {stats['with_ad']}  (fallback 1)")
    print(f"  ↳ só com page views:              {stats['with_page_only']}  (fallback 2)")
    print(f"  ↳ com forms + ads:                {stats['with_both']}")
    print(f"  ↳ sem nenhum:                     {stats['with_neither']}")
    print(f"")
    print(f"  Total de form submissions:        {len(all_forms)}")
    print(f"    ↳ com hsa_* (vieram de ad):     {stats['forms_with_ad_attr']}")
    print(f"    ↳ sem hsa_* (orgânico/LP):      {stats['forms_without_ad_attr']}")
    print(f"  Total de ad_interactions:         {len(all_ads)}")
    print(f"  Total de page views:              {len(all_pages)}")
    print(f"    ↳ com hsa_* na URL:             {stats['pages_with_ad_attr']}")

    print(f"\n→ Salvando CSVs...")
    write_csv(all_forms, args.forms_output)
    write_csv(all_ads, args.ads_output)
    write_csv(all_pages, args.pages_output)


if __name__ == "__main__":
    main()