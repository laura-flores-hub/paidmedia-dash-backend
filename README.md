# Objetivo
Pipeline em Python que coleta, de forma automatizada e periódica, dados de mídia paga (Meta Ads, Google Ads, LinkedIn Ads) e do CRM/eventos do HubSpot (Contacts, Deals, Forms, page views e interações de anúncio), consolida atribuição de conversões e centraliza tudo no Supabase (PostgreSQL). Das plataformas de mídia paga são extraídos os valores de investimento por campanha. Do HubSpot, dados de Contatos e Negócios ao longo do funil de vendas, mais os eventos brutos usados para atribuir cada submissão de formulário a um anúncio.

## Arquivos do projeto

| Arquivo | Papel |
|---|---|
| `main.py` | Orquestrador. Roda o pipeline completo fim a fim (ver fluxo abaixo). |
| `dashspy_ads.py` | Coleta Meta Ads, Google Ads e LinkedIn Ads. |
| `dashspy_hubspot.py` | Coleta HubSpot Contacts e Deals. |
| `hubspot_eventos_daily_historical_retry.py` | Extrai eventos brutos do HubSpot (forms, page views, interações de anúncio) via API de Events v3. |
| `consolidate_hubspot_forms.py` | 1ª consolidação: junta os três tipos de evento de formulário num registro por submissão. |
| `consolidate_conversions_forms_localsrc.py` | 2ª consolidação: cruza forms com page views/interações de anúncio e calcula atribuição final. |
| `supabase_event_uploader.py` | Módulo de envio genérico (usado pelo `main.py`/consolidação de forms) para `data_hs_ad_interactions_v2` e `data_hs_form_submissions_v2`. |

Cada um dos scripts de coleta (`dashspy_ads.py`, `dashspy_hubspot.py`, `hubspot_eventos_daily_historical_retry.py`) também funciona como CLI independente para depuração/reprocessamento manual — ver [Como executar](#como-executar).

## Fluxo do pipeline orquestrado (`main.py`)

### Fase 1 — Coleta/consolidação (nada é enviado ao Supabase ainda)

**Etapa A (paralelo):**
- `dashspy_ads` — Meta/Google/LinkedIn; corte = ontem, já embutido no script.
- `dashspy_hubspot` — Contacts + Deals; corte = `cutoff_ts` compartilhado da run.

**Etapa B (sequencial, mesmo `cutoff_ts` do HubSpot):**
- `hubspot_eventos_daily_historical_retry` (modo `daily` por padrão; `--historical` força modo histórico)
- `consolidate_hubspot_forms` (`--all-ready`)
- `consolidate_conversions_forms_localsrc` (build local, sem upload ainda)

Cada plataforma/fonte é coletada de forma independente — uma falha em uma não interrompe as demais. Ao final da fase, tudo que foi coletado com sucesso está salvo localmente (`outputs/`, `hubspot_eventos/`), garantindo que nenhum dado coletado seja perdido antes do envio.

### Fase 2 — Gate tudo-ou-nada

Só segue para o envio se **todas** as etapas da Fase 1 terminaram limpas (nenhuma unidade/etapa com erro). Se qualquer coisa falhou, **nada** é enviado ao Supabase nessa run — nem ads, nem HubSpot CRM, nem forms/interações/conversões.

### Fase 3 — Envio único ao Supabase (só roda se a Fase 2 aprovou)

- Meta/Google/LinkedIn Ads → `data_meta_v2` / `data_google_v2` / `data_linkedin_v2`
- HubSpot Contacts/Deals → `data_hs_contacts_v2` / `data_hs_deals_v2`
- Interações de anúncio brutas → `data_hs_ad_interactions_v2`
- Forms consolidados → `data_hs_form_submissions_v2`
- Conversões consolidadas (atribuição final) → `data_hs_forms_conversions_consolidated_v1`

### Retry automático (a nível de orquestrador)

Qualquer unidade/etapa cujo erro pareça transitório (timeout, conexão, rate limit, HTTP 429/500/502/503/504) é tentada de novo até 2 vezes extras (3 tentativas no total), com backoff entre tentativas. Erros que não batem com esse padrão falham na primeira tentativa.

### Trava de reexecução

No início, o `main.py` verifica a run mais recente em `status/main_orchestrator_status.json`. Se ela não terminou com `overall_status="success"` (sobrou qualquer coleta ou envio incompleto), a nova execução é recusada — é preciso resolver manualmente ou rodar com `--force`. Como esse relatório só é gravado no **final** de uma run completa, uma execução interrompida no meio (ex: `Ctrl+C`, ou o processo morto pelo sistema) não deixa registro nenhum — a trava simplesmente não vê essa run e a próxima execução roda normal, sem exigir `--force`.

### Robustez para volumes grandes / memória

A coleta incremental do HubSpot (Contacts/Deals) usa `lastmodifieddate`/`hs_lastmodifieddate` a partir da última coleta registrada no Supabase. Se uma run fica muito tempo sem rodar (ou é interrompida antes de enviar), a próxima janela incremental cresce proporcionalmente — em produção já vimos janelas de ~15 dias gerarem mais de 770 mil contatos modificados de uma vez.

Pra evitar que isso estoure a memória disponível:
- A paginação por janelas (`_collect_hubspot_windows_with_retry_point`) já divide qualquer janela que bata no limite de 10.000 resultados da Search API e salva um retry point em disco se uma janela falhar persistentemente (`hubspot-resume`/`deals-resume`).
- Depois da coleta, o processamento (conversão pro schema final + resolução de `has_valid_deal` via lookup de deals associados) roda em **lotes de 5.000** (`process_and_save_chunked`, ver `PROCESSING_CHUNK_SIZE` em `dashspy_hubspot.py`) em vez de tudo de uma vez — isso evita manter, ao mesmo tempo, a lista bruta inteira mais os dicionários de enriquecimento proporcionais ao volume total. Cada lote já é gravado no arquivo de saída antes do próximo começar.
- O reenvio manual de arquivos já coletados (`--retry`, em ambos `dashspy_ads.py` e `dashspy_hubspot.py`) lista os arquivos de `outputs/` pelo tamanho em disco, sem carregar nenhum deles na memória só para exibir o menu. Arquivos de HubSpot Contacts acima de 200 MB são enviados via streaming (`ijson`, lendo item a item) em vez de `json.loads()` do arquivo inteiro — necessário porque um único contact export já passou de 1.4 GB nesse pipeline.

## Como executar

```bash
python main.py
```
Roda o pipeline completo (Fases 1 a 3 acima), usando `daily` para `hubspot_eventos`.

```bash
python main.py --historical
```
Mesmo pipeline, mas roda `hubspot_eventos` em modo histórico (retroage em blocos de 3 meses) em vez de `daily`.

```bash
python main.py --force
```
Ignora a trava de reexecução (última run pendente/com erro) e roda mesmo assim.

### Scripts individuais (depuração / reprocessamento pontual)

```bash
python dashspy_ads.py [meta|meta-resume|google|linkedin|--retry]
python dashspy_hubspot.py [hubspot|deals|hubspot-all|hubspot-resume|deals-resume|--retry]
```
Roda o ciclo completo (coleta → confirmação → envio) para uma única fonte, sem passar pelo `main.py`. `meta` pergunta quais contas coletar antes de iniciar; `meta-resume` retoma a coleta de uma conta específica a partir do último `date_start` salvo. `hubspot-resume`/`deals-resume` retomam uma coleta que ficou com retry point pendente. `--retry` recarrega arquivos já salvos em `outputs/` e reenvia ao Supabase sem re-coletar nas APIs — pede confirmação por arquivo antes de enviar.

```bash
python hubspot_eventos_daily_historical_retry.py --run-type daily --cutoff-ts 2026-08-07T21:12:40Z
```
Roda a extração de eventos com um cutoff (`occurred_before`) específico, em vez de "agora" — útil para alinhar manualmente essa etapa com um cutoff que os outros scripts já usaram (ex: recuperando uma run que ficou incompleta). Sem `--cutoff-ts`, usa o instante atual. Sem `--run-type`, abre um menu interativo (`daily`/`historical`/`retry`).

```bash
python consolidate_hubspot_forms.py --all-ready
python consolidate_conversions_forms_localsrc.py
```
Consolidam localmente o que a extração de eventos já deixou pronto. O segundo pede confirmação interativa antes de enviar ao Supabase; falha com `PendingFormRunsError` se ainda houver runs de forms não consolidadas pelo primeiro.

## Variáveis de ambiente (`.env`)

| Variável | Usada por |
|---|---|
| `SUPABASE_URL`, `SUPABASE_KEY` | Todos os scripts que leem/escrevem no Supabase. |
| `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_IDS` | `dashspy_ads.py` (Meta). |
| `GOOGLE_ADS_YAML_PATH` | `dashspy_ads.py` (Google — credenciais reais ficam em `google-ads.yaml`). |
| `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_AD_ACCOUNT_IDS` | `dashspy_ads.py` (LinkedIn). |
| `HUBSPOT_TOKEN` | `dashspy_hubspot.py` e `hubspot_eventos_daily_historical_retry.py`. |
| `PATH_OUTPUTS_M` | Diretório de saída dos JSONs temporários de `dashspy_ads.py`/`dashspy_hubspot.py`. |
| `PATH_LOGS_M`, `PATH_LOGS_E`, `PATH_LOGS_F` | Diretórios de log (main/eventos/forms). |

## APIs
### API Meta
#### Autenticação:
- Token de acesso: `META_ACCESS_TOKEN`
- IDs das Contas de Anúncios: `META_AD_ACCOUNT_IDS` (separados por vírgula)

#### Endpoints:
##### URL base:
- https://graph.facebook.com/v25.0

##### URL Completa para retornar gastos em campanha:
- {urlbase}/{META_AD_ACCOUNT_ID}/insights?fields=campaign_id%2Ccampaign_name%2Cspend&level=campaign&time_increment=1&time_range=%7B'since'%3A'{data_inicial}'%2C'until'%3A'{data_final}'%7D{pagepathbase}

##### Onde:
- *data_inicial* = Data no formato aaaa-mm-dd
- *data_final* = Data no formato aaaa-mm-dd
- *pagepathbase* = &access_token={META_ACCESS_TOKEN}

#### Dados Retornados Esperados:
- *campaign_id*: ID único da campanha de anúncios
- *date_start*: datas de investimento de cada campanha de anúncios
- *campaign_name*: nomes das campanhas de anúncios
- *spend*: dados do investimento das campanhas de anúncios

#### Funcionamento esperado:
1. É esperado que a aplicação confirme, na tabela de gastos do Meta no Supabase, se a coluna de datas está vazia.
##### 1.1 Coluna vazia
Busca todos os gastos de todas as campanhas. Para isso, considera os valores e regras para respeitar as arquiteturas de segurança e limites da API:
- *data_inicial* = 2023-09-21
- *data_final* = data atual - 1
###### Paginação Obrigatória (`next`):
O script implementa um loop de paginação contínua. Ele lê o primeiro conjunto de dados e busca por uma chave `next` na resposta, fazendo requisições sequenciais para essa URL fornecida até que não existam mais páginas disponíveis.
###### Proteção Contra Rate Limiting e Erros de Conexão:
A extração é massiva, o que acionará as travas de volume da Meta. O script possui blocos `try/except` desenhados para capturar os seguintes erros de limite (throttling) e pausar 60s antes de tentar novamente (até 5×):
- **Código 1:** Unknown (tratado como transitório)
- **Código 4:** API Too Many Calls
- **Código 17:** API User Too Many Calls
- **Código 341:** Application limit reached

Erros de conexão (`ConnectionError`) e timeout (`ReadTimeout`) também são retentados com a mesma lógica.
###### Recuperação de erros mid-coleta:
Os dados são coletados em janelas anuais. Se um erro ocorrer durante a paginação de uma janela (incluindo cursor inválido `#2642`), o script salva imediatamente os registros das janelas já concluídas em `outputs/meta_<account_id>_<timestamp>.json` e encerra a coleta daquela conta. Use `python dashspy_ads.py meta-resume` para retomar a partir da última data disponível.
###### Batch Requests:
Ele respeita o limite rígido de 50 requisições por lote.

2. A coluna de datas não estando vazia, o script busca pela última data informada na tabela e faz a requisição com a seguinte estrutura:
- *data_inicial* = última data informada na tabela de gastos + 1
- *data_final* = data atual - 1

### API Google
#### Autenticação:
As credenciais estão discriminadas no arquivo `google-ads.yaml` (o caminho pode ser configurado via variável de ambiente `GOOGLE_ADS_YAML_PATH`; padrão: `google-ads.yaml`) nas seguintes variáveis:
- Developer Token: `developer_token`
- Client ID: `client_id`
- Client Secret: `client_secret`
- Refresh Token: `refresh_token`
- Customer ID: `login_customer_id`

#### Método de consulta:
A aquisição de dados utiliza a biblioteca oficial *google-ads* para Python, por meio do serviço *GoogleAdsService* com o método *search_stream*. As consultas são feitas em GAQL (Google Ads Query Language), estrutura similar ao SQL.

##### Query utilizada:
SELECT<br>
 campaign.id,<br>
 campaign.name,<br>
 segments.date,<br>
 metrics.cost_micros<br>
FROM campaign<br>
WHERE segments.date BETWEEN '{data_inicial}' AND '{data_final}'<br>
ORDER BY segments.date DESC

##### Onde:
- *data_inicial*: Data no formato aaaa-mm-dd
- *data_final*: Data no formato aaaa-mm-dd

##### Valores e suas respectivas variáveis:
- *ID da Campanha*: campaign.id
- *Data*: segments.date
- *Nome da Campanha*: campaign.name
- *Gasto*: metrics.cost_micros

#### Dados Capturados:
- *campaign.id*: ID único da campanha de anúncios
- *segments.date*: datas de investimento de cada campanha de anúncios
- *campaign.name*: nomes das campanhas de anúncios
- *metrics.cost_micros*: dados do investimento das campanhas de anúncios, em micros (1 unidade = R$ 0,000001) - convertido para reais dividindo por 1.000.000

#### Funcionamento esperado:
1. É esperado que a aplicação confirme, na tabela de gastos do Google no Supabase, se a coluna de datas está vazia.
##### 1.1 Se Coluna vazia
Busca todos os gastos de todas as campanhas. Para isso, considera os valores:
- *data_inicial* = 2021-11-22
- *data_final* = data atual - 1

2. A coluna de datas não estando vazia, o script busca pela última data informada na tabela e faz a requisição com a seguinte estrutura:
- *data_inicial* = última data informada na tabela de gastos + 1
- *data_final* = data atual - 1

### API LinkedIn Ads
#### Autenticação:
- Token de acesso: `LINKEDIN_ACCESS_TOKEN`
- IDs das Contas de Anúncios: `LINKEDIN_AD_ACCOUNT_IDS` (separados por vírgula)

#### Método de consulta:
A aquisição de dados utiliza chamadas diretas à API REST do LinkedIn via `subprocess curl`, com os cabeçalhos `Linkedin-Version` e `X-Restli-Protocol-Version` obrigatórios. A resposta JSON é processada diretamente em Python.

##### Endpoint utilizado:
- https://api.linkedin.com/rest/adAnalytics

##### Parâmetros:
- `q=analytics`
- `pivot=CAMPAIGN`
- `timeGranularity=DAILY`
- `accounts=List(urn:li:sponsoredAccount:{account_id})`
- `dateRange=(start:(...),end:(...))`
- `fields=dateRange,costInLocalCurrency,pivotValues`

#### Dados Capturados:
- *dateRange.start*: data de início do investimento
- *costInLocalCurrency*: valor investido na campanha
- *pivotValues*: URN da campanha (convertido para nome via endpoint `/adCampaignsV2/{id}`)
- *campaign_id*: ID numérico da campanha, extraído do URN em `pivotValues` (ex.: `urn:li:sponsoredCampaign:12345` → `12345`)

#### Funcionamento esperado:
1. Verifica a última data registrada na tabela do LinkedIn no Supabase.
2. Se vazia, inicia carga histórica desde 2023-09-01.
3. A coleta é feita em janelas diárias.
4. Inclui proteção contra rate limiting (HTTP 429) com backoff e retentativas.

### API HubSpot (CRM — Contacts/Deals)
#### Autenticação:
- Token de acesso: `HUBSPOT_TOKEN`

#### Método de consulta:
Utiliza a HubSpot Search API (v3), com filtros por período em janelas diárias (subdivididas automaticamente se uma janela bater no limite de 10.000 resultados por query). Todas as requisições usam POST com payload JSON.

##### Endpoint base:
- https://api.hubapi.com/crm/v3/objects

#### Modo de coleta:
- **FULL** (tabela vazia no Supabase): busca tudo desde `2025-08-01` filtrando por `createdate`.
- **INCREMENTAL** (tabela já tem dados): busca tudo criado **e** modificado desde a última coleta registrada (`dt_h_recording_data` mais recente na tabela), filtrando por `lastmodifieddate` (Contacts) ou `hs_lastmodifieddate` (Deals). O corte superior é fixo no início da run (`recording_ts`) — eventos novos durante a execução ficam pra próxima.

Se uma janela falhar persistentemente, um retry point é salvo em disco e a próxima tentativa não reprocessa do zero (`hubspot-resume`/`deals-resume`).

#### HubSpot Contacts

##### Endpoint:
- `/contacts/search` — POST

##### Dados Capturados:
- *hs_object_id*, *createdate*, *lastmodifieddate*
- *firstname*, *lastname*, *email*, *phone*, *company*
- *lifecyclestage*, *hs_lead_status*
- *hubspot_owner_id*, *num_associated_deals*
- *hs_analytics_source*, *hs_analytics_last_touch_converting_campaign*
- *numemployees*, *jobtitle*
- *not_qualified_reason*, *estado_de_lead*
- *hs_object_source_detail_1*, *hs_analytics_source_data_1*, *hs_analytics_source_data_2*
- *stage_of_the_deal*, *motivo_no_interesado*, *conversion_de_lead*
- *hubspot_team_id*, *form_submitted*, *country*, *region*, *main_country*
- *has_valid_deal*: booleano calculado — `True` se o contato não possui deals ou possui ao menos um deal fora dos pipelines excluídos (Business Partner, BDRs, Partnerships). Calculado buscando as associações contact→deals e o `pipeline` de cada deal via Associations/Deals Batch API (v4), em lotes de 100.

#### HubSpot Deals

##### Endpoint:
- `/deals/search` — POST

##### Dados Capturados:
- *hs_object_id*, *dealname*, *amount*
- *createdate*, *closedate*, *lastmodifieddate*
- *dealstage*, *pipeline* (mapeados para nomes legíveis)
- *hubspot_owner_id*, *ae_deal_won*, *ae_squad*
- *first_meeting_status*, *deal_source*, *pais*
- *contact_ids*: lista de IDs de contatos associados ao deal (via Associations Batch API v4)

### API HubSpot (Events v3 — forms, page views, interações de anúncio)
Usada por `hubspot_eventos_daily_historical_retry.py`, desacoplada da API de CRM acima — mesma autenticação (`HUBSPOT_TOKEN`), endpoint e paginação diferentes (paginação por cursor `paging.next.after`, não por janela de 10.000).

#### Tipos de evento extraídos:
- `e_submitted_form` — página/URL de origem da submissão
- `e_form_submission_v2` — evento base de submissão (contact_id, form_id, timestamp)
- `e_form_submission_metadata_v2` — título do form, lifecycle stage no momento da submissão
- `e_ad_interaction` — interações com anúncios (clique, utms, campanha)
- `e_visited_page` — page views

#### Funcionamento esperado:
1. Modo `daily`: janela = `daily_next_after` (cursor salvo em `hubspot_eventos/estado_extracao_eventos.json`) até `occurred_before` (agora, ou um valor fixo via `--cutoff-ts`).
2. Modo `historical`: retroage a partir de `historical_next_before` em blocos de 3 meses.
3. Cada run gera um manifesto (`hubspot_eventos/_runs/<run_id>.json`) com status por tipo de evento — permite retry seletivo (`--run-type retry`) sem repetir tipos que já deram certo.
4. `consolidate_hubspot_forms.py` junta os três eventos de formulário (por contact_id + `hs_form_id` numa janela de tempo configurável) em um registro por submissão.
5. `consolidate_conversions_forms_localsrc.py` cruza esses forms consolidados com `e_ad_interaction`/`e_visited_page` (evento mais recente antes da submissão, dentro de 15 minutos), filtra só forms classificados como TOFU/MOFU/BOFU (tabela `validation_funnel_forms_kws_v2` no Supabase) e grava a atribuição final.

## Supabase
### Tabelas:

- `data_meta_v2`

Field name | Type
-- | --
date_start | DATE
campaign_id | STRING
campaign_name | STRING
cost | FLOAT
ad_account_id | STRING
dt_h_recording_data | TIMESTAMP

- `data_google_v2`

Field name | Type
-- | --
campaign_id | STRING
campaign_name | STRING
spend | FLOAT
date | DATE
ad_account_id | STRING
dt_h_recording_data | TIMESTAMP

- `data_linkedin_v2`

Field name | Type
-- | --
date_start | DATE
campaign_id | STRING
campaign_name | STRING
cost | FLOAT
ad_account_id | STRING
dt_h_recording_data | TIMESTAMP

- `data_hs_contacts_v2` — upsert por `hs_object_id`

Field name | Type
-- | --
dt_h_recording_data | TIMESTAMP
hs_object_id | STRING
createdate | TIMESTAMP
lastmodifieddate | TIMESTAMP
firstname | STRING
lastname | STRING
email | STRING
phone | STRING
company | STRING
lifecyclestage | STRING
hs_lead_status | STRING
hubspot_owner_id | STRING
num_associated_deals | INTEGER
hs_analytics_source | STRING
hs_analytics_last_touch_converting_campaign | STRING
numemployees | STRING
jobtitle | STRING
not_qualified_reason | STRING
estado_de_lead | STRING
hs_object_source_detail_1 | STRING
hs_analytics_source_data_1 | STRING
hs_analytics_source_data_2 | STRING
stage_of_the_deal | STRING
motivo_no_interesado | STRING
conversion_de_lead | STRING
hubspot_team_id | STRING
form_submitted | STRING
country | STRING
region | STRING
main_country | STRING
has_valid_deal | BOOLEAN

- `data_hs_deals_v2` — upsert por `hs_object_id`

Field name | Type
-- | --
dt_h_recording_data | TIMESTAMP
hs_object_id | STRING
dealname | STRING
amount | FLOAT
createdate | TIMESTAMP
closedate | TIMESTAMP
lastmodifieddate | TIMESTAMP
dealstage | STRING
pipeline | STRING
hubspot_owner_id | STRING
ae_deal_won | STRING
ae_squad | STRING
first_meeting_status | STRING
deal_source | STRING
pais | STRING
contact_ids | ARRAY

- `data_hs_ad_interactions_v2` — eventos brutos `e_ad_interaction`, upsert por `event_id`, populada por `supabase_event_uploader.py` (ignora duplicatas, não atualiza linhas existentes).

- `data_hs_form_submissions_v2` — forms consolidados (saída de `consolidate_hubspot_forms.py`), upsert por `(contact_id, submitted_at)`, populada por `supabase_event_uploader.py`.

- `data_hs_forms_conversions_consolidated_v1` — atribuição final de conversão (saída de `consolidate_conversions_forms_localsrc.py`), upsert por `(contact_id, submitted_at)`. Contém campos prefixados `forms_*` (dados do formulário: título, URL, UTMs, referrer, `hsa_*`) e `ads_*` (dados do anúncio casado: campanha, adgroup, network, utms), mais `form_id`, `final_has_ad_attribution`.

- `validation_funnel_forms_kws_v2` — tabela de referência mantida manualmente (`form_id`, `selected_funnel_stage`). Só formulários listados aqui com estágio `tofu`/`mofu`/`bofu` entram na consolidação de conversões — qualquer form_id ausente ou sem estágio válido é excluído do funil, mesmo que a submissão tenha sido coletada normalmente.