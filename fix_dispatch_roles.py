"""One-off fix: 3 accounts had stale/wrong role values left over from before
dispatch was split into shop_dispatch / godown_dispatch.

    python fix_dispatch_roles.py
"""
import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
cur.execute("UPDATE dim_user SET role='godown_dispatch' WHERE username='godowndispatch'")
cur.execute("UPDATE dim_user SET role='shop_dispatch' WHERE username='shopdispatch'")
cur.execute("UPDATE dim_user SET password_hash='!' WHERE username='dispatch'")
conn.commit()
print("Done. godowndispatch -> godown_dispatch, shopdispatch -> shop_dispatch, dispatch -> disabled.")
conn.close()
