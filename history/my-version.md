## my version
### test_01
create table public.teste_01 (
  dt_h_recording_data timestamp with time zone null,
  hs_object_id text null,
  createdate timestamp with time zone null,
  lastmodifieddate timestamp with time zone null,
  firstname text null,
  lastname text null,
  email text null,
  phone text null,
  company text null,
  lifecyclestage text null,
  hs_lead_status text null,
  hubspot_owner_id text null,
  num_associated_deals integer null,
  hs_analytics_source text null,
  hs_analytics_last_touch_converting_campaign text null,
  numemployees text null,
  jobtitle text null,
  not_qualified_reason text null,
  estado_de_lead text null,
  hs_object_source_detail_1 text null,
  hs_analytics_source_data_1 text null,
  hs_analytics_source_data_2 text null,
  stage_of_the_deal text null,
  motivo_no_interesado text null,
  conversion_de_lead text null,
  hubspot_team_id text null,
  form_submitted text null,
  country text null,
  region text null,
  has_valid_deal boolean null,
  main_country text null,
  constraint teste_01_hs_object_id_key unique (hs_object_id)
) TABLESPACE pg_default;

### teste_data_deals_01
create table public.teste_data_deals_01 (
  dt_h_recording_data timestamp with time zone null,
  hs_object_id text not null,
  dealname text null,
  amount double precision null,
  createdate timestamp with time zone null,
  closedate timestamp with time zone null,
  lastmodifieddate timestamp with time zone null,
  dealstage text null,
  pipeline text null,
  hubspot_owner_id text null,
  ae_deal_won text null,
  ae_squad text null,
  first_meeting_status text null,
  deal_source text null,
  contact_ids text[] null,
  pais text null,
  constraint teste_data_deals_01_pkey primary key (hs_object_id)
) TABLESPACE pg_default;

### teste_data_google_01
create table public.teste_data_google_01 (
  campaign_name text null,
  spend double precision null,
  date date null,
  ad_account_id text null,
  dt_h_recording_data timestamp with time zone null
) TABLESPACE pg_default;

### teste_data_linkedin_01
create table public.teste_data_linkedin_01 (
  date_start date null,
  campaign_name text null,
  cost double precision null,
  ad_account_id text null,
  dt_h_recording_data timestamp with time zone null
) TABLESPACE pg_default;

### teste_data_meta_01
create table public.teste_data_meta_01 (
  date_start date null,
  campaign_name text null,
  cost double precision null,
  ad_account_id text null,
  dt_h_recording_data timestamp with time zone null
) TABLESPACE pg_default;