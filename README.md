# Objetivo
Código em Python que coleta, de forma automatizada e periódica, informações de cinco fontes externas — Meta Ads, Google Ads, LinkedIn Ads, HubSpot Contacts e HubSpot Deals — e as centraliza no Supabase (PostgreSQL). Das plataformas de mídia paga (Meta Ads, Google Ads e LinkedIn Ads), são extraídos os valores de investimento por campanha. Do HubSpot, são extraídos dados de Contatos e Negócios ao longo do funil de vendas.

## APIs 
### API Meta
#### Autenticação:
As credenciais estão discriminadas no arquivo .env nas seguintes variáveis:
- Token de acesso: `META_ACCESS_TOKEN`
- IDs das Contas de Anúncios: `META_AD_ACCOUNT_IDS` (separados por vírgula)

#### Endpoints: 
##### URL base:
- https://graph.facebook.com/v25.0

##### URL Completa para retornar gastos em campanha:
- {urlbase}/{META_AD_ACCOUNT_ID}/insights?fields=campaign_name%2Cspend&level=campaign&time_increment=1&time_range=%7B'since'%3A'{data_inicial}'%2C'until'%3A'{data_final}'%7D{pagepathbase}

##### Onde:
- *data_inicial* = Data no formato aaaa-mm-dd
- *data_final* = Data no formato aaaa-mm-dd
- *pagepathbase* = &access_token={META_ACCESS_TOKEN}

#### Dados Retornados Esperados:
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
###### Proteção Contra Rate Limiting:
A extração é massiva, o que acionará as travas de volume da Meta. O script possui blocos `try/except` desenhados para capturar os seguintes erros de limite (throttling) e pausar a execução temporariamente antes de tentar novamente:
- **Código 4:** API Too Many Calls
- **Código 17:** API User Too Many Calls
- **Código 341:** Application limit reached
###### Batch Requests:
Ele respeita o limite rígido de 50 requisições por lote.

2. A coluna de datas não estando vazia, o script busca pela última data informada na tabela e faz a requisição com a seguinte estrutura:
- *data_inicial* = última data informada na tabela de gastos + 1
- *data_final* = data atual - 1

### API Google
#### Autenticação:
As credenciais estão discriminadas no arquivo google-ads.yaml nas seguintes variáveis:
- Developer Token: `developer_token`
- Client ID: `client_id`
- Client Secret: `client_secret`
- Refresh Token: `refresh_token`
- Customer ID: `login_customer_id`

#### Método de consulta:
A aquisição de dados utiliza a biblioteca oficial *google-ads* para Python, por meio do serviço *GoogleAdsService* com o método *search_stream*. As consultas são feitas em GAQL (Google Ads Query Language), estrutura similar ao SQL.

##### Query utilizada:
SELECT<br>
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
- *Data*: segments.date
- *Nome da Campanha*: campaign.name
- *Gasto*: metrics.cost_micros

#### Dados Capturados:
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
As credenciais estão discriminadas no arquivo .env nas seguintes variáveis:
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

#### Funcionamento esperado:
1. Verifica a última data registrada na tabela do LinkedIn no Supabase.
2. Se vazia, inicia carga histórica desde 2023-09-01.
3. A coleta é feita em janelas trimestrais para evitar timeouts.
4. Inclui proteção contra rate limiting (HTTP 429) com backoff e até 5 tentativas.

### API HubSpot
#### Autenticação:
A credencial está discriminada no .env na seguinte variável:
- Token de acesso: `TOKEN_ACESSO_HUBSPOT`

#### Método de consulta:
Utiliza a HubSpot Search API (v3) com filtros por `createdate` em janelas diárias, para contornar o limite de 10.000 resultados por query. Todas as requisições usam POST com payload JSON.

##### Endpoint base:
- https://api.hubapi.com/crm/v3/objects

#### HubSpot Contacts

##### Endpoint:
- `/contacts/search` — POST

##### Filtro de período:
Contacts criados a partir da última data registrada no Supabase (com lookback de 45 dias), paginados em janelas de 1 dia.

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
- *hubspot_team_id*, *form_submitted*, *country*, *region*
- *has_valid_deal*: booleano calculado — `True` se o contato não possui deals ou possui ao menos um deal fora dos pipelines excluídos (Business Partner, BDRs, Partnerships)

#### HubSpot Deals

##### Endpoint:
- `/deals/search` — POST

##### Filtro de período:
Deals criados a partir da última data registrada no Supabase, paginados em janelas de 1 dia.

##### Dados Capturados:
- *hs_object_id*, *dealname*, *amount*
- *createdate*, *closedate*, *lastmodifieddate*
- *dealstage*, *pipeline* (mapeados para nomes legíveis)
- *hubspot_owner_id*, *ae_deal_won*, *ae_squad*
- *first_meeting_status*, *deal_source*, *pais*
- *contact_ids*: lista de IDs de contatos associados ao deal (via Associations Batch API v4)

## Supabase
### Tabelas:

- `teste_data_meta_01`

Field name | Type
-- | --
date_start | DATE
campaign_name | STRING
cost | FLOAT
ad_account_id | STRING
dt_h_recording_data | TIMESTAMP

- `teste_data_google_01`

Field name | Type
-- | --
campaign_name | STRING
spend | FLOAT
date | DATE
ad_account_id | STRING
dt_h_recording_data | TIMESTAMP

- `teste_data_linkedin_01`

Field name | Type
-- | --
date_start | DATE
campaign_name | STRING
cost | FLOAT
ad_account_id | STRING
dt_h_recording_data | TIMESTAMP

- `teste_01` (HubSpot Contacts)

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
has_valid_deal | BOOLEAN

- `teste_data_deals_01`

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
