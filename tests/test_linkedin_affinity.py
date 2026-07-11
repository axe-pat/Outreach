import json
from pathlib import Path
from types import SimpleNamespace

from outreach.cli import (
    apply_raw_candidate,
    execute_linkedin_company_run,
    infer_role_bucket,
    select_invite_candidates_with_affinity_lift,
)
from outreach.config import OutreachSettings
from outreach.linkedin_affinity import (
    affinity_candidate_qualified_for_lift,
    allocate_affinity_invite_cap,
    filter_affinity_pass_definitions,
    high_affinity_candidate_signals,
    plan_high_affinity_expansion,
    recommend_affinity_send_cap,
)
from outreach.services.linkedin import FilterRunResult


def _product_application_context() -> dict[str, object]:
    return {
        "target_lists": "jobs;resume_generator;pre_apply",
        "opportunity_titles": ["MBA Product Manager Intern"],
        "target_role_family": "product_pm",
        "target_role_is_concrete": True,
    }


def _sendable_candidate(**overrides: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "name": "Warm Product Leader",
        "title": "Senior Product Manager",
        "role_bucket": "Product",
        "score": 80,
        "linkedin_url": "https://www.linkedin.com/in/warm-product-leader/",
        "existing_connection": False,
        "shared_history_signals": ["Intuit"],
        "note_qc": {"verdict": "send"},
        "target_role_family": "product_pm",
        "target_role_source": "explicit_title",
        "target_role_is_concrete": True,
    }
    candidate.update(overrides)
    return candidate


def test_application_plus_outreach_plan_runs_bounded_history_and_role_passes() -> None:
    plan = plan_high_affinity_expansion(
        _product_application_context(),
        ex_companies=["Intuit", "Gojek", "Hevo Data"],
        shared_history_keywords=["Thapar", "Thapar Institute"],
    )

    assert plan.eligible is True
    assert len(plan.passes) == 10
    definitions = plan.pass_definitions
    assert definitions["affinity_history_intuit"]["query"] == "Intuit"
    assert definitions["affinity_history_intuit"]["shared_history_term"] == "Intuit"
    assert definitions["affinity_history_gojek"]["query"] == "Gojek"
    assert definitions["affinity_history_usc"]["school"] == (
        "University of Southern California"
    )
    assert definitions["affinity_history_marshall"]["school"] == (
        "USC Marshall School of Business"
    )
    assert definitions["affinity_role_product"]["query"] == "product"
    assert definitions["affinity_role_hiring"]["query"] == "hiring"
    assert definitions["affinity_role_head_of_product"]["query"] == "head of product"
    assert definitions["affinity_history_thapar"]["query"] == "Thapar"
    assert "affinity_history_thapar_institute" not in definitions
    assert all(item["limit"] == 6 for item in definitions.values())
    assert all(item["max_pages"] == 1 for item in definitions.values())
    assert all(item["run_if_below_pool_size"] == 36 for item in definitions.values())


def test_affinity_expansion_requires_both_top_account_and_role_evidence() -> None:
    ordinary = plan_high_affinity_expansion(
        {
            "target_lists": "yc;startup;hiring",
            "opportunity_titles": ["Product Manager"],
        }
    )
    irrelevant_application = plan_high_affinity_expansion(
        {
            "target_lists": "jobs;resume_generator;pre_apply",
            "opportunity_titles": ["Senior Accountant"],
        }
    )

    assert ordinary.eligible is False
    assert ordinary.passes == ()
    assert "no top-account evidence" in ordinary.reasons
    assert irrelevant_application.eligible is False
    assert irrelevant_application.passes == ()
    assert "no concrete relevant role evidence" in irrelevant_application.reasons


def test_strategic_adjacent_role_uses_role_specific_search_terms() -> None:
    plan = plan_high_affinity_expansion(
        {
            "target_lists": "relationship;priority;tier-a",
            "opportunity_titles": ["Business Operations and Strategy Manager"],
            "target_role_family": "bizops_strategy",
            "target_role_is_concrete": True,
        }
    )

    assert plan.eligible is True
    assert plan.target_role_family == "bizops_strategy"
    role_queries = {
        item.query for item in plan.passes if item.signal == "target_role"
    }
    assert role_queries == {"strategy", "hiring", "chief of staff"}


def test_affinity_passes_honor_existing_include_and_exclude_controls() -> None:
    plan = plan_high_affinity_expansion(_product_application_context())

    included = filter_affinity_pass_definitions(
        plan,
        include_passes=("affinity_history_intuit", "product_network"),
    )
    excluded = filter_affinity_pass_definitions(
        plan,
        exclude_passes=("affinity_history_intuit", "affinity_role_hiring"),
    )

    assert list(included) == ["affinity_history_intuit"]
    assert "affinity_history_intuit" not in excluded
    assert "affinity_role_hiring" not in excluded
    assert "affinity_history_gojek" in excluded


def test_send_cap_lift_requires_actual_scored_affinity_candidates() -> None:
    plan = plan_high_affinity_expansion(_product_application_context())
    candidates = [
        _sendable_candidate(
            name=f"Warm {company}",
            linkedin_url=f"https://www.linkedin.com/in/warm-{company.casefold()}/",
            shared_history_signals=[company],
        )
        for company in ("Intuit", "Gojek", "Hevo", "Optum", "Thapar")
    ]
    candidates.extend(
        [
            {"score": 100, "existing_connection": False},
            {
                "score": 100,
                "usc": True,
                "existing_connection": True,
            },
        ]
    )

    assert recommend_affinity_send_cap(candidates, plan=plan) == 5
    assert high_affinity_candidate_signals(
        {"usc_marshall": True, "shared_history_signals": ["Intuit"]}
    ) == ("USC Marshall", "Intuit")


def test_send_cap_stays_at_base_without_real_affinity_evidence() -> None:
    plan = plan_high_affinity_expansion(_product_application_context())

    assert recommend_affinity_send_cap(
        [{"score": 100}, {"score": 90, "shared_history_signals": ["Intuit"]}],
        plan=plan,
    ) == 3


def test_affinity_lift_gate_requires_every_sendability_field() -> None:
    valid = _sendable_candidate()
    invalid_candidates = [
        _sendable_candidate(existing_connection=True),
        _sendable_candidate(shared_history_signals=[]),
        _sendable_candidate(linkedin_url=""),
        _sendable_candidate(score=34),
        _sendable_candidate(note_qc={"verdict": "blocked"}),
        _sendable_candidate(target_role_is_concrete=False),
        _sendable_candidate(target_role_source="product_primary_default"),
    ]

    assert affinity_candidate_qualified_for_lift(valid) is True
    assert all(
        not affinity_candidate_qualified_for_lift(candidate)
        for candidate in invalid_candidates
    )


def test_affinity_cap_uses_only_unallocated_daily_headroom() -> None:
    company_cap, remaining, headroom = allocate_affinity_invite_cap(
        planned_cap=3,
        recommended_cap=5,
        remaining_invites=6,
        affinity_headroom=4,
    )
    no_room_cap, no_room_remaining, no_room_headroom = allocate_affinity_invite_cap(
        planned_cap=3,
        recommended_cap=5,
        remaining_invites=6,
        affinity_headroom=0,
    )

    assert (company_cap, remaining, headroom) == (5, 8, 2)
    assert (no_room_cap, no_room_remaining, no_room_headroom) == (3, 6, 0)


def test_generic_three_person_recommendation_does_not_raise_smaller_plan() -> None:
    company_cap, remaining, headroom = allocate_affinity_invite_cap(
        planned_cap=1,
        recommended_cap=3,
        remaining_invites=1,
        affinity_headroom=4,
    )

    assert (company_cap, remaining, headroom) == (1, 1, 4)


def test_delinea_shaped_candidates_require_real_affinity_and_defensible_route() -> None:
    plan = plan_high_affinity_expansion(_product_application_context())
    nick = _sendable_candidate(
        name="Nick B.",
        title="Sr. Product Manager",
        score=48,
        shared_history_signals=[],
    )
    kinjal = _sendable_candidate(
        name="Kinjal S.",
        title=(
            "Strategic Partner to Enterprise Clients | Driving Growth, Retention "
            "& Trust | Business Strategy"
        ),
        role_bucket="Adjacent",
        score=35,
        usc=True,
        shared_history_signals=[],
    )
    alexanne = _sendable_candidate(
        name="Alexanne M. Labrador",
        title="Executive Business Partner, Office of the CEO",
        role_bucket="Adjacent",
        score=56,
        shared_history_signals=["Intuit"],
    )

    assert infer_role_bucket(
        str(alexanne["title"]),
        str(alexanne["title"]),
        OutreachSettings(),
    ) == "Adjacent"
    assert affinity_candidate_qualified_for_lift(nick) is False
    assert affinity_candidate_qualified_for_lift(kinjal) is True
    assert affinity_candidate_qualified_for_lift(alexanne) is False
    assert recommend_affinity_send_cap([nick, kinjal, alexanne], plan=plan) == 3


def test_lift_slots_are_filled_only_by_qualified_affinity_candidates() -> None:
    ranked = [
        _sendable_candidate(
            name="Base One",
            linkedin_url="https://www.linkedin.com/in/base-one/",
            shared_history_signals=[],
        ),
        _sendable_candidate(
            name="Base Two",
            linkedin_url="https://www.linkedin.com/in/base-two/",
            shared_history_signals=[],
        ),
        _sendable_candidate(
            name="Ordinary Three",
            linkedin_url="https://www.linkedin.com/in/ordinary-three/",
            shared_history_signals=[],
        ),
        _sendable_candidate(
            name="Affinity Four",
            linkedin_url="https://www.linkedin.com/in/affinity-four/",
            shared_history_signals=["Gojek"],
        ),
        _sendable_candidate(
            name="Affinity Five",
            linkedin_url="https://www.linkedin.com/in/affinity-five/",
            usc=True,
            shared_history_signals=[],
        ),
    ]

    batch = select_invite_candidates_with_affinity_lift(
        ranked,
        planned_limit=2,
        effective_limit=4,
    )

    assert [candidate["name"] for candidate in batch] == [
        "Base One",
        "Base Two",
        "Affinity Four",
        "Affinity Five",
    ]


def test_history_pass_preserves_match_when_compact_card_omits_past_employer() -> None:
    settings = OutreachSettings()
    plan = plan_high_affinity_expansion(_product_application_context())
    deduped: dict[str, dict[str, object]] = {}

    kept = apply_raw_candidate(
        deduped=deduped,
        raw=SimpleNamespace(
            name="Avery Product",
            title="Head of Product @ Priority Product Co",
            raw_text="Head of Product @ Priority Product Co",
            connection_degree="2nd",
            snippet="",
            linkedin_url="https://www.linkedin.com/in/avery-product/",
            location="",
            subtitle="",
        ),
        company="Priority Product Co",
        pass_name="affinity_history_intuit",
        pass_config=plan.pass_definitions["affinity_history_intuit"],
        settings=settings,
        company_mode="default",
    )

    candidate = deduped["https://www.linkedin.com/in/avery-product/"]
    assert kept is True
    assert candidate["shared_history"] is True
    assert candidate["shared_history_signals"] == ["Intuit"]
    assert "Shared History" in candidate["triggers"]


def test_company_run_injects_affinity_passes_and_records_decision(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    class _Scraper:
        def __init__(self, _settings: OutreachSettings) -> None:
            pass

        def require_live_cdp_session(self) -> None:
            pass

        def extract_people_with_filters_live(self, **kwargs: object) -> FilterRunResult:
            calls.append(kwargs)
            return FilterRunResult(
                candidates=[],
                final_url="https://linkedin.test/people",
                visible_filter_text=[],
            )

    monkeypatch.setattr("outreach.cli.LinkedInScraper", _Scraper)
    monkeypatch.setattr(
        OutreachSettings,
        "artifacts_dir",
        property(lambda _self: tmp_path / "artifacts"),
    )
    artifact = execute_linkedin_company_run(
        settings=OutreachSettings(tracking_workspace_dir=tmp_path / "workspace"),
        company="Priority Product Co",
        dry_run=True,
        company_mode="default",
        note_context=_product_application_context(),
    )

    payload = json.loads(artifact.read_text(encoding="utf-8"))
    affinity = payload["affinity_expansion"]
    pass_names = [item["pass_name"] for item in payload["pass_summaries"]]
    assert affinity["eligible"] is True
    assert affinity["enabled_passes"] == [
        "affinity_history_intuit",
        "affinity_history_gojek",
        "affinity_history_usc",
        "affinity_history_marshall",
        "affinity_role_product",
        "affinity_role_hiring",
        "affinity_role_head_of_product",
        "affinity_history_thapar",
        "affinity_history_hevo",
        "affinity_history_optum",
    ]
    assert pass_names.index("existing_connections") < pass_names.index(
        "affinity_history_intuit"
    )
    assert pass_names.index("affinity_history_optum") < pass_names.index("product_usc")
    assert any(call.get("search_query") == "Intuit" for call in calls)
    assert any(
        call.get("school") == "USC Marshall School of Business" for call in calls
    )
