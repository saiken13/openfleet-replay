-- Data-integrity checks for a live OpenFleet database. Each line: check | violations (0 = OK).
-- Usage: docker compose exec -T postgres psql -U openfleet -d openfleet -q < scripts/integrity_checks.sql
\pset pager off
\pset tuples_only on
\pset format unaligned
-- Each line: check name | number of violations (0 = OK)
select 'at most one RUNNING run per robot (#016)', count(*) from (select robot_id from runs where status='RUNNING' group by robot_id having count(*)>1) x;
select 'current_run_id points at a RUNNING run of the same robot', count(*) from robots r left join runs u on u.id=r.current_run_id where r.current_run_id is not null and (u.status<>'RUNNING' or u.robot_id<>r.id);
select 'RUNNING run is the robot''s current run', count(*) from runs u join robots r on r.id=u.robot_id where u.status='RUNNING' and r.current_run_id is distinct from u.id;
select 'ended runs have ended_at >= started_at', count(*) from runs where status<>'RUNNING' and (ended_at is null or ended_at<started_at);
select 'RUNNING runs have no ended_at', count(*) from runs where status='RUNNING' and ended_at is not null;
select 'one run_started event per run', count(*) from runs u where (select count(*) from events e where e.run_id=u.id and e.type='run_started')<>1;
select 'ended runs have exactly one run_ended event', count(*) from runs u where u.status<>'RUNNING' and (select count(*) from events e where e.run_id=u.id and e.type='run_ended')<>1;
select 'no telemetry after a run ended (server time)', count(*) from telemetry_samples s join runs u on u.id=s.run_id join events e on e.run_id=u.id and e.type='run_ended' where s.received_at > e.received_at;
select 'telemetry seq unique per run', count(*) from (select run_id,seq from telemetry_samples group by 1,2 having count(*)>1) x;
select 'telemetry seq contiguous (no gaps) per run', count(*) from (select run_id from telemetry_samples group by run_id having max(seq)-min(seq)+1<>count(*)) x;
select 'topic samples belong to an existing telemetry batch', count(*) from topic_samples t where not exists (select 1 from telemetry_samples s where s.run_id=t.run_id and s.seq=t.seq);
select 'sample robot_id matches its run''s robot', count(*) from telemetry_samples s join runs u on u.id=s.run_id where s.robot_id<>u.robot_id;
select 'metrics within 0-100', count(*) from telemetry_samples where cpu_pct not between 0 and 100 or mem_pct not between 0 and 100 or battery_pct not between 0 and 100;
select 'at most one unresolved incident per (robot, dedup_key) (#007)', count(*) from (select robot_id,dedup_key from incidents where status<>'RESOLVED' group by 1,2 having count(*)>1) x;
select 'RESOLVED incidents have resolved_at >= started_at', count(*) from incidents where status='RESOLVED' and (resolved_at is null or resolved_at<started_at);
select 'unresolved incidents have no resolved_at', count(*) from incidents where status<>'RESOLVED' and resolved_at is not null;
select 'one incident_opened event per incident', count(*) from incidents i where (select count(*) from events e where e.type='incident_opened' and e.payload->>'incident_id'=i.id::text)<>1;
select 'RESOLVED incidents have one incident_resolved event', count(*) from incidents i where i.status='RESOLVED' and (select count(*) from events e where e.type='incident_resolved' and e.payload->>'incident_id'=i.id::text)<>1;
select 'last_detected_at within [started_at, resolved_at]', count(*) from incidents where last_detected_at<started_at or (resolved_at is not null and last_detected_at>resolved_at);
select 'robot status matches open incidents (live robots)', count(*) from robots r where r.status<>'OFFLINE' and r.status <> case when exists(select 1 from incidents i where i.robot_id=r.id and i.status<>'RESOLVED' and i.severity='CRITICAL') then 'ERROR' when exists(select 1 from incidents i where i.robot_id=r.id and i.status<>'RESOLVED' and i.severity='WARNING') then 'DEGRADED' else 'ONLINE' end;
select 'robots silent >10s are OFFLINE (allowing one 2s sweep)', count(*) from robots where status<>'OFFLINE' and (last_seen_at is null or last_seen_at < now() - interval '13 seconds');
select 'robots marked live were seen within the timeout', count(*) from robots where status<>'OFFLINE' and last_seen_at < now() - interval '13 seconds';
select 'robot_status_changed events form a consistent chain', count(*) from (select robot_id, payload->>'from' f, lag(payload->>'to') over (partition by robot_id order by id) prev from events where type='robot_status_changed') x where prev is not null and f<>prev;
select 'topic_states status matches latest topic sample', count(*) from topic_states ts where ts.status <> (select t.status from topic_samples t where t.robot_id=ts.robot_id and t.topic=ts.topic order by t.received_at desc, t.id desc limit 1);
