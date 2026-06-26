## his version
### test_01
create table public.teste_01 (
  id bigserial not null,
  dt_h_recording_data timestamp with time zone not null,
  hs_object_id text not null default ''::text,
  createdate timestamp with time zone not null,
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
  holding_dropdown text null,
  jobtitle text null,
  qual_o_erp_utilizado_por_sua_empresa_para_sua_gestao_financeira text null,
  not_qualified_reason text null,
  estado_de_lead text null,
  hs_object_source_detail_1 text null,
  stage_of_the_deal text null,
  motivo_no_interesado text null,
  conversion_de_lead text null,
  hubspot_team_id text null,
  campaign_name text null,
  hs_analytics_source_data_1 text null,
  hs_analytics_source_data_2 text null,
  form_submitted text null,
  has_valid_deal boolean null default true,
  country text null,
  region text null,
  constraint teste_01_pkey primary key (id)
) TABLESPACE pg_default;

create index IF not exists idx_hubspot_createdate on public.teste_01 using btree (createdate desc) TABLESPACE pg_default;

create index IF not exists idx_hubspot_hs_object_id on public.teste_01 using btree (hs_object_id) TABLESPACE pg_default;

### teste_data_deals_01
create table public.teste_data_deals_01 (
  id bigserial not null,
  dt_h_recording_data text null,
  hs_object_id text null,
  dealname text null,
  amount double precision null,
  createdate text null,
  closedate text null,
  lastmodifieddate text null,
  dealstage text null,
  pipeline text null,
  hubspot_owner_id text null,
  ae_deal_won text null,
  ae_squad text null,
  contact_ids text[] null,
  deal_source text null,
  first_meeting_status text null,
  constraint teste_data_deals_01_pkey primary key (id),
  constraint teste_data_deals_01_hs_object_id_key unique (hs_object_id)
) TABLESPACE pg_default;

### teste_data_google_01
create table public.teste_data_google_01 (
  id bigserial not null,
  date date not null,
  campaign_name text not null default ''::text,
  spend double precision null,
  dt_h_recording_data timestamp with time zone not null,
  ad_account_id text null,
  constraint teste_data_google_01_pkey primary key (id)
) TABLESPACE pg_default;

create index IF not exists idx_google_date on public.teste_data_google_01 using btree (date desc) TABLESPACE pg_default;

### teste_data_linkedin_01
create table public.teste_data_linkedin_01 (
  id bigserial not null,
  date_start date null,
  campaign_name text null,
  cost double precision null,
  ad_account_id text null,
  dt_h_recording_data timestamp with time zone null,
  constraint teste_data_linkedin_01_pkey primary key (id)
) TABLESPACE pg_default;

create index IF not exists idx_linkedin_date on public.teste_data_linkedin_01 using btree (date_start) TABLESPACE pg_default;

### teste_data_meta_01
create table public.teste_data_meta_01 (
  id bigserial not null,
  date_start date not null,
  campaign_name text not null default ''::text,
  cost double precision null,
  dt_h_recording_data timestamp with time zone not null,
  ad_account_id text not null default ''::text,
  constraint teste_data_meta_01_pkey primary key (id)
) TABLESPACE pg_default;

create index IF not exists idx_meta_date_start on public.teste_data_meta_01 using btree (date_start desc) TABLESPACE pg_default;