"""Layout metadata independent of card components and data projections."""

CARDS = (
    ("status", "Agent status", "RIGHT NOW"),
    ("activity", "Activity timeline", "THE PAPER TRAIL"),
    ("character", "Character", "PROFILE / STATE"),
    ("threads", "Threads", "CONNECTED IDEAS"),
    ("memory", "Memory", "WHAT STAYS"),
    ("jobs", "Background jobs", "BEHIND THE SCENES"),
    ("runs", "Recent runs", "FROM THE SOURCE"),
)

PAGES = {
    "overview": ("/", "Overview", "Inside the agent", (CARDS[0], CARDS[1], CARDS[2], CARDS[5])),
    "threads": (
        "/threads",
        "Threads",
        "Threads",
        (CARDS[3], ("episodes", "Episodes", "FROM THE SOURCE")),
    ),
    "memory": ("/memory", "Memory", "Memory", (CARDS[4], CARDS[5])),
    "runs": ("/runs", "Runs", "Runs", (("runs", "Runs", "FROM THE SOURCE"),)),
}
