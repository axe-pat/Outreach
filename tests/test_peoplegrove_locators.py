from __future__ import annotations

import csv
from pathlib import Path

from outreach.models import RawSearchCandidate
from outreach.peoplegrove_locators import (
    apply_exact_peoplegrove_locator_results,
    build_peoplegrove_locator_queue,
    canonical_linkedin_person_url,
    load_or_create_peoplegrove_locator_state,
    match_peoplegrove_locator_candidates,
    new_peoplegrove_locator_state,
    normalized_person_name,
    pending_peoplegrove_locator_targets,
    peoplegrove_locator_search_queries,
    peoplegrove_name_identity_variants,
    save_peoplegrove_locator_state,
    write_peoplegrove_locator_review_csv,
)
from outreach.tracking import (
    ContactRecord,
    OrganizationRecord,
    OrganizationType,
    OutreachWorkbook,
    SourceKind,
)


def _workbook(tmp_path: Path) -> OutreachWorkbook:
    workspace = tmp_path / "workspace"
    workbook = OutreachWorkbook(workspace)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-coursera",
            name="Coursera",
            organization_type=OrganizationType.COMPANY,
        )
    )
    return workbook


def _peoplegrove_contact(
    *,
    contact_id: str = "ct-aaron",
    full_name: str = "Aaron J. McCullough",
    linkedin_url: str = "",
) -> ContactRecord:
    return ContactRecord(
        contact_id=contact_id,
        organization_id="org-coursera",
        full_name=full_name,
        title="VP Product Management",
        contact_type="Product",
        target_lists="peoplegrove;usc-network;usc-product",
        linkedin_url=linkedin_url,
        source_kind=SourceKind.UNIVERSITY_DIRECTORY,
        source_url=(
            "https://usc.peoplegrove.com/hub/usc-career-network/person"
            "?userProfile=aaronmccullough"
        ),
        notes="peoplegrove_category=product_product_strategy | priority=high",
    )


def test_person_and_linkedin_normalization_is_strict_but_middle_initial_tolerant() -> None:
    assert normalized_person_name("Aaron J. McCullough, Jr.") == "aaron mccullough"
    assert normalized_person_name("Brett Abbey, MBA, PMP") == "brett abbey"
    assert normalized_person_name("Jiayin(Joy) Kuang") == "jiayin joy kuang"
    assert peoplegrove_locator_search_queries("Brett Abbey, MBA, PMP") == (
        "brett abbey",
    )
    assert peoplegrove_name_identity_variants("Inés (Guinard) Kirby") == (
        "ines kirby",
        "ines guinard kirby",
        "guinard kirby",
    )
    assert peoplegrove_name_identity_variants("Ki Bum (KB) Kim") == (
        "ki bum kim",
        "ki bum kb kim",
        "kb kim",
    )
    assert canonical_linkedin_person_url(
        "https://ca.linkedin.com/in/aaronmccullough?trk=search"
    ) == "https://www.linkedin.com/in/aaronmccullough/"
    assert canonical_linkedin_person_url("https://www.linkedin.com/company/coursera") == ""


def test_queue_contains_only_peoplegrove_contacts_without_any_locator(tmp_path: Path) -> None:
    workbook = _workbook(tmp_path)
    workbook.upsert_contact(_peoplegrove_contact())
    workbook.upsert_contact(
        _peoplegrove_contact(
            contact_id="ct-resolved",
            full_name="Resolved Alum",
            linkedin_url="https://www.linkedin.com/in/resolved/",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-regular",
            organization_id="org-coursera",
            full_name="Regular Contact",
            source_kind=SourceKind.LINKEDIN,
        )
    )

    queue = build_peoplegrove_locator_queue(workbook.base_dir)

    assert [row["contact_id"] for row in queue] == ["ct-aaron"]
    assert queue[0]["company"] == "Coursera"


def test_matcher_accepts_one_exact_usc_filtered_name_and_holds_ambiguity() -> None:
    target = {
        "contact_id": "ct-aaron",
        "organization_id": "org-coursera",
        "company": "Coursera",
        "full_name": "Aaron J. McCullough",
        "title": "VP Product Management",
    }
    exact = RawSearchCandidate(
        name="Aaron McCullough",
        title="Exec Product Leader",
        connection_degree="2nd",
        linkedin_url="https://www.linkedin.com/in/aaronmccullough/?trk=search",
    )
    unrelated = RawSearchCandidate(
        name="Aaron Orellana",
        title="Student",
        linkedin_url="https://www.linkedin.com/in/aaronore/",
    )

    resolved = match_peoplegrove_locator_candidates(target, [exact, unrelated])
    ambiguous = match_peoplegrove_locator_candidates(
        target,
        [
            exact,
            RawSearchCandidate(
                name="Aaron McCullough",
                title="Another person",
                linkedin_url="https://www.linkedin.com/in/aaron-mccullough-2/",
            ),
        ],
    )
    missing = match_peoplegrove_locator_candidates(target, [unrelated])

    assert resolved["status"] == "resolved_exact"
    assert resolved["linkedin_url"] == "https://www.linkedin.com/in/aaronmccullough/"
    assert ambiguous["status"] == "ambiguous_exact"
    assert not ambiguous["linkedin_url"]
    assert missing["status"] == "no_exact_match"


def test_matcher_accepts_explicit_parenthetical_variant_but_not_initial_only_name() -> None:
    nickname_target = {
        "contact_id": "ct-kb",
        "organization_id": "org-coursera",
        "company": "Coursera",
        "full_name": "Ki Bum (KB) Kim",
        "title": "Product",
    }
    nickname_result = match_peoplegrove_locator_candidates(
        nickname_target,
        [
            RawSearchCandidate(
                name="KB Kim",
                title="Product Leader",
                linkedin_url="https://www.linkedin.com/in/kb-kim/",
            )
        ],
    )
    incomplete_result = match_peoplegrove_locator_candidates(
        {**nickname_target, "full_name": "anna T."},
        [
            RawSearchCandidate(
                name="Anna M.",
                title="Product Leader",
                linkedin_url="https://www.linkedin.com/in/anna-m/",
            )
        ],
    )

    assert nickname_result["status"] == "resolved_exact"
    assert nickname_result["matched_name"] == "KB Kim"
    assert incomplete_result["status"] == "ambiguous_exact"
    assert not incomplete_result["linkedin_url"]


def test_state_resumes_and_review_csv_preserves_human_decisions(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    review_path = tmp_path / "review.csv"
    targets = [
        {
            "contact_id": "ct-aaron",
            "organization_id": "org-coursera",
            "company": "Coursera",
            "full_name": "Aaron McCullough",
            "title": "VP Product",
            "priority": "high",
            "source_url": "https://usc.peoplegrove.com/person/1",
        }
    ]
    state = new_peoplegrove_locator_state(workspace=tmp_path / "workspace", targets=targets)
    state["results"]["ct-aaron"] = {
        **targets[0],
        "status": "resolved_exact",
        "linkedin_url": "https://www.linkedin.com/in/aaronmccullough/",
    }
    save_peoplegrove_locator_state(state_path, state)
    write_peoplegrove_locator_review_csv(path=review_path, state=state)
    with review_path.open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    row["review_decision"] = "approved"
    row["review_notes"] = "verified"
    with review_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)

    resumed = load_or_create_peoplegrove_locator_state(
        path=state_path,
        workspace=tmp_path / "workspace",
        targets=targets,
    )
    write_peoplegrove_locator_review_csv(path=review_path, state=resumed)

    assert pending_peoplegrove_locator_targets(resumed) == []
    with review_path.open(encoding="utf-8", newline="") as handle:
        preserved = next(csv.DictReader(handle))
    assert preserved["review_decision"] == "approved"
    assert preserved["review_notes"] == "verified"


def test_apply_requires_exact_status_and_blocks_profile_owned_by_another_contact(
    tmp_path: Path,
) -> None:
    workbook = _workbook(tmp_path)
    workbook.upsert_contact(_peoplegrove_contact())
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-owner",
            organization_id="org-coursera",
            full_name="Existing Owner",
            linkedin_url="https://www.linkedin.com/in/already-owned/",
            source_kind=SourceKind.LINKEDIN,
        )
    )
    state = new_peoplegrove_locator_state(
        workspace=workbook.base_dir,
        targets=[
            {
                "contact_id": "ct-aaron",
                "organization_id": "org-coursera",
                "company": "Coursera",
                "full_name": "Aaron McCullough",
                "title": "VP Product",
                "priority": "high",
                "source_url": "https://usc.peoplegrove.com/person/1",
            }
        ],
    )
    state["results"]["ct-aaron"] = {
        "contact_id": "ct-aaron",
        "organization_id": "org-coursera",
        "company": "Coursera",
        "full_name": "Aaron McCullough",
        "title": "VP Product",
        "status": "resolved_exact",
        "linkedin_url": "https://www.linkedin.com/in/aaronmccullough/",
        "search_mode": "usc_school_and_current_company",
    }

    preview = apply_exact_peoplegrove_locator_results(
        workspace=workbook.base_dir,
        state=state,
        execute=False,
    )
    applied = apply_exact_peoplegrove_locator_results(
        workspace=workbook.base_dir,
        state=state,
        execute=True,
    )

    assert preview["ready_count"] == 1
    assert applied["applied_count"] == 1
    contact = next(item for item in workbook.list_contacts() if item.contact_id == "ct-aaron")
    assert contact.linkedin_url == "https://www.linkedin.com/in/aaronmccullough/"
    assert "method=exact_name_usc_school_and_company_filter" in contact.notes

    workbook.update_contact("ct-aaron", linkedin_url="")
    state["results"]["ct-aaron"]["linkedin_url"] = (
        "https://www.linkedin.com/in/already-owned/"
    )
    blocked = apply_exact_peoplegrove_locator_results(
        workspace=workbook.base_dir,
        state=state,
        execute=False,
    )
    assert blocked["blocked_count"] == 1
    assert blocked["blocked"][0]["reason"] == "linkedin_url_owned_by_other_contact"


def test_apply_reserves_new_profile_urls_within_the_same_batch(tmp_path: Path) -> None:
    workbook = _workbook(tmp_path)
    workbook.upsert_contact(_peoplegrove_contact(contact_id="ct-aaron"))
    workbook.upsert_contact(
        _peoplegrove_contact(contact_id="ct-other", full_name="Other Person")
    )
    targets = [
        {
            "contact_id": "ct-aaron",
            "organization_id": "org-coursera",
            "company": "Coursera",
            "full_name": "Aaron J. McCullough",
            "title": "VP Product",
            "priority": "high",
            "source_url": "https://usc.peoplegrove.com/person/1",
        },
        {
            "contact_id": "ct-other",
            "organization_id": "org-coursera",
            "company": "Coursera",
            "full_name": "Other Person",
            "title": "VP Product",
            "priority": "high",
            "source_url": "https://usc.peoplegrove.com/person/2",
        },
    ]
    state = new_peoplegrove_locator_state(workspace=workbook.base_dir, targets=targets)
    for target in targets:
        state["results"][target["contact_id"]] = {
            **target,
            "status": "resolved_exact",
            "linkedin_url": "https://www.linkedin.com/in/shared-profile/",
            "search_mode": "usc_school_and_current_company",
        }

    result = apply_exact_peoplegrove_locator_results(
        workspace=workbook.base_dir,
        state=state,
        execute=False,
    )

    assert result["ready_count"] == 1
    assert result["blocked_count"] == 1
    assert result["blocked"][0]["reason"] == "linkedin_url_owned_by_other_contact"
    assert result["blocked"][0]["owner_contact_id"] == result["ready"][0]["contact_id"]


def test_apply_requires_company_corroboration_or_explicit_review_and_holds_prior_apply(
    tmp_path: Path,
) -> None:
    workbook = _workbook(tmp_path)
    workbook.upsert_contact(
        _peoplegrove_contact(
            linkedin_url="https://www.linkedin.com/in/aaronmccullough/"
        )
    )
    target = {
        "contact_id": "ct-aaron",
        "organization_id": "org-coursera",
        "company": "Coursera",
        "full_name": "Aaron McCullough",
        "title": "VP Product",
        "priority": "high",
        "source_url": "https://usc.peoplegrove.com/person/1",
    }
    state = new_peoplegrove_locator_state(workspace=workbook.base_dir, targets=[target])
    state["results"]["ct-aaron"] = {
        **target,
        "status": "resolved_exact",
        "linkedin_url": "https://www.linkedin.com/in/aaronmccullough/",
        "matched_title": "Exec Product Leader",
        "search_mode": "usc_school",
        "applied_at": "2026-07-14T09:41:32+00:00",
    }

    held = apply_exact_peoplegrove_locator_results(
        workspace=workbook.base_dir,
        state=state,
        execute=True,
    )

    assert held["ready_count"] == 0
    assert held["review_required_count"] == 1
    assert held["held_existing_count"] == 1
    contact = next(item for item in workbook.list_contacts() if item.contact_id == "ct-aaron")
    assert "locator-review-hold" in contact.target_lists

    approved = apply_exact_peoplegrove_locator_results(
        workspace=workbook.base_dir,
        state=state,
        execute=True,
        review_decisions={"ct-aaron": "approved"},
    )

    assert approved["review_required_count"] == 0
    assert approved["released_existing_count"] == 1
    contact = next(item for item in workbook.list_contacts() if item.contact_id == "ct-aaron")
    assert "locator-review-hold" not in contact.target_lists


def test_apply_accepts_current_company_evidence_in_matched_headline(tmp_path: Path) -> None:
    workbook = _workbook(tmp_path)
    workbook.upsert_contact(_peoplegrove_contact())
    target = {
        "contact_id": "ct-aaron",
        "organization_id": "org-coursera",
        "company": "Coursera",
        "full_name": "Aaron McCullough",
        "title": "VP Product",
        "priority": "high",
        "source_url": "https://usc.peoplegrove.com/person/1",
    }
    state = new_peoplegrove_locator_state(workspace=workbook.base_dir, targets=[target])
    state["results"]["ct-aaron"] = {
        **target,
        "status": "resolved_exact",
        "linkedin_url": "https://www.linkedin.com/in/aaronmccullough/",
        "matched_title": "VP Product at Coursera",
        "search_mode": "usc_school",
    }

    result = apply_exact_peoplegrove_locator_results(
        workspace=workbook.base_dir,
        state=state,
        execute=False,
    )

    assert result["ready_count"] == 1
    assert result["review_required_count"] == 0
