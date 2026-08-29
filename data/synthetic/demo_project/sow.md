# StudioLane Statement of Work

## Fictional-project disclaimer

Lantern Vale Creative Cooperative, StudioLane, its users, and every scenario in
this benchmark are wholly fictional and were created solely for the SpecTrace
synthetic evaluation. They do not describe or derive from any real organization,
client, project, person, communication, or private artifact.

## Organization and product overview

Lantern Vale Creative Cooperative operates one fictional community arts center.
It commissions StudioLane as a browser-based reservation portal for registered
members who use its art studios. The portal also gives studio coordinators and
schedule administrators the controls needed to review selected reservations and
maintain the published schedule.

## Project objective

StudioLane will provide a small, auditable MVP for discovering studio sessions,
submitting and managing reservations, applying the approved review path, and
communicating reservation status. The MVP is limited to one location and to
accounts provisioned outside the product.

## User roles

- **Member:** Views studios and sessions, submits a reservation, views current
  and upcoming reservations, and cancels an eligible reservation.
- **Studio coordinator:** Reviews reservations that require manual approval and
  approves or declines them with a short reason.
- **Schedule administrator:** Maintains studio information, session capacity,
  availability, and temporary closures.
- **System:** Validates reservation attempts, prevents invalid reservations,
  records status changes, updates capacity, and sends transactional email.

The term "helper" does not identify an approved role.

## Approved scope

- **SOW-SCP-001 — Browser access:** StudioLane is a browser-based portal for
  pre-provisioned members, studio coordinators, and schedule administrators.
- **SOW-SCP-002 — Sign-in:** Pre-provisioned users can sign in and receive the
  permissions assigned to their approved role.
- **SOW-SCP-003 — Studio listings:** Members can view studio descriptions,
  accessibility notes, scheduled sessions, and remaining capacity.
- **SOW-SCP-004 — Reservation submission:** A signed-in member can submit a
  reservation for one listed studio session for themselves.
- **SOW-SCP-005 — Reservation validation:** Before accepting a reservation, the
  system checks closure state, exhausted capacity, an existing reservation by
  the same member for the session, and conflicts with that member's other
  reservations.
- **SOW-SCP-006 — Ordinary-studio confirmation:** A valid reservation for an
  ordinary studio is automatically confirmed. A later approved decision may
  define a different path for a named studio type.
- **SOW-SCP-007 — Status communication:** The portal displays reservation status
  and sends transactional email when a reservation is confirmed, approved, or
  declined.
- **SOW-SCP-008 — Member cancellation:** A member can cancel an upcoming
  reservation when it satisfies the approved cancellation rule. The precise
  cutoff remains unresolved in SOW-QUE-001.
- **SOW-SCP-009 — Coordinator review:** A studio coordinator can review, approve,
  or decline a reservation when an approved decision requires manual review.
- **SOW-SCP-010 — Schedule administration:** A schedule administrator can manage
  studio descriptions, accessibility notes, session capacity, availability, and
  temporary closures.
- **SOW-SCP-011 — Member reservation view:** A member can view the status and
  details of their current and upcoming reservations.
- **SOW-SCP-012 — Capacity restoration:** After a valid cancellation, the system
  restores one place to that session's available capacity.

## Constraints

- **SOW-CON-001 — Full-session behavior:** When a session has no remaining
  capacity, StudioLane displays it as unavailable and prevents another
  reservation from being submitted.
- **SOW-CON-002 — Initial post-full-session boundary:** The initial SOW approves
  no behavior after a session becomes full beyond displaying it as unavailable
  and blocking further reservations. Availability subscriptions, ordered
  queues, offers, and automatic allocation are neither approved nor explicitly
  excluded by the initial SOW.
- **SOW-CON-003 — Single location:** The MVP operates for one Lantern Vale
  location in one configured timezone.
- **SOW-CON-004 — Human scope control:** A client request does not change approved
  scope unless a human reviewer records an approved decision.

## Explicit exclusions

- **SOW-EXC-001 — Native mobile applications:** Native iOS and Android
  applications are excluded.
- **SOW-EXC-002 — Financial features:** Payments, subscriptions, invoicing, and
  financial calculations are excluded.
- **SOW-EXC-003 — External calendars:** Synchronization with external calendar
  providers is excluded.
- **SOW-EXC-004 — Guest reservations:** Guest reservations and booking on behalf
  of another person are excluded.
- **SOW-EXC-005 — Public account lifecycle:** Public registration, identity
  verification, and account recovery are excluded.
- **SOW-EXC-006 — Additional notification channels:** SMS, mobile push, and
  chat-platform notifications are excluded.
- **SOW-EXC-007 — Multiple locations:** Multi-location scheduling is excluded.

## Assumptions

- **SOW-ASM-001 — Account provisioning:** Member and staff accounts are
  provisioned outside StudioLane.
- **SOW-ASM-002 — Timezone:** All studios and users operate in one configured
  timezone.
- **SOW-ASM-003 — Email service:** A transactional email service is available to
  the application.
- **SOW-ASM-004 — Schedule accuracy:** Schedule administrators maintain accurate
  capacity and closure information.
- **SOW-ASM-005 — Reservation ownership:** Each reservation concerns one member
  and one listed studio session.

These assumptions are not additional requirements and must not be converted
into workflow behavior without supporting approved evidence.

## Unresolved questions

- **SOW-QUE-001 — Cancellation cutoff:** How long before a session begins may a
  member cancel it?
- **SOW-QUE-002 — Availability refresh target:** What measurable refresh or
  response target applies when availability changes?
- **SOW-QUE-003 — Helper role:** Does "helper" mean an existing coordinator, an
  administrator, or a new restricted role?
- **SOW-QUE-004 — History retention:** How long should historical reservation
  records remain visible?
- **SOW-QUE-005 — Email failure:** What should the system do when a transactional
  email cannot be delivered?

Unresolved questions remain open and are not approved capabilities.

## Original approved workflow

1. A pre-provisioned user signs in.
2. A member browses studios and available sessions.
3. The member selects one session and submits a reservation.
4. The system checks closure state, capacity, duplicates, and schedule conflicts.
5. An invalid attempt is rejected with an error; a valid ordinary-studio
   reservation is confirmed.
6. When an approved decision requires review, the system sends the reservation
   to a coordinator, who approves or declines it with a reason.
7. The system displays the resulting status and sends the applicable
   transactional email.
8. A member may cancel an upcoming reservation when the unresolved cancellation
   rule is later defined and satisfied.
9. After a valid cancellation, the system restores one place and communicates
   the cancellation according to current approved decisions.
10. An administrator maintains studio information, session capacity,
    availability, and temporary closures.

At initial approval, a full session is only shown as unavailable and blocks new
reservations. No later full-session lifecycle behavior is initially approved or
explicitly excluded.
