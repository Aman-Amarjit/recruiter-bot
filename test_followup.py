#!/usr/bin/env python3
import os
# Ensure SEND_DISABLED is true for dry-run before importing any scripts
os.environ['SEND_DISABLED'] = 'true'

import datetime, uuid
from scripts.db_client import supabase, logger
from scripts.followup import check_and_send_followups
email = f"john.doe+{uuid.uuid4().hex[:6]}@example.com"

def insert_test_data():
    # Insert a suppressed contact
    contact_res = supabase.table('contacts').insert({
        'company': 'TestCo',
        'name': 'John Doe',
        'email': email,
        'source': 'test',
        'confidence': 1.0,
        'suppressed': True,
        'status': 'pending'
    }).execute()
    contact_id = contact_res.data[0]['id']
    # Insert a listing
    listing_res = supabase.table('listings').insert({
        'title': 'Software Engineer',
        'company': 'TestCo',
        'source_url': f"https://example.com/job/{uuid.uuid4().hex[:8]}",
        'source': 'test',
        'description': 'We need a developer.',
        'domain_tag': 'ai_ml'
    }).execute()
    listing_id = listing_res.data[0]['id']
    # Insert application with status sent older than 7 days
    old_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=8)).isoformat()
    app_res = supabase.table('applications').insert({
        'listing_id': listing_id,
        'contact_id': contact_id,
        'status': 'sent',
        'email_body': 'Test email body',
        'followup_sent': False,
        'sent_at': old_date
    }).execute()
    return app_res.data[0]['id']

app_id = insert_test_data()
print('Inserted application id:', app_id)
# Run followup check
check_and_send_followups()
# Fetch updated application
app = supabase.table('applications').select('*').eq('id', app_id).execute().data[0]
print('Updated application status:', app['status'])
print('Followup sent flag:', app['followup_sent'])
