# StudioLane Approved Decision History

## Decision-memory rule

This history belongs to the wholly fictional StudioLane benchmark. Raw client
requests are evidence of what was asked, but they do not update approved scope.
Approved scope changes only when a human reviewer records an approved decision.
An analysis recommendation, request sequence, or unresolved discussion is not
an approval.

## DEC-001 — Approve the original MVP boundary

- **Date:** 2026-05-04
- **Status:** APPROVED
- **Triggering request ID:** None; initial project approval
- **Evidence affected:** SOW-SCP-001 through SOW-SCP-012, SOW-CON-001 through
  SOW-CON-004, SOW-EXC-001 through SOW-EXC-007, SOW-ASM-001 through
  SOW-ASM-005, and SOW-QUE-001 through SOW-QUE-005
- **Approves:** The scope, constraints, exclusions, assumptions, unresolved
  questions, and original workflow recorded in the StudioLane SOW.
- **Does not approve:** Any capability outside that evidence, including any
  post-full-session behavior not stated in the original workflow. This absence
  of approval is not an explicit rejection or exclusion.
- **Supersession:** None.
- **Remaining unresolved:** All SOW-QUE items remain unresolved.

## DEC-002 — Reject external-calendar synchronization

- **Date:** 2026-05-07
- **Status:** APPROVED_REJECTION
- **Triggering request ID:** None; pre-sequence stakeholder proposal
- **Evidence affected:** SOW-EXC-003
- **Approves:** Continued browser-based scheduling without an external-calendar
  integration.
- **Rejects:** Synchronization, event creation, or data exchange with any
  external calendar provider.
- **Supersession:** None. SOW-EXC-003 remains active.
- **Remaining unresolved:** None for the rejection itself.

## DEC-003 — Require review for ceramic-kiln reservations

- **Date:** 2026-05-10
- **Status:** APPROVED
- **Triggering request ID:** None; pre-sequence operational review
- **Evidence affected:** SOW-SCP-006 and SOW-SCP-009
- **Approves:** Valid ceramic-kiln reservations enter PENDING_REVIEW. A studio
  coordinator approves or declines each one with a short reason, after which the
  member receives the applicable transactional email.
- **Rejects:** Automatic confirmation of ceramic-kiln reservations.
- **Supersession:** Supersedes SOW-SCP-006 only for ceramic-kiln sessions.
  Automatic confirmation remains current for ordinary studios.
- **Remaining unresolved:** None for the routing decision.

## DEC-004 — Confirm valid-cancellation effects

- **Date:** 2026-05-12
- **Status:** APPROVED
- **Triggering request ID:** None; pre-sequence workflow clarification
- **Evidence affected:** SOW-SCP-008, SOW-SCP-012, and SOW-QUE-001
- **Approves:** After a cancellation is determined to be valid, the system
  records the cancellation, restores one place to the session, and sends a
  transactional cancellation email to the member.
- **Does not approve:** Cancellation outside the eventual approved cutoff.
- **Supersession:** Clarifies the effects of SOW-SCP-008 and SOW-SCP-012 without
  replacing either item.
- **Remaining unresolved:** SOW-QUE-001, the cancellation cutoff, remains open.

## DEC-005 — Approve limited availability alerts

- **Date:** 2026-05-21
- **Status:** APPROVED_WITH_OPEN_DETAILS
- **Triggering request ID:** CR-006
- **Evidence affected:** SOW-CON-001, SOW-CON-002, SOW-SCP-007, and
  SOW-QUE-005
- **Approves:** A member may opt in to an email alert for a full session. The
  subscription is stored, and an availability-change event may trigger an email
  when that session changes from full to having capacity.
- **Does not approve and does not reject:** Queue ordering, priority entitlement,
  reservation holds, timed offers, automatic reservations, or automatic
  promotion. A later request for one of these capabilities therefore requires
  scope review rather than being treated as a contradiction of DEC-005.
- **Supersession:** Partially supersedes SOW-CON-002 only by adding an opt-in
  availability-alert lifecycle. SOW-CON-001 continues to block reservations
  while capacity is exhausted.
- **Remaining unresolved:** Subscription expiration and removal, failed-email
  handling, and detailed behavior under simultaneous capacity changes.

## DEC-006 — Approve a limited ordered queue

- **Date:** 2026-05-23
- **Status:** APPROVED_WITH_OPEN_DETAILS
- **Triggering request ID:** CR-007
- **Evidence affected:** DEC-005, SOW-CON-001, and SOW-CON-002
- **Approves:** A member may join or leave a persistent queue for a full session.
  Queue order is first-joined, and the first queued member is selected for the
  first availability notification when capacity becomes available.
- **Does not approve and does not reject:** Holding capacity for a notified
  member, creating a reservation without member action, guaranteeing a place,
  skipping a member automatically, or automatically promoting a queued member.
  A later request for one of these capabilities therefore requires scope review
  rather than being treated as a contradiction of DEC-006.
- **Supersession:** Supersedes DEC-005's unordered alert-recipient behavior.
  DEC-005's email channel remains approved, but queue order determines the first
  notification recipient. SOW-CON-002 is further superseded only for the
  approved queue and notification behavior.
- **Remaining unresolved:** Notification expiry, removal or retention after a
  notification, handling an unresponsive first member, and when a later queued
  member may be notified.
