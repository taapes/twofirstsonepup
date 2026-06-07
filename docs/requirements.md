✅ FPL Draft Keeper League Website – Finalized Requirements
🎯 Purpose
A public, web-based platform that:
Syncs player data and gameweek stats from the FPL Draft API


Adds custom logic and rules for your Draft Keeper League


Supports administrative control for trades, drafts, IL, and rule enforcement


Tracks league history, gameweek stats, and player eligibility across seasons



🧩 Core Features
📡 FPL API Sync
Pull FPL player pool, manager rosters, and gameweek performance stats


Sync league standings, waiver priority, and player availability


🧾 Roster & Keeper Management
15-man rosters per team


Up to 5 keepers per season (6 if discovery keeper applies)


Max 4 years of keeper eligibility


Waiver keepers limited to 2 (from 2025 onward)


Traded players retain keeper history


Dropped players lose keeper eligibility


🔁 Transfers & Waivers
Waiver period: Start of GW to 24h before next GW


Free Agency: Final 24h before GW start


Auto-enforce waiver limits and eligibility rules


📅 Gameweek History
Capture full league “state” at the start of each gameweek:


Rosters


Points


Standings


Waiver moves


IL status


Anti-tanking flags


🏥 Injury List
One IL player per manager


Min 4 GW stay


Replacement must be same position


Post-GW38 return or waiver required


Admin-managed tracking


🧠 Anti-Tanking Rules
Flag managers with 3+ consecutive 0-minute players


Display infractions on homepage and admin panel


🔄 Trades
Allowed from GW38 end to Jan 31


Allow player-for-player, pick-for-player, or pick-for-pick


Conditional logic (free-text initially)


Update keeper clocks and draft board automatically


🧪 Discovery Draft
Snake format, 2 picks per manager


Held in September


If player joins PL during year → becomes bonus keeper


Only one bonus keeper (6th) allowed if successful


🧠 Drafts & Lottery
Annual draft:


Round 1 = lottery-based (10th = 40%, etc.)


Rounds 2+ = reverse standings


Keepers submitted pre-draft


Discovery draft and main draft logged via admin panel


🏆 Cup Tournaments
Cup (Top 6) and Pup Cup (Bottom 4 + Cup losers)


Start after GW28


Each round covers 2 GWs


Admin sets GWs for each round


Auto-score totals and determine bracket outcomes


💵 Payouts & Penalties
New structure starting 25/26 season


Auto-calculate final payouts based on standings and cup


Last-place fine redistributed to 1st place


🏠 Homepage Contents
League standings


Cup bracket (if active)


Ineligible players report (post-draft additions)


IL player tracker


Manager infractions (anti-tank, IL, ineligible)


Commissioner alerts (admin-posted notices)



🧱 Final Database Schema

🔹 
leagues
Stores each season and associated FPL data.
Field
Type
Notes
id
UUID
Primary key
fpl_league_id
String
External FPL ID
name
String
“FPL Draft 25/26”
season_year
Integer
e.g., 2025
draft_date
Date
For eligibility cutoff


🔹 
managers
Field
Type
Notes
id
UUID
Primary key
league_id
UUID
FK → leagues
fpl_manager_id
String
External ID from FPL API
name
String
Manager name
email
String
For notifications


🔹 
players
Field
Type
Notes
id
UUID
Primary key
fpl_id
Integer
External FPL player ID
name
String
Player name
position
String
GK, DEF, MID, FWD
current_team
String
Club name
status
String
From FPL (injured, suspended, etc.)
fpl_added_date
Date
When player was added to the FPL system
is_eligible
Boolean
False if added after league draft date


🔹 
gameweeks
Field
Type
Notes
id
Integer
GW number (1–38)
league_id
UUID
FK → leagues
start_date
Date
From FPL
end_date
Date
From FPL
is_locked
Boolean
Roster lock


🔹 
rosters
Tracks rostered players by manager and gameweek.
Field
Type
Notes
id
UUID
Primary key
manager_id
UUID
FK → managers
player_id
UUID
FK → players
gameweek_id
Integer
FK → gameweeks
source
String
‘drafted’, ‘waiver’, ‘trade’
keeper_years
Integer
0–4
original_year
Integer
When first acquired
is_keeper
Boolean
Active keeper status
is_discovery
Boolean
Part of discovery draft?


🔹 
transactions
Field
Type
Notes
id
UUID
Primary key
league_id
UUID
FK → leagues
gameweek_id
Integer
FK → gameweeks
manager_id
UUID
FK → managers
player_id
UUID
FK → players
type
String
‘waiver’, ‘free_agent’, ‘trade’
action
String
‘add’, ‘drop’
priority
Integer
If applicable
notes
Text
For conditions or context


🔹 
trades
Field
Type
Notes
id
UUID
Primary key
date
Date
Date of trade
league_id
UUID
FK → leagues
from_manager
UUID
FK → managers
to_manager
UUID
FK → managers
player_id
UUID
FK → players
draft_pick
String
Optional (e.g., “2026-R3”)
conditions
Text
Free-text conditions


🔹 
injury_list
Field
Type
Notes
id
UUID
Primary key
player_id
UUID
FK → players
manager_id
UUID
FK → managers
start_gw
Integer
Min 4 GW stay
end_gw
Integer
Nullable
replacement_id
UUID
FK → players
status
String
‘active’, ‘returned’, ‘waived’


🔹 
keeper_exceptions
For discovery draft success.
Field
Type
Notes
player_id
UUID
FK → players
manager_id
UUID
FK → managers
league_id
UUID
FK → leagues
validated_gw
Integer
When FPL added
is_valid
Boolean
True if successful discovery


🔹 
draft_picks
Field
Type
Notes
id
UUID
Primary key
round
Integer
Round of draft
pick_number
Integer
Within round
manager_id
UUID
FK → managers
player_id
UUID
FK → players
league_id
UUID
FK → leagues
source
String
‘draft’, ‘keeper’, ‘discovery’


🔹 
draft_lottery
Field
Type
Notes
id
UUID
Primary key
league_id
UUID
FK → leagues
manager_id
UUID
FK → managers
odds
Float
Assigned %
pick_result
Integer
Final order


🔹 
gameweek_points
Field
Type
Notes
manager_id
UUID
FK → managers
gameweek_id
Integer
FK → gameweeks
total_points
Integer
GW score
player_points
JSON
Map of player_id → points


🔹 
tournaments
Field
Type
Notes
id
UUID
Primary key
name
String
‘Cup’, ‘Pup Cup’
league_id
UUID
FK → leagues
start_gw
Integer
Start round
end_gw
Integer
End round


🔹 
tournament_matches
Field
Type
Notes
id
UUID
Primary key
tournament_id
UUID
FK → tournaments
round
Integer
1 = QF, 2 = SF, etc.
manager_a
UUID
FK → managers
manager_b
UUID
FK → managers
score_a
Integer
2-week total
score_b
Integer
2-week total
winner_id
UUID
FK → managers


🔹 
commissioner_alerts
Field
Type
Notes
id
UUID
Primary key
league_id
UUID
FK → leagues
message
Text
Markdown or HTML content
created_at
Date
Timestamp



