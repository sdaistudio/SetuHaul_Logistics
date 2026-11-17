# Client readout (hand-in #6)

*For: Head of Operations and the Jaipur warehouse planner. Plain language, one page.*

## What we built

A WhatsApp assistant for drivers who are running late or early. It understands English, Hindi and Hinglish, asks at most one or two short questions, and offers dock slots that the truck can really make. When the driver picks one, the system checks it again and books it in the dock schedule. If no safe answer exists, it hands the case to your coordinator with everything they need.

## Policy it follows

1. **A solver decides, not AI.** For every request it tries every way the waiting trucks could be docked, scores each with rules your planner can read and change (one YAML file), and picks the proven best. Same situation, same answer, every time. We checked it against Google's OR-Tools solver: identical results on every test.
2. **The computer decides capacity, not the chat.** Every slot offered passes the same rules your planner uses: right dock type, weight limit, dock not broken, truck can arrive in time, nothing else on the dock.
3. **Seeing a slot is not owning it.** Two drivers can see the same slot. The first one who chooses gets it; the other is told straight away and shown the next options.
4. **Priority comes from the shipment record**, never from what the driver says. "Urgent" in a message changes nothing.
5. **Promises are kept.** A truck that can still make its booking keeps it. Moving a confirmed truck to make room for a critical one is proposed to your coordinator, never done automatically.
6. **A person decides** when: someone may be hurt, no slot fits, the driver asks for a person, unloading would start after 21:00, the ETA is too vague, or the records contradict each other.

## What we measured on the 4 August Jaipur replay

- 12 driver conversations, median **2 minutes and 2 messages** to a usable outcome.
- **0 double bookings**, also under a test of 10 drivers grabbing one slot at the same instant.
- 1 of 26 offered slots was gone by the time the driver chose; the driver was told and re-offered.
- Looking ahead across all trucks avoided one escalation: a driver with a 13:30 deadline kept the only slot that worked for him, because the solver steered an earlier driver to a different dock.
- The one remaining "no slot" case (reefer dock in maintenance) reached a coordinator with a complete record.
- 3 clarification questions across 12 conversations.
- Language understanding: 100% on our test set, **80% on unseen messages** — the remaining 20% get a clarifying question or go to a person, never a wrong booking.

## Location without paying for constant GPS

We ask for a truck's position only when it would change the plan (6 trucks with uncertain arrival at 09:50; 1 position worth buying). GPS first, FASTag toll data second (works in dead zones and when GPS is switched off). If a truck's next toll crossing is overdue and its GPS is silent, a coordinator gets a review alert.

## What we found in your data (fix these first)

- Two trucks are booked on docks rated below their load (SHP1005 on D3, SHP1014 on D1).
- Dock D3's breakdown starts at 09:15, but its 09:00 slot still shows as open with a truck booked.
- Many unloads (75 min) are longer than their slots (60 min), so delays roll into the next truck.
- One driver appears on two moving trucks; the Gurugram night-shift contact is vacant with no email.

## Limits and open risks

- No GPS: we rely on what drivers say. We plan for the late end of their estimate.
- The demo runs one facility at a time on a single database writer. Production should move to the managed PostgreSQL design (included) before go-live across 6 facilities.
- Hindi and Hinglish phrasing varies widely; we will keep adding real messages to the test set every week.
- Unload times are estimates; overruns still ripple. The planning tool proposes resequencing but a person approves it.

## Decisions we need from you

1. Gate-to-dock time per facility (we assumed 15 minutes).
2. Can standard loads ever use the heavy or reefer bay when idle?
3. Who approves starts after 21:00, and within how many minutes?
4. Should a critical shipment be allowed to bump a normal one, and who tells the bumped driver?
