create table notes
(id bigint not null,
 note text,
 done boolean,
  approved_by text,
  approved_at timestamptz,
  primary key (id));
create index notes_done_idx on notes (done);
