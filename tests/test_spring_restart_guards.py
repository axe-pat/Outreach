from outreach.cli import _gate_high_value_followup_draft,_partition_initial_invites_for_review

def test_fallback_invite_and_reply_are_held_for_review():
    row={'name':'Example','title':'Product Manager','ai_messaging':{'status':'fallback'}}
    routine,protected=_partition_initial_invites_for_review([row],organization=None)
    assert not routine and len(protected)==1
    reply=_gate_high_value_followup_draft(dict(row),organizations_by_id={})
    assert reply['send_recommendation']=='review'
