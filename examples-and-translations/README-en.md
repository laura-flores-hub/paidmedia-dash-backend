# Objective
Python code that automatically and periodically collects information from five external sources — Meta Ads, Google Ads, LinkedIn Ads, HubSpot Contacts, and HubSpot Deals — and centralizes it in Supabase (PostgreSQL). From the paid media platforms (Meta Ads, Google Ads, and LinkedIn Ads), the investment values per campaign are extracted. From HubSpot, data on Contacts and Deals throughout the sales funnel is extracted.

## Pipeline Flow and Failure Handling

### Phase 1 — Collection

Each data source is collected independently. A failure in any platform is logged but does not interrupt the remaining collections. At the end of the phase, all collected records are saved as JSON files in the `outputs/` directory (`outputs/<platform>_<timestamp>.json`), ensuring no data is lost before the send step.

### Phase 2 — Confirmation and send

For each platform with new data, the script displays the path to the temporary file and requests explicit confirmation before sending to Supabase. Sending can be cancelled per platform without affecting the others.

### Send failure handling

If a send fails after confirmation, the platform is recorded in a failure list. At the end of the round, the script asks whether the user wants to retry the failed sends — this cycle repeats until all sends succeed or the user manually exits.

If the process is interrupted after collection but before sending, the files in `outputs/` remain available for resending via `--retry`, without needing to re-collect from the APIs.

#### How to run

```bash
python dashspy_v1.py
```
Runs the full pipeline: collects data from Meta Ads, Google Ads, LinkedIn Ads, HubSpot Contacts, and HubSpot Deals, saves the results locally for review, and waits for confirmation before sending to Supabase.

```bash
python dashspy_v1.py meta
python dashspy_v1.py google
python dashspy_v1.py linkedin
python dashspy_v1.py hubspot
python dashspy_v1.py deals
```
Runs the full cycle (collect → confirm → send) for a single platform. Useful for debugging or reprocessing a specific source without running the full pipeline. The `meta` subcommand asks which ad accounts to collect before starting.

```bash
python dashspy_v1.py meta-resume
```
Resumes a Meta Ads collection for a specific ad account from where it stopped. Prompts for the account to resume, finds the last `date_start` available in that account's output file, and collects from there + 1 day. Allows specifying a custom end date (default: yesterday).

```bash
python dashspy_v1.py --retry
```
Reloads JSON files previously saved in `outputs/` and resends them to Supabase without re-collecting from the APIs. Useful for recovering sends that failed after a successful collection run.

## APIs
### Meta API
#### Authentication:
The credentials are specified in the `.env` file in the following variables:
- Access Token: `META_ACCESS_TOKEN`
- Ad Account IDs: `META_AD_ACCOUNT_IDS` (comma-separated)

#### Endpoints:
##### Base URL:
- https://graph.facebook.com/v25.0

##### Full URL to return campaign spend:
- {urlbase}/{META_AD_ACCOUNT_ID}/insights?fields=campaign_id%2Ccampaign_name%2Cspend&level=campaign&time_increment=1&time_range=%7B'since'%3A'{start_date}'%2C'until'%3A'{end_date}'%7D{pagepathbase}

##### Where:
- *start_date* = Date in yyyy-mm-dd format
- *end_date* = Date in yyyy-mm-dd format
- *pagepathbase* = &access_token={META_ACCESS_TOKEN}

#### Expected Returned Data:
- *campaign_id*: unique ID of the ad campaign
- *date_start*: investment dates for each ad campaign
- *campaign_name*: names of the ad campaigns
- *spend*: investment data for the ad campaigns

#### Expected Behavior:
1. The application is expected to check, in the Meta spend table in Supabase, whether the date column is empty.
##### 1.1 Empty column
Fetches all spend data from all campaigns. To do this, it considers the values and rules in order to respect the Meta API's security architecture and limits:
- *start_date* = 2023-09-21
- *end_date* = current date − 1
###### Mandatory Pagination (`next`):
The script implements a continuous pagination loop. It reads the first dataset and looks for a `next` key in the response, making sequential requests to the provided URL until no more pages are available.
###### Rate Limiting and Connection Error Protection:
Extraction is massive, which will trigger Meta's volume throttling. The script has `try/except` blocks designed to catch the following throttling errors and pause 60s before retrying (up to 5×):
- **Code 1:** Unknown (treated as transient)
- **Code 4:** API Too Many Calls
- **Code 17:** API User Too Many Calls
- **Code 341:** Application limit reached

Connection errors (`ConnectionError`) and timeouts (`ReadTimeout`) are also retried with the same logic.
###### Mid-collection error recovery:
Data is collected in annual windows. If an error occurs during pagination within a window (including invalid cursor `#2642`), the script immediately saves the records from already-completed windows to `outputs/meta_<account_id>_<timestamp>.json` and stops collection for that account. Use `python dashspy_v1.py meta-resume` to resume from the last available date.
###### Batch Requests:
It respects the hard limit of 50 requests per batch.

2. If the date column is not empty, the script looks for the most recent date stored in the table and makes the request with the following structure:
- *start_date* = most recent date in the spend table + 1
- *end_date* = current date − 1

### Google API
#### Authentication:
The credentials are specified in the `google-ads.yaml` file (the path can be configured via the `GOOGLE_ADS_YAML_PATH` environment variable; default: `google-ads.yaml`) in the following variables:
- Developer Token: `developer_token`
- Client ID: `client_id`
- Client Secret: `client_secret`
- Refresh Token: `refresh_token`
- Customer ID: `login_customer_id`

#### Query Method:
Data acquisition uses the official *google-ads* library for Python, via the *GoogleAdsService* service with the *search_stream* method. Queries are written in GAQL (Google Ads Query Language), which has a structure similar to SQL.

##### Query used:
SELECT<br>
 campaign.id,<br>
 campaign.name,<br>
 segments.date,<br>
 metrics.cost_micros<br>
FROM campaign<br>
WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'<br>
ORDER BY segments.date DESC

##### Where:
- *start_date*: Date in yyyy-mm-dd format
- *end_date*: Date in yyyy-mm-dd format

##### Values and their respective variables:
- *Campaign ID*: campaign.id
- *Date*: segments.date
- *Campaign Name*: campaign.name
- *Spend*: metrics.cost_micros

#### Captured Data:
- *campaign.id*: unique ID of the ad campaign
- *segments.date*: investment dates for each ad campaign
- *campaign.name*: names of the ad campaigns
- *metrics.cost_micros*: investment data for the ad campaigns, in micros (1 unit = R$ 0.000001) — converted to reais by dividing by 1,000,000

#### Expected Behavior:
1. The application is expected to check, in the Google spend table in Supabase, whether the date column is empty.
##### 1.1 If the column is empty
Fetches all spend data from all campaigns. To do this, it uses the following values:
- *start_date* = 2021-11-22
- *end_date* = current date − 1

2. If the date column is not empty, the script looks for the most recent date stored in the table and makes the request with the following structure:
- *start_date* = most recent date in the spend table + 1
- *end_date* = current date − 1

### LinkedIn Ads API
#### Authentication:
The credentials are specified in the `.env` file in the following variables:
- Access Token: `LINKEDIN_ACCESS_TOKEN`
- Ad Account IDs: `LINKEDIN_AD_ACCOUNT_IDS` (comma-separated)

#### Query Method:
Data acquisition uses direct REST API calls via `subprocess curl`, with the required `Linkedin-Version` and `X-Restli-Protocol-Version` headers. The JSON response is processed directly in Python.

##### Endpoint used:
- https://api.linkedin.com/rest/adAnalytics

##### Parameters:
- `q=analytics`
- `pivot=CAMPAIGN`
- `timeGranularity=DAILY`
- `accounts=List(urn:li:sponsoredAccount:{account_id})`
- `dateRange=(start:(...),end:(...))`
- `fields=dateRange,costInLocalCurrency,pivotValues`

#### Captured Data:
- *dateRange.start*: campaign investment date
- *costInLocalCurrency*: amount invested in the campaign
- *pivotValues*: campaign URN (resolved to name via `/adCampaignsV2/{id}`)
- *campaign_id*: numeric campaign ID, extracted from the URN in `pivotValues` (e.g. `urn:li:sponsoredCampaign:12345` → `12345`)

#### Expected Behavior:
1. Checks the most recent date recorded in the LinkedIn table in Supabase.
2. If empty, starts historical load from 2023-09-01.
3. Collection is done in quarterly windows to avoid timeouts.
4. Includes rate limiting protection (HTTP 429) with backoff and up to 5 retries.

### HubSpot API
#### Authentication:
The credential is specified in the `.env` file in the following variable:
- Access Token: `TOKEN_ACESSO_HUBSPOT`

#### Query Method:
Uses the HubSpot Search API (v3) with `createdate` filters in daily windows, to work around the 10,000 results per query limit. All requests use POST with JSON payloads.

##### Base URL:
- https://api.hubapi.com/crm/v3/objects

#### HubSpot Contacts

##### Endpoint:
- `/contacts/search` — POST

##### Period Filter:
Contacts created from the most recent date recorded in Supabase (with a 45-day lookback), paginated in 1-day windows.

##### Captured Data:
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
- *has_valid_deal*: calculated boolean — `True` if the contact has no deals or has at least one deal outside the excluded pipelines (Business Partner, BDRs, Partnerships)

#### HubSpot Deals

##### Endpoint:
- `/deals/search` — POST

##### Period Filter:
Deals created from the most recent date recorded in Supabase, paginated in 1-day windows.

##### Captured Data:
- *hs_object_id*, *dealname*, *amount*
- *createdate*, *closedate*, *lastmodifieddate*
- *dealstage*, *pipeline* (mapped to readable names)
- *hubspot_owner_id*, *ae_deal_won*, *ae_squad*
- *first_meeting_status*, *deal_source*, *pais*
- *contact_ids*: list of contact IDs associated with the deal (via Associations Batch API v4)

## Supabase
### Tables:

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

- `data_hs_contacts_v2`

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

- `data_hs_deals_v2`

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
