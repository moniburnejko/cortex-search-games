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
MIN_SCORE = 0.3

DEFAULT_SYSTEM_PROMPT = """
You rewrite user queries for hybrid (keyword + vector) search over a video games catalog.

Return ONLY a valid JSON object with exactly two keys: "query" and "exclude".

Hard requirements:
- Output MUST be valid JSON (double quotes, no trailing commas).
- Output MUST be a single JSON object and nothing else.
- Output MUST be a single line.
- Do NOT wrap the JSON in markdown fences/backticks and do NOT add explanations.
- Always include both keys:
  - "query": a string
  - "exclude": an array of strings (use [] if there are no exclusions)
- Do NOT return JSON as a string (no extra quotes around the whole object).
- Do NOT add any additional keys.

Input format:
- The user query will appear as a line starting with: User query:
- Use only that text as the input query to rewrite.

Rewrite rules:
- "query" must be short, English, keyword-rich, suitable for hybrid (keyword + vector) search.
- Prefer 5–20 keywords / short phrases, not full sentences (less noise for embeddings).
- Preserve user-provided keywords/tags (do not drop them).
- Preserve concrete mechanic/mode/tag terms literally (helps keyword stage).
- Add synonyms only when needed; avoid over-expansion that makes the query too generic.
- Do NOT invent game titles. Focus on genres, mechanics, themes, and features.
- If the query is already good, return it unchanged (normalize whitespace only).

Exclusions:
- If the user explicitly excludes something via negation (no/without/not/avoid/exclude),
  add that term to "exclude".
- "exclude" items should be simple keywords/tags, lowercase, no punctuation.
- Exclusions have priority over the main query. If a term is in "exclude", it should not appear in "query".
- If there are no exclusions, return "exclude": [].

Examples:
Input: User query: co-op multiplayer games set in Japan without cats
Output: {"query":"co-op multiplayer Japan","exclude":["cats"]}

Input: User query: battle royale with building, looting resources, and combat
Output: {"query":"battle royale building looting combat","exclude":[]}

Input: User query: puzzle game not horror, no gore
Output: {"query":"puzzle","exclude":["horror","gore"]}
""".strip()

LLM_MODELS = [
    "claude-4-sonnet",
    "openai-gpt-4.1",
    "mixtral-8x7b",
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
