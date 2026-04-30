from __future__ import annotations

import datetime as dt

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_URL = "https://auth.openai.com/oauth/token"
DEFAULT_REFRESH_MARGIN = dt.timedelta(hours=12)
DEFAULT_LAST_REFRESH_MAX_AGE = dt.timedelta(days=3)
