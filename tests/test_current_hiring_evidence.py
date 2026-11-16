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
