"""The fixed set of sections every issue is organised into.

Keys are stable identifiers (stored on subscribers and in digests); labels are what readers see.
Order here is the order sections appear in the email.
"""

CATEGORIES: dict[str, dict[str, str]] = {
    "ai": {
        "label": "AI",
        "blurb": "Models, research, and the companies building them.",
        "scope": "Model releases, AI research results, AI lab business moves, AI products, "
        "compute deals for AI, notable AI failures or controversies.",
    },
    "big_tech": {
        "label": "Big Tech",
        "blurb": "Apple, Google, Microsoft, Amazon, Meta and friends.",
        "scope": "Strategy, earnings, product launches, leadership changes, and antitrust "
        "matters at the largest tech companies (when the story is not primarily about AI).",
    },
    "startups": {
        "label": "Startups & Money",
        "blurb": "Funding, acquisitions, IPOs, and companies to watch.",
        "scope": "Venture rounds of real significance (roughly $50M+ or unusually notable), "
        "M&A, IPOs, shutdowns, layoffs, and market moves in tech stocks.",
    },
    "dev": {
        "label": "Developers",
        "blurb": "Languages, tools, open source, and infrastructure.",
        "scope": "Programming languages, frameworks, developer tools, open-source projects and "
        "licensing, cloud and infrastructure, major outages and post-mortems.",
    },
    "security": {
        "label": "Security & Privacy",
        "blurb": "Breaches, vulnerabilities, and surveillance.",
        "scope": "Major breaches, actively exploited vulnerabilities, ransomware, state-backed "
        "hacking, privacy rulings, and surveillance tech.",
    },
    "hardware": {
        "label": "Chips & Hardware",
        "blurb": "Semiconductors, devices, and the supply chain.",
        "scope": "Semiconductors and fabs, consumer devices, data-center hardware, EV and "
        "battery tech when it is a technology story, export controls on chips.",
    },
    "policy": {
        "label": "Policy",
        "blurb": "Regulation, courts, and governments.",
        "scope": "Legislation, regulation, court rulings, and government action that affect "
        "tech companies or technology use, in any country.",
    },
    "science": {
        "label": "Science & Space",
        "blurb": "Research and spaceflight with a tech angle.",
        "scope": "Spaceflight, quantum computing, biotech and climate tech with a clear "
        "technology angle, notable research papers outside AI.",
    },
}

CATEGORY_KEYS = list(CATEGORIES.keys())


def label(key: str) -> str:
    return CATEGORIES.get(key, {}).get("label", key)
