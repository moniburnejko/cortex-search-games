use role cortexadmin;
use warehouse cortex_search_wh;
use database cortex_db;
use schema cortex_db.raw;

create or replace cortex search service games_svc_1_5
  on search_text
  attributes (release_year, supported_languages, categories, genres, tags)
  warehouse = cortex_search_wh
  target_lag = '12 hours'
  embedding_model = 'snowflake-arctic-embed-m-v1.5'
as (
  select
    app_id::varchar as app_id,
    name,
    about_the_game,
    short_description,
    release_year,
    supported_languages,
    categories,
    genres,
    tags,
    search_text
  from dt_games
);

create or replace cortex search service cat_games_svc_1_5
  on search_text
  attributes (release_year, supported_languages, categories, genres, tags)
  warehouse = cortex_search_wh
  target_lag = '12 hours'
  embedding_model = 'snowflake-arctic-embed-m-v1.5'
as (
  select
    app_id::varchar as app_id,
    name,
    about_the_game,
    short_description,
    release_year,
    supported_languages,
    categories,
    genres,
    tags,
    search_text
  from dt_cat_games
);

alter cortex search service games_svc_1_5
  set primary key (app_id);

alter cortex search service cat_games_svc_1_5
  set primary key (app_id);

-- named SCORING PROFILES --
alter cortex search service games_svc_1_5
  add scoring profile if not exists balanced_default
'{
  "functions": {
    "weights": {
      "texts": 1,
      "vectors": 1,
      "reranker": 1
    }
  }
}';

alter cortex search service games_svc_1_5
  add scoring profile if not exists keyword_focus
'{
  "functions": {
    "weights": {
      "texts": 3,
      "vectors": 1,
      "reranker": 1
    }
  }
}';

alter cortex search service games_svc_1_5
  add scoring profile if not exists low_latency
'{
  "reranker": "none",
  "functions": {
    "weights": {
      "texts": 2,
      "vectors": 1,
      "reranker": 1
    }
  }
}';

alter cortex search service games_svc_1_5
  add scoring profile if not exists semantic_focus
'{
  "functions": {
    "weights": {
      "texts": 1,
      "vectors": 3,
      "reranker": 1
    }
  }
}';

alter cortex search service games_svc_1_5
  add scoring profile if not exists keyword_extreme
'{
  "functions": {
    "weights": {
      "texts": 6,
      "vectors": 1,
      "reranker": 1
    }
  }
}';

alter cortex search service games_svc_1_5
  add scoring profile if not exists no_reranker_balanced
'{
  "reranker": "none",
  "functions": {
    "weights": {
      "texts": 1,
      "vectors": 1,
      "reranker": 1
    }
  }
}';

alter cortex search service games_svc_1_5
  add scoring profile if not exists reranker_heavy
'{
  "functions": {
    "weights": {
      "texts": 1,
      "vectors": 1,
      "reranker": 3
    }
  }
}';

alter cortex search service cat_games_svc_1_5
  add scoring profile if not exists balanced_default
'{
  "functions": {
    "weights": {
      "texts": 1,
      "vectors": 1,
      "reranker": 1
    }
  }
}';

alter cortex search service cat_games_svc_1_5
  add scoring profile if not exists keyword_focus
'{
  "functions": {
    "weights": {
      "texts": 3,
      "vectors": 1,
      "reranker": 1
    }
  }
}';

alter cortex search service cat_games_svc_1_5
  add scoring profile if not exists low_latency
'{
  "reranker": "none",
  "functions": {
    "weights": {
      "texts": 2,
      "vectors": 1,
      "reranker": 1
    }
  }
}';

alter cortex search service cat_games_svc_1_5
  add scoring profile if not exists semantic_focus
'{
  "functions": {
    "weights": {
      "texts": 1,
      "vectors": 3,
      "reranker": 1
    }
  }
}';

alter cortex search service cat_games_svc_1_5
  add scoring profile if not exists keyword_extreme
'{
  "functions": {
    "weights": {
      "texts": 6,
      "vectors": 1,
      "reranker": 1
    }
  }
}';

alter cortex search service cat_games_svc_1_5
  add scoring profile if not exists no_reranker_balanced
'{
  "reranker": "none",
  "functions": {
    "weights": {
      "texts": 1,
      "vectors": 1,
      "reranker": 1
    }
  }
}';

alter cortex search service cat_games_svc_1_5
  add scoring profile if not exists reranker_heavy
'{
  "functions": {
    "weights": {
      "texts": 1,
      "vectors": 1,
      "reranker": 3
    }
  }
}';

-- quick checks --
desc cortex search service games_svc_1_5;
desc cortex search service cat_games_svc_1_5;
