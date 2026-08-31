# CampusFlow Synthetic Project

This wholly fictional sample describes a small campus room-booking service and
contains no real organization, student, employee, or client information.

## Approved requirements

- A signed-in student can browse published study rooms and their available time slots.
- A student can reserve one available room for themselves.
- The system prevents overlapping reservations for the same student.
- A facilities coordinator can close a room temporarily and record a reason.
- The system shows reservation status and sends a confirmation email.

## Constraints

- The beta supports one fictional campus and one configured timezone.
- Accounts are provisioned outside CampusFlow.

## Exclusions

- Payments, public registration, and external-calendar synchronization are excluded.

## Assumptions

- A transactional email service will be available.

## Unresolved questions

- How long before a reservation may a student cancel?

## Decisions

- 2026-08-31 | Approve the fictional browser-based room-booking boundary above.
