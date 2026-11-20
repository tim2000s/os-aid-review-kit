const db = {};
const id = 1;
// ruleid: sql-string-interpolation-js
db.query(`SELECT * FROM entries WHERE id = ${id}`);
// ok: sql-string-interpolation-js
db.query('SELECT * FROM entries WHERE id = ?', [id]);

// ruleid: empty-catch-braces
try { JSON.parse('x'); } catch (e) {}
// ok: empty-catch-braces
try { JSON.parse('x'); } catch (e) { console.error(e); }

// ruleid: insecure-http-url
const u = 'http://example.org/api';
// ok: insecure-http-url
const v = 'http://localhost:1337/api';
