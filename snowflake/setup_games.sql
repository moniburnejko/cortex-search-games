use role cortexadmin;
use warehouse cortex_wh;
use database cortex_db;
use schema cortex_db.raw;

-- file format & stage
create file format if not exists ff_json
  type = json;
  
create stage if not exists  games_s3_stage
  url = 's3://cortex-mb/stream-games/'
  storage_integration = s3_int_cortex
  file_format = ff_json;

//list @games_s3_stage;

-- raw landing table 
create table if not exists games_raw_file (
  v variant,
  file_name varchar,
  load_ts timestamp_ltz
);

-- pipe
create pipe if not exists games_json_pipe
  auto_ingest = true
as 
  copy into games_raw_file
  from (
    select 
      $1,
      metadata$filename,
      current_timestamp()
    from @games_s3_stage
  )
  file_format = (format_name = ff_json);

//desc pipe games_json_pipe;
//alter pipe games_json_pipe refresh;
//select system$pipe_status('CORTEX_DB.RAW.GAMES_JSON_PIPE');

-- dynamic tables
create dynamic table if not exists dt_games
  target_lag = 'DOWNSTREAM'
  warehouse = cortex_wh
as
select
  f.value:name::varchar as name,
  f.value:detailed_description::varchar as detailed_description,
  f.value:short_description::varchar as short_description,
  case when is_object(f.value:tags) then object_keys(f.value:tags) else array_construct() end as tags,
  concat_ws(
    ' ',
    coalesce(f.value:name::varchar, ''),
    coalesce(f.value:detailed_description::varchar, ''),
    case when is_object(f.value:tags) then array_to_string(object_keys(f.value:tags), ', ') else '' end
  ) as search_text
from games_raw_file,
lateral flatten(input => v) f;

create dynamic table if not exists dt_cat_games
  target_lag = 'DOWNSTREAM'
  warehouse = cortex_wh
as 
select * from dt_games
where detailed_description ilike '%cats%';

//show tables;
//select * from games_raw_file limit 10;
//select count(*) from dt_games;
//select * from dt_games limit 10;
//select count(*) from dt_cat_games;
//select * from dt_cat_games limit 10;

    
-- cortex search services
use warehouse cortex_search_wh;

create cortex search service if not exists games_svc
  on search_text
  attributes (tags)
  warehouse = cortex_search_wh
  target_lag = '12 hours'
as (
  select
    name,
    short_description,
    detailed_description,
    tags,
    search_text
  from dt_games
);

create cortex search service if not exists  cat_games_svc
  on search_text
  attributes (tags)
  warehouse = cortex_search_wh
  target_lag = '12 hours'
as (
  select
    name,
    short_description,
    detailed_description,
    tags,
    search_text
  from dt_cat_games
);

-- named scoring profiles
alter cortex search service games_svc
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

alter cortex search service games_svc
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

alter cortex search service games_svc
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

alter cortex search service cat_games_svc
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

alter cortex search service cat_games_svc
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

alter cortex search service cat_games_svc
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