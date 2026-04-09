# Discovery Strategy

## Recommendation

Do not create a separate sheet for each avenue like YC, hacker houses, incubators,
professors, and job-derived companies. That looks tidy for a week and becomes messy
very quickly.

Use one workbook split by entity:

- `organizations.csv`
- `opportunities.csv`
- `contacts.csv`
- `touchpoints.csv`
- `sources.csv`

Then classify rows with:

- `organization_type`
- `opportunity_type`
- `target_lists`
- `status`

Example `target_lists` values:

- `jobs;referrals`
- `yc;startup`
- `la_startups;founder_outreach`
- `usc;research`
- `hacker_house;summer`

## Why This Grouping Works

It solves two problems at once:

1. You keep one system of record for every target.
2. You can still filter the workbook into the exact slices you care about.

That means one company can belong to multiple tracks without duplication. A startup
found from a YC directory can later also appear in the resume-generator jobs feed and
still remain one organization row with shared contacts and touchpoints.

## Suggested Source Tracks

### 1. Job-driven companies

Owner: resume generator pipeline

Flow:

- job feed discovers opening
- company is added to `organizations.csv`
- opening is added to `opportunities.csv`
- LinkedIn people search adds warm contacts to `contacts.csv`
- generated notes land in `touchpoints.csv`

### 2. Startup and YC discovery

Flow:

- discover startup from directory, batch, or founder list
- add startup to `organizations.csv` with `target_lists=yc;startup`
- add any visible internship or founder-call opening to `opportunities.csv`
- run LinkedIn discovery against the company and add contacts

### 3. Local ecosystem discovery

Examples:

- LA startup ecosystem lists
- SF early-stage startup lists
- accelerator portfolio pages
- incubators and builder communities

These should usually create organization-first records, then branch into LinkedIn or
email outreach based on who is reachable.

### 4. Hacker houses and entrepreneurial programs

These are usually a mix of organization and opportunity:

- organization: the house, residency, or program
- opportunity: summer residency, builder slot, fellowship, stipend, or sponsorship
- contact: organizer, founder, operator, or resident lead

### 5. USC professors and labs

This is person-first outreach but should still map into the same structure:

- organization: USC or the lab
- opportunity: research assistantship, project collaboration, summer role
- contact: professor, lab manager, PhD, research lead
- touchpoint: email-first in most cases

## Channel Strategy

Default channels by target type:

- startups and companies: LinkedIn first, email second
- professors and labs: email first, LinkedIn second
- hacker houses and communities: X, email, or website form plus LinkedIn
- warm referrals: direct LinkedIn message or email, depending on relationship strength

## Build Order

1. Keep the current LinkedIn pipeline as the person-discovery and note-generation engine.
2. Use the workbook as the single local source of truth.
3. Add importers from existing artifacts and the resume-generator job list.
4. Add new source-specific discoverers one at a time:
   startup directories, USC faculty pages, incubator lists, hacker house lists.
5. Add email outreach as another touchpoint channel instead of building a parallel system.
