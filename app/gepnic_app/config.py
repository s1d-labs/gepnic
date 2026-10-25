import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/scrape_hub")
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "6"))
HUB_APP_SECRET_FILE = os.environ.get("HUB_APP_SECRET_FILE", "/etc/gepnic-app/hub.secret")
SIG_WINDOW_S = 60
REPORTS_DIR = os.environ.get("GEPNIC_REPORTS_DIR", "/var/lib/gepnic-app/reports")   # hand-written journal + charts (not in git)

# object storage: R2 holds the bundles (and the pages of older tenders), B2 the documents of newer ones
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
B2_ENDPOINT = os.environ.get("B2_ENDPOINT", "")
B2_REGION = os.environ.get("B2_REGION", "")
B2_BUCKET = os.environ.get("B2_BUCKET", "")
B2_ACCESS_KEY_ID = os.environ.get("B2_ACCESS_KEY_ID", "")
B2_SECRET_ACCESS_KEY = os.environ.get("B2_SECRET_ACCESS_KEY", "")
