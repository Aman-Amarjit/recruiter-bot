-- Enable UUID extension if not enabled
create extension if not exists "uuid-ossp";

-- Listings table
create table listings (
  id uuid default uuid_generate_v4() primary key,
  title text not null,
  company text not null,
  source_url text unique not null,
  source text not null,
  description text,
  domain_tag text not null, -- 'ai_ml', 'cybersecurity', 'robotics'
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- Contacts table
create table contacts (
  id uuid default uuid_generate_v4() primary key,
  company text not null,
  name text,
  email text unique not null,
  source text not null,
  confidence numeric(3,2) not null check (confidence >= 0.0 and confidence <= 1.0),
  suppressed boolean default false not null,
  status text not null default 'pending', -- 'pending', 'processing', 'completed', 'failed' (Idempotency lock)
  last_emailed_at timestamp with time zone,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- Applications table
create table applications (
  id uuid default uuid_generate_v4() primary key,
  listing_id uuid references listings(id) on delete cascade not null,
  contact_id uuid references contacts(id) on delete cascade not null,
  status text not null default 'drafting', -- 'drafting', 'generating', 'held', 'approved', 'sending', 'sent', 'failed', 'resume_failed', 'replied', 'cancelled', 'recheck'
  -- 'held' indicates an application vetted by critique but awaiting manual approval before sending.
  -- 'recheck' flags held/approved applications to be re-drafted and re-evaluated by the LLM.
  email_body text not null,
  critique_score integer check (critique_score >= 1 and critique_score <= 10),
  failure_reason text, -- Persists resume-build or critique failure details

  resume_url text, -- Tailored resume URL hosted in Supabase Storage
  resume_project_selection jsonb, -- Audit trail of which projects/skills were selected
  extracted_keywords jsonb, -- All keywords found in job description
  matched_keywords jsonb, -- Subset of keywords matching candidate profile.json
  ats_match_rate numeric(5,2), -- Percentage match scored by builder
  sent_at timestamp with time zone,
  followup_at timestamp with time zone,
  followup_sent boolean default false not null,
  reply_sentiment text,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null,
  constraint unique_listing_contact unique (listing_id, contact_id)
);

-- Send logs table
create table send_log (
  id uuid default uuid_generate_v4() primary key,
  application_id uuid references applications(id) on delete cascade not null,
  sent_at timestamp with time zone default timezone('utc'::text, now()) not null,
  bounced boolean default false not null,
  opened boolean default false not null
);

-- Daily counters for circuit breaker and pacing
create table daily_counters (
  date date primary key default current_date,
  sends_today integer default 0 not null,
  cap integer default 5 not null,
  bounce_rate_trailing20 numeric(5,2) default 0.00 not null
);

-- Scraper circuit breaker/health state tracking
create table source_status (
  source text primary key, -- 'github', 'internshala', 'remoteok', 'linkedin'
  disabled_until timestamp with time zone,
  last_failure_reason text,
  consecutive_failures integer default 0 not null,
  updated_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- Indexing for speed and suppression checks
create index idx_contacts_email on contacts(email);
create index idx_contacts_suppressed on contacts(suppressed);
create index idx_applications_status on applications(status);

-- Atomic function to increment daily counters
create or replace function increment_sends_today(target_date date)
returns void as $$
begin
  insert into daily_counters (date, sends_today)
  values (target_date, 1)
  on conflict (date) do update
  set sends_today = daily_counters.sends_today + 1;
end;
$$ language plpgsql;

