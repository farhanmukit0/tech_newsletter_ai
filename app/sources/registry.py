"""Where candidate stories come from.

Curated on purpose: a short list of outlets with real reporting beats a long list of
rewrite mills. Add or remove feeds here; collectors skip any that fail.
"""

# (name, feed url, default weight). Weight nudges ranking for outlets that break news.
RSS_FEEDS: list[tuple[str, str, float]] = [
    # General tech reporting
    ("The Verge", "https://www.theverge.com/rss/index.xml", 1.0),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", 1.1),
    ("TechCrunch", "https://techcrunch.com/feed/", 1.0),
    ("Wired", "https://www.wired.com/feed/rss", 1.0),
    ("The Register", "https://www.theregister.com/headlines.atom", 0.9),
    ("Engadget", "https://www.engadget.com/rss.xml", 0.8),
    ("404 Media", "https://www.404media.co/rss/", 1.0),
    ("Rest of World", "https://restofworld.org/feed/latest/", 0.9),
    ("MIT Technology Review", "https://www.technologyreview.com/feed/", 1.0),
    ("IEEE Spectrum", "https://spectrum.ieee.org/feeds/feed.rss", 0.9),
    # AI
    ("OpenAI", "https://openai.com/news/rss.xml", 1.2),
    ("Google AI", "https://blog.google/technology/ai/rss/", 1.0),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml", 0.8),
    ("Simon Willison", "https://simonwillison.net/atom/everything/", 0.9),
    # Developers
    ("GitHub Blog", "https://github.blog/feed/", 0.8),
    # Security
    ("BleepingComputer", "https://www.bleepingcomputer.com/feed/", 1.0),
    ("Krebs on Security", "https://krebsonsecurity.com/feed/", 1.1),
    ("The Hacker News", "https://feeds.feedburner.com/TheHackersNews", 0.8),
    # Hardware
    ("Tom's Hardware", "https://www.tomshardware.com/feeds/all", 0.8),
    # Science & space
    ("SpaceNews", "https://spacenews.com/feed/", 0.9),
]

# Subreddits whose top posts are a decent signal of what practitioners are talking about.
SUBREDDITS: list[str] = [
    "technology",
    "MachineLearning",
    "LocalLLaMA",
    "programming",
    "hardware",
    "netsec",
]

# YouTube channels (name, channel id). Videos get transcribed on demand by the editor agent.
YOUTUBE_CHANNELS: list[tuple[str, str]] = [
    ("Fireship", "UCsBjURrPoezykLs9EqgamOA"),
    ("Two Minute Papers", "UCbfYPyITQ-7l4upoX8nvctg"),
    ("Marques Brownlee", "UCBJycsmduvYEL83R_U4JriQ"),
    ("Andrej Karpathy", "UCXUPKJO5MZQN11PqgIvyuvQ"),
    ("Y Combinator", "UCcefcZRL2oaA_uBNeo5UOWg"),
    ("Lex Fridman", "UCSHZKyawb77ixDdsGog4iWA"),
    ("AI Explained", "UCNJ1Ymd5yFuUPtn21xtRbbw"),
    ("Theo - t3.gg", "UCbRP3c757lWg9M-U7TyEkXA"),
]

# Hacker News: minimum points for a story to be considered.
HN_MIN_POINTS = 100
