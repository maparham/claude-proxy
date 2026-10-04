# Note: ticket periods vs. Anthropic's weekly window

- **Date:** 2026-10-03
- **Relates to:** `2026-10-03-paid-tickets-design.md`, sections 5 and 7.
- **Status:** decision record. The paid-tickets design enforces tickets day by day. This note keeps the problem and the alternative, prorating by window overlap, for when the day rule is revisited.

## 1. The problem

Anthropic's weekly limit is one budget that resets at a fixed moment every week. A user's weekly share is how much of the current week's budget they have used since that reset.

A ticket runs on its own dates. A week ticket bought on a Wednesday runs Wednesday to Tuesday. The capacity check puts two such tickets in the same slice one after the other, because they never overlap in time.

Take the Anthropic week that contains the handover. Ticket A is active for its first two days and may spend its full share of that week's budget in those two days. Ticket B starts on the Wednesday with a fresh counter and may also spend its full share of the same week's budget. A slice reserved as 5% can draw 10% of that week.

The capacity rule, "at every moment the active shares sum to at most 100", is true throughout. The budget is spent per Anthropic week, not per moment, so the rule checks the wrong unit.

How far it can go:

| Scenario | Reserved | Can be drawn from one week |
|---|---|---|
| One Lite slice, A hands to B on a Wednesday | 5% | 10% |
| Four Standard slices, all handing over on a Wednesday | 100% | 200% |
| One Lite slice filled with seven day tickets, each at the full share | 5% | 35% |
| One Lite slice filled with seven day tickets, each at one seventh of the share | 5% | 5% |

The last row is the rule the first draft of the design already had for day tickets. Week and month tickets have the same problem whenever they start on any day but the reset day.

The 5-hour bucket has the same shape: a handover at 14:00 lets both tickets draw their share from the 12:00–17:00 window. A 5-hour window is a tiny part of any ticket, so this is accepted in both approaches below.

## 2. The approaches

### A. Prorate by window overlap (kept for later)

Give each ticket, inside each Anthropic week, a weekly share equal to its slice share times the fraction of that week the ticket covers:

> weekly cap in this window = slice share × (days of this window the ticket covers) ÷ 7

The share stays measured the way it is today, against Anthropic's current weekly window; only the cap moves.

Worked example, a Lite week ticket running Wednesday to Tuesday with a Monday reset:

| Anthropic week | Days covered | Weekly cap |
|---|---|---|
| First | Wed, Thu, Fri, Sat, Sun | 5% × 5/7 = 3.57% |
| Second | Mon, Tue | 5% × 2/7 = 1.43% |
| Total over the ticket | 7 days | 5.00% |

Tickets in one slice never overlap in time, so their overlaps with any one Anthropic week add up to at most seven days and their prorated caps to at most the slice share. The slice can never draw more than it was reserved for.

A ticket bought at the reset gets its full share in one week, as before. A day ticket inside one week gets one seventh, which is the old day rule; proration generalises it.

The buyer still gets 5% of a week in total, but shaped to Anthropic's calendar rather than their own: in the example they can spend 3.57% any time in the first five days and 1.43% any time in the last two. They cannot carry the unused part of the first week into the second.

### B. Day as the unit (chosen)

Every ticket is a run of consecutive 24-hour days from its start. Each day gets one seventh of the slice share, measured from the user's own weighted tokens since that day began, converted to weekly-bucket points at the account's observed rate (tokens per point). Unused share does not carry over.

Why not the share attributed to the user's requests, as first planned: Anthropic reports utilization in whole-percent steps, and a Lite day is 0.71 points, smaller than one step. Attribution would read 0 for most of a day and then charge a whole point at once. The observed rate averages over many steps, so the day fills smoothly.

Any Anthropic week holds at most seven days of a slice, so the slice draws at most its share from that week, however its tickets turn over. The capacity check stays per moment and stays correct.

The buyer gets 0.71% of the weekly budget each day on a Lite ticket. They cannot have one heavy day.

### C. Align tickets to the reset (rejected)

Week tickets start only at Anthropic's reset and queue until then. A buyer gets a clean full share of one real week. It gives up selling a ticket that starts now and does nothing for month tickets, which span several resets anyway.

### D. Sell headroom (rejected)

Keep full-share caps and sell fewer slices, say 60–70% of the account, so turnover overshoot fits in the slack. Easy to explain, wastes capacity, and the overshoot is unbounded in the worst case (every slice turning over in the same week).

## 3. Comparison

For a Lite week ticket running Wednesday to Tuesday, Monday reset:

| | First draft | A. Prorate | B. Day unit |
|---|---|---|---|
| Can draw from one week, per slice | up to 2× share | share | share |
| Buyer may spend | 5% in each of two weeks | 3.57% any time Wed–Sun, 1.43% any time Mon–Tue | 0.71% each day |
| Flexibility inside the ticket | full | per window piece | per day |
| How usage is measured | share of Anthropic's current week | share of Anthropic's current week | own tokens since the day began, at the observed rate |
| Reset shown to the user | Anthropic's weekly reset | Anthropic's weekly reset | end of the current ticket day |
| Day ticket | special case (÷ 7) | falls out of the formula | is the unit |
| Bonus time | any number of seconds | any number of seconds | whole days |
| Protects the account from one user's burst | no | partly | yes |
| Honesty of "≈ Claude Pro" | overstates | close, with the calendar caveat | understates: Pro's week spread evenly over days |

## 4. Why the day was chosen

- It removes the day-ticket special case instead of generalising it.
- It is the easiest rule to tell a buyer: so many hours a day.
- It smooths usage, so one user cannot burn a week's worth in an evening while others on the account wait.
- Bonus time, queued tickets and the pricing page's estimate all become "days".

What it costs: a buyer cannot bank quiet days for a heavy one, and the comparison to Anthropic's own plans is weaker, since those let a subscriber spend the weekly budget on any day.

## 5. When to revisit

- **Buyers ask for flexibility.** Allow unused daily share to roll forward, but only within the same Anthropic week. Rolling across the reset brings the overshoot back. Rolling within the week is exactly approach A, so the two designs meet there and the switch is a change of cap, not of model.
- **Measurement.** Approach A needs nothing new: the share of Anthropic's current week is what the gateway measures today. Approach B needs usage over an arbitrary period; attribution over a day proved too coarse (whole-percent steps), so B converts the user's own tokens at the observed rate instead. If that rate turns out to be unreliable, A is the fallback.
- **The 5-hour bucket.** If handover overshoot there ever matters, the same proration applies: share × (hours of the current 5-hour window the ticket covers) ÷ 5.
