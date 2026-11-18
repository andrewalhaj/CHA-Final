-- Keep scoped facts outside the table queried by the first runtime.
create table cha_final.project_facts (
  id      bigint default nextval('cha_final.facts_id_seq'::regclass) primary key,
  project text not null,
  body    text not null
);
