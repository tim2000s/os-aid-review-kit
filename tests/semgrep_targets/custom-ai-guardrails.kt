class Repo(val db: android.database.sqlite.SQLiteDatabase) {
    fun find(id: String) {
        // ruleid: sql-string-interpolation-kotlin-swift
        db.rawQuery("SELECT * FROM bolus WHERE id = $id", null)
        // ok: sql-string-interpolation-kotlin-swift
        db.rawQuery("SELECT * FROM bolus WHERE id = ?", arrayOf(id))
        // ruleid: empty-catch-braces
        try { risky() } catch (e: Exception) {}
        // ok: empty-catch-braces
        try { risky() } catch (e: Exception) { log(e) }
    }

    // ruleid: unfinished-stub
    fun dose(): Double = TODO()

    // ok: unfinished-stub
    // TODO: tidy this later
    fun risky() {}
    fun log(e: Exception) {}
}
