func load() {
    // ruleid: empty-catch-braces
    do { try risky() } catch {}
    // ok: empty-catch-braces
    do { try risky() } catch { print(error) }
}

func dose() -> Double {
    // ruleid: unfinished-stub
    fatalError("Not implemented")
}

func risky() throws {}
