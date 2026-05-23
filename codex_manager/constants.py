from __future__ import annotations

import datetime as dt

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_URL = "https://auth.openai.com/oauth/token"
CHATGPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DEFAULT_REFRESH_MARGIN = dt.timedelta(hours=12)
DEFAULT_LAST_REFRESH_MAX_AGE = dt.timedelta(days=3)
DEFAULT_MAINTAIN_INTERVAL = "6h"
DEFAULT_RANDOMIZED_DELAY = "10min"
DEFAULT_MONITOR_INTERVAL = "5min"
DEFAULT_HISTORY_RETENTION_DAYS = 90
