-- Query:        EMEA-wide session demand for the baseline tab.
--               One row per (airport_code, trip_type, hex_id, child_hex_id,
--               time_bucket, dist_bucket) with `sessions` summed over the
--               lookback window. No airport filter — every EMEA-operational
--               airport with sessions in window appears.
--               v2 (2026-05-07): adds `child_hex_id` at resolution
--               {{hex_size}} + 1 for sub-hex pin placement.
--               v3 (2026-05-11): added `{{operational_clause}}` template
--               so callers can opt out of the `is_operational AND not is_deleted`
--               filter — needed for airports with stale flags in
--               `airport.airports` (e.g. IST, BUD, SZG that are clearly real
--               operational markets).
--               v3.1 (2026-05-11): `geos` CTE now overrides `city_id` for
--               five airports whose `airport.airports.city_id` is registered
--               to a city that does not match the dominant `lastcityid`
--               their sessions actually report (so the
--               `g.airport_city_id = ss.lastcityid` join was returning zero
--               rows). Overrides — derived from the dominant session city
--               per airport, all >60% concentration:
--                   REG → 2472 (Calabria)
--                   TRS → 1917 (Trieste)
--                   TSF → 2453 (Venezia)
--                   VCE → 2453 (Venezia)
--                   VRN → 2453 (Venezia)
--               The `lastcityid` join is intentional and stays in place — it
--               correctly excludes cross-city sessions (e.g. London → Berlin
--               flights initiated from a London app session shouldn't count
--               as Berlin airport demand).
-- Tables:       airport.airports, marketplace_data.sessions_stats
-- DataCentral:  https://datacentral.uberinternal.com/queryrunner/queries/{uuid}/overview
-- QueryBuilder: https://querybuilder.uberinternal.com/?query_uuid={uuid}
-- Generated:    2026-04-30 (v1) / 2026-05-07 (v2 — child_hex_id added)
-- Output dir:   outputs/2026-04-30_npi-coverage-curves/outputs/v2/
--
-- Parameters:
--   {{start_date}}          YYYY-MM-DD inclusive
--   {{end_date}}            YYYY-MM-DD exclusive
--   {{hex_size}}            H3 resolution (int), e.g. 7   — parent hex
--   {{child_hex_size}}      H3 resolution (int), e.g. 8   — child hex; caller sets to {{hex_size}} + 1.
--   {{operational_clause}}  SQL fragment, normally
--                           "and a.is_operational and not a.is_deleted"; pass
--                           an empty string to include non-operational rows.
--
-- Bucket parity (mandatory): time_bucket and dist_bucket case blocks below
-- are byte-identical to bpo_npi_hourly_fixed.sql:101-155 and to the per-airport
-- variant airport_session_demand.sql. Any change must be made in all three
-- files in the same commit.

set session hash_partition_count = 64;

with
    geos as (
        select distinct
            a.airport_code                                  as airport_code,
            cast(case
                when a.airport_code = 'REG' then 2472
                when a.airport_code = 'TRS' then 1917
                when a.airport_code = 'TSF' then 2453
                when a.airport_code = 'VCE' then 2453
                when a.airport_code = 'VRN' then 2453
                else a.city_id
            end as varchar)                                 as airport_city_id
        from airport.airports as a
        where 1=1
            and a.mega_region = 'EMEA'
            {{operational_clause}}),

    raw_sessions as (
        select
            g.airport_code                                                         as airport_code,
            g.airport_city_id                                                      as city_id,
            case
                when ss.pickupairportcode is not null then 'PU'
                else 'DO'
            end                                                                    as trip_type,
            case
                when ss.pickupairportcode is not null
                    then get_hexagon_addr(ss.lastshoppingtransition.destination.lat,
                                          ss.lastshoppingtransition.destination.lng,
                                          {{hex_size}})
                else get_hexagon_addr(ss.lastshoppingtransition.origin.lat,
                                      ss.lastshoppingtransition.origin.lng,
                                      {{hex_size}})
            end                                                                    as hex_id,
            case
                when ss.pickupairportcode is not null
                    then get_hexagon_addr(ss.lastshoppingtransition.destination.lat,
                                          ss.lastshoppingtransition.destination.lng,
                                          {{child_hex_size}})
                else get_hexagon_addr(ss.lastshoppingtransition.origin.lat,
                                      ss.lastshoppingtransition.origin.lng,
                                      {{child_hex_size}})
            end                                                                    as child_hex_id,
            hour(cast(ss.sessionstarttimems as timestamp))                         as hour_dt,
            dow(cast(ss.sessionstarttimems as timestamp))                          as day_of_wk,
            ss.averagedistancemiles * 1.60934                                      as dist_km,
            ss.sessionid                                                           as session_id
        from marketplace_data.sessions_stats as ss
            inner join geos as g
                on (g.airport_code = ss.pickupairportcode
                    or g.airport_code = ss.destinationairportcode)
                    and g.airport_city_id = ss.lastcityid
        where 1=1
            and ss.datestr >= '{{start_date}}'
            and ss.datestr <  '{{end_date}}'
            and ss.numshoppingtransitions > 0
            and ss.numvehicleviewsseen > 0
            and ((ss.pickupairportcode is not null and ss.destinationairportcode is null)
              or (ss.destinationairportcode is not null and ss.pickupairportcode is null))),

    bucketed as (
        select
            r.airport_code                              as airport_code,
            r.trip_type                                 as trip_type,
            r.hex_id                                    as hex_id,
            r.child_hex_id                              as child_hex_id,
            case
                when r.city_id in ('1007', '531', '2010', '1911', '202', '599', '240', '1878', '2007', '2095', '1879', '214') then
                    case
                        when r.day_of_wk in (5, 6) then
                            case
                                when r.hour_dt in (1, 2, 3, 4, 5, 6)        then 'wked_night'
                                when r.hour_dt in (7, 8, 9, 10, 11)         then 'wked_morning'
                                when r.hour_dt in (12, 13, 14, 15, 16, 17)  then 'wked_day'
                                when r.hour_dt in (18, 19, 20)              then 'wrek_evening'
                                when r.hour_dt in (21, 22, 23, 0, 24)       then 'wrek_late_evening'
                            else 'error' end
                        else
                            case
                                when r.hour_dt in (1, 2, 3, 4, 5, 6)        then 'wkd_night'
                                when r.hour_dt in (7, 8, 9, 10, 11)         then 'wkd_morning'
                                when r.hour_dt in (12, 13, 14, 15, 16, 17)  then 'wkd_day'
                                when r.hour_dt in (18, 19, 20)              then 'wkd_evening'
                                when r.hour_dt in (21, 22, 23, 0, 24)       then 'wkd_late_evening'
                            else 'error' end
                    end
                else
                    case
                        when r.day_of_wk in (6, 7) then
                            case
                                when r.hour_dt in (1, 2, 3, 4, 5, 6)        then 'wked_night'
                                when r.hour_dt in (7, 8, 9, 10, 11)         then 'wked_morning'
                                when r.hour_dt in (12, 13, 14, 15, 16, 17)  then 'wked_day'
                                when r.hour_dt in (18, 19, 20)              then 'wrek_evening'
                                when r.hour_dt in (21, 22, 23, 0, 24)       then 'wrek_late_evening'
                            else 'error' end
                        else
                            case
                                when r.hour_dt in (1, 2, 3, 4, 5, 6)        then 'wkd_night'
                                when r.hour_dt in (7, 8, 9, 10, 11)         then 'wkd_morning'
                                when r.hour_dt in (12, 13, 14, 15, 16, 17)  then 'wkd_day'
                                when r.hour_dt in (18, 19, 20)              then 'wkd_evening'
                                when r.hour_dt in (21, 22, 23, 0, 24)       then 'wkd_late_evening'
                            else 'error' end
                    end
            end                                         as time_bucket,
            case
                when r.dist_km * 1.0 >= 0.0   and r.dist_km * 1.0 <= 5.0    then '0-5 km'
                when r.dist_km * 1.0 > 5.0    and r.dist_km * 1.0 <= 10.0   then '5-10 km'
                when r.dist_km * 1.0 > 10.0   and r.dist_km * 1.0 <= 20.0   then '10-20 km'
                when r.dist_km * 1.0 > 20.0   and r.dist_km * 1.0 <= 30.0   then '20-30 km'
                when r.dist_km * 1.0 > 30.0   and r.dist_km * 1.0 <= 40.0   then '30-40 km'
                when r.dist_km * 1.0 > 40.0   and r.dist_km * 1.0 <= 50.0   then '40-50 km'
                when r.dist_km * 1.0 > 50.0   and r.dist_km * 1.0 <= 60.0   then '50-60 km'
                when r.dist_km * 1.0 > 60.0   and r.dist_km * 1.0 <= 70.0   then '60-70 km'
                when r.dist_km * 1.0 > 70.0   and r.dist_km * 1.0 <= 80.0   then '70-80 km'
                when r.dist_km * 1.0 > 80.0   and r.dist_km * 1.0 <= 90.0   then '80-90 km'
                when r.dist_km * 1.0 > 90.0   and r.dist_km * 1.0 <= 100.0  then '90-100 km'
                when r.dist_km * 1.0 > 100.0                                then '100+ km'
            else 'error' end                            as dist_bucket,
            r.session_id                                as session_id,
            r.dist_km                                   as dist_km
        from raw_sessions as r
        where r.hex_id is not null)

select
    b.airport_code                  as airport_code,
    b.trip_type                     as trip_type,
    b.hex_id                        as hex_id,
    b.child_hex_id                  as child_hex_id,
    b.time_bucket                   as time_bucket,
    b.dist_bucket                   as dist_bucket,
    count(b.session_id)             as sessions,
    avg(b.dist_km)                  as avg_dist_km
from bucketed as b
where 1=1
    and b.time_bucket <> 'error'
    and b.dist_bucket <> 'error'
group by 1, 2, 3, 4, 5, 6
