import os

API_BASE = os.getenv("FPL_API_BASE", "https://draft.premierleague.com/api")
LEAGUE_ID = os.getenv("FPL_DRAFT_LEAGUE_ID")  # set in Render Environment
