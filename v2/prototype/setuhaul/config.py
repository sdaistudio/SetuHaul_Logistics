"""Policy parameters in one place.

Every number here is a business decision, not a technical one. Each is listed
in the Decision Register (deliverable 2) with the reason and who must sign it
off. Change a value here and the whole system follows; nothing is hard-coded
elsewhere.
"""

# --- Time on the ground (ASSUMPTIONS until the client confirms) -------------
GATE_TO_DOCK_MIN = 15        # gate security + papers + yard drive, for a truck arriving from the road
YARD_TO_DOCK_MIN = 10        # a truck already parked in the yard, once called to a dock
DOCK_CHANGEOVER_MIN = 10     # previous truck pulls out, chocks/leveller reset, next truck reverses in
OVERRUN_BUFFER_MIN = 10      # once an unload passes its expected end, assume it needs this much more

# --- ETA planning buffers by confidence -------------------------------------
# HIGH: driver gave a clock time. MEDIUM: "around", or an unconfirmed plan.
# LOW: "late by an hour", a range, or a failed plausibility check.
ETA_BUFFER_MIN = {"HIGH": 0, "MEDIUM": 15, "LOW": 30}
ETA_OPTIMISTIC_MIN = {"HIGH": 0, "MEDIUM": 10, "LOW": 15}   # how much earlier the truck *might* arrive

# --- Capacity layer ----------------------------------------------------------
OFFER_VALID_MIN = 15          # an option shown to a driver is re-checked if picked after this
APPROVAL_DEADLINE_MAX_MIN = 15   # a hold awaiting a coordinator lasts at most this long ...
APPROVAL_LEAD_BEFORE_START_MIN = 30   # ... and must be decided this long before the planned start
EARMARK_TTL_MIN = 20          # a planner earmark (soft protection, not a promise) expires after this
MAX_OPTIONS_SHOWN = 3         # WhatsApp reply buttons allow 3; a list allows 10
MAX_CLARIFICATIONS = 2        # after two unanswered questions a person takes over

# --- Priority ----------------------------------------------------------------
PRIORITY_RANK = {"CRITICAL": 4, "HIGH": 3, "NORMAL": 2, "LOW": 1}
# Cost of one minute of waiting, by priority. Doubling ladder (8:4:2:1).
# Doc A used 100:40:15:5 and Doc B used 4:2:1:0.5; the planner report shows
# whether the plan changes under each (sensitivity), so the client can choose.
WAIT_WEIGHT = {"CRITICAL": 8, "HIGH": 4, "NORMAL": 2, "LOW": 1}
EXACT_NODE_LIMIT = 200_000     # branch-and-bound budget, counted in nodes (not seconds) so runs are reproducible

# --- Dock compatibility policy -----------------------------------------------
ALLOW_STANDARD_ON_HEAVY_DOCK = False   # "flex dock": needs client decision; planner can show the gain
ALLOW_STANDARD_ON_REEFER_DOCK = False

# --- Duplicate detection -----------------------------------------------------
DUPLICATE_WINDOW_MIN = 10
DUPLICATE_MIN_WORDS = 4      # never deduplicate short answers such as "1" or "yes"

# --- Safety words: these stop all scheduling talk and page the shift manager -
SAFETY_WORDS = ["accident", "injur", "injured", "hurt", "fire", "police", "hospital", "chot", "durghatna",
                "दुर्घटना", "चोट", "आग", "pulis", "aag lag"]
