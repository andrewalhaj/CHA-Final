create schema cha_final;

create table cha_final.facts (
  id   bigint generated always as identity primary key,
  body text not null
);

create table cha_final.events (
  id         bigint generated always as identity primary key,
  source     text not null,
  code       text not null,
  result     text not null,
  reason     text not null,
  created_at timestamptz not null default current_timestamp
);
