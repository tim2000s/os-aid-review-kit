import sqlite3

conn = sqlite3.connect("x.db")
cur = conn.cursor()
user = "u"

# ruleid: insecure-http-url
NS = "http://mysite.herokuapp.com/api/v1"
# ok: insecure-http-url
LOCAL = "http://127.0.0.1:1337"
# ruleid: hardcoded-https-url
NS2 = "https://nstest99.chickenkiller.com/api/v1/entries.json"
# ruleid: hardcoded-ip-address
HOST = "35.212.238.205"
# ok: hardcoded-ip-address
LOOP = "127.0.0.1"

# ruleid: sql-string-interpolation-python
cur.execute(f"SELECT * FROM t WHERE u = '{user}'")
# ruleid: sql-string-interpolation-python
cur.execute("SELECT * FROM t WHERE u = '%s'" % user)
# ok: sql-string-interpolation-python
cur.execute("SELECT * FROM t WHERE u = ?", (user,))

# ruleid: empty-catch-python
try:
    cur.execute("SELECT 1")
except Exception:
    pass

# ok: empty-catch-python
try:
    cur.execute("SELECT 1")
except Exception as e:
    print(e)

# ruleid: nightscout-secret-literal
API_SECRET = "abcdefghij123456"
# ruleid: nightscout-secret-literal
URL = "/api/v1/entries.json?token=demo1234-0123456789abcdef"
# ok: nightscout-secret-literal
API_SECRET_ENV = "API_SECRET"

bg = 100
# ruleid: inline-mmol-conversion
mmol = bg / 18.018
# ruleid: inline-mmol-conversion
mgdl = 5.5 * 18
# ok: inline-mmol-conversion
high = bg > 180
