use role cortexadmin;
use warehouse cortex_wh;
use database cortex_db;
use schema cortex_db.raw;

-- FILE FORMAT & STAGE --
create file format if not exists ff_json
  type = json;

create stage if not exists games_s3_stage
  url = 's3://cortex-mb/stream-games/'
  storage_integration = s3_int_cortex
  file_format = ff_json;

//list @games_s3_stage;

-- RAW LANDING TABLE --
create table if not exists games_raw_file (
  v variant,
  file_name varchar,
  load_ts timestamp_ltz
);

-- PIPE --
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


-- DYNAMIC TABLES --

-- DT GAMES TYPED
-- normalize fields (types, trim, nulls, array cleanup)
create or replace dynamic table dt_games_typed
  target_lag = 'DOWNSTREAM'
  warehouse = cortex_wh
as
with source as (
  select
    try_to_number(f.key)::int as app_id,
    rf.file_name,
    rf.load_ts,
    f.value as game
  from games_raw_file rf,
  lateral flatten(input => rf.v) f
),
normalized as (
  select
    app_id,
    file_name,
    load_ts,

    -- strings: trim, convert empty to null
    nullif(trim(game:name::varchar), '') as name,
    nullif(trim(game:about_the_game::varchar), '') as about_the_game,
    nullif(trim(game:short_description::varchar), '') as short_description,

    -- year: extract from release_date using regex, convert to int, convert empty/non-matching to null
    try_to_number(regexp_substr(game:release_date::varchar, '(19|20)[0-9]{2}'))::int as release_year,

    -- arrays: cleanup without joins/subqueries (incremental-friendly)
    array_sort(
      array_distinct(
        filter(
          transform(
            iff(
              is_array(game:supported_languages),
              to_array(game:supported_languages),
              array_construct()
            ),
            x -> nullif(trim(x::varchar), '')
          ),
          y -> y is not null
        )
      )
    ) as supported_languages,

    array_sort(
      array_distinct(
        filter(
          transform(
            iff(
              is_array(game:categories),
              to_array(game:categories),
              array_construct()
            ),
            x -> nullif(trim(x::varchar), '')
          ),
          y -> y is not null
        )
      )
    ) as categories,

    array_sort(
      array_distinct(
        filter(
          transform(
            iff(
              is_array(game:genres),
              to_array(game:genres),
              array_construct()
            ),
            x -> nullif(trim(x::varchar), '')
          ),
          y -> y is not null
        )
      )
    ) as genres,

    array_sort(
      array_distinct(
        filter(
          transform(
            case
              when is_object(game:tags) then object_keys(game:tags)
              when is_array(game:tags) then to_array(game:tags)
              else array_construct()
            end,
            x -> nullif(trim(x::varchar), '')
          ),
          y -> y is not null
        )
      )
    ) as tags
  from source
)
select
  app_id,
  file_name,
  load_ts,
  name,
  about_the_game,
  short_description,
  release_year,
  supported_languages,
  categories,
  genres,
  tags
from normalized;

-- DT GAMES DEDUPED
-- dedupe by app_id, keep latest row by load_ts/file_name
create or replace dynamic table dt_games_deduped
  target_lag = 'DOWNSTREAM'
  warehouse = cortex_wh
as
select *
from dt_games_typed
qualify row_number() over (
  partition by app_id
  order by load_ts desc, file_name desc
) = 1;

-- DT GAMES
-- build normalized search_text for cortex search
create or replace dynamic table dt_games
  target_lag = '12 hours'
  warehouse = cortex_wh
as
with final_rows as (
  select *
  from dt_games_deduped
),
search_ready as (
  select
    app_id,
    name,
    about_the_game,
    short_description,
    release_year,
    supported_languages,
    categories,
    genres,
    tags,

    -- search_text: lowercase + trim, with bounded description excerpt
    nullif(
      trim(
        concat_ws(
          ' ',
          nullif(lower(name), ''),
          nullif(lower(array_to_string(tags, ' ')), ''),
          nullif(lower(array_to_string(categories, ' ')), ''),
          nullif(lower(array_to_string(genres, ' ')), ''),
          nullif(lower(left(about_the_game, 2000)), '')
        )
      ),
      ''
    ) as search_text
  from final_rows
)
select
  app_id,
  name,
  about_the_game,
  short_description,
  release_year,
  supported_languages,
  categories,
  genres,
  tags,
  search_text
from search_ready
where app_id is not null
  and name is not null
  and search_text is not null;

-- DT CAT GAMES
-- subset of dt_games, just for testing llm rewrite, embeddings, scoring profiles, etc. on smaller dataset
create or replace dynamic table dt_cat_games
  target_lag = '12 hours'
  warehouse = cortex_wh
as
select * from dt_games
where search_text like '%cats%';

-- DT TEST DATA (show all)
-- smaller dataset with broader variety of attributes for testing show all functionality
create or replace dynamic table test_data 
  target_lag = '12 hours'
  warehouse = cortex_wh
as 
select * from dt_games
where release_year between 2014 and 2016;


//show tables;
//select * from games_raw_file limit 10;
//select count(*) from dt_games_typed;
//select * from dt_games_typed limit 10;
//select count(*) from dt_games_deduped;
//select * from dt_games_deduped limit 10;
//select count(*) from dt_games;
//select * from dt_games limit 10;
//select count(*) from dt_cat_games;
//select * from dt_cat_games limit 10;
//select count(*) from test_data;
//select * from test_data limit 10;
