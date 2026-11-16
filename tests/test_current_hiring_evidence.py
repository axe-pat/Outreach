from datetime import date
from outreach.account_tracker import current_hiring_evidence
from outreach.tracking import OpportunityRecord


def test_historical_or_closed_openings_do_not_count_as_current_hiring():
    def opp(key,title,discovered,status='Discovered'):
        return OpportunityRecord(opportunity_id=key,organization_id='org',title=title,
                                 discovered_at=discovered,status=status)
    rows=[opp('old','Product Manager','2026-07-01'),
          opp('fall','Product Intern Fall 2026','2026-11-15'),
          opp('closed','MBA Product Manager 2027','2026-11-15','Closed'),
          opp('spring','Product Intern Spring 2027','2026-11-15'),
          opp('graduate','MBA Product Manager 2027','2026-11-10')]
    assert [r.opportunity_id for r in current_hiring_evidence(rows,date(2026,11,16))]==['spring','graduate']


def test_uncapped_followups_allow_warm_contacts_but_keep_daily_action_limit():
    from dataclasses import replace
    from outreach.account_tracker import DailyPlanBudget,DailyPlanItem,_daily_plan_skip_reason
    item=DailyPlanItem(company='Warm',tier='A',campaign_action='continue_conversation',
        campaign_channel='linkedin',daily_action_priority=110,account_score=50,expected_linkedin_followups=1)
    used=dict(total_actions=0,companies=0,linkedin_invites=0,linkedin_followups=0,
        company_mapping=0,email_research=0,context_enrichment=0,email_drafts=0,large_company_invites=0)
    budget=DailyPlanBudget(max_linkedin_followups=-1,max_total_actions=1)
    assert _daily_plan_skip_reason(item,used,set(),budget)==''
    assert _daily_plan_skip_reason(item,used,set(),replace(budget,max_linkedin_followups=0))=='linkedin_followups_budget_exhausted'
    assert _daily_plan_skip_reason(item,{**used,'total_actions':1},set(),budget)=='daily_action_budget_exhausted'
