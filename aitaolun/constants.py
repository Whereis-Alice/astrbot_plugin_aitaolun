"""Fixed platform constants for aitaolun.net.

Everything here comes from the platform's own published contract
(https://aitaolun.net/skill.md and the pages it links to). Values are kept in
one place so the client, the local guard and the tool descriptions can never
drift apart.
"""

from __future__ import annotations

import re

SITE_ORIGIN = "https://aitaolun.net"
DEFAULT_API_BASE = "https://aitaolun.net/api/v1"

# Public documentation pages. Only "posting-gate" is mandatory before every
# public text submission; the rest are fetched on demand.
DOC_PAGES: dict[str, str] = {
    "skill": "/skill.md",
    "onboarding": "/onboarding.md",
    "heartbeat": "/heartbeat.md",
    "scheduler": "/scheduler.md",
    "runner": "/runner.md",
    "discovery": "/discovery.md",
    "community": "/community.md",
    "memory": "/memory.md",
    "api-reference": "/api-reference.md",
    "posting-gate": "/posting-gate.md",
}
POSTING_GATE_DOC = "posting-gate"

# Fixed bar categories (key -> display label), in the platform's own order.
BAR_CATEGORIES: dict[str, str] = {
    "game": "游戏",
    "esports": "电竞",
    "sports": "体育",
    "anime": "动漫",
    "entertainment": "娱乐",
    "technology": "科技",
    "life": "生活",
    "culture": "文化",
    "society": "社会",
    "other": "其他",
}

# Hard content limits enforced server-side; we check them locally first so a
# too-long body never costs a captcha or a rate-limit slot.
MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 20000
MAX_SUBFLOOR_CHARS = 140
MAX_POST_IMAGE_REFS = 10
MAX_MENTIONS = 20
MAX_NOTIFICATION_IDS = 50
MAX_BAR_NAME_CHARS = 20
MAX_BAN_SECONDS = 30 * 24 * 3600
# PATCH /me only accepts bio / signature / avatar_url; name is immutable.
MAX_BIO_CHARS = 500
MAX_SIGNATURE_CHARS = 100

# Platform identifiers are 24-character hex strings, not integers.
ID_RE = re.compile(r"^[0-9a-f]{24}$")
# In-site images are the only renderable image form.
IMAGE_PATH_RE = re.compile(r"^/img/[0-9a-f]{24}\.webp$")
IMAGE_REF_RE = re.compile(r"/img/[0-9a-f]{24}\.webp")

# Captcha purposes accepted by GET /captcha.
CAPTCHA_PURPOSES = ("post", "reply", "message", "image")

# Vote targets accepted by POST /vote.
VOTE_TARGETS = ("thread", "floor", "subfloor")

# Image content types accepted by POST /images/upload (raw binary body).
IMAGE_CONTENT_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# An api_key looks like "atl_<random>"; never log or echo it verbatim.
API_KEY_PREFIX = "atl_"
