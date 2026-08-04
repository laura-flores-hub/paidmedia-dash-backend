#!/usr/bin/env python3

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client


load_dotenv()

TARGET_TABLE = "data_hs_forms_conversions_consolidated_v1"
BATCH_SIZE = 500

# Coloque aqui o caminho do JSONL já gerado.
JSONL_FILE = Path(
    "outputs/consolidate_conversions/conversions_consolidated_20260717_195944.jsonl"
)


def get_supabase():
    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
    )

    if not url or not key:
        raise RuntimeError("Credenciais do Supabase não encontradas no .env.")

    return create_client(url, key)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {path}")

    rows = []

    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            line = line.strip()

            if not line:
                continue

            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"JSON inválido na linha {line_number}: {exc}"
                ) from exc

    return rows


def main():
    rows = read_jsonl(JSONL_FILE)

    print(f"Arquivo: {JSONL_FILE}")
    print(f"Linhas encontradas: {len(rows)}")
    print(f"Tabela de destino: {TARGET_TABLE}")

    answer = input("\nEnviar para o Supabase? [s/N]: ").strip().lower()

    if answer not in {"s", "sim", "y", "yes"}:
        print("Envio cancelado.")
        return

    sb = get_supabase()
    total_batches = (len(rows) + BATCH_SIZE - 1) // BATCH_SIZE
    uploaded = 0

    for batch_number, start in enumerate(
        range(0, len(rows), BATCH_SIZE),
        start=1,
    ):
        batch = rows[start:start + BATCH_SIZE]

        print(
            f"Enviando lote {batch_number}/{total_batches} "
            f"com {len(batch)} linhas..."
        )

        (
            sb.table(TARGET_TABLE)
            .upsert(
                batch,
                on_conflict="contact_id,submitted_at",
            )
            .execute()
        )

        uploaded += len(batch)
        print(f"Total enviado: {uploaded}/{len(rows)}")

    print(f"\nConcluído: {uploaded} linhas enviadas.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nEnvio interrompido.")
        sys.exit(130)
    except Exception as exc:
        print(f"\nErro no envio: {exc}")
        sys.exit(1)