# Relationship Engine

The outreach system should not optimize for sending invites. It should optimize for
building real conversations at companies where Akshat has a credible product/operator
or technical-PM fit.

## Operating Model

There are two parallel motions:

- volume outreach for live applications and opportunistic referrals
- account-based relationship building for a smaller dynamic list of high-fit companies

The second motion is the closed loop. A company stays active until it has either
produced real conversations, gone cold across enough channels, or fallen out of fit.

## Company Account Stages

- `unstarted`: the company is a fit, but no people have been mapped
- `contacts_found`: people exist in the tracker, but no outreach is logged
- `outreach_sent`: at least one invite/message has been sent
- `connected_no_conversation`: a person is connected, but no real reply or chat is logged
- `conversation`: there is a reply, coffee chat, or ongoing exchange
- `champion`: there is a referral, advocate, or strong warm relationship

## Daily Loop

Each daily run should do three things:

1. Process deltas from the prior day: accepted invites, replies, no-contact outcomes,
   and any manual coffee-chat notes.
2. Build the relationship loop artifact to decide which companies need follow-up,
   another LinkedIn wave, email research, or no action yet.
3. Continue normal discovery/apply/outreach volume without letting priority accounts
   disappear after one send.

## First Implemented Slice

`build-relationship-loop` reads the current workbook and writes a ranked artifact.
It does not mutate the workbook yet. That keeps the first slice safe while we test
the strategy on real data.

The artifact includes:

- company fit and account score
- relationship stage
- target relationship gap
- contact, sent invite, connected, and conversation counts
- next action
- reason for the action
- suggested follow-up message when a contact is connected but no conversation exists

## Next Build Slices

- LinkedIn reconcile: detect accepted invites and new replies, then update contact
  statuses and touchpoints.
- Follow-up generator: draft reply-aware LinkedIn follow-ups and coffee-chat asks.
- Persistent action queue: turn planner recommendations into explicit queued actions
  with due dates, owners, and completion status.
- Priority company list: let Akshat manually promote companies into a core account
  list while still allowing the system to suggest new high-fit candidates.
- Email/Wellfound expansion: add non-LinkedIn channels when LinkedIn volume does not
  create conversations.
