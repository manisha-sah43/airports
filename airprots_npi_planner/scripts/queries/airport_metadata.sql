-- Query:        Look up airport metadata (city_id, name, simplified_shape WKT, timezone).
-- Tables:       airport.airports, dwh.dim_city
-- Generated:    2026-04-30
-- Output dir:   outputs/2026-04-30_airport-btdm-grid/
--
-- Parameters:
--   {{airport_codes}}        — quoted comma-separated list e.g. 'FRA','LHR'
--   {{operational_clause}}   — SQL fragment, normally
--                              "and a.is_operational and not a.is_deleted";
--                              pass empty string to include non-operational rows.

select
    a.airport_code                                                  as airport_code,
    a.airport_name                                                  as airport_name,
    cast(a.city_id as bigint)                                       as city_id,
    a.city_name                                                     as city_name,
    dc.timezone                                                     as timezone,
    st_astext(a.airport_master_geofence_simplified_shape)           as airport_geofence_wkt
from airport.airports as a
    left join dwh.dim_city as dc on dc.city_id = a.city_id
where 1=1
    and a.airport_code in ({{airport_codes}})
    {{operational_clause}}
