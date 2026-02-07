PAGE_TITLE = "Find your new fave cat game"
PAGE_ICON = "🐱"
APP_TITLE = "🐱 Find your new fave cat game 🐱"
APP_CAPTION = "and help me find if my search app likes to hallucinate like Cheshire Cat"
QUERY_PLACEHOLDER = "e.g. co-op with cats in Japan"

DB = "CORTEX_DB"
SCHEMA = "RAW"
SERVICE = "CAT_GAMES_SVC"
SERVICE_FQN = f"{DB}.{SCHEMA}.{SERVICE}"

RETURN_COLS = ["NAME", "SHORT_DESCRIPTION", "DETAILED_DESCRIPTION", "TAGS"]
MIN_SCORE = 0.3

DEFAULT_SYSTEM_PROMPT = """
You rewrite user queries for semantic search over a video games catalog.
Return ONLY a JSON object with two keys: "query" and "exclude".
- "query" is the rewritten search query (English, keyword-rich).
- "exclude" is a list of keywords/tags that the user explicitly excludes
  (based on negations like "no", "without", "not", "exclude", "avoid").
Preserve user-provided tags/keywords (do not drop them).
If the query is already good, return it unchanged.
If you cannot improve it, return the original user query unchanged.
Do NOT invent game titles. Focus on genres, mechanics, themes, and features.
Example: {"query": "co-op sci-fi shooter space aliens", "exclude": ["cats"]}
If there are no exclusions, return an empty list: "exclude": []
""".strip()

LLM_MODELS = ["claude-4-sonnet", "openai-gpt-5-chat", "llama3.1-70b"]
SCORING_OPTIONS = ["balanced_default", "keyword_focus", "low_latency"]
DEFAULT_SCORING = "balanced_default"

LOCAL_CONN_NAME = "cortex"
LOCAL_CONN_TOML: str | None = None
