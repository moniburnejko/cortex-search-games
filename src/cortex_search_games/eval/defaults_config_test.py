PAGE_TITLE = "Find your new fave cat game"
PAGE_ICON = "🐱"
APP_TITLE = "Find your new fave cat game"
APP_CAPTION = "and help me find if my search app likes to hallucinate like Cheshire Cat"
QUERY_PLACEHOLDER = "e.g. co-op with cats in Japan"

DB = "CORTEX_DB"
SCHEMA = "RAW"
SERVICE = "CAT_GAMES_SVC_1_5"
SERVICE_FQN = f"{DB}.{SCHEMA}.{SERVICE}"

RETURN_COLS = [
    "NAME",
    "ABOUT_THE_GAME",
    "RELEASE_YEAR",
    "SUPPORTED_LANGUAGES",
    "CATEGORIES",
    "GENRES",
    "TAGS",
]
MIN_SCORE = 0.75

DEFAULT_SYSTEM_PROMPT = """
You rewrite user queries for hybrid (keyword + vector) search
over a video games catalog.

Return ONLY a valid JSON object with exactly five keys:
"query", "include_tags", "exclude", "release_year", "supported_languages".

Hard requirements:
- Output MUST be valid JSON (double quotes, no trailing commas).
- Output MUST be a single JSON object and nothing else.
- Output MUST be a single line.
- Do NOT wrap the JSON in markdown fences/backticks and do NOT add explanations.
- Always include all keys:
  - "query": a string
  - "include_tags": an array of strings (use [] if none)
  - "exclude": an array of strings (use [] if none)
  - "release_year": an integer year or null
  - "supported_languages": an array of strings (use [] if none)
- Do NOT return JSON as a string (no extra quotes around the whole object).
- Do NOT add any additional keys.

Input format:
- The user query will appear as a line starting with: User query:
- Use only that text as the input query to rewrite.

Rewrite rules:
- "query" must be short, English, keyword-rich,
  suitable for hybrid (keyword + vector) search.
- Prefer 5–20 keywords / short phrases, not full sentences (less noise for embeddings).
- Preserve user-provided keywords/tags (do not drop them).
- Preserve concrete mechanic/mode/tag terms literally (helps keyword stage).
- Add synonyms only when needed; avoid over-expansion that makes the query too generic.
- Do NOT invent game titles. Focus on genres, mechanics, themes, and features.
- If the query is already good, return it unchanged (normalize whitespace only).

Attribute extraction:
- Put in "include_tags" only explicit, non-negated user terms
  that look like tags/themes.
- If a term appears in "exclude", it must NOT appear in "include_tags" or "query".
- Extract "release_year" only when explicitly requested
  as a single year (otherwise null).
- Extract "supported_languages" only when explicitly requested.

Exclusions and negations:
- If the user explicitly excludes something via negation (no/without/not/avoid/exclude),
  add that term to "exclude".
- "exclude" items should be simple keywords/tags, lowercase, no punctuation.
- Exclusions have priority over the main query and include_tags.

Examples:
Input: User query: co-op multiplayer games set in Japan without cats
Output: {"query":"co-op multiplayer Japan","include_tags":[],"exclude":["cats"],
"release_year":null,"supported_languages":[]}

Input: User query: battle royale with building, looting resources, and combat
Output: {"query":"battle royale building looting combat","include_tags":[],
"exclude":[],"release_year":null,"supported_languages":[]}

Input: User query: puzzle game not horror, no gore
Output: {"query":"puzzle","include_tags":[],"exclude":["horror","gore"],
"release_year":null,"supported_languages":[]}

Input: User query: i want to play something like cyberpunk but with cats, no multiplayer
Output: {"query":"cyberpunk cats futuristic sci-fi single player",
"include_tags":["cyberpunk","cats"],"exclude":["multiplayer"],
"release_year":null,"supported_languages":[]}

Input: User query: french hidden object game from 2022 without timer
Output: {"query":"hidden object","include_tags":[],"exclude":["timer"],
"release_year":2022,"supported_languages":["french"]}
""".strip()

LLM_MODELS = [
    "claude-4-sonnet",
    "openai-gpt-4.1",
]
SCORING_OPTIONS = [
    "balanced_default",
    "semantic_focus",
    "keyword_extreme",
    "no_reranker_balanced",
    "reranker_heavy",
]
DEFAULT_SCORING = "balanced_default"

LOCAL_CONN_NAME = "cortex"
LOCAL_CONN_TOML: str | None = None
