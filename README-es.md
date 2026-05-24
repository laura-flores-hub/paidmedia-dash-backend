# Objetivo
Código en Python que recolecta, de forma automatizada y periódica, información de cinco fuentes externas — Meta Ads, Google Ads, LinkedIn Ads, HubSpot Contacts y HubSpot Deals — y la centraliza en Supabase (PostgreSQL). De las plataformas de medios pagos (Meta Ads, Google Ads y LinkedIn Ads) se extraen los valores de inversión por campaña. De HubSpot se extraen datos de Contactos y Negocios a lo largo del embudo de ventas.

## APIs
### API Meta
#### Autenticación:
Las credenciales están especificadas en el archivo `.env` en las siguientes variables:
- Token de acceso: `META_ACCESS_TOKEN`
- IDs de Cuentas Publicitarias: `META_AD_ACCOUNT_IDS` (separados por coma)

#### Endpoints:
##### URL base:
- https://graph.facebook.com/v25.0

##### URL completa para retornar los gastos de campaña:
- {urlbase}/{META_AD_ACCOUNT_ID}/insights?fields=campaign_name%2Cspend&level=campaign&time_increment=1&time_range=%7B'since'%3A'{fecha_inicial}'%2C'until'%3A'{fecha_final}'%7D{pagepathbase}

##### Donde:
- *fecha_inicial* = Fecha en formato aaaa-mm-dd
- *fecha_final* = Fecha en formato aaaa-mm-dd
- *pagepathbase* = &access_token={META_ACCESS_TOKEN}

#### Datos Esperados en la Respuesta:
- *date_start*: fechas de inversión de cada campaña publicitaria
- *campaign_name*: nombres de las campañas publicitarias
- *spend*: datos de la inversión de las campañas publicitarias

#### Funcionamiento Esperado:
1. Se espera que la aplicación verifique, en la tabla de gastos de Meta en Supabase, si la columna de fechas está vacía.
##### 1.1 Columna vacía
Busca todos los gastos de todas las campañas. Para ello, considera los valores y reglas para respetar la arquitectura de seguridad y los límites de la API:
- *fecha_inicial* = 2023-09-21
- *fecha_final* = fecha actual − 1
###### Paginación Obligatoria (`next`):
El script implementa un bucle de paginación continua. Lee el primer conjunto de datos y busca una clave `next` en la respuesta, haciendo solicitudes secuenciales a esa URL proporcionada hasta que no haya más páginas disponibles.
###### Protección Contra Rate Limiting:
La extracción es masiva, lo que activará los límites de volumen de Meta. El script cuenta con bloques `try/except` diseñados para capturar los siguientes errores de límite (throttling) y pausar la ejecución temporalmente antes de volver a intentar:
- **Código 4:** API Too Many Calls
- **Código 17:** API User Too Many Calls
- **Código 341:** Application limit reached
###### Batch Requests:
Respeta el límite estricto de 50 solicitudes por lote.

2. Si la columna de fechas no está vacía, el script busca la última fecha registrada en la tabla y realiza la solicitud con la siguiente estructura:
- *fecha_inicial* = última fecha registrada en la tabla de gastos + 1
- *fecha_final* = fecha actual − 1

### API Google
#### Autenticación:
Las credenciales están especificadas en el archivo `google-ads.yaml` en las siguientes variables:
- Developer Token: `developer_token`
- Client ID: `client_id`
- Client Secret: `client_secret`
- Refresh Token: `refresh_token`
- Customer ID: `login_customer_id`

#### Método de Consulta:
La adquisición de datos utiliza la biblioteca oficial *google-ads* para Python, a través del servicio *GoogleAdsService* con el método *search_stream*. Las consultas se hacen en GAQL (Google Ads Query Language), estructura similar a SQL.

##### Query utilizada:
SELECT<br>
 campaign.name,<br>
 segments.date,<br>
 metrics.cost_micros<br>
FROM campaign<br>
WHERE segments.date BETWEEN '{fecha_inicial}' AND '{fecha_final}'<br>
ORDER BY segments.date DESC

##### Donde:
- *fecha_inicial*: Fecha en formato aaaa-mm-dd
- *fecha_final*: Fecha en formato aaaa-mm-dd

##### Valores y sus respectivas variables:
- *Fecha*: segments.date
- *Nombre de la Campaña*: campaign.name
- *Gasto*: metrics.cost_micros

#### Datos Capturados:
- *segments.date*: fechas de inversión de cada campaña publicitaria
- *campaign.name*: nombres de las campañas publicitarias
- *metrics.cost_micros*: datos de inversión de las campañas publicitarias, en micros (1 unidad = R$ 0,000001) — convertido a reales dividiendo por 1.000.000

#### Funcionamiento Esperado:
1. Se espera que la aplicación verifique, en la tabla de gastos de Google en Supabase, si la columna de fechas está vacía.
##### 1.1 Si la columna está vacía
Busca todos los gastos de todas las campañas. Para ello, considera los valores:
- *fecha_inicial* = 2021-11-22
- *fecha_final* = fecha actual − 1

2. Si la columna de fechas no está vacía, el script busca la última fecha registrada en la tabla y realiza la solicitud con la siguiente estructura:
- *fecha_inicial* = última fecha registrada en la tabla de gastos + 1
- *fecha_final* = fecha actual − 1

### API LinkedIn Ads
#### Autenticación:
Las credenciales están especificadas en el archivo `.env` en las siguientes variables:
- Token de acceso: `LINKEDIN_ACCESS_TOKEN`
- IDs de Cuentas Publicitarias: `LINKEDIN_AD_ACCOUNT_IDS` (separados por coma)

#### Método de Consulta:
La adquisición de datos utiliza llamadas directas a la API REST de LinkedIn mediante `subprocess curl`, con los encabezados obligatorios `Linkedin-Version` y `X-Restli-Protocol-Version`. La respuesta JSON se procesa directamente en Python.

##### Endpoint utilizado:
- https://api.linkedin.com/rest/adAnalytics

##### Parámetros:
- `q=analytics`
- `pivot=CAMPAIGN`
- `timeGranularity=DAILY`
- `accounts=List(urn:li:sponsoredAccount:{account_id})`
- `dateRange=(start:(...),end:(...))`
- `fields=dateRange,costInLocalCurrency,pivotValues`

#### Datos Capturados:
- *dateRange.start*: fecha de inversión de la campaña
- *costInLocalCurrency*: monto invertido en la campaña
- *pivotValues*: URN de la campaña (resuelto a nombre mediante `/adCampaignsV2/{id}`)

#### Funcionamiento Esperado:
1. Verifica la última fecha registrada en la tabla de LinkedIn en Supabase.
2. Si está vacía, inicia la carga histórica desde 2023-09-01.
3. La recolección se realiza en ventanas trimestrales para evitar timeouts.
4. Incluye protección contra rate limiting (HTTP 429) con backoff y hasta 5 reintentos.

### API HubSpot
#### Autenticación:
La credencial está especificada en el `.env` en la siguiente variable:
- Token de acceso: `TOKEN_ACESSO_HUBSPOT`

#### Método de Consulta:
Utiliza la HubSpot Search API (v3) con filtros por `createdate` en ventanas diarias, para evitar el límite de 10.000 resultados por query. Todas las solicitudes usan POST con payload JSON.

##### URL base:
- https://api.hubapi.com/crm/v3/objects

#### HubSpot Contacts

##### Endpoint:
- `/contacts/search` — POST

##### Filtro de Período:
Contactos creados a partir de la última fecha registrada en Supabase (con lookback de 45 días), paginados en ventanas de 1 día.

##### Datos Capturados:
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
- *has_valid_deal*: booleano calculado — `True` si el contacto no tiene negocios o tiene al menos uno fuera de los pipelines excluidos (Business Partner, BDRs, Partnerships)

#### HubSpot Deals

##### Endpoint:
- `/deals/search` — POST

##### Filtro de Período:
Negocios creados a partir de la última fecha registrada en Supabase, paginados en ventanas de 1 día.

##### Datos Capturados:
- *hs_object_id*, *dealname*, *amount*
- *createdate*, *closedate*, *lastmodifieddate*
- *dealstage*, *pipeline* (mapeados a nombres legibles)
- *hubspot_owner_id*, *ae_deal_won*, *ae_squad*
- *first_meeting_status*, *deal_source*, *pais*
- *contact_ids*: lista de IDs de contactos asociados al negocio (via Associations Batch API v4)

## Supabase
### Tablas:

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
