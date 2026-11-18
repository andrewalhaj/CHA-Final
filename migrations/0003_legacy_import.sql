-- A single completed snapshot marks the initial legacy import.
create table cha_final.legacy_import (
  singleton       boolean default true primary key check (singleton),
  snapshot_digest text not null
);
